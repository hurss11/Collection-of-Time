# -*- coding: utf-8 -*-
"""认证：账号存储、密码哈希、签名会话、CSRF 与登录限流。

只用标准库。安全设计要点：

1. 密码用 PBKDF2-HMAC-SHA256（默认 30 万次迭代）加盐哈希，永不落盘明文；
2. 会话是服务端用密钥签名的令牌（HMAC-SHA256），有效期可配，
   令牌里带上密码指纹，改密后所有旧会话立即失效；
3. Cookie 走 HttpOnly + SameSite=Strict，前端 JS 读不到会话值；
4. 另有可读的 CSRF Cookie，写操作必须把它的值回填到请求头（双提交校验）；
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

PBKDF2_ITERATIONS = 300_000
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

SESSION_COOKIE_PREFIX = "v1"


class AuthError(Exception):
    """认证类错误。"""

    def __init__(self, message: str, *, status: int = 401, retry_after: int = 0) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.retry_after = retry_after


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
                payload = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    self._data = payload
                    return self._data
            except (json.JSONDecodeError, OSError):
                pass

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
        if not hmac.compare_digest(account.username, (username or "").strip()):
            hash_password("dummy-password-for-timing", iterations=PBKDF2_ITERATIONS)
            return None
        return account if verify_password(account.record, password) else None

    def change_password(self, current: str, new: str) -> None:
        account = self.account()
        if account is None:
            raise AuthError("尚未创建管理员账号", status=400)
        if not verify_password(account.record, current):
            raise AuthError("当前密码不正确", status=403)
        if hmac.compare_digest(current, new):
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
        if account is None or not hmac.compare_digest(
            str(payload.get("f", "")), password_fingerprint(account.record)
        ):
            return None
        if not hmac.compare_digest(str(payload.get("u", "")), account.username):
            return None

        return payload

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
