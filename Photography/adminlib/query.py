# -*- coding: utf-8 -*-
"""查询与展示投影：搜索、筛选、排序、分页，以及「可直接渲染」的字段。

前端只负责把这里给出的结构画出来。所有判断都收在这里：

   - 哪个关键词算命中（id / 标题 / 相册 / 地点 / 描述 / EXIF / 标签）；
   - 怎么排序、怎么分页；
   - 文件缺失怎么标记、EXIF 怎么排序、时长和嵌入地址怎么算。

这样手机端与桌面端只有「渲染方式」的差别，没有逻辑分叉，
也不需要把同一套规则在 JS 里再写一遍。
"""
from __future__ import annotations

import re
from pathlib import Path

from . import store

# ---------- 常量 ----------

MEDIA_KINDS = ("photo", "video")

# 媒体条目 EXIF 的展示顺序与中文名（前端不再自己拼）
EXIF_LABELS: dict[str, list[tuple[str, str]]] = {
    "photo": [
        ("camera", "相机"),
        ("lens", "镜头"),
        ("focalLength", "焦距"),
        ("aperture", "光圈"),
        ("shutter", "快门"),
        ("iso", "ISO"),
        ("dimensions", "尺寸"),
        ("dateTimeOriginal", "拍摄时间"),
    ],
    "video": [
        ("camera", "设备"),
        ("fps", "帧率"),
        ("codec", "编码"),
        ("dimensions", "尺寸"),
        ("dateTimeOriginal", "拍摄时间"),
    ],
}

# 缩略图 / 表格里那行「相机 · 快门 · 光圈 · ISO」用到的字段
EXIF_SUMMARY_KEYS = ("camera", "lens", "focalLength", "shutter", "aperture", "iso", "fps", "codec")

PROVIDER_LABELS = {
    "file": "本地视频",
    "youtube": "YouTube",
    "bilibili": "哔哩哔哩",
    "vimeo": "Vimeo",
    "embed": "外部嵌入",
}

PUBLIC_SORTS = [
    {"value": "date-desc", "label": "拍摄时间（新 → 旧）"},
    {"value": "date-asc", "label": "拍摄时间（旧 → 新）"},
    {"value": "title-asc", "label": "标题（A → Z）"},
    {"value": "album", "label": "相册分组"},
]

PUBLIC_TYPES = [
    {"value": "all", "label": "全部"},
    {"value": "photo", "label": "照片"},
    {"value": "video", "label": "视频"},
]

ADMIN_SORTS = {
    "photos": [
        {"value": "date-desc", "label": "时间（新 → 旧）"},
        {"value": "date-asc", "label": "时间（旧 → 新）"},
        {"value": "title-asc", "label": "标题（A → Z）"},
        {"value": "id-asc", "label": "ID"},
    ],
    "videos": [
        {"value": "date-desc", "label": "时间（新 → 旧）"},
        {"value": "date-asc", "label": "时间（旧 → 新）"},
        {"value": "title-asc", "label": "标题（A → Z）"},
        {"value": "id-asc", "label": "ID"},
    ],
    "albums": [
        {"value": "id-asc", "label": "ID"},
        {"value": "name-asc", "label": "名称（A → Z）"},
        {"value": "count-desc", "label": "条目数（多 → 少）"},
    ],
}

DEFAULT_PAGE_SIZE = {"photos": 200, "videos": 200, "albums": 200}
MAX_PAGE_SIZE = 500


# ============================================================
# 基础工具
# ============================================================

def text(value: object) -> str:
    """统一转成去空白的字符串（None / 非字符串一律安全处理）。"""
    return str(value).strip() if value is not None else ""


def is_remote(value: str) -> bool:
    return value.startswith(("http://", "https://", "//", "data:"))


def _file_exists(root: Path, value: str) -> bool:
    if not value or is_remote(value):
        return False
    try:
        return (root / value).is_file()
    except OSError:
        return False


def parse_int(value: object, default: int, *, low: int = 0, high: int = 10_000_000) -> int:
    """把查询参数转成整数，越界或非法时回落到默认值。"""
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def parse_duration(value: object) -> float | None:
    """时长归一化：接受 48、"48"、"00:48"、"1:02:33"、null。"""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value) if float(value) >= 0 else None

    raw = text(value)
    if re.fullmatch(r"\d+(\.\d+)?", raw):
        return float(raw)

    parts = raw.split(":")
    if not 2 <= len(parts) <= 3:
        return None
    try:
        numbers = [float(piece) for piece in parts]
    except ValueError:
        return None
    if any(number < 0 for number in numbers):
        return None
    return sum(number * (60 ** (len(numbers) - 1 - index)) for index, number in enumerate(numbers))


