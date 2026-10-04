"""外链视频封面：从服务商那里取缩略图。

这是整个项目里**唯一一处服务端主动出网**的地方，所以边界划得很死：

- 只允许 `https`；
- 只允许白名单域名（见 `META_HOSTS` / `IMAGE_HOSTS`）—— 支持 `.hdslb.com` 这种
  按域名边界匹配的后缀写法，`evil-hdslb.com` 不算；
- 拒绝 IP 字面量（含 IPv6）、拒绝带 userinfo 的 URL（`https://evil@i.ytimg.com/…` 这种
  看着像白名单、实际连的是别处）、拒绝 443 以外的端口；
- 跳转每一跳都重新过一遍上述校验，最多 3 跳；
- 单个响应最多 6MB；单次连接 5 秒、整次抓取 12 秒，且**超时用硬墙钟兜底**
  （`urlopen` 的 timeout 是按地址算的，一个域名解析出多个地址时费用翻倍，
  光靠它会让保存表单的人干等）；
- 拿到的字节必须能认出**真实**图片格式（JPEG / PNG / WebP / AVIF），扩展名以字节为准，
  不信远端给的文件名与 Content-Type。

结论：可被影响的只有白名单域名，不存在「拿本站当跳板去访问任意内网地址」的面。
即便如此，`admin.py --no-net-fetch` 还能把出网整个关掉（纯离线部署用）。

链接识别是纯计算（`plan()`，不联网），抓取只按 `plan()` 给出的固定地址进行：
YouTube 用固定的 `i.ytimg.com/vi/<id>/…`；B 站与 Vimeo 先问官方接口拿缩略图地址，
再把那个地址按图片白名单校验一遍。用户填的链接**本身不会被请求**。
"""
from __future__ import annotations

import ipaddress
import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import query

USER_AGENT = "Collection-of-Time/1.0 (cover fetch)"
MAX_BYTES = 6 * 1024 * 1024          # 单张封面最多 6MB
MAX_JSON_BYTES = 512 * 1024          # 接口 JSON 最多 512KB
MIN_BYTES = 4 * 1024                 # 小于这个体积多半是「无缩略图」占位图，换下一个候选
CONNECT_TIMEOUT = 5                  # 单次 socket 操作上限（秒）
TOTAL_TIMEOUT = 12                   # 整次抓取的全局预算（秒）：多候选共享，用完就放弃
AUTOFILL_TIMEOUT = 6                 # 保存表单时自动抓取的预算：短一点，别让人干等
MAX_HOPS = 3

# 允许访问的「元信息」接口（视频还在不在、缩略图地址）
META_HOSTS = ("api.bilibili.com", "vimeo.com", "www.youtube.com")
# 能抓到封面的来源（「其它外链」各站规则不统一，本地文件不需要）
FETCHABLE_PROVIDERS = ("youtube", "bilibili", "vimeo")
# 允许访问的图片域名（前导点 = 按域名边界做后缀匹配）
IMAGE_HOSTS = ("i.ytimg.com", ".hdslb.com", ".vimeocdn.com")
# 自检用：每个白名单项挑一个具体主机来探测（我们只可能连这些）
PROBE_HOSTS = ("api.bilibili.com", "i0.hdslb.com", "i.ytimg.com", "vimeo.com", "i.vimeocdn.com")

IMAGE_MAGIC = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"RIFF", ".webp"),               # 具体再看第 8~12 字节是不是 WEBP
)


class ThumbError(RuntimeError):
    """抓取失败（原因都是给人看的中文短句）。"""


# ---------------------------------------------------------------- URL 安全


def _host_allowed(host: str, allowed: tuple[str, ...]) -> bool:
    for entry in allowed:
        if entry.startswith("."):
            if host.endswith(entry) and host != entry[1:]:
                return True
        elif host == entry:
            return True
    return False


def check_url(url: str, allowed: tuple[str, ...]) -> str:
    """校验可不可以访问这个地址；通过则返回规范化后的 host。"""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https":
        raise ThumbError(f"只允许 https：{url[:60]}")
    if parts.username or parts.password:
        raise ThumbError("地址里不允许带用户名/密码（可能指向别的站点）")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise ThumbError(f"地址没有域名：{url[:60]}")
    try:
        parts.port
    except ValueError as exc:
        raise ThumbError("地址里的端口不合法") from exc
    if parts.port not in (None, 443):
        raise ThumbError(f"只允许 443 端口：{parts.port}")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ThumbError("不允许直接抓取 IP 地址")
    if not _host_allowed(host, allowed):
        raise ThumbError(f"域名不在白名单里：{host}")
    return host


def _https(url: str) -> str:
    """把白名单域名上的 http 地址升级成 https（B 站接口返回的 pic 就是 http）。"""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "http":
        return urllib.parse.urlunsplit(("https",) + parts[1:])
    return url


