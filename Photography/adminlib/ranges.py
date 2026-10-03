# -*- coding: utf-8 -*-
"""HTTP Range（单段）支持 —— `<video>` 的进度条与拖拽靠它。

为什么必须有：媒体元素要显示**可拖动的进度条**，得先知道时长与「可寻址范围」。
浏览器为此会发 `Range: bytes=0-` 之类的请求，只有服务端回 `206 Partial Content`
（带 `Content-Range` 与 `Accept-Ranges`）时它才认为「这个资源可以按需取片段」。
一律回 200 整份时，很多文件（moov 在末尾的 mp4、webm、以及大文件）拿不到时长，
表现就是**播放器没有进度条、不能拖到中间**，而且每次都要重下整份。

只实现最常用的一段：

    bytes=1000-1999   闭区间
    bytes=1000-       到文件末尾
    bytes=-1000       最后 1000 字节

多段 Range（`bytes=0-99,200-299`）极少见，且 RFC 9110 允许服务器忽略 Range，
这里按「整份 200」处理；越界 / 语法错误同样回整份（不回 416），避免兼容性坑。
"""
from __future__ import annotations

import mimetypes
from pathlib import Path

CHUNK = 64 * 1024


class PartialReader:
    """包一层文件对象，让 `shutil.copyfileobj` 只读到区间末尾。

    stdlib 的 `do_GET` 拿到什么就读到 EOF，所以「只发一段」得靠这个包装。
    """

    def __init__(self, handle, length: int) -> None:
        self.handle = handle
        self.remaining = max(0, int(length))

    def read(self, size: int = -1) -> bytes:
        if self.remaining <= 0:
            return b""
        want = self.remaining if size is None or size < 0 else min(size, self.remaining)
        data = self.handle.read(want)
        if not data:
            self.remaining = 0
            return b""
        self.remaining -= len(data)
        return data

    def close(self) -> None:
        self.handle.close()


def parse_single_range(header: str, size: int) -> tuple[int, int] | None:
    """解析单段 Range；返回 (start, end) 闭区间，不适用时返回 None（按整份发）。"""
    header = (header or "").strip()
    if not header.lower().startswith("bytes=") or size <= 0:
        return None

    spec = header[len("bytes="):].split(",")[0].strip()
    if "-" not in spec:
        return None
    start_text, _, end_text = spec.partition("-")

    try:
        if not start_text:
            tail = int(end_text)
            if tail <= 0:
                return None
            start = max(0, size - tail)
            end = size - 1
        else:
            start = int(start_text)
            end = int(end_text) if end_text.strip() else size - 1
    except ValueError:
        return None

    if start < 0 or start >= size or end < start:
        return None
    return start, min(end, size - 1)


def content_type_of(path: Path) -> str:
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
        ctype += "; charset=utf-8"
    return ctype


def send_partial(handler, path: Path, start: int, end: int):    # noqa: ANN001, ANN201
    """回 206 + `Content-Range`，返回只含该区间的可读对象（HEAD 时返回空体）。"""
    stat = path.stat()
    length = end - start + 1
    handler.send_response(206)
    handler.send_header("Content-Type", content_type_of(path))
    handler.send_header("Content-Length", str(length))
    handler.send_header("Content-Range", f"bytes {start}-{end}/{stat.st_size}")
    handler.send_header("Last-Modified", handler.date_time_string(int(stat.st_mtime)))
    handler.end_headers()          # 这里会补上 Accept-Ranges / 安全响应头

    handle = path.open("rb")
    handle.seek(start)
    # HEAD 只回头：包成 0 字节，交给 copyfile 时自然什么都不写
    body_length = 0 if getattr(handler, "command", "GET") == "HEAD" else length
    return PartialReader(handle, body_length)
