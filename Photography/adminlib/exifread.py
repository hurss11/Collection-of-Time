# -*- coding: utf-8 -*-
"""纯标准库的 JPEG EXIF 解析。

与前端 assets/js/exif.js 保持一致的字段映射，用于上传照片时自动填充拍摄参数。

用法：
    from adminlib.exifread import read_exif, to_entry_exif
    raw = read_exif(path)            # {tag: value}，无 EXIF 时返回 {}
    entry = to_entry_exif(raw)       # 可直接写进 photos.json 的 exif 字段
"""
from __future__ import annotations

import struct
from pathlib import Path

# TIFF 标签 → (英文字段名, 中文展示名)
TAG_LABELS: dict[int, tuple[str, str]] = {
    0x010F: ("Make", "相机品牌"),
    0x0110: ("Model", "相机型号"),
    0x0112: ("Orientation", "方向"),
    0x0132: ("DateTime", "修改时间"),
    0x9003: ("DateTimeOriginal", "拍摄时间"),
    0x829A: ("ExposureTime", "快门"),
    0x829D: ("FNumber", "光圈"),
    0x8827: ("ISO", "ISO"),
    0x920A: ("FocalLength", "焦距"),
    0x9209: ("Flash", "闪光灯"),
    0xA002: ("PixelXDimension", "宽度"),
    0xA003: ("PixelYDimension", "高度"),
    0xA434: ("LensModel", "镜头"),
    0x9291: ("SubSecTimeOriginal", "亚秒"),
}

TAG_EXIF_IFD = 0x8769

# TIFF 数据类型 → (字节数, struct 格式字符)
TYPE_INFO: dict[int, tuple[int, str]] = {
    1: (1, "B"),   # BYTE
    2: (1, "c"),   # ASCII
    3: (2, "H"),   # SHORT
    4: (4, "I"),   # LONG
    5: (8, "II"),  # RATIONAL
    7: (1, "B"),   # UNDEFINED
    9: (4, "i"),   # SLONG
    10: (8, "ii"), # SRATIONAL
}

MAX_IFD_ENTRIES = 512


class ExifError(ValueError):
    """EXIF 解析失败（不是 JPEG、缺少 APP1、TIFF 头异常等）。"""


def _read_value(data: bytes, tiff: int, type_id: int, count: int, value_offset: int,
                endian: str) -> object:
    """读取一个 IFD 条目的值。"""
    info = TYPE_INFO.get(type_id)
    if info is None:
        return None
    size, fmt = info
    total = size * count
    if total <= 0:
        return None

    # 值不超过 4 字节时内联存放，否则存放的是偏移量
    if total <= 4:
        start = value_offset
    else:
        start = tiff + struct.unpack_from(f"{endian}I", data, value_offset)[0]

    if start < 0 or start + total > len(data):
        return None

    if type_id == 2:  # ASCII
        raw = data[start:start + count]
        return raw.split(b"\x00", 1)[0].decode("ascii", "replace").strip()

    if type_id in (5, 10):  # RATIONAL / SRATIONAL
        results = []
        for i in range(count):
            num, den = struct.unpack_from(f"{endian}{fmt}", data, start + i * 8)
            results.append(num / den if den else None)
        return results[0] if count == 1 else results

    values = struct.unpack_from(f"{endian}{count}{fmt}", data, start)
    return values[0] if count == 1 else list(values)


def _read_ifd(data: bytes, tiff: int, dir_start: int, endian: str) -> dict[int, object]:
    """解析一个 IFD，返回 {tag: value}。"""
    out: dict[int, object] = {}
    if dir_start < 0 or dir_start + 2 > len(data):
        return out

    (entries,) = struct.unpack_from(f"{endian}H", data, dir_start)
    entries = min(entries, MAX_IFD_ENTRIES)

    for i in range(entries):
        entry = dir_start + 2 + i * 12
        if entry + 12 > len(data):
            break

        tag, type_id, count = struct.unpack_from(f"{endian}HHI", data, entry)
        value_offset = entry + 8

        if tag == TAG_EXIF_IFD:
            out[tag] = struct.unpack_from(f"{endian}I", data, value_offset)[0]
            continue

        try:
            out[tag] = _read_value(data, tiff, type_id, count, value_offset, endian)
        except struct.error:
            continue  # 单个标签损坏时跳过

    return out


def parse_exif(buffer: bytes) -> dict[int, object]:
    """从 JPEG 字节流解析 EXIF，返回 {tag: value}。

    任何畸形 / 截断的输入都会被归一化成 ExifError，不会抛出 struct.error 之类的底层异常。
    """
    try:
        return _parse_exif(buffer)
    except ExifError:
        raise
    except (struct.error, IndexError, KeyError, ValueError) as exc:
        raise ExifError(f"EXIF 结构损坏：{type(exc).__name__}") from exc


