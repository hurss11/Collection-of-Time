# -*- coding: utf-8 -*-
"""纯标准库的视频容器解析：MP4/MOV/M4V/3GP、Matroska/WebM、AVI。

为什么需要它：视频的时长 / 分辨率 / 帧率 / 编码 / 拍摄设备本来只能靠 ffprobe，
但服务器上「只有 ffmpeg、没有 ffprobe」甚至「两个都没有」都很常见 ——
这里直接读容器头部，不装任何外部程序也能把元数据识别出来。

返回值与 adminlib.media.probe_media 对齐，便于「ffprobe 优先、本地解析兜底」地合并：
    duration   int | None      秒（四舍五入）
    resolution "1920 × 1080"
    fps        "25 fps"
    codec      "H.264"
    device     "iPhone 15 Pro"  （来自 Apple 元数据，没有则为空）
    createdAt  "2026-01-18 06:42:00"（容器里的原始时间，即相机时钟）

任何畸形文件都只会得到空字段，绝不抛异常 —— 解析失败不应该影响上传。
"""
from __future__ import annotations

import struct
from datetime import datetime, timedelta
from pathlib import Path

# 编码标识 → 展示名。ffprobe 的 codec_name 与 MP4/AVI 的 fourcc 都查这张表。
CODEC_NAMES: dict[str, str] = {
    "h264": "H.264", "avc1": "H.264", "avc3": "H.264", "x264": "H.264",
    "hevc": "H.265 / HEVC", "h265": "H.265 / HEVC", "hvc1": "H.265 / HEVC", "hev1": "H.265 / HEVC",
    "vp8": "VP8", "vp08": "VP8", "vp9": "VP9", "vp09": "VP9",
    "av1": "AV1", "av01": "AV1",
    "mpeg4": "MPEG-4", "mp4v": "MPEG-4", "fmp4": "MPEG-4", "divx": "DivX (MPEG-4)",
    "xvid": "Xvid (MPEG-4)", "3ivx": "MPEG-4",
    "mjpeg": "Motion JPEG", "mjpg": "Motion JPEG", "jpeg": "Motion JPEG",
    "prores": "ProRes", "apch": "ProRes 422 HQ", "apcn": "ProRes 422",
    "apcs": "ProRes 422 LT", "apco": "ProRes 422 Proxy", "ap4h": "ProRes 4444",
    "ap4x": "ProRes 4444 XQ", "prra": "ProRes RAW", "prrb": "ProRes RAW HQ",
    "v_mpeg4/iso/avc": "H.264", "v_mpegh/iso/hevc": "H.265 / HEVC",
    "v_vp8": "VP8", "v_vp9": "VP9", "v_av1": "AV1",
    "v_mpeg4/iso/asp": "MPEG-4", "v_mpeg2": "MPEG-2", "v_mjpeg": "Motion JPEG",
    "vc1": "VC-1", "wmv3": "WMV9", "dvvideo": "DV", "theora": "Theora",
    "cvid": "Cinepak", "rawvideo": "RAW",
}

FLAT_FIELDS = ("duration", "resolution", "fps", "codec", "device", "createdAt")

# 容器里已经带时间时区的品牌（Apple 写的是 UTC）
EPOCH_1904 = datetime(1904, 1, 1)
MAX_BUDGET = 8 * 1024 * 1024          # 单次解析最多读取的字节数
EBML_PREFIX = 4 * 1024 * 1024         # Matroska / AVI 只看文件开头这么多
AVI_MAX_HEADER = 4 * 1024 * 1024


def empty_info() -> dict[str, object]:
    return {"duration": None, "resolution": "", "fps": "", "codec": "", "device": "", "createdAt": ""}


def friendly_codec(name: str | bytes | None) -> str:
    """把 codec_name / fourcc 换成好看的展示名；认不出来就原样返回。"""
    if not name:
        return ""
    text = name.decode("ascii", "ignore") if isinstance(name, bytes) else str(name)
    text = text.strip().rstrip("\x00").strip()
    if not text:
        return ""
    return CODEC_NAMES.get(text.lower(), CODEC_NAMES.get(text.lower().rstrip("\x00 "), text))


