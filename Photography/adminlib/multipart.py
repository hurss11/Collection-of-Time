# -*- coding: utf-8 -*-
"""流式 multipart/form-data 解析（标准库，只用 email 解析 part 头部）。

为什么不用现成的解析方式
------------------------
`email.parser.BytesParser` 需要先把**整个请求体**读进内存，再在消息树里给每个
part 复制一份 payload；`cgi.FieldStorage` 在 Python 3.13 已被移除，而且同样偏内存。
实测（1.6GB 的机器，未改动前的代码）：一次 300MB 的上传会把服务进程 RSS 顶到
327MB——峰值与请求体大小 1:1。而 `MAX_BODY_BYTES` 允许到 520MB，比整机内存还大，
两个并发请求就足以触发内核 OOM（线上确实发生过：anon-rss ≈ 870MB）。

这里的做法是「边读边写」：文件 part 直接流进临时文件，普通字段留在内存里但设上限，
因此**峰值内存与文件大小无关**（只与 chunk 大小、字段长度有关）。part 的头部仍然交给
`email.parser.BytesHeaderParser`，这样 `filename="中文名.jpg"`（RFC 2231 / RFC 2047
编码）之类的细节与标准库保持完全一致。

调用方拿到的是「临时文件路径 + 大小」而不是字节串；临时文件由调用方 move 到最终
位置，并在请求结束时统一清理。
"""
from __future__ import annotations

import email.parser
import email.policy
import os
import time
from pathlib import Path

CHUNK_SIZE = 256 * 1024           # 每次从 socket 读的字节数，也是峰值里最大的一块
MAX_FIELD_BYTES = 256 * 1024      # 普通表单字段（album / tags / description…）上限
MAX_HEADER_BYTES = 32 * 1024      # 单个 part 的头部上限

_HEADER_PARSER = email.parser.BytesHeaderParser(policy=email.policy.default)
_TEMP_SEQ = 0


class MultipartError(Exception):
    """请求体不是合法的 multipart，或超出了调用方给的上限。"""


class LimitedReader:
    """按 Content-Length 限长读取，并统计已读字节。

    限长很重要：http.server 在同一条 keep-alive 连接上复用同一个 handler，
    多读一个字节就会把下一个请求的开头吃掉（表现为 `Unsupported method` 之类的怪错）。
    """

    def __init__(self, raw, limit: int) -> None:                 # noqa: ANN001
        self.raw = raw
        self.remaining = max(0, int(limit))
        self.consumed = 0

    def read(self, size: int = CHUNK_SIZE) -> bytes:
        if self.remaining <= 0:
            return b""
        data = self.raw.read(min(size, self.remaining))
        if not data:
            self.remaining = 0
            return b""
        self.remaining -= len(data)
        self.consumed += len(data)
        return data


def boundary_from_content_type(content_type: str) -> str:
    """从 Content-Type 里取 boundary（交给 email 处理引号与转义）。"""
    message = _HEADER_PARSER.parsebytes(
        f"Content-Type: {content_type}\r\n\r\n".encode("utf-8", "replace")
    )
    boundary = message.get_boundary()
    if not boundary:
        raise MultipartError("multipart/form-data 缺少 boundary")
    if len(boundary) > 200:
        raise MultipartError("boundary 过长")
    return boundary


def new_temp_path(temp_dir: Path) -> Path:
    global _TEMP_SEQ
    _TEMP_SEQ += 1
    return Path(temp_dir) / f"part-{os.getpid()}-{time.monotonic_ns()}-{_TEMP_SEQ}.tmp"


class _FileSink:
    """文件 part 的落盘目标：写临时文件，超限立刻报错。"""

    def __init__(self, path: Path, max_bytes: int) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self.size = 0
        self.handle = path.open("wb")

    def write(self, data: bytes) -> None:
        self.size += len(data)
        if self.size > self.max_bytes:
            raise MultipartError(f"单个文件超过上限 {self.max_bytes // 1048576}MB")
        self.handle.write(data)

    def close(self) -> None:
        self.handle.close()

    def discard(self) -> None:
        try:
            self.handle.close()
        except OSError:
            pass
        try:
            self.path.unlink()
        except OSError:
            pass


class _FieldSink:
    """普通字段（非文件）的目标：留在内存里，但设上限。"""

    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        self.buffer = bytearray()

    def write(self, data: bytes) -> None:
        self.buffer += data
        if len(self.buffer) > self.max_bytes:
            raise MultipartError(f"表单字段过大（上限 {self.max_bytes // 1024}KB）")

    def close(self) -> None:
        pass

    def discard(self) -> None:
        self.buffer = bytearray()


class _NullSink:
    """没有 name 的 part：读完即丢。"""

    def write(self, data: bytes) -> None:
        pass

    def close(self) -> None:
        pass

    def discard(self) -> None:
        pass


def _more(reader: LimitedReader, buf: bytes) -> tuple[bytes, bool]:
    chunk = reader.read()
    if not chunk:
        return buf, False
    return buf + chunk, True