def duration_text(seconds: float | None) -> str:
    """秒数 → "00:48" / "12:05" / "1:02:33"。"""
    if seconds is None:
        return ""
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


_ID_PATTERNS = {
    "youtube": [
        re.compile(r"(?:youtube\.com/watch\?[^#]*\bv=)([\w-]{6,})", re.I),
        re.compile(r"(?:youtu\.be/)([\w-]{6,})", re.I),
        re.compile(r"(?:youtube\.com/(?:embed|shorts|live)/)([\w-]{6,})", re.I),
    ],
    "bilibili": [
        re.compile(r"/video/(BV[\w]{6,})", re.I),
        re.compile(r"[?&]bvid=(BV[\w]{6,})", re.I),
        re.compile(r"/video/av(\d+)", re.I),            # 老式的 av 号，取到数字后统一加 av 前缀
        re.compile(r"[?&]aid=(\d+)", re.I),
    ],
    "vimeo": [re.compile(r"vimeo\.com/(?:video/)?(\d{5,})", re.I)],
}


def video_id(provider: str, raw: str) -> str:
    """从链接或裸 ID 里取视频标识；取不出来返回空串。

    B 站的 av 号统一成 `av123` 的形式返回，调用方据此决定用 `aid=` 还是 `bvid=`。
    """
    provider = text(provider).lower()
    src = text(raw)
    if not src or provider in ("", "file", "embed"):
        return ""
    for pattern in _ID_PATTERNS.get(provider, []):
        matched = pattern.search(src)
        if matched:
            found = matched.group(1)
            if provider == "bilibili" and found.isdigit():
                return f"av{found}"
            return found
    if provider == "bilibili" and re.fullmatch(r"av\d+", src, re.I):
        return src.lower()
    if re.fullmatch(r"[\w-]{6,}", src):
        return src
    return ""


def embed_url(provider: str, raw: str) -> str:
    """把链接（或裸 ID）转成可直接放进 iframe 的播放地址。"""
    provider = text(provider).lower()
    src = text(raw)
    if not src or provider in ("", "file"):
        return ""

    video = video_id(provider, src)
    if not video:
        return src          # 认不出来就原样交给 iframe

    if provider == "youtube":
        return f"https://www.youtube.com/embed/{video}?rel=0"
    if provider == "bilibili":
        if video.lower().startswith("av"):
            return (f"https://player.bilibili.com/player.html?aid={video[2:]}"
                    "&autoplay=0&danmaku=0&high_quality=1")
        return (f"https://player.bilibili.com/player.html?bvid={video}"
                "&autoplay=0&danmaku=0&high_quality=1")
    if provider == "vimeo":
        return f"https://player.vimeo.com/video/{video}"
    return src


def provider_label(provider: str) -> str:
    return PROVIDER_LABELS.get(text(provider).lower(), PROVIDER_LABELS["embed"])


def is_embed(provider: str) -> bool:
    """外链视频（src 不是本地文件路径）。"""
    return text(provider).lower() not in ("", "file")


def date_key(value: object) -> str:
    """把各种时间写法压成可比较的 "YYYY-MM-DDTHH:MM:SS"；认不出来返回空串。"""
    raw = text(value)
    if not raw:
        return ""

    candidate = raw.replace("/", "-").replace(" ", "T")
    matched = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:T(\d{1,2}):(\d{2})(?::(\d{2}))?)?", candidate)
    if not matched:
        return ""
    year, month, day = matched.group(1), matched.group(2), matched.group(3)
    hour, minute, second = matched.group(4) or "00", matched.group(5) or "00", matched.group(6) or "00"
    return f"{year}-{int(month):02d}-{int(day):02d}T{int(hour):02d}:{minute}:{second}"


def friendly_date(value: object) -> str:
    """展示用日期：有日期显示日期，有时分则一并显示。"""
    key = date_key(value)
    if not key:
        return text(value)
    day, _, clock = key.partition("T")
    return day if clock == "00:00:00" else f"{day} {clock}"


