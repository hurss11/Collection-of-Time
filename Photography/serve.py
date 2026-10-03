# -*- coding: utf-8 -*-
"""Photography 项目的本地静态服务器。

用途：浏览器的 fetch / ES Module 在 file:// 协议下会被拦截，
      通过本脚本以 HTTP 方式访问即可正常加载站点。

除静态文件外，本脚本还提供与正式后台**完全相同**的公开只读接口
（/api/public/*），因此 `python serve.py` 即可单独预览整个作品集，
不必启动 admin.py。查询逻辑复用 adminlib.query，两端不会跑偏。

用法：
    python serve.py            # 默认 http://127.0.0.1:8000
    python serve.py 8080       # 指定端口
"""
from __future__ import annotations

import functools
import http.server
import json
import os
import sys
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from adminlib import query, ranges, store                           # noqa: E402
from adminlib.exifread import ExifError, read_exif, to_entry_exif  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
ROOT_PATH = Path(ROOT)
DEFAULT_PORT = 8000
IMAGE_DIR = ROOT_PATH / "assets" / "img" / "photos"

# 静态白名单：与 admin.py 保持一致，项目根目录下的后端源码、adminlib/、
# admin.config.json（含密码哈希与会话密钥）等一律不对外提供。
STATIC_ROOT_FILES = {"index.html", "favicon.svg"}
STATIC_ROOT_DIRS = {"assets", "data"}

STORE = store.Store(ROOT_PATH)


def first(params: dict[str, list[str]], key: str, default: str = "") -> str:
    values = params.get(key) or []
    return (values[0] if values else "") or default


