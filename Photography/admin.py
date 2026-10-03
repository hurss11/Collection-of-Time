#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Photography 后台服务（后端）。

只用 Python 标准库，直接丢到服务器上就能跑，无需 pip install。

架构：前后端分离
    - 本文件只提供 JSON API（/api/*）与静态资源（可选）；
    - 界面在 admin/ 目录，是可独立部署的纯静态前端，通过 fetch 调 API；
    - 认证走 HttpOnly Cookie 会话（不是把口令塞在请求头里），
      写操作另需 CSRF 双提交校验。

功能：
    - 账号：首次创建管理员、登录 / 登出 / 改密、登录失败限流
    - 相册 / 照片 / 视频条目的增删改查（写回 data/*.json，原子写入 + 自动备份）
    - 图片与视频上传，自动落位到 assets/ 对应目录
    - 上传图片时自动读取 EXIF 填充相机 / 快门 / 光圈 / ISO 等参数
    - 有 ffmpeg 时自动生成视频封面与图片缩略图
    - JSON 导入 / 导出备份

用法：
    python admin.py --create-user             # 创建管理员账号（交互输入密码）
    python admin.py                           # 启动，默认 http://127.0.0.1:8080/admin/
    python admin.py --port 9000 --session-hours 8
    python admin.py --print-systemd           # 输出 systemd 单元文件

服务器部署请参考 README 的「内容后台」一节。
"""
from __future__ import annotations

import argparse
import getpass
import hmac
import ipaddress
import json
import mimetypes
import os
import socket
import subprocess
import sys
import time
import traceback
from email import policy, utils as email_utils
from email.parser import BytesParser
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from adminlib import auth, media, query, schema, store    # noqa: E402
from adminlib.exifread import ExifError, read_exif, to_entry_exif  # noqa: E402

ROOT = Path(__file__).resolve().parent
ADMIN_DIR = ROOT / "admin"
IMAGE_DIR = ROOT / "assets" / "img" / "photos"
THUMB_DIR = IMAGE_DIR / "thumbs"
VIDEO_DIR = ROOT / "assets" / "video"
POSTER_DIR = VIDEO_DIR / "posters"
CONFIG_PATH = ROOT / auth.CONFIG_NAME

ALLOWED_UPLOAD_SUFFIXES = media.IMAGE_SUFFIXES | media.VIDEO_SUFFIXES
MAX_UPLOAD_BYTES = 512 * 1024 * 1024          # 单文件 512MB
MAX_BATCH_BYTES = 480 * 1024 * 1024           # 一次批量上传的总体积上限（小于 MAX_BODY_BYTES 留余量）
MAX_BODY_BYTES = MAX_UPLOAD_BYTES + 8 * 1024 * 1024
HISTORY_KEEP = 60                             # 内存中保留的最近操作记录

# 无需登录即可访问的接口（精确匹配）与公开接口前缀
PUBLIC_API = {"/api/health", "/api/auth/session", "/api/auth/setup", "/api/auth/login"}
PUBLIC_PREFIXES = ("/api/public/",)

# 静态资源白名单：项目根目录里还放着后端源码、adminlib/、admin.config.json
# （含密码哈希与会话密钥）与 data/.backups/，它们绝不能被当作普通文件下载。
# 因此只放行前端真正会用到的路径，其余一律 404。
STATIC_ROOT_FILES = {"index.html", "favicon.svg"}
STATIC_ROOT_DIRS = {"assets", "data"}

# 允许「缓存但每次重验证」的路径：命中 304 时只回响应头，省掉整包流量；
# 又因为是重验证而不是长缓存，改完刷新立刻生效（不会看到旧封面）。
CACHE_REVALIDATE_PREFIXES = ("/assets/", "/admin/js/", "/admin/css/")
CACHE_REVALIDATE_FILES = ("/favicon.svg",)

STORE = store.Store(ROOT)
# 查找顺序：COT_FFMPEG 环境变量 → 项目 bin/ → 系统 PATH；verify 会真跑一次 -version
TOOLS = media.detect_tools(ROOT, verify=True)
ACCOUNTS = auth.AccountStore(CONFIG_PATH)
SESSIONS = auth.SessionManager(ACCOUNTS, ttl_hours=12.0)
LIMITER = auth.RateLimiter()

# 简易操作记录，方便在后台「动态」里看到刚才做了什么
HISTORY: list[dict[str, object]] = []


def remember(action: str, detail: str, ok: bool = True) -> None:
    HISTORY.insert(0, {
        "at": time.strftime("%H:%M:%S"),
        "action": action,
        "detail": detail,
        "ok": ok,
    })
    del HISTORY[HISTORY_KEEP:]


# ============================================================
# 响应工具
# ============================================================

class ApiError(Exception):
    """业务错误，会被转成 JSON 错误响应；可携带字段级错误供表单标红。"""

    def __init__(self, message: str, status: int = HTTPStatus.BAD_REQUEST,
                 field_errors: list[dict[str, str]] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.field_errors = field_errors or []


def _first(params: dict[str, list[str]], key: str, default: str = "") -> str:
    """取查询参数的第一个值（缺省或空串时回落到 default）。"""
    values = params.get(key) or []
    return (values[0] if values else "") or default


# 可信代理来源：本机、链路本地与 RFC1918 / ULA 内网段。
# 刻意不含 100.64.0.0/10（运营商 CGNAT）与文档 / 保留段——这些地址可能直接来自
# 公网侧，采信它们的 X-Forwarded-For 等于给「伪造转发头绕过限流」留后门。
TRUSTED_PROXY_NETS = tuple(ipaddress.ip_network(net) for net in (
    "127.0.0.0/8", "::1/128",
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "169.254.0.0/16", "fe80::/10", "fc00::/7",
))


def _is_local_peer(peer: str) -> bool:
    """对端是否来自本机 / 内网（说明前面有反向代理，才值得采信转发头）。"""
    try:
        address = ipaddress.ip_address(peer.split("%")[0])
    except ValueError:
        return False
    return any(address in net for net in TRUSTED_PROXY_NETS)


class Handler(SimpleHTTPRequestHandler):
    server_version = "CollectionOfTime"     # 不暴露具体版本号
    sys_version = ""                        # 也不暴露 Python 版本
    protocol_version = "HTTP/1.1"

    def version_string(self) -> str:
        """响应头里的 Server 值：整站统一成产品名，减少指纹。"""
        return self.server_version

    def __init__(self, *args, **kwargs) -> None:
        # 静态资源一律相对项目根目录解析。若交给默认行为（当前工作目录），
        # 从别处执行 `python /path/to/admin.py` 会让站点资源全部 404，
        # 而 API 照常工作——这种「一半正常」最难排查。
        kwargs["directory"] = str(ROOT)
        super().__init__(*args, **kwargs)

    # ---------- 基础 ----------

    def client_ip(self) -> str:
        """真实客户端 IP，用于限流与日志。

        反向代理（nginx 与后端同机）下 socket 对端永远是 127.0.0.1，直接拿它
        对所有人计数会退化成「反代IP|用户名」：别人试错 5 次就能把管理员锁在门外。
        因此**只在对端是内网 / 回环地址时**采信 `X-Forwarded-For` 的第一跳；
        对端是公网地址时不采信，避免伪造这个头绕过限流。
        """
        peer = (self.client_address[0] if self.client_address else "") or ""
        forwarded = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        if not forwarded or not _is_local_peer(peer):
            return peer
        try:
            candidate = ipaddress.ip_address(forwarded.split("%")[0])
        except ValueError:
            return peer
        return peer if candidate.is_unspecified else str(candidate)

    def address_string(self) -> str:
        """日志与限流统一用真实客户端 IP。"""
        return self.client_ip()

    def log_message(self, fmt: str, *args) -> None:            # noqa: A003
        sys.stderr.write("  %s - %s\n" % (self.address_string(), fmt % args))

    def cache_control(self) -> str:
        """按路径决定缓存策略。

        - `/api/**`：可能带会话数据，一律不缓存；
        - 前端静态资源：允许缓存但每次重验证（命中 304 只回响应头），
          刷新时省掉整包流量，同时改完立刻生效；
        - HTML 外壳：不缓存，升级后打开就是新页面。
        """
        path = self.path.split("?", 1)[0]
        if path.startswith("/api/"):
            return "no-store"
        if path.startswith(CACHE_REVALIDATE_PREFIXES) or path in CACHE_REVALIDATE_FILES:
            return "public, no-cache"
        return "no-store, must-revalidate"

    def end_headers(self) -> None:
        self.send_header("Cache-Control", self.cache_control())
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy", self.content_security_policy())

        # 仅当前端被部署到其它源（--allow-origin）时才回跨域头；
        # 带 Cookie 的跨域必须回具体 Origin，不能用 *
        origin = self.cors_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Credentials", "true")
            self.send_header("Vary", "Origin")

        super().end_headers()

    def content_security_policy(self) -> str:
        """按页面给出 CSP，收紧可执行来源。

        前台要允许外链播放器（YouTube / 哔哩哔哩等）与外部图片；
        后台不嵌任何外部资源，只放行同源与 `--allow-origin` 列出的接口地址。
        前端已不使用内联脚本 / 内联样式，因此这里不需要 'unsafe-inline'。
        """
        origins = [origin for origin in (getattr(self.server, "allow_origins", []) or [])
                   if origin and origin != "*"]

        if self.path.startswith("/admin"):
            connect = " ".join(["'self'", *origins])
            return "; ".join([
                "default-src 'self'",
                "script-src 'self'",
                "style-src 'self'",
                "img-src 'self' data: https:",
                "media-src 'self' https:",
                f"connect-src {connect}",
                "font-src 'self'",
                "frame-ancestors 'self'",
                "base-uri 'self'",
                "form-action 'self'",
                "object-src 'none'",
            ])

        connect = " ".join(["'self'", *origins])
        return "; ".join([
            "default-src 'self'",
            "script-src 'self'",
            "style-src 'self'",
            "img-src 'self' data: https: http:",
            "media-src 'self' https: http:",
            "frame-src https: http:",              # 外链视频播放器
            f"connect-src {connect}",
            "font-src 'self'",
            "frame-ancestors 'self'",
            "base-uri 'self'",
            "form-action 'self'",
            "object-src 'none'",
        ])

    def list_directory(self, path):                            # noqa: ANN001
        """禁止目录列表：只提供具体文件，避免把目录内容整份摊开。"""
        self.send_error(HTTPStatus.FORBIDDEN, "Directory listing is disabled")
        return None

    def cors_origin(self) -> str:
        allowed = getattr(self.server, "allow_origins", []) or []
        if not allowed:
            return ""
        request_origin = (self.headers.get("Origin") or "").strip()
        if not request_origin:
            return ""
        return request_origin if (request_origin in allowed or "*" in allowed) else ""

    def _send(self, status: int, body: bytes, content_type: str,
              extra: list[tuple[str, str]] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or []):
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, payload: object, status: int = HTTPStatus.OK,
                  extra: list[tuple[str, str]] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", extra)

    def send_error_json(self, message: str, status: int = HTTPStatus.BAD_REQUEST,
                        extra: list[tuple[str, str]] | None = None) -> None:
        self.send_json({"ok": False, "error": message}, status, extra)

    # ---------- 请求体 ----------

    def handle_one_request(self) -> None:
        """每个请求开始时清空请求体缓存。

        关键：http.server 会用**同一个 Handler 实例**处理同一条 keep-alive 连接上的
        所有请求，所以请求级缓存必须在每个请求开头重置——否则第二个请求会读到
        上一个请求的 body（既是数据错乱，也是安全隐患）。
        """
        self._cached_body = None
        super().handle_one_request()

    def read_body(self) -> bytes:
        """读取并缓存请求体（缓存生命周期＝单个请求，见 handle_one_request）。

        必须保证「无论业务是否需要请求体，都把 Content-Length 指定的字节读完」：
        否则在 HTTP/1.1 keep-alive 连接上，残留字节会被当成下一个请求的开头，
        出现类似 `Unsupported method ('{}GET')` 的诡异错误。
        """
        if self._cached_body is not None:
            return self._cached_body

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.close_connection = True
            raise ApiError("Content-Length 不合法") from None

        if length <= 0:
            self._cached_body = b""
            return self._cached_body

        if length > MAX_BODY_BYTES:
            # 不读就无法保持连接同步，直接断开
            self.close_connection = True
            raise ApiError(
                f"请求体过大（{length / 1048576:.1f}MB，上限 {MAX_BODY_BYTES // 1048576}MB）",
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )

        data = self.rfile.read(length)
        self._cached_body = data or b""
        return self._cached_body

    def read_json(self) -> object:
        body = self.read_body()
        if not body:
            return {}
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(f"JSON 解析失败：{exc}") from exc

    def read_multipart(self) -> tuple[dict[str, str], dict[str, list[dict]]]:
        """解析 multipart/form-data。

        标准库的 cgi 模块在 Python 3.13 已被移除，这里用 email 解析器实现。
        同名文件字段会出现多次（批量上传），因此文件一律收集成列表。
        """
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            raise ApiError("该接口需要 multipart/form-data 请求")

        body = self.read_body()
        if not body:
            raise ApiError("请求体为空")

        raw = (
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
            + body
        )
        message = BytesParser(policy=policy.default).parsebytes(raw)

        if not message.is_multipart():
            raise ApiError("无法解析 multipart 内容")

        fields: dict[str, str] = {}
        files: dict[str, list[dict]] = {}

        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if not name:
                continue

            filename = part.get_filename()
            payload = part.get_payload(decode=True) or b""

            if filename:
                files.setdefault(name, []).append({
                    "filename": filename,
                    "data": payload,
                    "content_type": part.get_content_type(),
                })
            else:
                fields[name] = payload.decode("utf-8", "replace")

        return fields, files

    # ---------- 认证 / 会话 ----------

    @property
    def cookies(self) -> dict[str, str]:
        return auth.parse_cookies(self.headers.get("Cookie", ""))

    @property
    def secure_cookie(self) -> bool:
        """判断是否应给 Cookie 加 Secure：显式开启，或经 HTTPS 反代。"""
        if getattr(self.server, "force_secure_cookie", False):   # type: ignore[attr-defined]
            return True
        proto = (self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
        return proto == "https"

    def current_session(self) -> dict[str, object] | None:
        """返回当前会话 payload；未登录或已过期则为 None。"""
        if getattr(self.server, "auth_disabled", False):         # type: ignore[attr-defined]
            account = ACCOUNTS.account()
            return {"u": account.username if account else "dev", "dev": True}
        return SESSIONS.verify(self.cookies.get(auth.COOKIE_SESSION, ""))

    def csrf_matches(self) -> bool:
        """双提交校验：Cookie 里的 CSRF 值必须等于请求头里的值。"""
        cookie = self.cookies.get(auth.COOKIE_CSRF, "")
        header = self.headers.get("X-CSRF-Token", "")
        return bool(cookie) and bool(header) and hmac.compare_digest(cookie, header)

    def session_cookies(self, session_token: str, max_age: int,
                        csrf_token: str | None = None) -> list[tuple[str, str]]:
        secure = self.secure_cookie
        out = [
            ("Set-Cookie", auth.build_cookie(
                auth.COOKIE_SESSION, session_token, max_age=max_age, secure=secure,
            )),
        ]
        if csrf_token is not None:
            out.append(("Set-Cookie", auth.build_cookie(
                auth.COOKIE_CSRF, csrf_token, max_age=max_age, http_only=False, secure=secure,
            )))
        return out

    @staticmethod
    def expired_cookies() -> list[tuple[str, str]]:
        return [
            ("Set-Cookie", auth.clear_cookie(auth.COOKIE_SESSION)),
            ("Set-Cookie", auth.clear_cookie(auth.COOKIE_CSRF)),
        ]

    def require_auth(self, path: str, method: str) -> bool:
        """统一入口校验：公开接口免登录；所有写操作都要过 CSRF。"""
        mutating = method in ("POST", "PUT", "DELETE", "PATCH")

        # 公开接口（健康检查 / 会话查询 / 建号 / 登录 / 只读站点数据）不要求已登录，
        # 但登录与建号本身也校验 CSRF，防「登录 CSRF」把用户登进攻击者的账号
        public = path in PUBLIC_API or path.startswith(PUBLIC_PREFIXES)
        if not public and self.current_session() is None:
            self.send_error_json("未登录或会话已过期", HTTPStatus.UNAUTHORIZED)
            return False

        if mutating and not self.csrf_matches():
            self.send_error_json("CSRF 校验失败：请刷新页面后重试", HTTPStatus.FORBIDDEN)
            return False
        return True

    # ---------- 路由 ----------

    def do_GET(self) -> None:                                   # noqa: N802
        self._route("GET")

    def do_HEAD(self) -> None:                                  # noqa: N802
        self._route("GET")

    def do_POST(self) -> None:                                  # noqa: N802
        self._route("POST")

    def do_PUT(self) -> None:                                   # noqa: N802
        self._route("PUT")

    def do_DELETE(self) -> None:                                # noqa: N802
        self._route("DELETE")

    def do_OPTIONS(self) -> None:                               # noqa: N802
        """跨域预检（仅在配置了 --allow-origin 时响应）。"""
        if not self.cors_origin():
            self.send_error_json("未开启跨域访问", HTTPStatus.FORBIDDEN)
            return

        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-CSRF-Token")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def _route(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        params = parse_qs(parsed.query)

        # 带 body 的请求一律先读完，保证 keep-alive 连接上的后续请求不会错位
        if method in ("POST", "PUT", "DELETE", "PATCH"):
            try:
                self.read_body()
            except ApiError as exc:
                self.send_error_json(exc.message, exc.status)
                return

        try:
            if path.startswith("/api/"):
                if not self.require_auth(path, method):
                    return
                self.handle_api(method, path, params)
                return

            if method not in ("GET", "HEAD"):
                self.send_error_json("不支持的请求方法", HTTPStatus.METHOD_NOT_ALLOWED)
                return

            if path in ("/admin", "/admin/"):
                self.serve_admin_index()
                return
            if path.startswith("/admin/"):
                self.serve_admin_asset(path)
                return

            # 其余走静态资源（方便在同源模式下直接预览站点）
            if getattr(self.server, "serve_static", True):        # type: ignore[attr-defined]
                super().do_GET()
            else:
                self.send_error_json("静态资源已关闭（--no-static）", HTTPStatus.NOT_FOUND)
        except ApiError as exc:
            payload: dict[str, object] = {"ok": False, "error": exc.message}
            if exc.field_errors:
                payload["fieldErrors"] = exc.field_errors
            self.send_json(payload, exc.status)
        except auth.AuthError as exc:
            extra = [("Retry-After", str(exc.retry_after))] if exc.retry_after else None
            self.send_error_json(exc.message, exc.status, extra)
        except store.StoreError as exc:
            remember("保存失败", str(exc), ok=False)
            self.send_error_json(str(exc), HTTPStatus.CONFLICT)
        except BrokenPipeError:
            pass
        except Exception as exc:                                # noqa: BLE001
            traceback.print_exc()
            self.send_error_json(f"服务端异常：{exc}", HTTPStatus.INTERNAL_SERVER_ERROR)

    # ---------- 静态 ----------

    def static_allowed(self, target: Path) -> bool:
        """静态白名单：只有前端真正需要的路径才允许对外提供。

        以解析后的真实路径判断（而非请求字符串），因此
        `/assets/../admin.config.json` 这类穿越写法同样会被拒绝。
        """
        try:
            relative = target.resolve().relative_to(ROOT.resolve())
        except (OSError, ValueError):
            return False

        parts = relative.parts
        if any(part.startswith(".") for part in parts):
            return False                       # .git / .backups / .gitignore 等
        if not parts:
            return True                        # 站点根目录，交给 index.html 兜底
        if len(parts) == 1:
            # 根目录下的白名单文件，或允许目录本身（目录列表行为保持原样）
            return parts[0] in STATIC_ROOT_FILES or parts[0] in STATIC_ROOT_DIRS
        return parts[0] in STATIC_ROOT_DIRS

    def send_head(self):                       # noqa: ANN201
        if not self.static_allowed(Path(self.translate_path(self.path))):
            self.send_error(HTTPStatus.NOT_FOUND, "Not Found")
            return None
        return super().send_head()

    def not_modified(self, modified_at: float, extra: list[tuple[str, str]] | None = None) -> bool:
        """客户端带了 If-Modified-Since 且文件没变 → 回 304，省掉整包内容。"""
        since_header = self.headers.get("If-Modified-Since") or ""
        if not since_header:
            return False
        try:
            since = email_utils.parsedate_to_datetime(since_header).timestamp()
        except (TypeError, ValueError):
            return False
        if int(modified_at) > since:
            return False

        self.send_response(HTTPStatus.NOT_MODIFIED)
        self.send_header("Last-Modified", self.date_time_string(int(modified_at)))
        for key, value in (extra or []):
            self.send_header(key, value)
        self.end_headers()
        return True

    def serve_admin_index(self) -> None:
        index = ADMIN_DIR / "index.html"
        if not index.is_file():
            self.send_error_json("后台界面缺失：admin/index.html", HTTPStatus.NOT_FOUND)
            return
        self._send(HTTPStatus.OK, index.read_bytes(), "text/html; charset=utf-8")

    def serve_admin_asset(self, path: str) -> None:
        relative = path[len("/admin/"):]
        target = (ADMIN_DIR / relative).resolve()

        try:
            target.relative_to(ADMIN_DIR.resolve())
        except ValueError:
            self.send_error_json("非法路径", HTTPStatus.FORBIDDEN)
            return

        if not target.is_file():
            self.send_error_json(f"资源不存在：{relative}", HTTPStatus.NOT_FOUND)
            return

        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        modified_at = int(target.stat().st_mtime)
        if self.not_modified(modified_at):
            return
        self._send(HTTPStatus.OK, target.read_bytes(), ctype,
                   extra=[("Last-Modified", self.date_time_string(modified_at))])

    # ============================================================
    # API
    # ============================================================

    def handle_api(self, method: str, path: str, params: dict[str, list[str]]) -> None:
        segments = [s for s in path[len("/api/"):].split("/") if s]

        if not segments:
            raise ApiError("缺少接口路径", HTTPStatus.NOT_FOUND)

        head = segments[0]

        if head == "health":
            self.send_json({"ok": True, "time": time.strftime("%Y-%m-%d %H:%M:%S")})
            return

        if head == "auth":
            self.api_auth(method, segments[1:])
            return

        if head == "state" and method == "GET":
            self.api_state()
            return

        if head == "tools" and method == "GET":
            self.send_json({"ok": True, "tools": TOOLS.as_dict()})
            return

        if head == "public":
            self.api_public(method, segments[1:], params)
            return

        if head == "schema" and method == "GET":
            self.api_schema()
            return

        if head == "items":
            self.api_items(method, segments[1:], params)
            return

        if head == "videos" and method == "POST" and len(segments) >= 3 and segments[2] == "poster":
            self.api_video_poster(segments)
            return

        if head == "item" and method == "GET":
            self.api_item(segments[1:])
            return

        if head == "data":
            self.api_data(method, segments[1:])
            return

        if head == "upload":
            if method == "GET" and len(segments) > 1 and segments[1] == "options":
                self.upload_options()
                return
            if method == "POST":
                self.api_upload()
                return

        if head == "backups":
            self.api_backups(method, segments[1:])
            return

        if head == "export" and method == "GET":
            self.api_export()
            return

        if head == "import" and method == "POST":
            self.api_import()
            return

        if head == "files" and method == "GET":
            self.api_files()
            return

        raise ApiError(f"未知接口：{path}", HTTPStatus.NOT_FOUND)

    # ============================================================
    # 认证接口
    # ============================================================

    def api_auth(self, method: str, rest: list[str]) -> None:
        action = rest[0] if rest else ""

        if action == "session" and method == "GET":
            self.api_auth_session()
            return
        if action == "setup" and method == "POST":
            self.api_auth_setup()
            return
        if action == "login" and method == "POST":
            self.api_auth_login()
            return
        if action == "logout" and method == "POST":
            self.api_auth_logout()
            return
        if action == "password" and method == "POST":
            self.api_auth_password()
            return

        raise ApiError(f"未知的认证接口：/api/auth/{action}", HTTPStatus.NOT_FOUND)

    def api_auth_session(self) -> None:
        """前端启动时先调这里：拿到登录状态，并顺带下发 CSRF Cookie。"""
        session = self.current_session()
        account = ACCOUNTS.account()

        # 每次查询会话都保证客户端持有 CSRF Cookie，登录 / 建号时才能带上
        csrf = self.cookies.get(auth.COOKIE_CSRF) or auth.SessionManager.new_csrf_token()
        extra = [("Set-Cookie", auth.build_cookie(
            auth.COOKIE_CSRF, csrf, max_age=86400, http_only=False, secure=self.secure_cookie,
        ))]

        self.send_json({
            "ok": True,
            "authenticated": session is not None,
            "user": (account.as_public_dict() if session is not None and account else None),
            "setupRequired": not ACCOUNTS.has_account,
            "authDisabled": bool(getattr(self.server, "auth_disabled", False)),
            "expiresIn": SESSIONS.ttl_seconds if session is not None else 0,
        }, extra=extra)

    def api_auth_setup(self) -> None:
        """首次创建管理员账号（仅在还没有账号时可用）。"""
        if ACCOUNTS.has_account:
            raise ApiError("管理员账号已存在，请直接登录", HTTPStatus.CONFLICT)

        payload = self.read_json()
        if not isinstance(payload, dict):
            raise ApiError("请求体必须是 JSON 对象")

        username = str(payload.get("username") or "").strip()
        password = str(payload.get("password") or "")
        confirm = str(payload.get("confirm") or password)

        if password != confirm:
            raise ApiError("两次输入的密码不一致")

        account = ACCOUNTS.create_account(username, password)
        token, ttl = SESSIONS.issue(account)
        csrf = auth.SessionManager.new_csrf_token()

        remember("创建管理员", account.username)
        self.send_json(
            {"ok": True, "user": account.as_public_dict(), "expiresIn": ttl},
            extra=self.session_cookies(token, ttl, csrf),
        )

    def api_auth_login(self) -> None:
        payload = self.read_json()
        if not isinstance(payload, dict):
            raise ApiError("请求体必须是 JSON 对象")

        username = str(payload.get("username") or "").strip()
        password = str(payload.get("password") or "")
        ip = self.address_string()
        key = auth.client_key(ip, username)

        # 先查限流，被锁时直接拒绝且不再做昂贵的哈希运算
        wait = LIMITER.check(key)
        if wait > 0:
            remember("登录被限流", f"{username}@{ip}", ok=False)
            raise auth.AuthError(
                f"尝试次数过多，请 {wait} 秒后再试", status=HTTPStatus.TOO_MANY_REQUESTS, retry_after=wait,
            )

        if not ACCOUNTS.has_account:
            raise auth.AuthError("尚未创建管理员账号", status=HTTPStatus.PRECONDITION_REQUIRED)

        account = ACCOUNTS.verify(username, password)
        if account is None:
            remaining = LIMITER.record_failure(key)
            remember("登录失败", f"{username or '(空)'}@{ip}", ok=False)

            # 本次失败刚好触发锁定：直接回 429 并带上等待秒数，
            # 让界面立刻进入倒计时，而不是等用户再试一次才知道被锁
            blocked = LIMITER.check(key)
            if blocked > 0:
                raise auth.AuthError(
                    f"密码错误次数过多，请 {blocked} 秒后再试",
                    status=HTTPStatus.TOO_MANY_REQUESTS, retry_after=blocked,
                )

            raise auth.AuthError(
                f"用户名或密码不正确，还可尝试 {remaining} 次", status=HTTPStatus.UNAUTHORIZED,
            )

        LIMITER.record_success(key)
        token, ttl = SESSIONS.issue(account)
        csrf = auth.SessionManager.new_csrf_token()

        remember("登录成功", f"{account.username}@{ip}")
        self.send_json(
            {"ok": True, "user": account.as_public_dict(), "expiresIn": ttl},
            extra=self.session_cookies(token, ttl, csrf),
        )

    def api_auth_logout(self) -> None:
        session = self.current_session()
        if session:
            remember("退出登录", str(session.get("u", "")))

        # 会话 Cookie 清掉，但 CSRF Cookie 换一个新值继续保留：
        # 这样退出后紧接着的登录请求能直接带上 CSRF，不必多一次往返。
        extra = [("Set-Cookie", auth.clear_cookie(auth.COOKIE_SESSION))]
        extra.append(("Set-Cookie", auth.build_cookie(
            auth.COOKIE_CSRF, auth.SessionManager.new_csrf_token(),
            max_age=86400, http_only=False, secure=self.secure_cookie,
        )))
        self.send_json({"ok": True}, extra=extra)

    def api_auth_password(self) -> None:
        payload = self.read_json()
        if not isinstance(payload, dict):
            raise ApiError("请求体必须是 JSON 对象")

        current = str(payload.get("current") or "")
        new = str(payload.get("password") or "")
        confirm = str(payload.get("confirm") or new)

        if new != confirm:
            raise ApiError("两次输入的新密码不一致")

        session = self.current_session() or {}
        ip = self.address_string()
        key = auth.client_key(ip, str(session.get("u", "")))

        wait = LIMITER.check(key)
        if wait > 0:
            raise auth.AuthError(
                f"尝试次数过多，请 {wait} 秒后再试", status=HTTPStatus.TOO_MANY_REQUESTS, retry_after=wait,
            )

        try:
            ACCOUNTS.change_password(current, new)
        except auth.AuthError:
            LIMITER.record_failure(key)
            remember("改密失败", f"{session.get('u', '')}@{ip}", ok=False)
            raise

        LIMITER.record_success(key)
        remember("修改密码", str(session.get("u", "")))

        # 会话密钥里带的是旧密码指纹，改密后旧会话自动失效 → 直接签发新的
        account = ACCOUNTS.account()
        assert account is not None
        token, ttl = SESSIONS.issue(account)
        self.send_json(
            {"ok": True, "message": "密码已更新，其它设备的登录状态已失效", "expiresIn": ttl},
            extra=self.session_cookies(token, ttl),
        )

    # ---------- 状态 ----------

    def api_state(self) -> None:
        items = STORE.load_all()
        albums = items["albums"]

        orphan_report: list[str] = []
        album_ids = {str(a.get("id")) for a in albums}
        for collection in ("photos", "videos"):
            for item in items[collection]:
                album = str(item.get("album") or "")
                if album and album not in album_ids:
                    orphan_report.append(f"{item.get('id')} → 相册 {album}")

        # 找出被引用但不存在的媒体文件
        missing_report: list[str] = []
        for collection in ("photos", "videos"):
            for item in items[collection]:
                # 外链视频的 src 是链接/BV 号，不是本地路径，跳过检查
                is_embed = collection == "videos" and str(item.get("provider", "file")).lower() != "file"

                if collection == "videos" and not is_embed and not str(item.get("poster") or ""):
                    # 本地视频没有封面：前台只会显示占位块，因此在这里提示补封面
                    missing_report.append(
                        f"{item.get('id')}.poster → 未设置（本地视频建议生成封面：python tools/make_posters.py）"
                    )

                for key in ("src", "thumb", "poster"):
                    if key == "src" and is_embed:
                        continue
                    value = str(item.get(key) or "")
                    if value.startswith(("http://", "https://")):
                        continue
                    if value and not (ROOT / value).is_file():
                        missing_report.append(f"{item.get('id')}.{key} → {value}")

        self.send_json({
            "ok": True,
            "root": str(ROOT),
            "tools": TOOLS.as_dict(),
            "counts": {name: len(value) for name, value in items.items()},
            "albums": [{"id": a.get("id"), "name": a.get("name")} for a in albums],
            "orphanAlbums": orphan_report,
            "missingFiles": missing_report[:50],
            "missingFileCount": len(missing_report),
            "backups": [
                {**b.as_dict(), "sizeText": query.human_size(b.size)}
                for b in STORE.list_backups()[:20]
            ],
            "history": HISTORY[:20],
            "user": (ACCOUNTS.account().as_public_dict() if ACCOUNTS.account() else None),
            "authDisabled": bool(getattr(self.server, "auth_disabled", False)),
            "sessionHours": round(SESSIONS.ttl_seconds / 3600, 1),
        })

    # ============================================================
    # 公开只读接口（无需登录）
    # ============================================================

    def api_public(self, method: str, rest: list[str], params: dict[str, list[str]]) -> None:
        """作品集浏览所需的搜索 / 筛选 / 排序 / 分页全部在这里完成。"""
        if method != "GET":
            raise ApiError("公开接口只支持 GET", HTTPStatus.METHOD_NOT_ALLOWED)

        action = rest[0] if rest else "site"
        albums = STORE.load("albums")
        photos = STORE.load("photos")
        videos = STORE.load("videos")

        if action == "site":
            self.send_json(query.site_payload(albums, photos, videos, ROOT))
            return

        if action == "gallery":
            raw_tags = _first(params, "tags").replace("，", ",")
            tags = tuple(tag.strip() for tag in raw_tags.split(",") if tag.strip())
            page_size = query.parse_int(
                _first(params, "pageSize", "24"), 24, low=1, high=query.MAX_PAGE_SIZE,
            )
            self.send_json(query.gallery_payload(
                albums, photos, videos, ROOT,
                q=_first(params, "q"),
                kind=_first(params, "type", "all"),
                album=_first(params, "album"),
                tags=tags,
                sort=_first(params, "sort", "date-desc"),
                page=query.parse_int(_first(params, "page", "1"), 1, low=1),
                page_size=page_size,
            ))
            return

        if action == "albums":
            self.send_json({
                "ok": True,
                "albums": query.album_cards(albums, photos, videos, ROOT),
            })
            return

        if action == "exif":
            self.api_public_exif(_first(params, "path"))
            return

        raise ApiError(f"未知的公开接口：/api/public/{action}", HTTPStatus.NOT_FOUND)

    def api_public_exif(self, path_value: str) -> None:
        """按路径解析原文件 EXIF（原先在浏览器里做的解析，移到服务端）。"""
        relative = query.text(path_value).lstrip("/")
        if not relative:
            raise ApiError("缺少 path 参数")

        target = (ROOT / relative).resolve()
        try:
            inside = target.relative_to(IMAGE_DIR.resolve())
        except ValueError:
            raise ApiError("只能解析照片目录下的文件", HTTPStatus.FORBIDDEN) from None
        assert inside is not None
        if not target.is_file():
            raise ApiError("文件不存在", HTTPStatus.NOT_FOUND)

        try:
            entry = to_entry_exif(read_exif(target)) or {}
        except ExifError as exc:
            raise ApiError(f"EXIF 解析失败：{exc}") from None

        rows = [
            {"label": label, "value": query.text(entry.get(key))}
            for key, label in query.EXIF_LABELS["photo"]
            if query.text(entry.get(key))
        ]
        self.send_json({"ok": True, "path": relative, "exif": rows, "raw": entry})

    # ============================================================
    # 后台：表单 schema / 列表 / 单条
    # ============================================================

    def api_schema(self) -> None:
        """表单字段、排序项、上传限制都由后端给出，前端不再内置这些定义。"""
        self.send_json({
            "ok": True,
            "nouns": schema.nouns(),
            "collections": {
                name: {"fields": schema.fields_for(name)} for name in schema.collections()
            },
            "providers": schema.PROVIDERS,
            "sorts": query.ADMIN_SORTS,
            "columns": {name: query.admin_columns(name) for name in schema.collections()},
            "upload": self.upload_options_payload(),
        })

    def upload_options_payload(self) -> dict:
        albums = [
            {
                "value": query.text(album.get("id")),
                "label": query.text(album.get("name")) or query.text(album.get("id")),
            }
            for album in STORE.load("albums")
        ]
        return {
            "albums": albums,
            "accept": sorted(ALLOWED_UPLOAD_SUFFIXES),
            "maxFileBytes": MAX_UPLOAD_BYTES,
            "maxBatchBytes": MAX_BATCH_BYTES,
        }

    def upload_options(self) -> None:
        payload = self.upload_options_payload()
        self.send_json({"ok": True, "upload": payload, "tools": TOOLS.as_dict()})

    def api_items(self, method: str, rest: list[str], params: dict[str, list[str]]) -> None:
        """后台表格数据：搜索 / 相册 / 标签 / 排序 / 分页都在服务端。"""
        if not rest:
            raise ApiError("缺少集合名（albums / photos / videos）")

        collection = rest[0]
        if collection not in store.COLLECTIONS:
            raise ApiError(f"未知集合：{collection}", HTTPStatus.NOT_FOUND)

        if method == "GET":
            default_size = query.DEFAULT_PAGE_SIZE.get(collection, 200)
            self.send_json(query.admin_items_payload(
                STORE, ROOT, collection,
                q=_first(params, "q"),
                album=_first(params, "album"),
                tag=_first(params, "tag"),
                sort=_first(params, "sort"),
                page=query.parse_int(_first(params, "page", "1"), 1, low=1),
                page_size=query.parse_int(
                    _first(params, "pageSize", str(default_size)), default_size,
                    low=1, high=query.MAX_PAGE_SIZE,
                ),
            ))
            return

        if method == "POST":
            payload = self.read_json()
            if not isinstance(payload, dict):
                raise ApiError("提交内容必须是 JSON 对象")
            saved, warnings = self._save_item(collection, payload)
            self.send_json({"ok": True, "item": saved, "warnings": warnings})
            return

        if method == "DELETE" and len(rest) >= 2:
            item_id = rest[1]
            remove_file = (rest[2] if len(rest) > 2 else "") == "file"
            removed = self._remove_item(collection, item_id, remove_file)
            remember("删除条目", f"{collection} / {item_id}", ok=removed is not None)
            self.send_json({"ok": True, "deleted": item_id, "removedFiles": removed or []})
            return

        raise ApiError(f"不支持的请求：{method} {'/'.join(rest)}", HTTPStatus.METHOD_NOT_ALLOWED)

    def api_item(self, rest: list[str]) -> None:
        """单条详情：返回「已摊平」的表单值，前端直接填进控件即可。"""
        if len(rest) < 2:
            raise ApiError("缺少集合名与条目 id")

        collection, item_id = rest[0], rest[1]
        if collection not in store.COLLECTIONS:
            raise ApiError(f"未知集合：{collection}", HTTPStatus.NOT_FOUND)

        raw = next(
            (item for item in STORE.load(collection) if query.text(item.get("id")) == item_id),
            None,
        )
        if raw is None:
            raise ApiError(f"条目不存在：{item_id}", HTTPStatus.NOT_FOUND)

        self.send_json({
            "ok": True,
            "collection": collection,
            "id": item_id,
            "values": schema.flatten_item(collection, raw),
            "item": raw,
        })

    def _save_item(self, collection: str, payload: dict) -> tuple[dict, list[str]]:
        """新增与编辑共用一条路径：归一化 → 校验 → 合并已有条目 → 落盘。"""
        item = schema.normalize_submission(collection, payload)
        errors = schema.validate(collection, item)
        if errors:
            raise ApiError("提交内容有误，请检查标红的字段", HTTPStatus.BAD_REQUEST, errors)

        # 编辑时保留表单没有覆盖的字段（尤其是 EXIF 的其它键）
        existing = None
        item_id = query.text(item.get("id"))
        if item_id:
            existing = next(
                (row for row in STORE.load(collection) if query.text(row.get("id")) == item_id),
                None,
            )
        item = schema.merge_with_existing(item, existing)

        try:
            saved, warnings = STORE.upsert(collection, item)
        except store.StoreError as exc:
            raise ApiError(str(exc)) from exc

        remember("保存条目", f"{collection} / {saved.get('id')}")
        return saved, warnings

    # ---------- 数据集合 ----------

    def api_data(self, method: str, rest: list[str]) -> None:
        if not rest:
            raise ApiError("缺少集合名（albums / photos / videos）")

        collection = rest[0]
        if collection not in store.COLLECTIONS:
            raise ApiError(f"未知集合：{collection}", HTTPStatus.NOT_FOUND)

        if method == "GET" and len(rest) == 1:
            self.send_json({"ok": True, "collection": collection, "items": STORE.load(collection)})
            return

        if method == "PUT" and len(rest) == 1:
            payload = self.read_json()
            items = payload.get("items") if isinstance(payload, dict) else payload
            warnings = STORE.save(collection, items)
            remember("覆盖保存", f"{collection}：{len(items)} 条")
            self.send_json({"ok": True, "count": len(items), "warnings": warnings})
            return

        if method == "POST" and len(rest) == 1:
            payload = self.read_json()
            if not isinstance(payload, dict):
                raise ApiError("新增条目必须是 JSON 对象")
            saved, warnings = self._save_item(collection, payload)
            self.send_json({"ok": True, "item": saved, "warnings": warnings})
            return

        if method == "DELETE" and len(rest) >= 2:
            item_id = rest[1]
            remove_file = (rest[2] if len(rest) > 2 else "") == "file"
            deleted_files = self._remove_item(collection, item_id, remove_file)
            remember("删除条目", f"{collection} / {item_id}", ok=deleted_files is not None)
            self.send_json({"ok": True, "deleted": item_id, "removedFiles": deleted_files or []})
            return

        raise ApiError(f"不支持的请求：{method} {'/'.join(rest)}", HTTPStatus.METHOD_NOT_ALLOWED)

    def _remove_item(self, collection: str, item_id: str, remove_file: bool) -> list[str] | None:
        """删除条目，可选同时删除对应媒体文件。"""
        items = STORE.load(collection)
        target = next((i for i in items if str(i.get("id")) == item_id), None)
        if target is None:
            raise ApiError(f"条目不存在：{item_id}", HTTPStatus.NOT_FOUND)

        STORE.delete(collection, item_id)

        removed: list[str] = []
        if remove_file:
            for key in ("src", "thumb", "poster"):
                value = str(target.get(key) or "")
                if not value or value.startswith("http"):
                    continue
                candidate = (ROOT / value).resolve()
                try:
                    candidate.relative_to(ROOT.resolve())
                except ValueError:
                    continue                      # 越界路径一律不删
                if candidate.is_file() and candidate.parent in (
                    IMAGE_DIR, THUMB_DIR, VIDEO_DIR, POSTER_DIR
                ):
                    try:
                        candidate.unlink()
                        removed.append(value)
                    except OSError:
                        pass
        return removed

    # ---------- 上传 ----------

    def api_upload(self) -> None:
        """批量上传：一次请求可带多个文件，逐条返回结果，前端只负责展示。"""
        fields, files = self.read_multipart()

        uploads: list[dict] = []
        for name in ("files", "file"):
            uploads.extend(files.get(name) or [])

        if not uploads:
            raise ApiError("没有收到文件（字段名应为 files）")

        album = (fields.get("album") or "").strip() or "uncategorized"
        tags = [t.strip() for t in (fields.get("tags") or "").replace("，", ",").split(",") if t.strip()]

        # 上传时可选带一张封面图片（字段名 posterFile），用于本批次的视频
        cover = next((part for part in (files.get("posterFile") or []) if part.get("data")), None)

        results = [self._handle_upload(upload, fields, album, tags, cover) for upload in uploads]
        ok_count = sum(1 for result in results if result["ok"])
        video_count = sum(1 for result in results if result.get("kind") == "video" and result["ok"])

        warnings: list[str] = []
        if cover and not video_count:
            warnings.append("这次上传没有视频，附带的封面图片已忽略（照片请用「缩略图」）")

        remember("上传", f"{ok_count}/{len(results)} 个文件", ok=ok_count > 0)
        self.send_json({
            "ok": True,
            "results": results,
            "summary": {"total": len(results), "ok": ok_count, "failed": len(results) - ok_count},
            "warnings": warnings,
            "tools": TOOLS.as_dict(),
        })

    def _handle_upload(self, upload: dict, fields: dict[str, str], album: str,
                       tags: list[str], cover: dict | None = None) -> dict:
        """处理单个文件；失败只影响这一条，整体仍返回 200 供前端逐条显示。"""
        name = store.safe_filename(upload.get("filename") or "", fallback="upload")
        try:
            if not upload.get("data"):
                raise ApiError("文件内容为空")
            if len(upload["data"]) > MAX_UPLOAD_BYTES:
                raise ApiError(f"单个文件超过上限 {MAX_UPLOAD_BYTES // 1048576}MB")

            suffix = Path(name).suffix.lower()
            if suffix not in ALLOWED_UPLOAD_SUFFIXES:
                raise ApiError(
                    f"不支持的文件类型 {suffix or '(无扩展名)'}；"
                    f"允许：{', '.join(sorted(ALLOWED_UPLOAD_SUFFIXES))}"
                )

            kind = (fields.get("kind") or "auto").strip().lower()
            if kind == "auto":
                kind = "video" if suffix in media.VIDEO_SUFFIXES else "image"
            if kind not in ("image", "video"):
                raise ApiError(f"未知的 kind：{kind}")

            title = (fields.get("title") or "").strip() or Path(name).stem
            if kind == "image":
                entry, warnings = self._save_image(upload, name, album, title, tags, fields)
                collection = "photos"
            else:
                entry, warnings = self._save_video(upload, name, album, title, tags, fields, cover)
                collection = "videos"

            saved, store_warnings = STORE.upsert(collection, entry)
        except (ApiError, store.StoreError, OSError) as exc:
            return {"name": name, "ok": False, "error": str(exc)}

        return {
            "name": name,
            "ok": True,
            "id": saved.get("id"),
            "collection": collection,
            "kind": kind,
            "src": saved.get("src", ""),
            "thumb": saved.get("thumb", ""),
            "poster": saved.get("poster", ""),
            "exifSummary": query.exif_summary(saved, "video" if kind == "video" else "photo"),
            "warnings": list(warnings) + list(store_warnings),
        }

    def _save_image(self, upload: dict, original: str, album: str, title: str,
                    tags: list[str], fields: dict[str, str]) -> tuple[dict, list[str]]:
        target = store.unique_path(IMAGE_DIR, original)
        target.write_bytes(upload["data"])
        relative = store.ensure_relative(ROOT, target)

        warnings: list[str] = []
        entry: dict[str, object] = {
            "id": STORE.next_id("photos"),
            "title": title,
            "album": album,
            "src": relative,
            "thumb": relative,
            "tags": tags,
        }

        # 自动读取 EXIF
        if (fields.get("readExif", "1") not in ("0", "false")):
            try:
                exif = to_entry_exif(read_exif(target))
                if exif:
                    entry["exif"] = exif
                else:
                    warnings.append("未从图片中读到 EXIF 字段")
            except ExifError as exc:
                warnings.append(f"EXIF 读取跳过：{exc}")

        # 缩略图（有 ffmpeg 才做）
        if TOOLS.can_transcode and fields.get("makeThumb", "1") not in ("0", "false"):
            thumb = store.unique_path(THUMB_DIR, f"{target.stem}.jpg")
            ok, err = media.make_thumbnail(TOOLS.ffmpeg, target, thumb)   # type: ignore[arg-type]
            if ok:
                entry["thumb"] = store.ensure_relative(ROOT, thumb)
            else:
                warnings.append(f"缩略图生成失败，已回退到原图：{err}")
        elif target.stat().st_size > 5 * 1024 * 1024:
            warnings.append("未安装 ffmpeg，大图直接作为缩略图会拖慢加载，建议压缩后再上传")

        exif = entry.get("exif") or {}
        entry["date"] = (fields.get("date") or exif.get("dateTimeOriginal") or "").strip()
        entry["location"] = (fields.get("location") or "").strip()
        entry["description"] = (fields.get("description") or "").strip()
        return entry, warnings

    def _save_video(self, upload: dict, original: str, album: str, title: str,
                    tags: list[str], fields: dict[str, str],
                    cover: dict | None = None) -> tuple[dict, list[str]]:
        target = store.unique_path(VIDEO_DIR, original)
        target.write_bytes(upload["data"])
        relative = store.ensure_relative(ROOT, target)

        warnings: list[str] = []
        exif: dict[str, str] = {}
        item_id = STORE.next_id("videos")
        entry: dict[str, object] = {
            "id": item_id,
            "title": title,
            "album": album,
            "provider": "file",
            "src": relative,
            "poster": "",
            "posterTime": 0,
            "tags": tags,
        }

        # 一次探测拿到时长 / 分辨率 / 帧率 / 编码
        info: dict[str, object] = {}
        if TOOLS.can_probe:
            info = media.probe_media(TOOLS.ffprobe, target)     # type: ignore[arg-type]
            if info.get("duration"):
                entry["duration"] = info["duration"]
            if info.get("resolution"):
                entry["resolution"] = info["resolution"]
            if info.get("fps"):
                exif["fps"] = str(info["fps"])
            if info.get("codec"):
                exif["codec"] = str(info["codec"])
        else:
            warnings.append("未安装 ffprobe，无法自动读取时长与分辨率，请手动补充")

        # 封面：上传时带了图片就用它；否则抓帧，默认第 0 秒（第一帧）
        if cover and cover.get("data"):
            poster, problems = self._store_cover(item_id, cover)
            warnings.extend(problems)
            if poster:
                entry["poster"] = poster
                entry["posterTime"] = 0
        elif TOOLS.can_transcode and fields.get("makePoster", "1") not in ("0", "false"):
            seek = self.poster_time(fields)
            ok, err, poster = self._capture_poster(item_id, target, seek)
            if ok:
                entry["poster"] = poster
                entry["posterTime"] = round(seek, 3)
            else:
                warnings.append(f"封面抓帧失败：{err}")
        elif not TOOLS.can_transcode:
            warnings.append("未安装 ffmpeg，未生成封面；可在后台为该视频上传封面图片")

        if exif:
            entry["exif"] = exif
        entry["date"] = (fields.get("date") or "").strip()
        entry["location"] = (fields.get("location") or "").strip()
        entry["description"] = (fields.get("description") or "").strip()
        return entry, warnings

    # ---------- 视频封面 ----------

    @staticmethod
    def poster_time(fields: dict[str, str], default: float = 0.0) -> float:
        """抓帧时间点：默认 0（第一帧）；表单里填了非负数字就用表单值。

        接受两个字段名：上传表单用 `posterTime`，更换封面接口用 `time`。
        """
        raw = query.text(fields.get("time") or fields.get("posterTime"))
        if not raw:
            return default
        try:
            value = float(raw)
        except ValueError:
            return default
        return value if value >= 0 else default

    @staticmethod
    def _cover_suffix(filename: str) -> str:
        suffix = Path(store.safe_filename(filename, fallback="cover")).suffix.lower()
        return suffix if suffix in media.IMAGE_SUFFIXES else ""

    def _cover_target(self, item_id: str, suffix: str) -> Path:
        """一个视频只保留一张封面：assets/video/posters/<id><ext>。"""
        return POSTER_DIR / f"{store.safe_filename(item_id, fallback='video')}{suffix}"

    def _drop_stale_covers(self, item_id: str, keep: Path) -> None:
        """换封面后清掉同一视频的旧封面，避免 posters/ 越积越多。"""
        stem = store.safe_filename(item_id, fallback="video")
        for old in POSTER_DIR.glob(f"{stem}.*"):
            if old.resolve() != keep.resolve():
                try:
                    old.unlink()
                except OSError:
                    pass

    def _store_cover(self, item_id: str, cover: dict) -> tuple[str, list[str]]:
        """把上传的封面图片落盘，返回相对路径与提示。"""
        suffix = self._cover_suffix(cover.get("filename") or "")
        if not suffix:
            return "", ["封面必须是图片（jpg / png / webp / avif / tiff）"]
        target = self._cover_target(item_id, suffix)
        try:
            target.write_bytes(cover["data"])
        except OSError as exc:
            return "", [f"封面保存失败：{exc}"]
        self._drop_stale_covers(item_id, target)
        return store.ensure_relative(ROOT, target), []

    def _capture_poster(self, item_id: str, video: Path, seek: float) -> tuple[bool, str, str]:
        """从本地视频抓一帧当封面；seek=0 就是第一帧。"""
        if not TOOLS.can_transcode:
            return False, "未安装 ffmpeg，无法抓帧，请改为上传封面图片", ""
        target = self._cover_target(item_id, ".jpg")
        ok, err = media.extract_frame(TOOLS.ffmpeg, video, target, at=seek, width=1280)
        if not ok:
            return False, err, ""
        self._drop_stale_covers(item_id, target)
        return True, "", store.ensure_relative(ROOT, target)

    def api_video_poster(self, rest: list[str]) -> None:
        """为某个视频设置封面：上传一张图片，或从视频里抓一帧（默认第一帧）。"""
        if len(rest) < 3:
            raise ApiError("用法：POST /api/videos/{id}/poster")

        item_id = rest[1]
        item = next(
            (row for row in STORE.load("videos") if query.text(row.get("id")) == item_id), None,
        )
        if item is None:
            raise ApiError(f"视频不存在：{item_id}", HTTPStatus.NOT_FOUND)

        fields, files = self.read_multipart()
        cover = next((part for part in (files.get("file") or []) if part.get("data")), None)
        warnings: list[str] = []

        if cover:
            poster, problems = self._store_cover(item_id, cover)
            if not poster:
                raise ApiError(problems[0] if problems else "封面保存失败")
            warnings.extend(problems)
        else:
            provider = query.text(item.get("provider")) or "file"
            if provider != "file":
                raise ApiError("外链视频无法抓帧，请上传一张封面图片")
            src = query.text(item.get("src"))
            video = (ROOT / src).resolve() if src else None
            if video is None or not video.is_file():
                raise ApiError(f"视频文件不存在，无法抓帧：{src or '(未设置 src)'}")
            seek = self.poster_time(fields, default=0.0)
            ok, err, poster = self._capture_poster(item_id, video, seek)
            if not ok:
                raise ApiError(f"抓帧失败：{err}")
            item["posterTime"] = round(seek, 3)

        item["poster"] = poster
        saved, store_warnings = STORE.upsert("videos", item)
        warnings.extend(store_warnings)

        remember("更换视频封面", f"{item_id} ← {query.text(saved.get('poster'))}")
        self.send_json({
            "ok": True,
            "item": saved,
            "posterUrl": query.text(saved.get("poster")),
            "posterTime": saved.get("posterTime", 0),
            "warnings": warnings,
        })

    # ---------- 备份 ----------

    def api_backups(self, method: str, rest: list[str]) -> None:
        if method == "GET":
            self.send_json({
                "ok": True,
                "backups": [
                    {**b.as_dict(), "sizeText": query.human_size(b.size)}
                    for b in STORE.list_backups()
                ],
                "directory": str(STORE.backup_dir),
            })
            return

        if method == "POST" and len(rest) == 2 and rest[1] == "restore":
            token = rest[0]
            collection = STORE.restore_backup(token)
            remember("恢复备份", f"{token} → {collection}")
            self.send_json({"ok": True, "restored": collection, "from": token})
            return

        raise ApiError("不支持的备份操作", HTTPStatus.METHOD_NOT_ALLOWED)

    def api_export(self) -> None:
        bundle = STORE.export_bundle()
        body = json.dumps(bundle, ensure_ascii=False, indent=2).encode("utf-8")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        remember("导出备份", f"{stamp}.json")
        self._send(
            HTTPStatus.OK, body, "application/json; charset=utf-8",
            [("Content-Disposition", f'attachment; filename="photography-{stamp}.json"')],
        )

    def api_import(self) -> None:
        content_type = self.headers.get("Content-Type", "")

        if "multipart/form-data" in content_type:
            fields, files = self.read_multipart()
            upload = (files.get("file") or [None])[0]
            if not upload:
                raise ApiError("没有收到文件（字段名应为 file）")
            try:
                payload = json.loads(upload["data"].decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ApiError(f"导入文件不是合法 JSON：{exc}") from exc
            replace = (fields.get("mode") or "replace") != "merge"
        else:
            payload = self.read_json()
            replace = True

        summary = STORE.import_bundle(payload, replace=replace)
        remember("导入数据", f"{summary}（{'覆盖' if replace else '合并'}）")
        self.send_json({"ok": True, "imported": summary, "mode": "replace" if replace else "merge"})

    # ---------- 文件清单 ----------

    def api_files(self) -> None:
        def listing(directory: Path, url_prefix: str) -> list[dict]:
            if not directory.is_dir():
                return []
            files = []
            for path in sorted(directory.iterdir()):
                if not path.is_file() or path.name.startswith("."):
                    continue
                files.append({
                    "name": path.name,
                    "url": f"{url_prefix}/{path.name}",
                    "size": path.stat().st_size,
                })
            return files

        self.send_json({
            "ok": True,
            "images": listing(IMAGE_DIR, "assets/img/photos"),
            "thumbs": listing(THUMB_DIR, "assets/img/photos/thumbs"),
            "videos": listing(VIDEO_DIR, "assets/video"),
            "posters": listing(POSTER_DIR, "assets/video/posters"),
        })


# ============================================================
# 启动
# ============================================================

def local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def systemd_unit(port: int, session_hours: float) -> str:
    python = sys.executable
    return f"""[Unit]
Description=Collection of Time - Photography Admin
After=network.target

[Service]
Type=simple
WorkingDirectory={ROOT}
ExecStart={python} {ROOT / 'admin.py'} --host 127.0.0.1 --port {port} --session-hours {session_hours}
Restart=on-failure
RestartSec=3
User={os.environ.get('USER') or 'www-data'}

[Install]
WantedBy=multi-user.target
"""


def nginx_config(domain: str, port: int, name: str = "photography",
                 upstream: str = "127.0.0.1", tls: bool = True,
                 webroot: str = "/var/www/html") -> str:
    """生成 nginx 反向代理配置。

    tls=False 只输出 HTTP 段（申请证书前必须先有它，否则 443 段引用的
    证书文件还不存在，`nginx -t` 会直接失败）。

    关键点：转发 `X-Forwarded-Proto`，后端据此给会话 Cookie 打上 Secure，
    否则浏览器会带着一个可在明文链路里被截获的 Cookie 访问 HTTPS 站点。
    """
    cert_dir = f"/etc/letsencrypt/live/{domain}"

    header = (
        f"# Collection of Time - {name} 反向代理\n"
        f"# 由 `python admin.py --print-nginx --domain {domain}` 生成，改动前请先备份。\n"
        f"# 安装：/etc/nginx/sites-available/{name}.conf → 软链到 sites-enabled/ → nginx -t → reload\n"
    )

    http_block = f"""server {{
    listen 80;
    listen [::]:80;
    server_name {domain};

    # certbot --webroot 的校验文件走这里，其余请求全部跳 HTTPS
    location /.well-known/acme-challenge/ {{
        root {webroot};
        default_type "text/plain";
    }}

    location / {{
        return 308 https://$host$request_uri;
    }}
}}
"""

    if not tls:
        return header + "\n" + http_block

    root_path = str(ROOT)
    https_block = f"""server {{
    # 老版本 nginx 用这种写法；1.25.1+ 可改为 `listen 443 ssl;` 加 `http2 on;`
    # （新写法在旧版本上是未知指令，会让 nginx -t 直接失败，所以默认用兼容写法）
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name {domain};

    ssl_certificate     {cert_dir}/fullchain.pem;
    ssl_certificate_key {cert_dir}/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_session_cache   shared:SSL:10m;
    ssl_session_timeout 1d;
    ssl_session_tickets off;

    # HSTS：确认该域名只走 HTTPS 之后再考虑加 includeSubDomains
    add_header Strict-Transport-Security "max-age=31536000" always;

    # 首屏基本是 JS / CSS / JSON，压缩后体积约为原来的三成
    gzip on;
    gzip_vary on;
    gzip_min_length 512;
    gzip_proxied any;
    gzip_types text/plain text/css text/html application/javascript application/json
               application/xml image/svg+xml;

    # 上传 4K 视频可能几百 MB，别让 nginx 提前掐断
    client_max_body_size 512m;
    proxy_request_buffering off;
    proxy_http_version 1.1;
    proxy_read_timeout 300s;
    proxy_send_timeout 300s;

    # 后端的安全响应头（CSP 等）原样透传，不要在这里覆盖
    proxy_pass http://{upstream}:{port};

    # 后端据此判断「本次请求走的是 HTTPS」，从而给会话 Cookie 加 Secure
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header Host $host;

    # 纵深防御：源码 / 配置文件即便漏到站点目录也不会被下载
    location = /admin.config.json {{ return 404; }}
    location ~ /\\.(?!well-known) {{ return 404; }}

    # 想再快一档（几十人以上并发）：静态文件交给 nginx，Python 只跑 API。
    #   1) 让后端只跑 API：./run.sh start --host 127.0.0.1 --no-static
    #   2) 取消下面两段的注释（root 已按当前项目目录填好）
    # location / {{
    #     root {root_path};
    #     try_files $uri $uri/ /index.html;
    # }}
    # location /assets/ {{
    #     root {root_path};
    #     expires -1;            # 每次都重验证：命中 304 不传内容，改完立刻生效
    # }}
}}
"""

    return header + "\n" + http_block + "\n" + https_block


# ============================================================
# 账号维护（命令行）
# ============================================================

def _prompt_password(prompt: str = "密码") -> str:
    """读取密码。

    非交互部署（systemd / 容器 / run.sh）直接读环境变量 ADMIN_PASSWORD；
    只要设了就优先使用，避免“提示说会用却仍弹交互”的坑。
    """
    env = os.environ.get("ADMIN_PASSWORD") or ""
    if env:
        if not _valid_password(env):
            print("环境变量 ADMIN_PASSWORD 不符合要求（至少 8 位）。", file=sys.stderr)
            return ""
        print(f"（已从环境变量 ADMIN_PASSWORD 读取{prompt}）")
        return env

    if not sys.stdin.isatty():
        print("非交互环境需要设置 ADMIN_PASSWORD 环境变量。", file=sys.stderr)
        return ""

    while True:
        value = getpass.getpass(f"{prompt}（至少 8 位）：")
        if not _valid_password(value):
            print("  太短了，请至少输入 8 位。")
            continue
        if value != getpass.getpass("再输一次确认："):
            print("  两次输入不一致，请重来。")
            continue
        return value


def _valid_password(value: str) -> bool:
    return len(value) >= 8


def _default_username() -> str:
    """非交互部署用 ADMIN_USERNAME 指定账号名。"""
    return (os.environ.get("ADMIN_USERNAME") or "").strip()


def command_create_user(username: str = "") -> int:
    existing = ACCOUNTS.account()
    if existing is not None:
        print(f"管理员账号已存在：{existing.username}")
        print("如需改密：python admin.py --reset-password")
        return 1

    name = username.strip() or _default_username()
    if not name:
        name = (input("用户名 [admin]：").strip() or "admin") if sys.stdin.isatty() else "admin"

    password = _prompt_password()
    if not password:
        print("\n未创建账号（没有拿到可用密码）。", file=sys.stderr)
        return 1
    account = ACCOUNTS.create_account(name, password)
    print(f"\n已创建管理员账号：{account.username}")
    print(f"配置文件：{CONFIG_PATH}（已设为仅本人可读）")
    print("现在可以启动服务并登录：python admin.py")
    return 0


def command_reset_password(username: str = "") -> int:
    account = ACCOUNTS.account()
    if account is None:
        print("还没有管理员账号，请先执行：python admin.py --create-user")
        return 1

    target = username.strip() or _default_username() or account.username
    if target != account.username:
        print(f"账号不匹配（当前账号：{account.username}）")
        return 1

    password = _prompt_password("新密码")
    if not password:
        print("\n未修改密码（没有拿到可用密码）。", file=sys.stderr)
        return 1
    ACCOUNTS.load()["account"] = {
        **account.record,
        **auth.hash_password(password),
        "updatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    ACCOUNTS.load()["account"]["username"] = account.username
    ACCOUNTS.save()
    ACCOUNTS.rotate_session_secret()      # 顺手让所有旧会话失效

    print(f"\n已重置 {account.username} 的密码，所有已登录设备需要重新登录。")
    return 0


def command_ffmpeg_status() -> int:
    """打印 FFmpeg 探测结果（不启动服务）。"""
    tools = media.detect_tools(ROOT, verify=True)
    print(f"来源   : {tools.source_label}")
    print(f"ffmpeg : {tools.ffmpeg or '未找到'}")
    print(f"ffprobe: {tools.ffprobe or '未找到'}")

    if tools.version:
        print(f"版本   : {tools.version}")
    elif tools.available:
        print("版本   : 无法执行——二进制可能不完整或架构不匹配")
        return 1
    else:
        print("\n未安装不影响后台使用（仅跳过缩略图与视频封面）。")
        print("如需启用： python tools/fetch_ffmpeg.py   或   ./run.sh install-ffmpeg")
    return 0 if tools.available else 1


def command_fetch_ffmpeg(extra: list[str]) -> int:
    """转交给 tools/fetch_ffmpeg.py，避免出现两套下载逻辑。"""
    script = ROOT / "tools" / "fetch_ffmpeg.py"
    if not script.is_file():
        print(f"找不到下载脚本：{script}", file=sys.stderr)
        return 1
    return subprocess.call([sys.executable, str(script), *extra])


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Photography 后台服务（仅标准库）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python admin.py --create-user        创建管理员账号\n"
               "  python admin.py                      启动服务（默认 127.0.0.1:8080）\n"
               "  python admin.py --print-systemd      生成 systemd 单元文件\n",
    )
    parser.add_argument("--host", default="127.0.0.1",
                        help="监听地址，默认 127.0.0.1（仅本机，配合 SSH 转发最安全）")
    parser.add_argument("--port", type=int, default=8080, help="监听端口，默认 8080")
    parser.add_argument("--session-hours", type=float, default=12.0,
                        help="登录会话有效期（小时），默认 12")
    parser.add_argument("--secure-cookie", action="store_true",
                        help="强制给 Cookie 加 Secure（HTTPS 反代下会自动识别，一般不用加）")
    parser.add_argument("--no-auth", action="store_true",
                        help="关闭认证（仅允许在本机监听时使用，用于本地调试）")
    parser.add_argument("--no-static", action="store_true",
                        help="不提供静态文件，只跑 API（前后端分开部署时用）")
    parser.add_argument("--allow-origin", action="append", default=[],
                        metavar="ORIGIN",
                        help="允许跨域的前端地址，可重复；例如 https://admin.example.com")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    parser.add_argument("--print-systemd", action="store_true",
                        help="打印 systemd 单元文件后退出")
    parser.add_argument("--print-nginx", action="store_true",
                        help="打印 nginx HTTPS 反向代理配置后退出（配合 --domain 使用）")
    parser.add_argument("--domain", default="", metavar="HOST",
                        help="对外域名，例如 photos.example.com（--print-nginx 用）")
    parser.add_argument("--nginx-name", default="photography", metavar="NAME",
                        help="nginx 站点配置名，默认 photography（--print-nginx 用）")
    parser.add_argument("--http-only", action="store_true",
                        help="只生成 HTTP 段（申请证书前用），配合 --print-nginx")
    parser.add_argument("--ffmpeg-status", action="store_true",
                        help="打印 FFmpeg 探测结果后退出")
    parser.add_argument("--fetch-ffmpeg", action="store_true",
                        help="下载对应平台的 FFmpeg 静态构建到 bin/ 后退出")
    parser.add_argument("--create-user", nargs="?", const="", default=None,
                        metavar="USERNAME", help="创建管理员账号后退出")
    parser.add_argument("--reset-password", nargs="?", const="", default=None,
                        metavar="USERNAME", help="重置管理员密码后退出")

    args = parser.parse_args()

    if args.create_user is not None:
        return command_create_user(args.create_user)
    if args.reset_password is not None:
        return command_reset_password(args.reset_password)

    if args.ffmpeg_status:
        return command_ffmpeg_status()
    if args.fetch_ffmpeg:
        return command_fetch_ffmpeg([])

    if args.print_systemd:
        print(systemd_unit(args.port, args.session_hours))
        return 0

    if args.print_nginx:
        if not args.domain:
            print("缺少 --domain，例如：python admin.py --print-nginx --domain photos.example.com",
                  file=sys.stderr)
            return 2
        print(nginx_config(args.domain, args.port, name=args.nginx_name, tls=not args.http_only))
        return 0

    external = args.host not in ("127.0.0.1", "localhost", "::1")

    if args.no_auth and external:
        print("拒绝启动：--no-auth 只能与 127.0.0.1 一起使用", file=sys.stderr)
        return 2

    SESSIONS.ttl_seconds = max(300, int(args.session_hours * 3600))

    if external and not args.secure_cookie:
        print("警告：当前以明文 HTTP 对外监听，密码与会话 Cookie 会以明文传输。", file=sys.stderr)
        print("      请放在 HTTPS 反向代理之后（识别到 X-Forwarded-Proto: https 时 Cookie 会自动带 Secure），",
              file=sys.stderr)
        print("      或改用 SSH 端口转发访问（见 DEPLOY.md「部署方式」）。", file=sys.stderr)

    if not ADMIN_DIR.is_dir():
        print(f"警告：后台界面目录不存在：{ADMIN_DIR}", file=sys.stderr)
    if not TOOLS.available:
        print("提示：未检测到 ffmpeg，视频封面与图片缩略图将跳过（上传仍可用）", file=sys.stderr)
        print("      需要的话可自带静态构建：python tools/fetch_ffmpeg.py 或 ./run.sh install-ffmpeg",
              file=sys.stderr)

    ThreadingHTTPServer.allow_reuse_address = True
    ThreadingHTTPServer.daemon_threads = True

    try:
        httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        print(f"无法监听 {args.host}:{args.port} → {exc}", file=sys.stderr)
        return 1

    httpd.auth_disabled = args.no_auth                    # type: ignore[attr-defined]
    httpd.force_secure_cookie = args.secure_cookie        # type: ignore[attr-defined]
    httpd.serve_static = not args.no_static               # type: ignore[attr-defined]
    httpd.allow_origins = list(args.allow_origin)         # type: ignore[attr-defined]

    display_host = "127.0.0.1" if not external else args.host
    url = f"http://{display_host}:{args.port}/admin/"

    account = ACCOUNTS.account()
    if account is None:
        account_text = "未创建（首次打开界面会引导创建）"
    else:
        account_text = account.username

    print("Photography 后台已启动")
    print(f"  管理界面 : {url}")
    if not args.no_static:
        print(f"  站点预览 : http://{display_host}:{args.port}/")
    print(f"  数据目录 : {STORE.data_dir}")
    print(f"  登录账号 : {account_text}")
    print(f"  会话时长 : {SESSIONS.ttl_seconds / 3600:.1f} 小时")
    print(f"  认证     : {'已关闭（--no-auth）' if args.no_auth else 'HttpOnly Cookie 会话'}")
    print(f"  FFmpeg   : {TOOLS.source_label}" + (f"（{TOOLS.version[:60]}）" if TOOLS.version else ""))
    if not TOOLS.available:
        print("             → 可执行 ./run.sh install-ffmpeg 下载静态构建到 bin/")
    if args.allow_origin:
        print(f"  跨域白名单 : {', '.join(args.allow_origin)}")
    if external:
        print(f"  访问地址 : http://{local_ip()}:{args.port}/admin/")
    if account is None:
        print("\n  下一步：在浏览器里创建管理员账号，或执行 python admin.py --create-user")
    print("  按 Ctrl+C 停止。")

    if not args.no_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:                      # noqa: BLE001
            pass

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