def _skip_padding_and_eol(reader: LimitedReader, buf: bytes) -> tuple[bytes, str]:
    """分隔符之后：可选 transport-padding，然后是 CRLF（还有下一个 part）或 --（结束）。"""
    while True:
        buf = buf.lstrip(b" \t")
        if buf.startswith(b"--"):
            return buf[2:], "end"
        if buf.startswith(b"\r\n"):
            return buf[2:], "part"
        if buf.startswith(b"\n"):                 # 容错：只发 LF 的客户端
            return buf[1:], "part"
        buf, ok = _more(reader, buf)
        if not ok:
            raise MultipartError("multipart 分隔符后缺少 CRLF")


def parse(reader: LimitedReader, boundary: str, *, temp_dir: Path,
          max_file_bytes: int, max_total_bytes: int | None = None,
          max_field_bytes: int = MAX_FIELD_BYTES
          ) -> tuple[dict[str, str], dict[str, list[dict]]]:
    """把 multipart 请求体解析成 (fields, files)。

    fields: 普通字段 → 字符串
    files:  文件字段名 → list，每项 {"filename", "content_type", "path", "size"}
    临时文件建在 temp_dir（不同 part 各自一个文件）。
    """
    marker = b"--" + boundary.encode("utf-8", "surrogateescape")
    delimiter = b"\r\n" + marker
    keep = len(delimiter) - 1
    temp_dir = Path(temp_dir)
    if not temp_dir.is_dir():
        temp_dir.mkdir(parents=True, exist_ok=True)
        try:
            temp_dir.chmod(0o700)         # 只给服务自己看
        except OSError:
            pass

    fields: dict[str, str] = {}
    files: dict[str, list[dict]] = {}
    sinks: list[object] = []
    total_file_bytes = 0

    def cleanup() -> None:
        for item in sinks:
            item.discard()

    # ---- 跳过 preamble，定位第一个分隔符 ----
    buf = b""
    while True:
        pos = buf.find(marker)
        if pos >= 0:
            buf = buf[pos + len(marker):]
            break
        # 没找到就只留「可能是半个分隔符」的尾巴，再读下一块
        buf = buf[-(len(marker) - 1):] if len(buf) >= len(marker) else buf
        buf, ok = _more(reader, buf)
        if not ok:
            raise MultipartError("请求体里没有找到 multipart 分隔符")

    try:
        # ---- 逐个 part ----
        while True:
            buf, kind = _skip_padding_and_eol(reader, buf)
            if kind == "end":
                break

            # 头部：读到空行为止
            raw_headers = b""
            while True:
                newline = buf.find(b"\n")
                if newline < 0:
                    buf, ok = _more(reader, buf)
                    if not ok:
                        raise MultipartError("multipart 请求体不完整（part 头部被截断）")
                    if len(buf) > MAX_HEADER_BYTES:
                        raise MultipartError("part 头部过大")
                    continue
                line, buf = buf[:newline + 1], buf[newline + 1:]
                raw_headers += line
                if len(raw_headers) > MAX_HEADER_BYTES:
                    raise MultipartError("part 头部过大")
                if line in (b"\r\n", b"\n"):
                    break

            head = _HEADER_PARSER.parsebytes(raw_headers)
            name = head.get_param("name", header="content-disposition")
            filename = head.get_filename()

            if not name:
                sink: object = _NullSink()
            elif filename:
                sink = _FileSink(new_temp_path(temp_dir), max_file_bytes)
            else:
                sink = _FieldSink(max_field_bytes)
            sinks.append(sink)

            # 正文：流式写进 sink，直到真正的分隔符
            while True:
                pos = buf.find(delimiter)
                if pos >= 0:
                    after = buf[pos + len(delimiter):]
                    # 必须看清分隔符后面是 CRLF 还是 --，数据不够就再读一点
                    while len(after.lstrip(b" \t")) < 2 and reader.remaining > 0:
                        extra = reader.read()
                        if not extra:
                            break
                        after += extra
                    probe = after.lstrip(b" \t")
                    if probe.startswith((b"--", b"\r\n", b"\n")):
                        sink.write(buf[:pos])
                        buf = after
                        break
                    # 正文里恰好出现 "\r\n--boundary" 但不是真分隔符：当作正文
                    sink.write(buf[:pos + len(delimiter)])
                    buf = after
                    continue
                if len(buf) > keep:
                    sink.write(buf[:-keep])
                    buf = buf[-keep:]
                buf, ok = _more(reader, buf)
                if not ok:
                    raise MultipartError("multipart 请求体不完整（缺少结束分隔符）")

            sink.close()
            if isinstance(sink, _FileSink):
                total_file_bytes += sink.size
                if max_total_bytes is not None and total_file_bytes > max_total_bytes:
                    raise MultipartError(
                        f"单批总大小超过上限 {max_total_bytes // 1048576}MB"
                    )
                files.setdefault(name, []).append({
                    "filename": filename,
                    "content_type": head.get_content_type(),
                    "path": sink.path,
                    "size": sink.size,
                })
            elif isinstance(sink, _FieldSink):
                fields[name] = sink.buffer.decode("utf-8", "replace")
    except MultipartError:
        cleanup()
        raise

    return fields, files
