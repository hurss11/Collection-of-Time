# -*- coding: utf-8 -*-
"""表单 schema、提交值归一化与字段级校验。

后台表单的「有哪些字段、什么类型、是否必填、怎么分组」全部由这里提供，
前端只按 type 画控件；提交时前端直接把控件的原始值（扁平的 key/value）发回来，
由这里负责：

   - 把 "exif.camera" 这类扁平键还原成嵌套结构；
   - 把 "48" / "1, 2" 这类字符串转成数字与数组；
   - 按字段给出可直接标红的错误。

这样规则只有一份，改校验不用同时改两端。
"""
from __future__ import annotations

import re

from . import query
from .query import text

PROVIDERS = [
    {"value": "file", "label": "本地视频文件"},
    {"value": "bilibili", "label": "哔哩哔哩"},
    {"value": "youtube", "label": "YouTube"},
    {"value": "vimeo", "label": "Vimeo"},
    {"value": "embed", "label": "其它外链"},
]

PROVIDER_VALUES = {option["value"] for option in PROVIDERS}

# 收集器：告诉前端每个集合有哪些字段
FIELDS: dict[str, list[dict]] = {
    "albums": [
        {"key": "id", "label": "ID", "type": "text", "required": True, "mono": True,
         "hint": "唯一标识，被照片 / 视频的 album 字段引用"},
        {"key": "name", "label": "名称", "type": "text", "required": True},
        {"key": "description", "label": "描述", "type": "text"},
        {"key": "cover", "label": "封面图路径", "type": "text", "mono": True,
         "placeholder": "assets/img/ph-01.svg"},
    ],
    "photos": [
        {"key": "title", "label": "标题", "type": "text", "required": True},
        {"key": "album", "label": "相册", "type": "album"},
        {"key": "date", "label": "拍摄时间", "type": "text", "placeholder": "2026-01-18 06:42:00"},
        {"key": "location", "label": "地点", "type": "text"},
        {"key": "tags", "label": "标签（逗号分隔）", "type": "tags"},
        {"key": "src", "label": "大图路径", "type": "text", "mono": True,
         "placeholder": "assets/img/photos/xxx.jpg"},
        {"key": "thumb", "label": "缩略图路径", "type": "text", "mono": True,
         "hint": "留空则使用大图"},
        {"key": "description", "label": "描述", "type": "textarea"},
        {"key": "exif.camera", "label": "相机", "type": "text", "group": "EXIF 拍摄参数"},
        {"key": "exif.lens", "label": "镜头", "type": "text", "group": "EXIF 拍摄参数"},
        {"key": "exif.focalLength", "label": "焦距", "type": "text", "group": "EXIF 拍摄参数"},
        {"key": "exif.aperture", "label": "光圈", "type": "text", "group": "EXIF 拍摄参数"},
        {"key": "exif.shutter", "label": "快门", "type": "text", "group": "EXIF 拍摄参数"},
        {"key": "exif.iso", "label": "ISO", "type": "text", "group": "EXIF 拍摄参数"},
        {"key": "exif.dimensions", "label": "尺寸", "type": "text", "group": "EXIF 拍摄参数"},
    ],
    "videos": [
        {"key": "title", "label": "标题", "type": "text", "required": True},
        {"key": "album", "label": "相册", "type": "album"},
        {"key": "provider", "label": "来源", "type": "detected", "from": "src",
         "hint": "按上面的链接自动识别（本地文件 / 哔哩哔哩 / YouTube / Vimeo / 其它外链），不用手选"},
        {"key": "src", "label": "文件路径 / 视频链接 / BV 号", "type": "text", "mono": True,
         "placeholder": "assets/video/xxx.mp4 或 BV1xxxxxxxxx 或 https://…"},
        {"key": "poster", "label": "封面图", "type": "text", "mono": True, "upload": "poster",
         "hint": "外链视频必填：卡片上显示的就是这张图（留空会尝试自动获取）。也可以点旁边的按钮上传一张"},
        {"key": "posterTime", "label": "抓帧时间（秒）", "type": "number"},
        {"key": "duration", "label": "时长（秒）", "type": "number"},
        {"key": "resolution", "label": "分辨率", "type": "text", "placeholder": "3840 × 2160"},
        {"key": "date", "label": "拍摄时间", "type": "text", "placeholder": "2026-01-18 06:20:00"},
        {"key": "location", "label": "地点", "type": "text"},
        {"key": "tags", "label": "标签（逗号分隔）", "type": "tags"},
        {"key": "description", "label": "描述", "type": "textarea"},
        {"key": "exif.camera", "label": "设备", "type": "text", "group": "视频参数"},
        {"key": "exif.fps", "label": "帧率", "type": "text", "group": "视频参数"},
        {"key": "exif.codec", "label": "编码", "type": "text", "group": "视频参数"},
    ],
}

NOUNS = {"albums": "相册", "photos": "照片", "videos": "视频"}

# 上传时没选相册的条目会挂到这个相册下。
# 它不会凭空出现在数据里：上传到它时、或启动时发现历史条目引用了它，才会自动补建
# （见 admin.ensure_default_album），这样没用到它的站点不会多出一个空相册。
DEFAULT_ALBUM_ID = "uncategorized"
DEFAULT_ALBUM: dict[str, str] = {
    "id": DEFAULT_ALBUM_ID,
    "name": "未分类",
    "description": "上传时没有选择相册的条目会自动归到这里。",
    "cover": "",
}

# 数字字段（前端可能发字符串过来）
_NUMBER_KEYS = {"posterTime", "duration"}
# 数组字段（逗号分隔的字符串也接受）
_LIST_KEYS = {"tags"}
# 不需要写回 JSON 的内部字段
_INTERNAL_KEYS = {"search", "sortKeys", "cells", "collection"}