def _parse_exif(buffer: bytes) -> dict[int, object]:
    if len(buffer) < 4 or buffer[0:2] != b"\xff\xd8":
        raise ExifError("不是有效的 JPEG 文件（缺少 SOI 标记）")

    # 1) 遍历标记段，定位 APP1 / Exif
    offset = 2
    tiff = -1
    while offset + 4 <= len(buffer):
        if buffer[offset] != 0xFF:
            offset += 1
            continue

        marker = buffer[offset + 1]
        if marker in (0xDA, 0xD9):  # SOS / EOI 之后不再有 EXIF
            break

        seg_size = struct.unpack_from(">H", buffer, offset + 2)[0]
        seg_start = offset + 4

        if marker == 0xE1 and buffer[seg_start:seg_start + 6] == b"Exif\x00\x00":
            tiff = seg_start + 6
            break

        offset = seg_start + seg_size - 2

    if tiff < 0:
        raise ExifError("该图片不包含 EXIF 信息")
    if tiff + 8 > len(buffer):
        raise ExifError("EXIF 数据被截断")

    # 2) TIFF 头：字节序 + 魔数 42
    endian_mark = buffer[tiff:tiff + 2]
    if endian_mark == b"II":
        endian = "<"
    elif endian_mark == b"MM":
        endian = ">"
    else:
        raise ExifError("未知的 TIFF 字节序")

    (magic,) = struct.unpack_from(f"{endian}H", buffer, tiff + 2)
    if magic != 42:
        raise ExifError("TIFF 头校验失败")

    (ifd0_offset,) = struct.unpack_from(f"{endian}I", buffer, tiff + 4)
    ifd0 = _read_ifd(buffer, tiff, tiff + ifd0_offset, endian)

    exif_ifd: dict[int, object] = {}
    pointer = ifd0.get(TAG_EXIF_IFD)
    if isinstance(pointer, int):
        exif_ifd = _read_ifd(buffer, tiff, tiff + pointer, endian)

    return {**ifd0, **exif_ifd}


def read_exif(path: str | Path) -> dict[int, object]:
    """从文件读取并解析 EXIF；文件不存在或非 JPEG 时抛出 ExifError。"""
    file_path = Path(path)
    if not file_path.is_file():
        raise ExifError(f"文件不存在：{file_path}")
    # 只读文件头部即可，避免把整个视频 / 大图读进内存
    with file_path.open("rb") as fh:
        buffer = fh.read(1024 * 1024)
    return parse_exif(buffer)


# ---------- 值格式化（与前端 display 逻辑对应） ----------

def _format_datetime(value: object) -> str | None:
    """EXIF 时间 "2026:01:18 06:42:05" → "2026-01-18 06:42:05"。"""
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if len(raw) >= 19 and raw[4] == ":" and raw[7] == ":":
        # 索引：0-3 年 / 5-6 月 / 8-9 日 / 11-18 时间
        return f"{raw[0:4]}-{raw[5:7]}-{raw[8:10]} {raw[11:19]}"
    return raw or None


def _format_exposure(value: object) -> str | None:
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    if value >= 1:
        return f"{round(float(value), 2):g} s"
    return f"1/{round(1 / float(value))} s"


def _format_aperture(value: object) -> str | None:
    return f"f/{round(float(value), 1):g}" if isinstance(value, (int, float)) else None


def _format_focal(value: object) -> str | None:
    return f"{round(float(value), 1):g} mm" if isinstance(value, (int, float)) else None


def _first(value: object) -> object:
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


def to_entry_exif(raw: dict[int, object], *, fallback_date: str = "") -> dict[str, str]:
    """把原始标签映射转成 photos.json / videos.json 里 exif 字段的形状。

    只保留有值且非空的字段，方便直接合并进条目。
    """
    make = raw.get(0x010F)
    model = raw.get(0x0110)

    camera = ""
    if model:
        # 相机型号通常已包含品牌前缀，避免出现 "Canon Canon EOS R5"
        model_text = str(model).strip()
        make_text = str(make).strip() if make else ""
        if make_text and not model_text.lower().startswith(make_text.lower()):
            camera = f"{make_text} {model_text}"
        else:
            camera = model_text

    width = _first(raw.get(0xA002))
    height = _first(raw.get(0xA003))
    dimensions = f"{width} × {height}" if width and height else ""

    candidates = {
        "camera": camera,
        "lens": str(raw.get(0xA434)).strip() if raw.get(0xA434) else "",
        "focalLength": _format_focal(_first(raw.get(0x920A))) or "",
        "aperture": _format_aperture(_first(raw.get(0x829D))) or "",
        "shutter": _format_exposure(_first(raw.get(0x829A))) or "",
        "iso": f"ISO {_first(raw.get(0x8827))}" if _first(raw.get(0x8827)) else "",
        "dateTimeOriginal": _format_datetime(raw.get(0x9003) or raw.get(0x0132)) or fallback_date,
        "dimensions": dimensions,
    }
    return {key: value for key, value in candidates.items() if value}