class _Reader:
    """按需读取文件片段，绝不把整个视频读进内存。"""

    def __init__(self, path: Path, budget: int = MAX_BUDGET) -> None:
        self.fh = path.open("rb")
        self.size = path.stat().st_size
        self.budget = budget

    def read(self, offset: int, length: int) -> bytes:
        length = min(length, self.budget)
        if length <= 0 or offset < 0 or offset >= self.size:
            return b""
        self.budget -= length
        self.fh.seek(offset)
        return self.fh.read(length)

    def prefix(self, length: int) -> bytes:
        return self.read(0, min(length, self.size))

    def close(self) -> None:
        try:
            self.fh.close()
        except OSError:
            pass


# ============================================================
# 工具
# ============================================================

def _dimensions(width: object, height: object) -> str:
    try:
        w, h = int(width or 0), int(height or 0)
    except (TypeError, ValueError):
        return ""
    return f"{w} × {h}" if w > 0 and h > 0 else ""


def _fps_text(fps: float) -> str:
    if not fps or fps <= 0 or fps > 1000:
        return ""
    rounded = round(fps)
    value = rounded if abs(fps - rounded) < 0.05 else round(fps, 2)
    return f"{value:g} fps"


def _clock(value: datetime | None) -> str:
    if value is None:
        return ""
    if not 1970 <= value.year <= 2200:      # 没写入时间的容器会给 1904 或 0
        return ""
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _from_unix_1904(seconds: int) -> str:
    if seconds <= 0:
        return ""
    return _clock(EPOCH_1904 + timedelta(seconds=seconds))


def parse_iso_clock(text: str) -> str:
    """解析 "2026-01-18T06:42:00.000000Z" 这类时间串 → "2026-01-18 06:42:00"。

    容器里的时间按相机时钟原样保留（不做时区换算），与用户手填的格式保持一致。
    """
    raw = str(text or "").strip()
    if not raw:
        return ""
    cleaned = raw.replace("Z", "+00:00")
    try:
        return _clock(datetime.fromisoformat(cleaned).replace(tzinfo=None))
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return _clock(datetime.strptime(raw[: len(fmt) + 4], fmt))
        except ValueError:
            continue
    return ""


# ============================================================
# ISO-BMFF：MP4 / MOV / M4V / 3GP
# ============================================================

def _box_at(reader: _Reader, offset: int, end: int) -> tuple[bytes, int, int] | None:
    """读一个 box 头，返回 (类型, 内容起点, 内容长度)。"""
    head = reader.read(offset, 16)
    if len(head) < 8:
        return None
    size = struct.unpack_from(">I", head, 0)[0]
    kind = head[4:8]
    header = 8
    if size == 1:                                    # 64 位长度
        if len(head) < 16:
            return None
        size = struct.unpack_from(">Q", head, 8)[0]
        header = 16
    elif size == 0:                                  # 一直到文件末尾
        size = end - offset
    if size < header or offset + size > end:
        return None
    return kind, offset + header, size - header


def _iter_boxes(reader: _Reader, start: int, end: int):
    offset = start
    while offset + 8 <= end:
        box = _box_at(reader, offset, end)
        if box is None:
            return
        yield box
        offset += (box[1] - offset) + box[2]          # 头上长度 + 内容长度


def _u32(reader: _Reader, offset: int) -> int:
    raw = reader.read(offset, 4)
    return struct.unpack(">I", raw)[0] if len(raw) == 4 else 0


def _u64(reader: _Reader, offset: int) -> int:
    raw = reader.read(offset, 8)
    return struct.unpack(">Q", raw)[0] if len(raw) == 8 else 0


