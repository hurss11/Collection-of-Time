#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""线上部署自检：确认 HTTPS 反代 + Secure Cookie 是真的生效。

背景：后台的会话 Cookie 只有在「请求确实走 HTTPS」时才带 Secure 标记
（识别 `X-Forwarded-Proto: https`，或显式 `--secure-cookie`）。反代漏配
这个转发头时，页面看着一切正常，但 Cookie 会在明文链路上裸奔。
本脚本就是从外部把这条链路验一遍，避免「配了反代就算完成」。

用法：
    python tools/check_https.py https://photos.example.com
    python tools/check_https.py https://photos.example.com --insecure      # 自签证书
    python tools/check_https.py https://photos.example.com --json          # 机器可读
    python tools/check_https.py https://photos.example.com \\
        --username admin --password '你的密码'                              # 连会话 Cookie 一起验

退出码：0 全部通过（允许 WARN），1 有 FAIL，2 参数或网络错误。

注意：带上 --username/--password 时会真的登录一次。密码错误会吃掉一次
      登录失败次数（5 次 / 5 分钟锁定），别在脚本里反复试错密码。
"""
from __future__ import annotations

import argparse
import http.client
import json
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlparse

DEFAULT_TIMEOUT = 15.0
SECURITY_HEADERS = (
    "Content-Security-Policy",
    "X-Content-Type-Options",
    "X-Frame-Options",
    "Referrer-Policy",
)
BLOCKED_PATHS = ("/admin.config.json", "/admin.py", "/adminlib/query.py", "/run.sh")
OPEN_PATHS = ("/data/", "/assets/img/")


class Reporter:
    """收集结果并按项目风格输出（[通过] / [注意] / [失败]）。"""

    def __init__(self) -> None:
        self.results: list[dict[str, object]] = []

    def add(self, ok: bool | None, name: str, detail: str) -> None:
        level = "pass" if ok is True else "warn" if ok is None else "fail"
        self.results.append({"level": level, "name": name, "detail": detail})
        mark = {"pass": "[通过]", "warn": "[注意]", "fail": "[失败]"}[level]
        print(f"{mark} {name}  -- {detail}")

    @property
    def failed(self) -> list[dict[str, object]]:
        return [r for r in self.results if r["level"] == "fail"]

    @property
    def warned(self) -> list[dict[str, object]]:
        return [r for r in self.results if r["level"] == "warn"]

    def summary(self) -> None:
        total = len(self.results)
        print("\n" + "=" * 58)
        print(f"合计 {total} 项，通过 {total - len(self.failed) - len(self.warned)} 项，"
              f"注意 {len(self.warned)} 项，失败 {len(self.failed)} 项")
        if self.failed:
            print("失败项：")
            for item in self.failed:
                print(f"  - {item['name']}：{item['detail']}")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """不自动跟随跳转，才能看见 3xx 本身。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):      # noqa: ANN001
        return None


def fetch(url: str, *, method: str = "GET", headers: dict[str, str] | None = None,
          body: bytes | None = None, context: ssl.SSLContext | None = None,
          timeout: float = DEFAULT_TIMEOUT) -> tuple[int, list[tuple[str, str]], str]:
    """发一次请求，4xx/5xx/3xx 都不抛异常，返回 (状态码, 响应头, 正文片段)。"""
    handlers: list[urllib.request.BaseHandler] = [NoRedirect()]
    if context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=context))
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(65536)
            return response.status, list(response.headers.items()), raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read(65536) if exc.fp else b""
        return exc.code, list(exc.headers.items()) if exc.headers else [], raw.decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        raise ConnectionError(str(exc.reason)) from exc


def header_of(headers: list[tuple[str, str]], name: str) -> str:
    for key, value in headers:
        if key.lower() == name.lower():
            return value
    return ""


def all_headers(headers: list[tuple[str, str]], name: str) -> list[str]:
    return [value for key, value in headers if key.lower() == name.lower()]


def parse_cookies(headers: list[tuple[str, str]]) -> dict[str, dict[str, object]]:
    """把 Set-Cookie 拆成 {名字: {属性: 值}}，属性名小写便于判断 Secure/HttpOnly。"""
    cookies: dict[str, dict[str, object]] = {}
    for raw in all_headers(headers, "Set-Cookie"):
        parts = [p.strip() for p in raw.split(";") if p.strip()]
        if not parts or "=" not in parts[0]:
            continue
        name, _, value = parts[0].partition("=")
        attrs: dict[str, object] = {"value": value}
        for part in parts[1:]:
            key, _, attr_value = part.partition("=")
            attrs[key.strip().lower()] = attr_value or True
        cookies[name.strip()] = attrs
    return cookies