# ============================================================
# EXIF
# ============================================================

def exif_rows(item: dict, kind: str) -> list[dict[str, str]]:
    """按固定顺序输出 EXIF 行，前端直接遍历渲染。"""
    exif = item.get("exif")
    if not isinstance(exif, dict):
        return []
    rows = []
    for key, label in EXIF_LABELS.get(kind, EXIF_LABELS["photo"]):
        value = text(exif.get(key))
        if value:
            rows.append({"label": label, "value": value})
    return rows


def exif_summary(item: dict, kind: str) -> str:
    """一行摘要，用于列表 / 卡片副标题。"""
    exif = item.get("exif")
    if not isinstance(exif, dict):
        return ""
    return " · ".join(text(exif[key]) for key in EXIF_SUMMARY_KEYS if text(exif.get(key)))


# ============================================================
# 媒体条目（照片 + 视频统一结构）
# ============================================================

def normalize_media(raw: dict, kind: str) -> dict:
    """把 data/*.json 里的原始条目规范化成统一的媒体条目。"""
    exif = raw.get("exif") if isinstance(raw.get("exif"), dict) else {}
    provider = text(raw.get("provider"))
    if kind == "video" and not provider:
        provider = "file"

    src = text(raw.get("src"))
    tags = [text(tag) for tag in (raw.get("tags") or []) if text(tag)]

    item = {
        "kind": kind,
        "id": text(raw.get("id")),
        "title": text(raw.get("title")) or "未命名作品",
        "album": text(raw.get("album")),
        "src": src,
        "thumb": text(raw.get("thumb")) or src,
        "poster": text(raw.get("poster")),
        "date": text(raw.get("date")) or text(exif.get("dateTimeOriginal")),
        "location": text(raw.get("location")),
        "description": text(raw.get("description")),
        "tags": tags,
        "exif": exif,
        "provider": provider,
        "duration": parse_duration(raw.get("duration")),
        "resolution": text(raw.get("resolution")),
    }

    # 搜索索引只拼一次，避免每次输入都重算
    item["search"] = " ".join([
        item["id"], item["title"], item["album"], item["location"], item["description"],
        text(exif.get("camera")), text(exif.get("lens")), item["resolution"],
        " ".join(tags),
    ]).lower()
    return item


def media_items(photos: list[dict], videos: list[dict]) -> list[dict]:
    items = [normalize_media(raw, "photo") for raw in photos]
    items += [normalize_media(raw, "video") for raw in videos]
    return items


# ============================================================
# 筛选 / 排序 / 分页
# ============================================================

def search_terms(query: object) -> list[str]:
    return [term for term in re.split(r"\s+", text(query).lower()) if term]


def matches(item: dict, *, terms: list[str], kind: str = "", album: str = "", tags: list[str] = ()) -> bool:
    if kind and kind != "all" and item.get("kind") != kind:
        return False
    if album and item.get("album") != album:
        return False
    if tags and not all(tag in item.get("tags", []) for tag in tags):
        return False
    haystack = item.get("search", "")
    return all(term in haystack for term in terms)


def sort_media(items: list[dict], sort: str) -> list[dict]:
    """带日期的在前并按时间排，不带日期的沉底（避免空值乱序）。"""
    sort = text(sort) or "date-desc"
    if sort in ("date-desc", "date-asc"):
        dated = [i for i in items if date_key(i.get("date"))]
        undated = [i for i in items if not date_key(i.get("date"))]
        dated.sort(key=lambda i: date_key(i.get("date")), reverse=(sort == "date-desc"))
        undated.sort(key=lambda i: i.get("id", ""))
        return dated + undated
    if sort == "title-asc":
        return sorted(items, key=lambda i: (i.get("title", "").casefold(), i.get("id", "")))
    if sort == "album":
        # 相册分组：组内仍然「新 → 旧」，没有日期的沉到组尾
        return sorted(items, key=lambda i: (
            i.get("album", "").casefold(),
            not date_key(i.get("date")),
            _reverse_date(i),
        ))
    if sort == "id-asc":
        return sorted(items, key=lambda i: i.get("id", ""))
    return sort_media(items, "date-desc")