def _matrix_swaps(width: int, height: int, a: float, d: float) -> bool:
    """tkhd 的显示矩阵若是 90°/270°，画面宽高要交换（手机竖屏视频）。"""
    return abs(a) < 0.01 and abs(d) < 0.01 and width != height


def _read_tkhd(reader: _Reader, body: int, track: dict) -> None:
    """body 是 box 内容起点：前 4 字节为 version/flags，其后才是字段。"""
    if reader.read(body, 1) == b"\x01":
        return                                        # v1 字段偏移不同：交给 stsd 的编码尺寸兜底
    raw = reader.read(body + 4 + 36, 44)              # matrix(36) + width(4) + height(4)
    if len(raw) < 44:
        return
    a = struct.unpack_from(">i", raw, 0)[0] / 65536   # 显示矩阵 a / d
    d = struct.unpack_from(">i", raw, 16)[0] / 65536
    width = round(struct.unpack_from(">I", raw, 36)[0] / 65536)
    height = round(struct.unpack_from(">I", raw, 40)[0] / 65536)
    if width and height:
        track["tkhd"] = (width, height, _matrix_swaps(width, height, a, d))


def _read_mvhd(reader: _Reader, body: int, out: dict) -> None:
    version = reader.read(body, 1)
    if version == b"\x01":
        created = _u64(reader, body + 4)
        timescale = _u32(reader, body + 20)
        duration = _u64(reader, body + 24)
    else:
        created = _u32(reader, body + 4)
        timescale = _u32(reader, body + 12)
        duration = _u32(reader, body + 16)
    if timescale:
        out["duration"] = round(duration / timescale)
    if not out.get("createdAt"):
        out["createdAt"] = _from_unix_1904(created)


def _read_stsd(reader: _Reader, body: int, end: int, track: dict) -> None:
    count = _u32(reader, body + 4)
    if count < 1:
        return
    first = _box_at(reader, body + 8, end)
    if first is None:
        return
    kind, entry, _size = first
    track["codec"] = friendly_codec(kind)
    raw = reader.read(entry + 24, 4)                 # VisualSampleEntry: width, height
    if len(raw) == 4:
        width, height = struct.unpack(">HH", raw)
        if width and height:
            track["coded"] = (width, height)


def _read_stsz(reader: _Reader, body: int, track: dict) -> None:
    sample_count = _u32(reader, body + 8)
    if sample_count:
        track["samples"] = sample_count


def _scan_stbl(reader: _Reader, start: int, end: int, track: dict) -> None:
    for kind, body, _size in _iter_boxes(reader, start, end):
        if kind == b"stsd":
            _read_stsd(reader, body, end, track)
        elif kind == b"stsz":
            _read_stsz(reader, body, track)


def _scan_minf(reader: _Reader, start: int, end: int, track: dict) -> None:
    for kind, body, size in _iter_boxes(reader, start, end):
        if kind == b"stbl":
            _scan_stbl(reader, body, body + size, track)


def _scan_mdia(reader: _Reader, start: int, end: int, track: dict) -> None:
    for kind, body, size in _iter_boxes(reader, start, end):
        if kind == b"mdhd":
            version = reader.read(body, 1)
            if version == b"\x01":
                timescale = _u32(reader, body + 20)
                duration = _u64(reader, body + 24)
            else:
                timescale = _u32(reader, body + 12)
                duration = _u32(reader, body + 16)
            if timescale:
                track["media"] = (timescale, duration)
        elif kind == b"hdlr":
            # hdlr 是 FullBox：version/flags(4) + pre_defined(4) + handler_type(4)
            track["handler"] = reader.read(body + 8, 4)
        elif kind == b"minf":
            _scan_minf(reader, body, body + size, track)


def _read_keys(reader: _Reader, body: int, end: int) -> list[str]:
    count = _u32(reader, body + 4)
    names: list[str] = []
    offset = body + 8
    for _ in range(min(count, 64)):
        size = _u32(reader, offset)
        if size < 8 or offset + size > end:
            break
        names.append(reader.read(offset + 8, size - 8).decode("utf-8", "replace").strip("\x00"))
        offset += size
    return names