# ---------------------------------------------------------------- 出网


class _GuardedRedirect(urllib.request.HTTPRedirectHandler):
    """跳转也要过白名单，否则「白名单域名 → 302 → 内网」就绕过了。"""

    def __init__(self, allowed: tuple[str, ...]) -> None:
        self.allowed = allowed
        self.hops = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):      # noqa: ANN001
        self.hops += 1
        if self.hops > MAX_HOPS:
            raise ThumbError(f"跳转超过 {MAX_HOPS} 次，停止")
        target = urllib.parse.urljoin(req.full_url, newurl)
        check_url(target, self.allowed)
        return super().redirect_request(req, fp, code, msg, headers, target)


def _read_limited(response, *, limit: int, deadline: float | None = None) -> bytes:
    """按上限读取响应体：超过 limit 或超过 deadline 立刻放弃（不把远端内容整段读进内存）。"""
    total = 0
    chunks: list[bytes] = []
    while True:
        if deadline is not None and time.monotonic() > deadline:
            raise ThumbError(f"抓取超时（整次预算 {TOTAL_TIMEOUT} 秒已用完）")
        chunk = response.read(min(65536, limit + 1 - total))
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise ThumbError(f"远端内容超过 {limit // (1024 * 1024)}MB，已放弃")
        chunks.append(chunk)
    return b"".join(chunks)


def _with_deadline(func, timeout: float, *, on_timeout: str | None = None):
    """在硬墙钟内跑 `func`；到点就放弃。

    为什么不能只靠 `urlopen(timeout=…)`：那个超时是**按地址**算的，而一个域名往往
    解析出多个地址（`i.ytimg.com` 就有 IPv4 + IPv6 两个）。地址连不通时实测是
    「timeout × 地址数」，还可能被 DNS 拖住 —— 光靠它会让人在表单上等半分钟。
    所以这里再用一根线程兜底；被放弃的那根线程会因为超时/连接失败自己结束。
    """
    box: dict = {}

    def run() -> None:
        try:
            box["value"] = func()
        except BaseException as exc:            # noqa: BLE001 - 原样带回调用方
            box["error"] = exc

    worker = threading.Thread(target=run, daemon=True, name="cover-fetch")
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise ThumbError(on_timeout
                         or f"抓取超时（{timeout:.0f} 秒预算已用完），请稍后重试或手动上传封面")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _request(url: str, *, allowed: tuple[str, ...], headers: dict[str, str] | None = None,
             limit: int = MAX_BYTES, deadline: float | None = None) -> tuple[int, dict[str, str], bytes]:
    """校验并 GET 一个地址，返回 (状态码, 响应头, 正文)。

    网络访问全部收敛在这里：单测可以直接替换它，把校验逻辑与真实网络分开。

    `deadline` 是整次抓取的全局截止时刻（`time.monotonic()`）：多候选共享一份预算，
    否则「三个候选各超时一次」会让保存表单的人干等几十秒。
    """
    check_url(url, allowed)
    if deadline is not None and time.monotonic() > deadline:
        raise ThumbError(f"抓取超时（整次预算 {TOTAL_TIMEOUT} 秒已用完）")
    budget = CONNECT_TIMEOUT
    if deadline is not None:
        budget = max(1.0, min(CONNECT_TIMEOUT, deadline - time.monotonic()))

    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        **(headers or {}),
    })
    opener = urllib.request.build_opener(_GuardedRedirect(allowed))
    status, response_headers, body = 0, {}, b""
    try:
        with opener.open(request, timeout=budget) as response:
            status = response.status
            response_headers = {k.lower(): v for k, v in response.headers.items()}
            body = _read_limited(response, limit=limit, deadline=deadline)
    except urllib.error.HTTPError as exc:
        status = exc.code
        response_headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
        try:
            body = exc.read(limit)
        except Exception:                                   # noqa: BLE001 - 读错误体失败不影响主流程
            body = b""
    except ThumbError:
        raise
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # 把主机名写进消息：出网被挡时「连不上远端」这种话很难查（连的是谁？）
        host = urllib.parse.urlsplit(url).hostname or url
        raise ThumbError(f"连不上 {host}：{exc}") from exc
    return status, response_headers, body