def _reverse_date(item: dict) -> str:
    """把日期取反用于「相册分组内新 → 旧」排序。"""
    key = date_key(item.get("date"))
    if not key:
        return ""            # 空值排最后
    # 9 - 每位数字，保证字符串序与时间序相反
    return "".join(str(9 - int(ch)) if ch.isdigit() else ch for ch in key)


def paginate(items: list[dict], page: int, page_size: int) -> tuple[list[dict], dict]:
    page_size = max(1, min(MAX_PAGE_SIZE, page_size))
    total = len(items)
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, pages))
    start = (page - 1) * page_size
    chunk = items[start:start + page_size]
    meta = {
        "total": total,
        "page": page,
        "pageSize": page_size,
        "pages": pages,
        "hasMore": start + len(chunk) < total,
    }
    return chunk, meta


def query_media(items: list[dict], *, q: str = "", kind: str = "all", album: str = "",
                tags: tuple[str, ...] = (), sort: str = "date-desc",
                page: int = 1, page_size: int = 24) -> tuple[list[dict], dict]:
    """搜索 → 筛选 → 排序 → 分页，一次到位。"""
    terms = search_terms(q)
    selected = [i for i in items
                if matches(i, terms=terms, kind=kind, album=album, tags=list(tags))]
    page_items, meta = paginate(sort_media(selected, sort), page, page_size)
    return page_items, meta


# ============================================================
# 公共只读 API 的展示结构
# ============================================================

def album_lookup(albums: list[dict]) -> dict[str, dict]:
    return {text(a.get("id")): a for a in albums}


def _album_name(albums: dict[str, dict], album_id: str) -> str:
    found = albums.get(album_id)
    if found is None:
        return "未分类" if not album_id else album_id
    return text(found.get("name")) or album_id


def media_card(item: dict, albums: dict[str, dict], root: Path) -> dict:
    """作品卡片：字段都算好，前端只做 HTML 拼接。"""
    is_video = item["kind"] == "video"
    embed = is_embed(item["provider"])

    # 视频只能用封面（poster）当图片：拿 mp4 当 <img> 只会碎图；
    # 照片用缩略图（没有 thumb 时 normalize 已回落到原图）。
    image_path = item["poster"] if is_video else item["thumb"]

    album_name = _album_name(albums, item["album"])
    location = item["location"]
    subtitle = " · ".join(part for part in (album_name if item["album"] else "", location) if part)

    return {
        "id": item["id"],
        "kind": item["kind"],
        "title": item["title"],
        "subtitle": subtitle,
        "albumId": item["album"],
        "albumName": album_name,
        "date": item["date"],
        "dateText": friendly_date(item["date"]),
        "year": date_key(item["date"])[:4],
        "location": location,
        "description": item["description"],
        "tags": item["tags"],
        "imageUrl": image_path,
        "imageMissing": bool(image_path) and not is_remote(image_path) and not _file_exists(root, image_path),
        "srcUrl": "" if embed else item["src"],
        "srcMissing": bool(item["src"]) and not embed and not _file_exists(root, item["src"]),
        "provider": item["provider"],
        "providerLabel": provider_label(item["provider"]) if is_video else "",
        "embedUrl": embed_url(item["provider"], item["src"]) if is_video else "",
        "durationText": duration_text(item["duration"]) if is_video else "",
        "resolution": item["resolution"] if is_video else "",
        "exif": exif_rows(item, item["kind"]),
        "exifSummary": exif_summary(item, item["kind"]),
        "isEmbed": embed,
    }


def media_detail(item: dict, albums: dict[str, dict], root: Path) -> dict:
    """灯箱用的详情：卡片字段 + 原图地址 + EXIF 原文件解析入口。"""
    detail = media_card(item, albums, root)
    detail["fullUrl"] = detail["srcUrl"] or detail["imageUrl"]
    detail["parseExifPath"] = item["src"] if item["kind"] == "photo" else ""
    detail["exifSource"] = "entry" if detail["exif"] else "file"
    return detail


