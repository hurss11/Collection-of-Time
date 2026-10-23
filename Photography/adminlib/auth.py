# -*- coding: utf-8 -*-
"""认证：账号存储、密码哈希、签名会话、CSRF 与登录限流。

只用标准库。安全设计要点：

1. 密码用 PBKDF2-HMAC-SHA256（默认 60 万次）加盐哈希，永不落盘明文；迭代数随记录存盘，
   旧记录在下次登录成功时自动用新参数重哈希；
2. 会话是服务端用密钥签名的令牌（HMAC-SHA256），有效期可配，
   令牌里带上密码指纹，改密后所有旧会话立即失效；
3. Cookie 走 HttpOnly + SameSite=Strict，前端 JS 读不到会话值；
4. 另有可读的 CSRF Cookie，写操作必须把它的值回填到请求头（双提交校验）；已登录时
   该值还要求等于由会话令牌派生出来的值，因此能在本站写 Cookie 的攻击者也伪造不出；
5. 登录失败按 IP 限流并锁定，前后端都能拿到剩余次数与等待秒数。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

# ---------- 参数 ----------

PBKDF2_ITERATIONS = 600_000       # OWASP 对 PBKDF2-HMAC-SHA256 的现行建议值；
                                  # 旧配置（30 万）留下的哈希会在下次登录成功时自动升级
PBKDF2_ALGO = "sha256"
SALT_BYTES = 16
SESSION_SECRET_BYTES = 32

DEFAULT_MAX_ATTEMPTS = 5          # 窗口内允许的连续失败次数
DEFAULT_WINDOW_SECONDS = 300      # 失败计数窗口
DEFAULT_LOCK_SECONDS = 300        # 触发后的锁定时长
GLOBAL_MAX_ATTEMPTS = 60          # 全局失败上限，缓解分布式撞库

CONFIG_NAME = "admin.config.json"
COOKIE_SESSION = "cot_session"
COOKIE_CSRF = "cot_csrf"

# 配置文件损坏时的处置建议。会直接作为 API 错误文案回给客户端，所以不写绝对路径。
CORRUPT_CONFIG_HINT = (
    "为避免覆盖可能还能恢复的账号数据，已停止加载。请先备份该文件并检查内容；"
    "确认要重新开始时，把它改名或删除，再执行 python admin.py --create-user"
)

SESSION_COOKIE_PREFIX = "v1"


class AuthError(Exception):
    """认证类错误。"""

    def __init__(self, message: str, *, status: int = 401, retry_after: int = 0) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.retry_after = retry_after


def _const_eq(left: str, right: str) -> bool:
    """定长比较两个字符串。

    `hmac.compare_digest` 收到含非 ASCII 的 str 会抛 TypeError——中文用户名或
    中文密码会让登录/改密直接 500，所以统一先编码成 UTF-8 再比。
    """
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


# ============================================================
# 密码
# ============================================================

def hash_password(password: str, *, salt: bytes | None = None,
                  iterations: int = PBKDF2_ITERATIONS) -> dict[str, object]:
    """返回可直接落盘的哈希结构。"""
    if not isinstance(password, str) or len(password) < 8:
        raise AuthError("密码至少 8 位", status=400)

    salt = salt or os.urandom(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(PBKDF2_ALGO, password.encode("utf-8"), salt, iterations)
    return {
        "algo": f"pbkdf2_{PBKDF2_ALGO}",
        "iterations": iterations,
        "salt": salt.hex(),
        "hash": digest.hex(),
    }


def verify_password(record: dict[str, object], password: str) -> bool:
    """恒定时间比对，避免通过响应时间侧信道推断密码。"""
    if not isinstance(record, dict) or not isinstance(password, str):
        return False

    try:
        salt = bytes.fromhex(str(record.get("salt", "")))
        expected = bytes.fromhex(str(record.get("hash", "")))
        iterations = int(record.get("iterations") or PBKDF2_ITERATIONS)
    except (ValueError, TypeError):
        return False

    if not salt or not expected:
        return False

    digest = hashlib.pbkdf2_hmac(PBKDF2_ALGO, password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(digest, expected)


def password_fingerprint(record: dict[str, object]) -> str:
    """取哈希前 8 字节作为指纹，用于「改密即踢下线」。"""
    return str(record.get("hash", ""))[:16]


# ============================================================
# 账号存储
# ============================================================

@dataclass
class Account:
    username: str
    record: dict[str, object]

    def as_public_dict(self) -> dict[str, object]:
        return {
            "username": self.username,
            "createdAt": self.record.get("createdAt", ""),
            "updatedAt": self.record.get("updatedAt", ""),
        }


@dataclass
class _Failures:
    """某个 key 的失败记录。"""

    stamps: list[float] = field(default_factory=list)
    locked_until: float = 0.0


class AccountStore:
    """单管理员账号 + 会话密钥的配置文件读写。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._data: dict[str, object] | None = None

    # ---------- 读写 ----------

    def load(self) -> dict[str, object]:
        if self._data is not None:
            return self._data

        if self.path.is_file():
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8-sig"))
            except (json.JSONDecodeError, OSError) as exc:
                # 绝不能像「全新安装」那样返回空账号：那会让 /api/auth/setup 重新打开，
                # 而且随后的 save() 会把这份（可能还救得回来的）配置直接覆盖掉。
                raise AuthError(
                    f"{CONFIG_NAME} 无法读取或解析（{exc}）。{CORRUPT_CONFIG_HINT}",
                    status=500,
                ) from exc
            if not isinstance(payload, dict):
                raise AuthError(
                    f"{CONFIG_NAME} 的顶层不是 JSON 对象。{CORRUPT_CONFIG_HINT}",
                    status=500,
                )
            self._data = payload
            return self._data

        self._data = {"version": 1, "account": None, "sessionSecret": secrets.token_hex(SESSION_SECRET_BYTES)}
        return self._data

    def save(self) -> None:
        data = self.load()
        data["updatedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.path.parent.mkdir(parents=True, exist_ok=True)

        temp = self.path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        try:
            os.chmod(temp, 0o600)          # 含密码哈希与会话密钥，仅本人可读
        except OSError:
            pass
        os.replace(temp, self.path)

    # ---------- 账号 ----------

    @property
    def has_account(self) -> bool:
        account = self.load().get("account")
        return isinstance(account, dict) and bool(account.get("username"))

    def account(self) -> Account | None:
        raw = self.load().get("account")
        if not isinstance(raw, dict) or not raw.get("username"):
            return None
        return Account(str(raw["username"]), raw)

    def create_account(self, username: str, password: str) -> Account:
        """首次创建账号；已存在则拒绝（改密请用 change_password）。"""
        if self.has_account:
            raise AuthError("管理员账号已存在", status=409)

        name = (username or "").strip()
        if not (3 <= len(name) <= 32):
            raise AuthError("用户名需为 3–32 个字符", status=400)

        record = hash_password(password)
        record["createdAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
        record["updatedAt"] = record["createdAt"]

        self.load()["account"] = {"username": name, **record}
        self.save()
        return self.account()      # type: ignore[return-value]

    def verify(self, username: str, password: str) -> Account | None:
        account = self.account()
        if account is None:
            # 即便没有账号也走一次哈希，避免通过耗时判断账号是否存在
            hash_password("dummy-password-for-timing", iterations=PBKDF2_ITERATIONS)
            return None
        if not _const_eq(account.username, (username or "").strip()):
            hash_password("dummy-password-for-timing", iterations=PBKDF2_ITERATIONS)
            return None
        if not verify_password(account.record, password):
            return None
        # 迭代数低于当前目标值时顺手升级；返回带新记录的账号，
        # 否则会话载荷里的口令指纹会对不上刚写进盘的新哈希。
        return self._upgrade_hash(account, password) or account

    def _upgrade_hash(self, account: Account, password: str) -> Account | None:
        """登录成功时把低迭代数的旧哈希就地升级，返回带新记录的账号。"""
        try:
            iterations = int(account.record.get("iterations") or 0)
        except (TypeError, ValueError):
            return None
        if iterations >= PBKDF2_ITERATIONS:
            return None

        data = self.load()
        current = data.get("account")
        if not isinstance(current, dict):
            return None
        # 期间可能已被改密 / 重建：哈希变了就不覆盖
        if str(current.get("hash", "")) != str(account.record.get("hash", "")):
            return None

        record = hash_password(password)
        record["createdAt"] = current.get("createdAt", "")
        record["updatedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
        name = str(current.get("username") or account.username)

        fresh: dict[str, object] = {"username": name, **record}
        data["account"] = fresh
        self.save()
        return Account(name, fresh)

    def change_password(self, current: str, new: str) -> None:
        account = self.account()
        if account is None:
            raise AuthError("尚未创建管理员账号", status=400)
        if not verify_password(account.record, current):
            raise AuthError("当前密码不正确", status=403)
        if _const_eq(current, new):
            raise AuthError("新密码不能与当前密码相同", status=400)

        record = hash_password(new)
        record["createdAt"] = account.record.get("createdAt", "")
        record["updatedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.load()["account"] = {"username": account.username, **record}
        self.save()

    # ---------- 会话密钥 ----------

    @property
    def session_secret(self) -> bytes:
        secret = str(self.load().get("sessionSecret") or "")
        if len(secret) < 32:
            secret = secrets.token_hex(SESSION_SECRET_BYTES)
            self.load()["sessionSecret"] = secret
            self.save()
        return secret.encode("utf-8")

    def rotate_session_secret(self) -> None:
        """轮换密钥 → 所有已签发的会话立即失效。"""
        self.load()["sessionSecret"] = secrets.token_hex(SESSION_SECRET_BYTES)
        self.save()


# ============================================================
# 登录限流
# ============================================================

class RateLimiter:
    """滑动窗口 + 锁定。同一进程内生效，重启即清零。"""

    def __init__(self, *, max_attempts: int = DEFAULT_MAX_ATTEMPTS,
                 window: int = DEFAULT_WINDOW_SECONDS,
                 lock_seconds: int = DEFAULT_LOCK_SECONDS) -> None:
        self.max_attempts = max_attempts
        self.window = window
        self.lock_seconds = lock_seconds
        self._buckets: dict[str, _Failures] = {}
        self._global: list[float] = []

    def _bucket(self, key: str) -> _Failures:
        return self._buckets.setdefault(key, _Failures())

    def check(self, key: str) -> int:
        """返回还需等待的秒数；0 表示可以继续尝试。"""
        now = time.time()
        bucket = self._bucket(key)

        if bucket.locked_until > now:
            return int(bucket.locked_until - now) + 1

        bucket.stamps = [t for t in bucket.stamps if now - t < self.window]
        if len(bucket.stamps) >= self.max_attempts:
            bucket.locked_until = now + self.lock_seconds
            return self.lock_seconds

        recent_global = [t for t in self._global if now - t < self.window]
        if len(recent_global) >= GLOBAL_MAX_ATTEMPTS:
            return 60

        return 0

    def record_failure(self, key: str) -> int:
        """记录一次失败，返回剩余可尝试次数。"""
        now = time.time()
        bucket = self._bucket(key)
        bucket.stamps.append(now)
        self._global.append(now)

        remaining = max(0, self.max_attempts - len(bucket.stamps))
        if remaining == 0:
            bucket.locked_until = now + self.lock_seconds
        return remaining

    def record_success(self, key: str) -> None:
        self._buckets.pop(key, None)

    def reset(self) -> None:
        self._buckets.clear()
        self._global.clear()


# ============================================================
# 会话令牌
# ============================================================

def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


class SessionManager:
    """无状态签名会话：令牌自包含，服务端只验签名与有效期。"""

    def __init__(self, store: AccountStore, *, ttl_hours: float = 12.0) -> None:
        self.store = store
        self.ttl_seconds = max(300, int(ttl_hours * 3600))

    def issue(self, account: Account) -> tuple[str, int]:
        """签发令牌，返回 (token, 有效期秒数)。"""
        now = int(time.time())
        payload = {
            "u": account.username,
            "iat": now,
            "exp": now + self.ttl_seconds,
            "n": secrets.token_hex(8),
            "f": password_fingerprint(account.record),
        }
        body = _b64e(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        signature = hmac.new(self.store.session_secret, body.encode("ascii"), hashlib.sha256).digest()
        return f"{SESSION_COOKIE_PREFIX}.{body}.{_b64e(signature)}", self.ttl_seconds

    def verify(self, token: str) -> dict[str, object] | None:
        """校验令牌，返回 payload；任何异常一律视为无效。"""
        if not token or token.count(".") != 2:
            return None

        prefix, body, signature = token.split(".")
        if prefix != SESSION_COOKIE_PREFIX:
            return None

        expected = hmac.new(self.store.session_secret, body.encode("ascii"), hashlib.sha256).digest()
        try:
            if not hmac.compare_digest(_b64d(signature), expected):
                return None
            payload = json.loads(_b64d(body))
        except (ValueError, TypeError, json.JSONDecodeError):
            return None

        if not isinstance(payload, dict):
            return None
        if int(payload.get("exp") or 0) <= int(time.time()):
            return None

        # 改过密码的旧会话立即失效
        account = self.store.account()
        if account is None or not _const_eq(
            str(payload.get("f", "")), password_fingerprint(account.record)
        ):
            return None
        if not _const_eq(str(payload.get("u", "")), account.username):
            return None

        return payload

    def csrf_for(self, token: str) -> str:
        """派生与会话绑定的 CSRF 值（无状态：同一会话恒定）。

        这样即便攻击者能在本站写入 Cookie（同站子域、HTTP 降级注入），也拿不到
        HttpOnly 的会话令牌，就伪造不出能通过校验的一对值。
        """
        mac = hmac.new(self.store.session_secret, b"csrf:" + token.encode("utf-8"), hashlib.sha256).digest()
        return _b64e(mac[:18])

    @staticmethod
    def new_csrf_token() -> str:
        return secrets.token_urlsafe(24)


# ============================================================
# Cookie 工具
# ============================================================

def parse_cookies(header: str) -> dict[str, str]:
    """解析 Cookie 请求头。"""
    jar: dict[str, str] = {}
    for chunk in (header or "").split(";"):
        name, sep, value = chunk.strip().partition("=")
        if sep and name:
            jar[name.strip()] = value.strip()
    return jar


def build_cookie(name: str, value: str, *, max_age: int, http_only: bool = True,
                 secure: bool = False, path: str = "/", same_site: str = "Strict") -> str:
    """拼装 Set-Cookie 值。SameSite=Strict 时不要带其它属性的隐患。"""
    parts = [f"{name}={value}", f"Path={path}", f"Max-Age={max_age}", f"SameSite={same_site}"]
    if http_only:
        parts.append("HttpOnly")
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def clear_cookie(name: str, *, path: str = "/") -> str:
    return f"{name}=; Path={path}; Max-Age=0; SameSite=Strict"


def client_key(ip: str, username: str = "") -> str:
    """限流键：IP + 用户名，避免一个 IP 换个用户名就绕开计数。"""
    return f"{ip}|{(username or '').strip().lower()}"