def connectivity(timeout: float = 3.0) -> dict[str, dict]:
    """探测各服务商域名能不能连上（只做 TCP 443 连接，不发任何请求）。

    给排查用：`python3 admin.py --net-status` / `./run.sh net-check`。
    海外机房常连不上 B 站，也有不少机房连不上 YouTube —— 那时自动抓封面会失败，
    但手动上传封面、外链播放本身都不受影响。

    `timeout` 是**每个域名**的硬墙钟：`socket` 的超时是按地址算的，一个域名解析出
    多个地址时会成倍拖长，探针自己不能比被测的东西还慢。
    """
    report: dict[str, dict] = {}
    for host in PROBE_HOSTS:
        started = time.monotonic()

        def probe(host=host) -> None:
            with socket.create_connection((host, 443), timeout=timeout):
                return None

        try:
            _with_deadline(probe, timeout, on_timeout=f"连接超时（{timeout:.0f} 秒）")
            report[host] = {"ok": True, "seconds": round(time.monotonic() - started, 2)}
        except (ThumbError, OSError) as exc:
            detail = str(exc) if isinstance(exc, ThumbError) else f"{type(exc).__name__}: {exc}"
            report[host] = {"ok": False, "seconds": round(time.monotonic() - started, 2),
                            "error": detail}
    return report


# ---------------------------------------------------------------- 图片


def image_suffix(data: bytes) -> str:
    """按**字节内容**判断图片格式；认不出来返回空串。"""
    if len(data) < 12:
        return ""
    for magic, suffix in IMAGE_MAGIC:
        if not data.startswith(magic):
            continue
        if suffix == ".webp" and data[8:12] != b"WEBP":
            continue
        return suffix
    if data[4:8] == b"ftyp" and data[8:12] in (b"avif", b"avis"):
        return ".avif"
    return ""


def _pick_image(url: str, *, allowed: tuple[str, ...], headers: dict[str, str] | None = None,
                min_bytes: int = 0, deadline: float | None = None) -> tuple[bytes, str, str]:
    """抓一张图片，返回 (内容, 扩展名, 实际地址)。"""
    status, response_headers, body = _request(url, allowed=allowed, headers=headers, deadline=deadline)
    if status != 200:
        raise ThumbError(f"远端返回 {status}：{url.split('//')[-1][:60]}")
    suffix = image_suffix(body)
    if not suffix:
        content_type = response_headers.get("content-type", "?")
        raise ThumbError(f"远端的不是能识别的图片（Content-Type: {content_type}）")
    if len(body) < min_bytes:
        raise ThumbError(f"拿到的图片太小（{len(body)} 字节），可能没有缩略图")
    return body, suffix, url


# ---------------------------------------------------------------- 链接识别


def plan(provider: str, src: str) -> dict:
    """纯计算：识别出视频 ID 与要访问的地址，不联网。

    返回 `{"provider", "label", "videoId", "embedUrl", "kind", "targets"}`；
    认不出来直接抛 `ThumbError`（消息可直接展示给用户）。
    """
    provider = query.text(provider).lower()
    label = query.provider_label(provider)
    if provider in ("", "file"):
        raise ThumbError("本地视频不需要抓取封面")
    if provider == "embed":
        raise ThumbError("「其它外链」没有统一的缩略图规则，请手动上传一张封面")

    video_id = query.video_id(provider, src)
    if not video_id:
        raise ThumbError(f"识别不出{label}的视频 ID，请检查链接（也可以只填 ID / BV 号）")

    if provider == "youtube":
        # maxres 对老视频常常是 404，hqdefault 一定有；两个就够，多一个候选多一份等待
        targets = [("image", f"https://i.ytimg.com/vi/{video_id}/{name}.jpg", IMAGE_HOSTS)
                   for name in ("maxresdefault", "hqdefault")]
        kind = "image-direct"
    elif provider == "bilibili":
        if video_id.lower().startswith("av"):
            param = f"aid={video_id[2:]}"
        else:
            param = f"bvid={video_id}"
        targets = [("meta", f"https://api.bilibili.com/x/web-interface/view?{param}", META_HOSTS)]
        kind = "bilibili"
    else:                                        # vimeo
        quoted = urllib.parse.quote(f"https://vimeo.com/{video_id}", safe="")
        targets = [("meta", f"https://vimeo.com/api/oembed.json?url={quoted}", META_HOSTS)]
        kind = "vimeo"

    return {
        "provider": provider,
        "label": label,
        "videoId": video_id,
        "embedUrl": query.embed_url(provider, src),
        "kind": kind,
        "targets": targets,
    }


def probe_target(provider: str, src: str) -> dict:
    """「这个视频还在吗」要问的地址（官方接口，全在白名单内）。

    与 `plan()` 的区别：抓封面时 YouTube 用的是固定缩略图地址，而判断存在与否要问
    oEmbed 接口。返回 `{"kind", "label", "videoId", "url", "headers"}`。
    """
    provider = query.text(provider).lower()
    if provider == "embed":
        raise ThumbError("「其它外链」无法自动判断，请自行确认")
    if provider in ("", "file"):
        raise ThumbError("本地视频不需要检查外链")

    info = plan(provider, src)
    if info["provider"] == "youtube":
        # plan() 给 YouTube 的 kind 是 "image-direct"（抓封面走固定缩略图地址），
        # 但「还在不在」要问 oEmbed 接口，所以这里按 provider 判断而不是 kind
        watch = urllib.parse.quote(f"https://www.youtube.com/watch?v={info['videoId']}", safe="")
        return {
            "kind": "youtube",
            "label": info["label"],
            "videoId": info["videoId"],
            "url": f"https://www.youtube.com/oembed?url={watch}&format=json",
            "headers": {"Accept": "application/json"},
        }

    target = info["targets"][0][1]
    headers = {"Referer": "https://www.bilibili.com/"} if info["kind"] == "bilibili" else None
    return {
        "kind": info["kind"],
        "label": info["label"],
        "videoId": info["videoId"],
        "url": target,
        "headers": headers,
    }