def album_cards(albums: list[dict], photos: list[dict], videos: list[dict], root: Path) -> list[dict]:
    """相册总览卡片，含条目数与封面兜底（没有封面时用第一张照片）。"""
    counts: dict[str, int] = {}
    first_image: dict[str, str] = {}
    for kind, raw_items in (("photo", photos), ("video", videos)):
        for raw in raw_items:
            album_id = text(raw.get("album"))
            if not album_id:
                continue
            counts[album_id] = counts.get(album_id, 0) + 1
            if album_id not in first_image:
                candidate = text(raw.get("thumb")) or text(raw.get("src")) or text(raw.get("poster"))
                if candidate and not is_remote(candidate):
                    first_image[album_id] = candidate

    cards = []
    for raw in albums:
        album_id = text(raw.get("id"))
        cover = text(raw.get("cover")) or first_image.get(album_id, "")
        cards.append({
            "id": album_id,
            "name": text(raw.get("name")) or album_id,
            "description": text(raw.get("description")),
            "cover": cover,
            "coverMissing": bool(cover) and not _file_exists(root, cover),
            "count": counts.get(album_id, 0),
        })
    return cards


def facets(albums: list[dict], media: list[dict]) -> dict[str, list[dict]]:
    """筛选栏用的聚合：只列出真正有内容的相册与标签。"""
    lookup = album_lookup(albums)
    album_counts: dict[str, int] = {}
    tag_counts: dict[str, int] = {}
    for item in media:
        if item["album"]:
            album_counts[item["album"]] = album_counts.get(item["album"], 0) + 1
        for tag in item["tags"]:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1

    album_facets = [
        {"value": album_id, "label": _album_name(lookup, album_id), "count": count}
        for album_id, count in album_counts.items()
    ]
    album_facets.sort(key=lambda f: (-f["count"], f["label"]))

    tag_facets = [{"value": tag, "label": tag, "count": count} for tag, count in tag_counts.items()]
    tag_facets.sort(key=lambda f: (-f["count"], f["label"]))

    return {"albums": album_facets, "tags": tag_facets}


def site_stats(albums: list[dict], media: list[dict]) -> dict[str, int]:
    tags = {tag for item in media for tag in item["tags"]}
    return {
        "photos": sum(1 for item in media if item["kind"] == "photo"),
        "videos": sum(1 for item in media if item["kind"] == "video"),
        "albums": len(albums),
        "tags": len(tags),
    }


# ============================================================
# 后台表格：列定义 + 每行「已算好」的单元格
# ============================================================

ADMIN_COLUMNS = {
    "photos": [
        {"key": "thumb", "label": "预览"},
        {"key": "title", "label": "标题"},
        {"key": "album", "label": "相册"},
        {"key": "date", "label": "时间"},
        {"key": "tags", "label": "标签"},
        {"key": "exif", "label": "EXIF"},
        {"key": "actions", "label": ""},
    ],
    "videos": [
        {"key": "thumb", "label": "封面"},
        {"key": "title", "label": "标题"},
        {"key": "provider", "label": "来源"},
        {"key": "duration", "label": "时长"},
        {"key": "resolution", "label": "分辨率"},
        {"key": "album", "label": "相册"},
        {"key": "actions", "label": ""},
    ],
    "albums": [
        {"key": "thumb", "label": "封面"},
        {"key": "id", "label": "ID"},
        {"key": "name", "label": "名称"},
        {"key": "description", "label": "描述"},
        {"key": "count", "label": "条目"},
        {"key": "actions", "label": ""},
    ],
}


def admin_columns(collection: str) -> list[dict]:
    return ADMIN_COLUMNS.get(collection, [])


def _thumb_cell(value: str, fallback: str, root: Path, *, wide: bool = False) -> dict:
    return {
        "kind": "thumb",
        "url": "" if is_remote(value) else value,
        "remoteUrl": value if is_remote(value) else "",
        "missing": bool(value) and not is_remote(value) and not _file_exists(root, value),
        "fallback": fallback,
        "wide": wide,
    }


def _admin_search_index(collection: str, raw: dict, album_name: str) -> str:
    exif = raw.get("exif") if isinstance(raw.get("exif"), dict) else {}
    parts = [
        raw.get("id"), raw.get("title"), raw.get("name"), raw.get("description"),
        album_name, raw.get("album"), raw.get("location"), raw.get("resolution"),
        exif.get("camera") if isinstance(exif, dict) else "",
        exif.get("lens") if isinstance(exif, dict) else "",
    ]
    tags = raw.get("tags")
    if isinstance(tags, list):
        parts.extend(tags)
    return " ".join(text(part) for part in parts if part).lower()