class Handler(http.server.SimpleHTTPRequestHandler):
    """开发用静态处理器：禁用缓存，便于修改后刷新即生效；另带公开只读接口。"""

    server_version = "CollectionOfTimeDev"   # 不暴露版本号与 Python 版本
    sys_version = ""

    def version_string(self) -> str:
        return self.server_version

    def cache_control(self) -> str:
        """静态资源缓存但重验证（304 省流量且改完立刻生效），其余不缓存。"""
        path = self.path.split("?", 1)[0]
        if path.startswith(("/assets/", "/admin/js/", "/admin/css/")) or path == "/favicon.svg":
            return "public, no-cache"
        return "no-store, must-revalidate"

    def end_headers(self) -> None:  # noqa: D102
        self.send_header("Cache-Control", self.cache_control())
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "same-origin")
        if getattr(self, "_accept_ranges", False):
            self.send_header("Accept-Ranges", "bytes")
        # 与 admin.py 保持一致：前端不用内联脚本 / 样式，因此无需 'unsafe-inline'
        self.send_header("Content-Security-Policy", "; ".join([
            "default-src 'self'",
            "script-src 'self'",
            "style-src 'self'",
            "img-src 'self' data: https: http:",
            "media-src 'self' https: http:",
            "frame-src https: http:",
            "connect-src 'self'",
            "font-src 'self'",
            "frame-ancestors 'self'",
            "base-uri 'self'",
            "form-action 'self'",
            "object-src 'none'",
        ]))
        super().end_headers()

    def list_directory(self, path):                             # noqa: ANN001
        """禁止目录列表（与 admin.py 一致）。"""
        self.send_error(http.HTTPStatus.FORBIDDEN, "Directory listing is disabled")
        return None

    def static_allowed(self, target: Path) -> bool:
        """静态白名单：只放行前端真正需要的路径，其余一律 404。

        以解析后的真实路径判断（而非请求字符串），因此
        `/assets/../admin.config.json` 这类穿越写法同样会被拒绝。
        """
        try:
            relative = target.resolve().relative_to(ROOT_PATH.resolve())
        except (OSError, ValueError):
            return False

        parts = relative.parts
        if any(part.startswith(".") for part in parts):
            return False                       # .git / .backups / .gitignore 等
        if not parts:
            return True                        # 站点根目录，交给 index.html 兜底
        if len(parts) == 1:
            return parts[0] in STATIC_ROOT_FILES or parts[0] in STATIC_ROOT_DIRS
        return parts[0] in STATIC_ROOT_DIRS

    def send_head(self):                       # noqa: ANN201
        target = Path(self.translate_path(self.path))
        if not self.static_allowed(target):
            self.send_error(http.HTTPStatus.NOT_FOUND, "Not Found")
            return None

        if target.is_file():
            # 视频进度条 / 拖拽需要 206（详见 adminlib/ranges.py）
            self._accept_ranges = True
            size = target.stat().st_size
            rng = ranges.parse_single_range(self.headers.get("Range") or "", size)
            if rng is not None:
                return ranges.send_partial(self, target, rng[0], rng[1])

        return super().send_head()

    def log_message(self, fmt: str, *args) -> None:  # noqa: D102
        sys.stderr.write("  %s\n" % (fmt % args))

    def do_GET(self) -> None:                                   # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if path.startswith("/api/public/"):
            self.handle_public(path[len("/api/public/"):], parse_qs(parsed.query))
            return
        self._accept_ranges = False
        handle = self.send_head()
        if not handle:
            return
        try:
            if self.command != "HEAD":          # HEAD 只回头，不写体
                self.copyfile(handle, self.wfile)
        finally:
            handle.close()

    def do_HEAD(self) -> None:                                  # noqa: N802
        self.do_GET()

    # ---------- 公开只读接口 ----------

    def send_json(self, payload: object, status: int = http.HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def handle_public(self, action: str, params: dict[str, list[str]]) -> None:
        action = action.strip("/") or "site"
        albums = STORE.load("albums")
        photos = STORE.load("photos")
        videos = STORE.load("videos")

        if action == "site":
            self.send_json(query.site_payload(albums, photos, videos, ROOT_PATH))
            return

        if action == "albums":
            self.send_json({
                "ok": True,
                "albums": query.album_cards(albums, photos, videos, ROOT_PATH),
            })
            return

        if action == "gallery":
            raw_tags = first(params, "tags").replace("，", ",")
            tags = tuple(tag.strip() for tag in raw_tags.split(",") if tag.strip())
            self.send_json(query.gallery_payload(
                albums, photos, videos, ROOT_PATH,
                q=first(params, "q"),
                kind=first(params, "type", "all"),
                album=first(params, "album"),
                tags=tags,
                sort=first(params, "sort", "date-desc"),
                page=query.parse_int(first(params, "page", "1"), 1, low=1),
                page_size=query.parse_int(
                    first(params, "pageSize", "24"), 24, low=1, high=query.MAX_PAGE_SIZE,
                ),
            ))
            return

        if action == "exif":
            self.handle_public_exif(first(params, "path"))
            return

        self.send_json({"ok": False, "error": f"未知的公开接口：{action}"},
                       http.HTTPStatus.NOT_FOUND)

    def handle_public_exif(self, path_value: str) -> None:
        relative = query.text(path_value).lstrip("/")
        target = (ROOT_PATH / relative).resolve() if relative else ROOT_PATH
        try:
            target.relative_to(IMAGE_DIR.resolve())
        except ValueError:
            self.send_json({"ok": False, "error": "只能解析照片目录下的文件"},
                           http.HTTPStatus.FORBIDDEN)
            return
        if not relative or not target.is_file():
            self.send_json({"ok": False, "error": "文件不存在"}, http.HTTPStatus.NOT_FOUND)
            return

        try:
            entry = to_entry_exif(read_exif(target)) or {}
        except ExifError as exc:
            self.send_json({"ok": False, "error": f"EXIF 解析失败：{exc}"},
                           http.HTTPStatus.BAD_REQUEST)
            return

        rows = [
            {"label": label, "value": query.text(entry.get(key))}
            for key, label in query.EXIF_LABELS["photo"]
            if query.text(entry.get(key))
        ]
        self.send_json({"ok": True, "path": relative, "exif": rows, "raw": entry})


class Server(http.server.ThreadingHTTPServer):
    """线程化的预览服务。

    媒体元素会为同一份视频并发发多个 Range 请求（而且要能中途取消），
    单线程服务器上这些请求会互相排队，拖进度条就会卡；另外客户端掐断连接时
    socketserver 默认会打一整段 traceback，这里静默处理。
    """

    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address) -> None:     # noqa: ANN001
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    handler = functools.partial(Handler, directory=ROOT)

    with Server(("127.0.0.1", port), handler) as httpd:
        url = f"http://127.0.0.1:{port}/"
        print(f"Photography 已启动：{url}")
        print(f"  公开接口：{url}api/public/site")
        print("按 Ctrl+C 停止服务。")
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n已停止。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