def _image_from_meta(plan_info: dict, *, deadline: float | None = None) -> tuple[str, tuple[str, ...]]:
    """问官方接口要缩略图地址（B 站 / Vimeo）。返回 (图片地址, 允许的域名)。"""
    kind = plan_info["kind"]
    url = plan_info["targets"][0][1]
    headers = {}
    if kind == "bilibili":
        # B 站的接口与图片 CDN 都看 Referer，缺了容易 403
        headers = {"Referer": "https://www.bilibili.com/"}

    status, _headers, body = _request(url, allowed=META_HOSTS, headers=headers,
                                     limit=MAX_JSON_BYTES, deadline=deadline)
    if status != 200:
        raise ThumbError(f"{plan_info['label']}接口返回 {status}，无法获取封面")
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except ValueError as exc:
        raise ThumbError(f"{plan_info['label']}接口返回的不是 JSON") from exc

    if kind == "bilibili":
        if payload.get("code") != 0:
            raise ThumbError(f"B 站返回 {payload.get('code')}：{payload.get('message') or '视频不可见'}")
        picture = query.text((payload.get("data") or {}).get("pic"))
    else:
        picture = query.text(payload.get("thumbnail_url"))

    if not picture:
        raise ThumbError(f"{plan_info['label']}没有给出缩略图地址")
    picture = _https(picture)
    check_url(picture, IMAGE_HOSTS)                # 接口给的地址也要过一遍白名单
    return picture, IMAGE_HOSTS


def _fetch(info: dict, deadline: float, budget: float) -> dict:
    """按 `plan()` 给出的地址抓一张图（内部用 `deadline` 尽早失败）。"""
    if info["kind"] == "image-direct":
        # YouTube：固定地址，逐个候选试（maxres 对老视频常常是 404）
        best: tuple[bytes, str, str] | None = None
        failure = ""
        for _kind, url, allowed in info["targets"]:
            if time.monotonic() > deadline:
                raise ThumbError(f"抓取超时（{budget:.0f} 秒预算已用完），请稍后重试或手动上传封面")
            try:
                body, suffix, source = _pick_image(url, allowed=allowed, min_bytes=MIN_BYTES,
                                                   deadline=deadline)
            except ThumbError as exc:
                failure = str(exc)
                continue
            best = (body, suffix, source)
            break
        if best is None:
            raise ThumbError(f"没取到{info['label']}的缩略图：{failure or '链接可能已失效'}")
        body, suffix, source = best
    else:
        picture, allowed = _image_from_meta(info, deadline=deadline)
        headers = {"Referer": "https://www.bilibili.com/"} if info["kind"] == "bilibili" else None
        body, suffix, source = _pick_image(picture, allowed=allowed, headers=headers,
                                           min_bytes=MIN_BYTES, deadline=deadline)

    return {
        "data": body,
        "suffix": suffix,
        "provider": info["provider"],
        "label": info["label"],
        "videoId": info["videoId"],
        "embedUrl": info["embedUrl"],
        "sourceUrl": source,
    }


def fetch_cover(provider: str, src: str, *, budget: float = TOTAL_TIMEOUT) -> dict:
    """抓一张封面回来。返回 `{"data", "suffix", "provider", "label", "videoId", "sourceUrl"}`。

    `budget` 是整次抓取的秒数上限：先用 `plan()` 把参数错误立刻报掉（不联网），
    再用硬墙钟兜住整个抓取过程 —— 一个连不上的服务商不该让人在表单上等半分钟。
    """
    info = plan(provider, src)                       # 参数不对立刻抛，不浪费时间
    limit = max(1.0, float(budget))
    deadline = time.monotonic() + limit
    return _with_deadline(lambda: _fetch(info, deadline, limit), limit)


__all__ = ["ThumbError", "check_url", "image_suffix", "plan", "probe_target", "fetch_cover",
           "connectivity", "META_HOSTS", "IMAGE_HOSTS", "PROBE_HOSTS", "MAX_BYTES", "MIN_BYTES",
           "TOTAL_TIMEOUT", "AUTOFILL_TIMEOUT"]