def admin_rows(collection: str, raw_items: list[dict], albums: list[dict],
               media_counts: dict[str, int] | None = None, *, root: Path) -> list[dict]:
    lookup = album_lookup(albums)
    rows = []
    for raw in raw_items:
        item_id = text(raw.get("id"))
        album_id = text(raw.get("album"))
        album_name = _album_name(lookup, album_id) if collection in ("photos", "videos") else ""
        if collection == "albums":
            album_name = ""

        if collection == "photos":
            cells = [
                _thumb_cell(text(raw.get("thumb")) or text(raw.get("src")), "无图", root),
                {"kind": "title", "text": text(raw.get("title")) or "未命名", "sub": item_id},
                {"kind": "text", "text": album_name},
                {"kind": "sub", "text": friendly_date(raw.get("date")) or "–"},
                {"kind": "tags", "tags": [text(tag) for tag in (raw.get("tags") or []) if text(tag)]},
                {"kind": "sub", "text": exif_summary(raw, "photo") or "–"},
                {"kind": "actions", "edit": f"{collection}:{item_id}", "del": f"{collection}:{item_id}"},
            ]
        elif collection == "videos":
            provider = text(raw.get("provider")) or "file"
            cells = [
                _thumb_cell(text(raw.get("poster")), "无封面", root, wide=True),
                {"kind": "title", "text": text(raw.get("title")) or "未命名", "sub": item_id},
                {"kind": "badge", "text": provider, "tone": "file" if provider == "file" else "embed",
                 "title": provider_label(provider)},
                {"kind": "sub", "text": duration_text(parse_duration(raw.get("duration"))) or "–"},
                {"kind": "sub", "text": text(raw.get("resolution")) or "–"},
                {"kind": "text", "text": album_name},
                # poster 动作：打开「更换封面」对话框（上传图片或抓帧）
                {"kind": "actions", "edit": f"{collection}:{item_id}", "del": f"{collection}:{item_id}",
                 "poster": f"{collection}:{item_id}"},
            ]
        else:
            cells = [
                _thumb_cell(text(raw.get("cover")), "无封面", root),
                {"kind": "sub", "text": item_id},
                {"kind": "title", "text": text(raw.get("name"))},
                {"kind": "muted", "text": text(raw.get("description")) or "–"},
                {"kind": "text", "text": str((media_counts or {}).get(item_id, 0))},
                {"kind": "actions", "edit": f"{collection}:{item_id}", "del": f"{collection}:{item_id}"},
            ]

        rows.append({
            "id": item_id,
            "collection": collection,
            "cells": cells,
            "search": _admin_search_index(collection, raw, album_name),
            # 排序 / 筛选用的键（前端不再自己比较字段）
            "albumId": album_id,
            "tags": [text(tag) for tag in (raw.get("tags") or []) if text(tag)],
            "sortKeys": {
                "id": item_id,
                "title": text(raw.get("title")),
                "name": text(raw.get("name")),
                "date": date_key(raw.get("date")),
                "hasDate": bool(date_key(raw.get("date"))),
                "count": (media_counts or {}).get(item_id, 0),
            },
        })
    return rows


def admin_query(collection: str, rows: list[dict], *, q: str = "", album: str = "",
                tag: str = "", sort: str = "", page: int = 1,
                page_size: int | None = None) -> tuple[list[dict], dict]:
    """后台表格的搜索 / 排序 / 分页（与公开端共用同一套搜索语义）。"""
    terms = search_terms(q)
    selected = [row for row in rows if all(term in row["search"] for term in terms)]

    if album:
        selected = [row for row in selected if row.get("albumId") == album]
    if tag:
        selected = [row for row in selected if tag in (row.get("tags") or [])]

    selected = sort_admin_rows(collection, selected, sort)
    return paginate(selected, page, page_size or DEFAULT_PAGE_SIZE.get(collection, 200))