def _scan_ilst(reader: _Reader, start: int, end: int, keys: list[str], out: dict) -> None:
    for kind, body, size in _iter_boxes(reader, start, end):
        try:
            index = int.from_bytes(kind, "big") - 1
        except ValueError:
            continue
        name = keys[index] if 0 <= index < len(keys) else ""
        value = _read_data_box(reader, body, body + size)
        if not name or not value:
            continue
        if name == "com.apple.quicktime.model":
            out["device"] = value
        elif name == "com.apple.quicktime.make" and not out.get("device"):
            out["device"] = value
        elif name in ("com.apple.quicktime.creationdate",) and not out.get("createdAt"):
            out["createdAt"] = parse_iso_clock(value)


def _read_data_box(reader: _Reader, start: int, end: int) -> str:
    for kind, body, size in _iter_boxes(reader, start, end):
        if kind == b"data":
            payload = reader.read(body + 8, min(size - 8, 512))
            return payload.decode("utf-8", "replace").strip("\x00").strip()
    return ""


def _scan_meta(reader: _Reader, start: int, end: int, out: dict) -> None:
    keys: list[str] = []
    for kind, body, size in _iter_boxes(reader, start, end):
        if kind == b"keys":
            keys = _read_keys(reader, body, body + size)
        elif kind == b"ilst":
            _scan_ilst(reader, body, body + size, keys, out)


def _scan_udta(reader: _Reader, start: int, end: int, out: dict) -> None:
    for kind, body, size in _iter_boxes(reader, start, end):
        if kind == b"meta":
            _scan_meta(reader, body + 4, body + size, out)   # meta 是 FullBox
        elif kind == b"\xa9mod":
            out.setdefault("device", reader.read(body + 4, 128).decode("utf-8", "replace").strip("\x00").strip())
        elif kind == b"\xa9mak":
            out.setdefault("make", reader.read(body + 4, 128).decode("utf-8", "replace").strip("\x00").strip())
        elif kind == b"\xa9day":
            out.setdefault("createdAt", parse_iso_clock(reader.read(body + 4, 128).decode("utf-8", "replace")))


def _scan_trak(reader: _Reader, start: int, end: int, out: dict) -> None:
    if out.get("_video_done"):
        return
    track: dict = {}
    for kind, body, size in _iter_boxes(reader, start, end):
        if kind == b"tkhd":
            _read_tkhd(reader, body, track)
        elif kind == b"mdia":
            _scan_mdia(reader, body, body + size, track)

    if track.get("handler") != b"vide":        # 音频/字幕轨不算（否则 fps 会算错）
        return

    coded = track.get("coded") or track.get("tkhd", (0, 0))[:2]
    if coded and coded[0] and coded[1]:
        width, height = coded
        if track.get("tkhd") and track["tkhd"][2]:
            width, height = height, width
        out["resolution"] = _dimensions(width, height)

    timescale, duration = track.get("media", (0, 0))
    if timescale and duration and track.get("samples"):
        out["fps"] = _fps_text(track["samples"] / (duration / timescale))
    if track.get("codec"):
        out["codec"] = track["codec"]
    if not out.get("duration") and timescale and duration:
        out["duration"] = round(duration / timescale)
    out["_video_done"] = True


def _probe_isobmff(reader: _Reader) -> dict:
    out: dict[str, object] = empty_info()
    out["_video_done"] = False
    for kind, body, size in _iter_boxes(reader, 0, reader.size):
        if kind == b"moov":
            for child, cbody, csize in _iter_boxes(reader, body, body + size):
                if child == b"mvhd":
                    _read_mvhd(reader, cbody, out)
                elif child == b"trak":
                    _scan_trak(reader, cbody, cbody + csize, out)
                elif child == b"udta":
                    _scan_udta(reader, cbody, cbody + csize, out)
    if out.get("make") and not out.get("device"):
        out["device"] = out["make"]
    out.pop("_video_done", None)
    out.pop("make", None)
    return out


