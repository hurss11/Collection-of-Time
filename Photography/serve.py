# -*- coding: utf-8 -*-
"""Photography 项目的本地静态服务器。

用途：浏览器的 fetch / ES Module 在 file:// 协议下会被拦截，
      通过本脚本以 HTTP 方式访问即可正常加载 JSON 数据与解析 EXIF。

用法：
    python serve.py            # 默认 http://127.0.0.1:8000
    python serve.py 8080       # 指定端口
"""
from __future__ import annotations

import functools
import http.server
import os
import socketserver
import sys
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PORT = 8000


class Handler(http.server.SimpleHTTPRequestHandler):
    """开发用静态处理器：禁用缓存，便于修改后刷新即生效。"""

    def end_headers(self) -> None:  # noqa: D102
        self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

    def log_message(self, fmt: str, *args) -> None:  # noqa: D102
        sys.stderr.write("  %s\n" % (fmt % args))


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    handler = functools.partial(Handler, directory=ROOT)

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", port), handler) as httpd:
        url = f"http://127.0.0.1:{port}/"
        print(f"Photography 已启动：{url}")
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