def sort_admin_rows(collection: str, rows: list[dict], sort: str) -> list[dict]:
    """后台表格排序：默认按时间（相册按 ID），无日期的沉底。"""
    if not sort:
        sort = "id-asc" if collection == "albums" else "date-desc"

    def keys(row: dict) -> dict:
        return row.get("sortKeys") or {}

    if sort == "count-desc":
        return sorted(rows, key=lambda r: (-int(keys(r).get("count") or 0), keys(r).get("id", "")))
    if sort == "name-asc":
        return sorted(rows, key=lambda r: (keys(r).get("name", "").casefold(), keys(r).get("id", "")))
    if sort == "title-asc":
        return sorted(rows, key=lambda r: (keys(r).get("title", "").casefold(), keys(r).get("id", "")))
    if sort == "id-asc":
        return sorted(rows, key=lambda r: keys(r).get("id", ""))
    if sort == "date-asc":
        return sorted(rows, key=lambda r: (keys(r).get("hasDate") is False,
                                          keys(r).get("date", ""), keys(r).get("id", "")))
    # date-desc（默认）
    return sorted(rows, key=lambda r: (keys(r).get("hasDate") is False,
                                       _reverse_date({"date": keys(r).get("date", "")}),
                                       keys(r).get("id", "")))


def human_size(size: int) -> str:
    """字节 → 人类可读（备份列表用）。"""
    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


# ============================================================
# 整块响应（admin.py 与 serve.py 共用，逻辑只写一份）
# ============================================================

def album_entry_counts(store_obj: "store.Store") -> dict[str, int]:
    """每个相册下有多少条照片 / 视频。"""
    counts: dict[str, int] = {}
    for collection in ("photos", "videos"):
        for item in store_obj.load(collection):
            album_id = text(item.get("album"))
            if album_id:
                counts[album_id] = counts.get(album_id, 0) + 1
    return counts


def site_payload(albums: list[dict], photos: list[dict], videos: list[dict], root: Path) -> dict:
    """公开端首屏：统计、相册总览、筛选项。"""
    media = media_items(photos, videos)
    return {
        "ok": True,
        "stats": site_stats(albums, media),
        "albums": album_cards(albums, photos, videos, root),
        "facets": facets(albums, media),
        "sorts": PUBLIC_SORTS,
        "types": PUBLIC_TYPES,
    }


def gallery_payload(albums: list[dict], photos: list[dict], videos: list[dict], root: Path,
                    *, q: str = "", kind: str = "all", album: str = "", tags: tuple[str, ...] = (),
                    sort: str = "date-desc", page: int = 1, page_size: int = 24) -> dict:
    """公开端作品列表：筛选 / 排序 / 分页都在服务端做完。"""
    media = media_items(photos, videos)
    page_items, meta = query_media(
        media, q=q, kind=kind, album=album, tags=tuple(tags),
        sort=sort, page=page, page_size=page_size,
    )
    lookup = album_lookup(albums)
    return {
        "ok": True,
        "items": [media_card(item, lookup, root) for item in page_items],
        "meta": meta,
        "facets": facets(albums, media),
        "active": {
            "q": text(q), "type": kind, "album": text(album),
            "tags": list(tags), "sort": text(sort) or "date-desc",
        },
        "sorts": PUBLIC_SORTS,
        "types": PUBLIC_TYPES,
    }


def admin_items_payload(store_obj: "store.Store", root: Path, collection: str, *,
                        q: str = "", album: str = "", tag: str = "", sort: str = "",
                        page: int = 1, page_size: int | None = None) -> dict:
    """后台表格：列定义 + 每行算好的单元格 + 分页信息。"""
    albums = store_obj.load("albums")
    counts = album_entry_counts(store_obj) if collection == "albums" else None
    rows = admin_rows(collection, store_obj.load(collection), albums, counts, root=root)
    page_rows, meta = admin_query(
        collection, rows, q=q, album=album, tag=tag, sort=sort, page=page, page_size=page_size,
    )
    return {
        "ok": True,
        "collection": collection,
        "columns": admin_columns(collection),
        "items": page_rows,
        "meta": meta,
    }


__all__ = [
    "ADMIN_SORTS", "DEFAULT_PAGE_SIZE", "MEDIA_KINDS", "PUBLIC_SORTS", "PUBLIC_TYPES",
    "admin_columns", "admin_items_payload", "admin_query", "admin_rows", "album_cards",
    "album_entry_counts", "album_lookup", "date_key", "duration_text", "embed_url",
    "exif_rows", "exif_summary", "facets", "friendly_date", "gallery_payload",
    "human_size", "is_embed", "is_remote", "matches", "media_card", "media_detail",
    "media_items", "normalize_media", "paginate", "parse_duration", "parse_int",
    "provider_label", "query_media", "search_terms", "site_payload", "site_stats",
    "sort_media", "store", "text", "video_id",
]