# ============================================================
# Matroska / WebM（EBML）
# ============================================================


def _ebml_vint(data: bytes, offset: int, *, keep_marker: bool) -> tuple[int, int]:
    """读一个 EBML 变长整数，返回 (值, 占用字节数)；读不出来返回 (-1, 0)。"""
    if offset >= len(data):
        return -1, 0
    first = data[offset]
    if first == 0:
        return -1, 0
    length = 1
    mask = 0x80
    while not first & mask:
        mask >>= 1
        length += 1
        if length > 8:
            return -1, 0
    if offset + length > len(data):
        return -1, 0
    raw = int.from_bytes(data[offset:offset + length], "big")
    if not keep_marker:
        raw &= (1 << (7 * length)) - 1
    return raw, length


def _ebml_uint(data: bytes) -> int:
    return int.from_bytes(data, "big") if data else 0


def _ebml_float(data: bytes) -> float:
    if len(data) == 4:
        return struct.unpack(">f", data)[0]
    if len(data) == 8:
        return struct.unpack(">d", data)[0]
    return 0.0


def _ebml_elements(data: bytes, start: int, end: int):
    """迭代同一层级的 EBML 元素，yield (id, 内容起点, 内容终点)。"""
    offset = start
    while offset < end:
        elem_id, id_len = _ebml_vint(data, offset, keep_marker=True)
        if elem_id < 0:
            return
        size, size_len = _ebml_vint(data, offset + id_len, keep_marker=False)
        if size < 0:
            return
        body = offset + id_len + size_len
        limit = min(body + size, end) if size else end
        if limit <= offset:                          # 防御：长度异常时前进一格
            return
        yield elem_id, body, limit
        offset = limit


def _read_matroska_track(data: bytes, start: int, end: int) -> dict:
    track: dict = {}
    for elem_id, body, limit in _ebml_elements(data, start, end):
        if elem_id == 0x83:                          # TrackType：1 = 视频
            track["type"] = _ebml_uint(data[body:limit])
        elif elem_id == 0x86:                        # CodecID
            track["codec"] = friendly_codec(data[body:limit].decode("ascii", "ignore"))
        elif elem_id == 0x23E383:                    # DefaultDuration（纳秒/帧）
            track["frame_ns"] = _ebml_uint(data[body:limit])
        elif elem_id == 0xE0:                        # Video
            for leaf, lbody, llimit in _ebml_elements(data, body, limit):
                if leaf == 0xB0:
                    track["width"] = _ebml_uint(data[lbody:llimit])
                elif leaf == 0xBA:
                    track["height"] = _ebml_uint(data[lbody:llimit])
    return track


def _probe_matroska(reader: _Reader) -> dict:
    out = empty_info()
    data = reader.prefix(EBML_PREFIX)
    scale = 1_000_000
    duration = 0.0
    tracks: list[dict] = []

    for elem_id, body, limit in _ebml_elements(data, 0, len(data)):
        if elem_id != 0x18538067:                    # Segment
            continue
        for sub, sbody, slim in _ebml_elements(data, body, limit):
            if sub == 0x1549A966:                    # Info
                for leaf, lbody, llimit in _ebml_elements(data, sbody, slim):
                    if leaf == 0x2AD7B1:
                        scale = _ebml_uint(data[lbody:llimit]) or scale
                    elif leaf == 0x4489:
                        duration = _ebml_float(data[lbody:llimit])
            elif sub == 0x1654AE6B:                  # Tracks
                for entry, ebody, elimit in _ebml_elements(data, sbody, slim):
                    if entry == 0xAE:
                        tracks.append(_read_matroska_track(data, ebody, elimit))

    video = next((t for t in tracks if t.get("type") == 1), tracks[0] if tracks else None)
    if not video:
        return out
    if duration:
        out["duration"] = round(duration * scale / 1e9)
    if video.get("frame_ns"):
        out["fps"] = _fps_text(1e9 / video["frame_ns"])
    out["resolution"] = _dimensions(video.get("width"), video.get("height"))
    out["codec"] = video.get("codec") or ""
    return out