def check_server_header(server: str) -> tuple[bool | None, str]:
    """Server 头不该暴露运行时；但反代往往会用自己的 Server（nginx/1.24.0）。

    所以分三档：Python/BaseHTTP 这类运行时泄露算失败，反代自身版本只提示，
    既不会把 nginx 的正常行为误报成问题，也不会漏掉「后端裸奔」的情况。
    """
    if not server:
        return True, "未返回 Server 头"

    lowered = server.lower()
    runtime_markers = ("python", "basehttp", "simplehttp", "werkzeug", "gunicorn", "uvicorn")
    if any(marker in lowered for marker in runtime_markers):
        return False, f"暴露运行时信息：{server}"

    proxy_names = ("nginx", "apache", "caddy", "traefik", "envoy", "cloudflare", "openresty", "haproxy")
    if any(lowered.startswith(name) for name in proxy_names):
        if any(ch.isdigit() for ch in server):
            return None, f"反代自身的版本号：{server}（可用 server_tokens off 去掉）"
        return True, server

    if any(ch.isdigit() for ch in server):
        return None, f"含版本号：{server}"
    return True, server


def check_tls_cert(host: str, port: int, context: ssl.SSLContext,
                   insecure: bool) -> tuple[bool | None, str]:
    """看证书链是否可信、是否覆盖该域名、还有多久到期。"""
    if insecure:
        return None, "已指定 --insecure，跳过证书校验"
    try:
        conn = http.client.HTTPSConnection(host, port, timeout=DEFAULT_TIMEOUT, context=context)
        conn.connect()
        cert = conn.sock.getpeercert() or {}          # type: ignore[union-attr]
        conn.close()
    except (OSError, ssl.SSLError) as exc:
        return False, f"TLS 握手失败：{exc}"

    if not cert:
        return None, "未能读取证书信息（可能是自签证书）"

    names = [value for kind, value in cert.get("subjectAltName", ()) if kind == "DNS"]
    matched = host in names or any(
        name.startswith("*.") and host.endswith(name[1:]) and host.count(".") == name.count(".")
        for name in names
    )
    not_after = cert.get("notAfter")
    left_days = None
    if not_after:
        seconds = ssl.cert_time_to_seconds(not_after)
        left_days = int((seconds - time.time()) // 86400)

    if not matched:
        return False, f"证书不覆盖 {host}（SAN：{', '.join(names) or '无'}）"
    if left_days is not None and left_days < 0:
        return False, f"证书已于 {not_after} 过期"
    if left_days is not None and left_days < 14:
        return None, f"证书 {not_after} 到期，只剩 {left_days} 天，记得续期"
    return True, f"证书有效，{not_after} 到期（剩 {left_days} 天）"


def check_redirect(host: str, http_port: int) -> tuple[bool, str]:
    """明文 80 端口必须跳到 HTTPS，否则密码还是会以明文发出去。"""
    url = f"http://{host}:{http_port}/"
    status, headers, _ = fetch(url)
    if status in (301, 302, 307, 308):
        location = header_of(headers, "Location")
        if location.startswith("https://"):
            return True, f"HTTP {status} → {location}"
        return False, f"跳转目标不是 HTTPS：{location}"
    if status == 200:
        return False, "80 端口直接返回 200，说明明文访问没有被跳转（密码会明文传输）"
    return False, f"预期 3xx 跳转，实际 {status}"


def run_checks(args: argparse.Namespace) -> Reporter:
    parsed = urlparse(args.url)
    host = parsed.hostname or ""
    https_port = parsed.port or 443
    http_port = args.http_port or 80
    base = f"https://{host}" + (f":{https_port}" if https_port != 443 else "")

    insecure = args.insecure
    context = ssl.create_default_context()
    if insecure:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

    report = Reporter()

    # 1. 站点可达
    status, headers, _ = fetch(base + "/", context=context)
    report.add(status == 200, "HTTPS 站点可访问", f"GET / → {status}")

    server = header_of(headers, "Server")
    ok, detail = check_server_header(server)
    report.add(ok, "Server 头", detail)

    # 2. 明文跳转 + 证书
    try:
        ok, detail = check_redirect(host, http_port)
        report.add(ok, "HTTP → HTTPS 跳转", detail)
    except ConnectionError as exc:
        report.add(None, "HTTP → HTTPS 跳转", f"无法连接 {host}:{http_port}（{exc}）；"
                                             f"只放行 443 也可以接受，但请确认 80 不会明文提供服务")
    ok, detail = check_tls_cert(host, https_port, context, insecure)
    report.add(ok, "TLS 证书", detail)

    # 3. 反代有没有剥掉后端安全响应头
    missing = [name for name in SECURITY_HEADERS if not header_of(headers, name)]
    report.add(not missing, "安全响应头未丢失",
               "齐全：" + "、".join(SECURITY_HEADERS) if not missing else "缺失：" + "、".join(missing))
    report.add(bool(header_of(headers, "Strict-Transport-Security")) or None,
               "HSTS",
               header_of(headers, "Strict-Transport-Security") or "未下发（建议确认全站 HTTPS 后补上）")

    # 4. Cookie 是不是带上了 Secure —— 这是 P1 的核心
    status, headers, _ = fetch(base + "/api/auth/session", context=context)
    cookies = parse_cookies(headers)
    if not cookies:
        report.add(False, "Cookie 属性", f"会话接口没有下发 Cookie（HTTP {status}）")
    else:
        problems = []
        for name, attrs in cookies.items():
            if not attrs.get("secure"):
                problems.append(f"{name} 缺 Secure")
            if not attrs.get("httponly") and name.startswith("cot_session"):
                problems.append(f"{name} 缺 HttpOnly")
            if not attrs.get("samesite"):
                problems.append(f"{name} 缺 SameSite")
        report.add(not problems, "Cookie 属性（Secure/HttpOnly/SameSite）",
                   "、".join(f"{n}[{', '.join(k for k in a if k not in ('value',))}]"
                             for n, a in cookies.items()) if not problems
                   else "；".join(problems) + "（多为反代漏配 X-Forwarded-Proto）")

    # 5. 静态白名单与目录列表（反代往往只转发，这里顺带确认没被绕过）
    for path in OPEN_PATHS:
        try:
            status, _, _ = fetch(base + path, context=context)
            report.add(status in (403, 404), f"目录列表已关闭 {path}", f"→ {status}")
        except ConnectionError as exc:
            report.add(False, f"目录列表已关闭 {path}", f"连接失败：{exc}")
    for path in BLOCKED_PATHS:
        try:
            status, _, _ = fetch(base + path, context=context)
            report.add(status == 404, f"源码/配置不可下载 {path}", f"→ {status}")
        except ConnectionError as exc:
            report.add(False, f"源码/配置不可下载 {path}", f"连接失败：{exc}")

    # 6. 后台接口确实要登录
    status, _, _ = fetch(base + "/api/state", context=context)
    report.add(status in (401, 403), "后台接口需要登录", f"GET /api/state → {status}")

    # 7. 可选：真的登录一次，看会话 Cookie 的 Secure
    if args.username:
        jar = {name: str(attrs["value"]) for name, attrs in cookies.items()}
        csrf = jar.get("cot_csrf", "")
        headers_out = {"Content-Type": "application/json", "X-CSRF-Token": csrf}
        if jar:
            headers_out["Cookie"] = "; ".join(f"{k}={v}" for k, v in jar.items())
        payload = json.dumps({"username": args.username, "password": args.password}).encode("utf-8")
        status, headers, _ = fetch(base + "/api/auth/login", method="POST", headers=headers_out,
                                   body=payload, context=context)
        if status != 200:
            report.add(False, "登录并检查会话 Cookie", f"POST /api/auth/login → {status}（检查账号密码，"
                                                       f"或该账号已被限流锁定）")
        else:
            session = parse_cookies(headers).get("cot_session")
            if not session:
                report.add(False, "登录并检查会话 Cookie", "登录成功但没有下发会话 Cookie")
            else:
                ok = bool(session.get("secure")) and bool(session.get("httponly"))
                report.add(ok, "登录并检查会话 Cookie",
                           f"cot_session Secure={bool(session.get('secure'))} "
                           f"HttpOnly={bool(session.get('httponly'))} "
                           f"SameSite={session.get('samesite')}")

    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="检查线上部署的 HTTPS 反代与 Cookie 安全属性",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("url", help="站点地址，例如 https://photos.example.com")
    parser.add_argument("--insecure", action="store_true", help="跳过证书校验（自签证书用）")
    parser.add_argument("--http-port", type=int, default=0, help="明文端口，默认 80（用于验证跳转）")
    parser.add_argument("--username", default="", help="可选：登录用的管理员账号")
    parser.add_argument("--password", default="", help="可选：登录用的密码")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = parser.parse_args()

    parsed = urlparse(args.url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        print("地址格式不对，应形如 https://photos.example.com", file=sys.stderr)
        return 2
    if parsed.scheme != "https":
        print("提示：这不是 HTTPS 地址——下面的检查多半会失败，正是要暴露的问题。", file=sys.stderr)
    if args.username and not args.password:
        print("带了 --username 就要一起给 --password", file=sys.stderr)
        return 2

    print(f"检查目标：{args.url}（{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC）\n")
    try:
        report = run_checks(args)
    except ConnectionError as exc:
        print(f"连接失败：{exc}", file=sys.stderr)
        return 2
    except socket.timeout:
        print("连接超时", file=sys.stderr)
        return 2

    report.summary()

    if args.json:
        print(json.dumps({"results": report.results,
                          "failed": len(report.failed),
                          "warned": len(report.warned)},
                         ensure_ascii=False, indent=2))
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
