"""外链视频的「资源是否还在」检查。

站内只保存链接、封面与这份检查结果；播放交给原站。所以这里回答一个问题：
**这个链接指向的视频现在还能打开吗？**

判定只问服务商官方接口（与抓封面同一套白名单，见 `thumbs.py`）：

| 来源 | 问法 | 判定 |
| --- | --- | --- |
| 哔哩哔哩 | `api.bilibili.com/x/web-interface/view` | `code=0` → 在；`-404 / 62002 / 62004 / -403` → 没了或不可见 |
| Vimeo | `vimeo.com/api/oembed.json` | 200 → 在；403/404 → 没了或私有 |
| YouTube | `www.youtube.com/oembed` | 200 → 在；401/403/404 → 没了或不可嵌入 |
| 其它外链 | — | 不判定（不能去请求任意地址），标成「未检查」 |

区分两件事：接口**明确回答「不存在」**才是 `gone`；超时、连不上、返回奇怪的东西一律
`unknown`。保存表单时 `gone` 会拦下（这就是「校验链接」），`unknown` 只提示不拦，
免得一次网络抖动让人存不进去。

结果写在 `.run/link-status.json`（运行时状态，不属于站点内容，所以不进 `data/`、不随备份走）：

    {"v-004": {"status": "ok", "message": "视频可访问", "at": "2026-10-04 12:40:11"}}
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import query, thumbs

ROOT = Path(__file__).resolve().parent.parent
STATUS_PATH = ROOT / ".run" / "link-status.json"

STATUS_LABELS = {
    "ok": "可访问",
    "gone": "已失效",
    "unknown": "未确认",
}
STATUS_TONES = {
    "ok": "ok",
    "gone": "danger",
    "unknown": "muted",
}

# 接口明确表示「这个视频不存在 / 你看不到」的业务码（B 站）
_BILIBILI_GONE = {-404: "视频不存在或已被删除", 62002: "稿件不可见（可能仅自己可见或审核中）",
                  62004: "稿件审核中", -403: "访问权限不足（可能已设为私密）"}

_cache: dict = {"mtime": None, "data": {}}


def _load() -> dict:
    """读状态文件（按 mtime 缓存，避免每条卡片都读一次磁盘）。"""
    try:
        mtime = STATUS_PATH.stat().st_mtime
    except OSError:
        _cache.update(mtime=None, data={})
        return {}
    if _cache["mtime"] != mtime:
        try:
            data = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        _cache.update(mtime=mtime, data=data if isinstance(data, dict) else {})
    return _cache["data"]


def statuses() -> dict:
    """{条目 id: {"status", "message", "at"}}。"""
    return _load()


def status_for(item_id: str) -> dict:
    return _load().get(query.text(item_id)) or {}


def save_statuses(data: dict) -> None:
    try:
        STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATUS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return
    _cache.update(mtime=None, data={})


def record(item_id: str, result: dict) -> dict:
    """记下一条的结果；`result` 为空表示这次没检查（从文件里删掉这条）。"""
    item_id = query.text(item_id)
    if not item_id:
        return statuses()
    data = dict(_load())
    if result:
        data[item_id] = result
    else:
        data.pop(item_id, None)
    save_statuses(data)
    return data


def label(result: dict) -> str:
    return STATUS_LABELS.get(query.text(result.get("status")), "未检查")


def tone(result: dict) -> str:
    return STATUS_TONES.get(query.text(result.get("status")), "muted")


# ---------------------------------------------------------------- 判定


def _result(status: str, message: str) -> dict:
    return {"status": status, "message": message, "at": time.strftime("%Y-%m-%d %H:%M:%S")}


def check(provider: str, src: str, *, budget: float = thumbs.TOTAL_TIMEOUT) -> dict:
    """检查一条外链。返回 `{"status", "message", "at"}`；本地视频与空链接返回 {}。"""
    provider = query.text(provider).lower() or "file"
    if provider == "file" or not query.text(src):
        return {}
    if provider == "embed":
        return _result("unknown", "「其它外链」无法自动判断，请自行确认")

    try:
        info = thumbs.probe_target(provider, src)     # 认不出 ID 会抛，这里当作「不是有效链接」
    except thumbs.ThumbError as exc:
        return _result("unknown", str(exc))

    try:
        status, _response_headers, body = thumbs._request(
            info["url"], allowed=thumbs.META_HOSTS, headers=info.get("headers"),
            limit=thumbs.MAX_JSON_BYTES, deadline=time.monotonic() + max(1.0, float(budget)),
        )
    except thumbs.ThumbError as exc:
        return _result("unknown", str(exc))

    return _interpret(info, status, body)


def _interpret(info: dict, status: int, body: bytes) -> dict:
    kind = info["kind"]
    label = info["label"]

    if kind == "bilibili":
        try:
            payload = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            return _result("unknown", f"{label}接口返回的不是 JSON（HTTP {status}）")
        code = payload.get("code")
        if code == 0:
            return _result("ok", "视频可访问")
        message = query.text(payload.get("message")) or "未知原因"
        if code in _BILIBILI_GONE:
            return _result("gone", _BILIBILI_GONE[code])
        return _result("unknown", f"{label}返回 {code}：{message}")

    if kind == "vimeo":
        if status == 200:
            return _result("ok", "视频可访问")
        if status in (403, 404):
            return _result("gone", "视频不存在、已删除或设为私密")
        return _result("unknown", f"{label}接口返回 {status}")

    # youtube：oEmbed
    if kind == "youtube":
        if status == 200:
            return _result("ok", "视频可访问")
        if status == 404:
            return _result("gone", "视频不存在或已被删除")
        if status in (401, 403):
            return _result("gone", "视频不可嵌入（可能已设为私密）")
        return _result("unknown", f"{label}接口返回 {status}")

    # 兜底：遇到没见过的形态（比如哪天 plan() 换了 kind）宁可说「未确认」，也不要谎报「在」
    return _result("unknown", f"{label} 返回了无法判断的响应（HTTP {status}）")


def check_item(item: dict, *, budget: float = thumbs.TOTAL_TIMEOUT) -> dict:
    """检查一个条目的 `provider` / `src`（只读，不写文件）。"""
    return check(query.text(item.get("provider")), query.text(item.get("src")), budget=budget)


def check_all(items: list[dict], *, budget: float = thumbs.TOTAL_TIMEOUT) -> dict:
    """检查全部外链视频并落盘；返回 `{"results": {...}, "counts": {...}}`。"""
    data = dict(_load())
    counts = {"ok": 0, "gone": 0, "unknown": 0, "skipped": 0}
    for item in items:
        item_id = query.text(item.get("id"))
        if not item_id:
            continue
        result = check_item(item, budget=budget)
        if not result:
            counts["skipped"] += 1
            data.pop(item_id, None)
            continue
        data[item_id] = result
        counts[result["status"]] = counts.get(result["status"], 0) + 1
    save_statuses(data)

    ids = {query.text(item.get("id")) for item in items}
    return {
        "results": {key: value for key, value in data.items() if key in ids},
        "counts": counts,
    }