_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def fields_for(collection: str) -> list[dict]:
    return FIELDS.get(collection, [])


def nouns() -> dict[str, str]:
    return dict(NOUNS)


def collections() -> list[str]:
    return list(FIELDS)


def _to_number(value: object) -> object:
    raw = text(value)
    if not raw:
        return ""
    try:
        number = float(raw)
    except ValueError:
        return value
    return int(number) if number.is_integer() else number


def _to_tags(value: object) -> list[str]:
    if isinstance(value, list):
        return [text(tag) for tag in value if text(tag)]
    chunks = str(value or "").replace("，", ",").split(",")
    return [chunk.strip() for chunk in chunks if chunk.strip()]


def normalize_submission(collection: str, payload: dict) -> dict:
    """把前端发来的扁平表单值还原成落盘的条目结构。"""
    allowed = {field["key"] for field in fields_for(collection)}
    item: dict[str, object] = {}

    for key, value in payload.items():
        # id 是记录主键：photos / videos 的 schema 里没有它，但绝不能丢，
        # 否则编辑会被当成新增，产生重复条目。
        if key == "id":
            item["id"] = text(value)
            continue
        if key in _INTERNAL_KEYS or key not in allowed:
            continue

        if key in _LIST_KEYS:
            item[key] = _to_tags(value)
            continue
        if key in _NUMBER_KEYS:
            number = _to_number(value)
            if number != "":
                item[key] = number
            continue
        if key.startswith("exif."):
            field = key.split(".", 1)[1]
            exif = item.setdefault("exif", {})
            assert isinstance(exif, dict)
            # 空值也要写入：保存时据此判断「用户把它清空了」
            exif[field] = text(value)
            continue

        item[key] = text(value)

    # 视频的「来源」不由作者选：按链接自动识别（填错来源会让自动抓封面、跳原站、
    # 资源检查这些后续逻辑全部对不上号）。认不出来时（空、怪协议）按本地文件处理。
    if collection == "videos":
        item["provider"] = query.detect_provider(text(item.get("src"))) or "file"
    return item


def merge_with_existing(new_item: dict, existing: dict | None) -> dict:
    """把提交内容与库中已有条目合并。

    - 顶层键：整体覆盖（表单里没有的键保持原值，避免误删字段）；
    - `exif`：逐键合并，且以空值清除单个键——这样表单只覆盖它自己有的字段，
      库里其它 EXIF（例如上传时写入的 `dateTimeOriginal`）不会因为编辑而消失。
    """
    if existing is None:
        item = dict(new_item)
        exif = item.get("exif")
        if isinstance(exif, dict):
            pruned = {key: value for key, value in exif.items() if value}
            if pruned:
                item["exif"] = pruned
            else:
                item.pop("exif", None)
        return item

    merged = {**existing, **new_item}
    submitted = new_item.get("exif") if isinstance(new_item.get("exif"), dict) else {}
    combined = {**(existing.get("exif") or {}), **submitted}
    pruned = {key: value for key, value in combined.items() if value}
    if pruned:
        merged["exif"] = pruned
    else:
        merged.pop("exif", None)
    return merged


def flatten_item(collection: str, item: dict) -> dict:
    """把落盘条目摊平成表单值，前端 setValue 即可，不需要自己拆嵌套。"""
    values: dict[str, object] = {}
    exif = item.get("exif") if isinstance(item.get("exif"), dict) else {}

    for field in fields_for(collection):
        key = field["key"]
        if key.startswith("exif."):
            values[key] = text(exif.get(key.split(".", 1)[1]))
        elif key in _LIST_KEYS:
            values[key] = list(item.get(key) or [])
        elif key in _NUMBER_KEYS:
            number = item.get(key)
            values[key] = "" if number in (None, "") else number
        else:
            values[key] = text(item.get(key))
    return values


def validate(collection: str, item: dict) -> list[dict[str, str]]:
    """字段级校验：返回 [{"field": 扁平键, "message": 说明}]。"""
    errors: list[dict[str, str]] = []

    def require(key: str, message: str) -> None:
        if not text(item.get(key)):
            errors.append({"field": key, "message": message})

    if collection == "albums":
        album_id = text(item.get("id"))
        if not album_id:
            errors.append({"field": "id", "message": "ID 不能为空"})
        elif not _ID_PATTERN.match(album_id):
            errors.append({"field": "id", "message": "ID 只能包含字母、数字、点、下划线与连字符（≤64）"})
        require("name", "名称不能为空")
        return errors

    require("title", f"{NOUNS.get(collection, '条目')}标题不能为空")

    if collection == "videos":
        provider = text(item.get("provider")) or "file"
        src = text(item.get("src"))
        if provider not in PROVIDER_VALUES:
            errors.append({"field": "provider", "message": f"未知的来源：{provider}"})
        if not src:
            # 来源是按链接认的，没有链接就认不出来 —— 所以这里只说「要填链接」
            require("src", "必须填写文件路径、视频链接或视频 ID（来源会按它自动识别）")
        elif provider != "file":
            # 外链少了 src 会存出一条「点开什么都没有」的假视频（前台只能显示未配置 src）
            require("poster", "外链视频必须填写封面图（站内不播放，卡片上显示的就是这张图）")

    return errors


__all__ = [
    "FIELDS", "NOUNS", "PROVIDERS", "PROVIDER_VALUES", "collections", "fields_for",
    "flatten_item", "merge_with_existing", "nouns", "normalize_submission", "query", "validate",
]