# ============================================================
# AVI（RIFF）
# ============================================================

def _riff_children(data: bytes, start: int, end: int):
    offset = start
    while offset + 8 <= end:
        kind = data[offset:offset + 4]
        size = int.from_bytes(data[offset + 4:offset + 8], "little")
        body = offset + 8
        limit = min(body + size, end)
        yield kind, body, limit
        offset = body + size + (size & 1)


def _riff_leaves(data: bytes, start: int, end: int, depth: int = 0):
    """展开 RIFF 树，yield (块类型, 内容起点, 内容终点)（LIST 容器递归展开）。"""
    if depth > 6:
        return
    for kind, body, limit in _riff_children(data, start, end):
        if kind == b"LIST":
            yield from _riff_leaves(data, body + 4, limit, depth + 1)
        else:
            yield kind, body, limit


def _probe_avi(reader: _Reader) -> dict:
    out: dict[str, object] = empty_info()
    data = reader.prefix(AVI_MAX_HEADER)
    frames = micros_per_frame = rate = scale = 0
    is_video = False
    for kind, body, limit in _riff_leaves(data, 12, len(data)):
        if kind == b"avih":
            micros_per_frame = int.from_bytes(data[body:body + 4], "little")
            frames = int.from_bytes(data[body + 16:body + 20], "little")
        elif kind == b"strh":
            is_video = data[body:body + 4] == b"vids"     # 音频流不参与
            if is_video:
                out["codec"] = friendly_codec(data[body + 4:body + 8])
                scale = int.from_bytes(data[body + 20:body + 24], "little")
                rate = int.from_bytes(data[body + 24:body + 28], "little")
        elif kind == b"strf" and is_video and not out.get("resolution"):
            width = int.from_bytes(data[body + 4:body + 8], "little", signed=True)
            height = int.from_bytes(data[body + 8:body + 12], "little", signed=True)
            out["resolution"] = _dimensions(abs(width), abs(height))

    fps = (rate / scale) if scale else (1_000_000 / micros_per_frame if micros_per_frame else 0)
    if fps:
        out["fps"] = _fps_text(fps)
        if frames:
            out["duration"] = round(frames / fps)
    return out


# ============================================================
# 对外入口
# ============================================================

ISO_BMFF_BOXES = {b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide", b"pnot"}


def detect_container(path: Path) -> str:
    """按文件头判断容器类型：isobmff / matroska / avi / unknown。"""
    try:
        with path.open("rb") as fh:
            head = fh.read(16)
    except OSError:
        return "unknown"
    if len(head) >= 12 and head[4:8] in ISO_BMFF_BOXES:
        return "isobmff"
    if head[:4] == b"\x1aE\xdf\xa3":
        return "matroska"
    if head[:4] == b"RIFF" and head[8:12] in (b"AVI ", b"AVIX"):
        return "avi"
    return "unknown"


def probe(path: str | Path) -> dict[str, object]:
    """解析视频元数据；任何异常都退化成空字段（不影响上传流程）。"""
    info = empty_info()
    file_path = Path(path)
    if not file_path.is_file():
        return info
    container = detect_container(file_path)
    if container == "unknown":
        return info

    reader: _Reader | None = None
    try:
        reader = _Reader(file_path)
        if container == "isobmff":
            parsed = _probe_isobmff(reader)
        elif container == "matroska":
            parsed = _probe_matroska(reader)
        else:
            parsed = _probe_avi(reader)
    except (OSError, struct.error, ValueError, IndexError, KeyError):
        return info
    finally:
        if reader is not None:
            reader.close()

    for key in FLAT_FIELDS:
        value = parsed.get(key)
        if value:
            info[key] = value
    return info
