# -*- coding: utf-8 -*-
"""ffmpeg / ffprobe 的可选封装。

后台的所有媒体处理（视频封面、图片缩略图、时长与分辨率探测）都依赖 FFmpeg，
但 FFmpeg 不是运行后台的必需条件：
   - 未安装时，上传仍可正常工作，只是跳过封面 / 缩略图生成并返回 warning。
   - 这样保证在最小化的服务器环境里后台不会因为缺依赖而不可用。

FFmpeg 的查找顺序（前者优先）：
   1. 环境变量 COT_FFMPEG / COT_FFPROBE（显式指定，优先级最高）
   2. 项目内的 bin/ 目录（推荐！把静态构建塞进 bin/ 即可随项目一起打包到服务器）
   3. 系统 PATH（apt / brew / winget 安装的版本）

把二进制放到 bin/ 里是「打包到服务器」最省事的方式：
   python tools/fetch_ffmpeg.py          # 自动下载对应平台的静态构建到 bin/
   ./run.sh install-ffmpeg               # 等价的一键命令
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

TIMEOUT_PROBE = 20
TIMEOUT_TRANSCODE = 300
TIMEOUT_VERSION = 10

VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".tif", ".tiff"}

# 来源标记 → 展示名
SOURCE_LABELS = {
    "env": "环境变量指定",
    "local": "项目内置 bin/",
    "system": "系统 PATH",
    "missing": "未找到",
}
# 数值越小优先级越高
SOURCE_RANK = {"env": 0, "local": 1, "system": 2, "missing": 9}


@dataclass(frozen=True)
class Tools:
    """FFmpeg 工具链的可用状态与来源。"""

    ffmpeg: str | None = None
    ffprobe: str | None = None
    source: str = "missing"
    version: str = ""

    @property
    def can_transcode(self) -> bool:
        return self.ffmpeg is not None

    @property
    def can_probe(self) -> bool:
        return self.ffprobe is not None

    @property
    def available(self) -> bool:
        return self.ffmpeg is not None or self.ffprobe is not None

    @property
    def source_label(self) -> str:
        return SOURCE_LABELS.get(self.source, self.source)

    def as_dict(self) -> dict[str, object]:
        return {
            "ffmpeg": self.ffmpeg,
            "ffprobe": self.ffprobe,
            "canTranscode": self.can_transcode,
            "canProbe": self.can_probe,
            "source": self.source,
            "sourceLabel": self.source_label,
            "version": self.version,
        }


# ============================================================
# 探测
# ============================================================

def _binary_name(name: str) -> str:
    """按平台补上可执行文件后缀。"""
    return f"{name}.exe" if os.name == "nt" else name


def _make_executable(path: Path) -> None:
    """确保内置二进制有可执行权限（从压缩包解出来常常没有）。"""
    if os.name == "nt":
        return
    try:
        current = path.stat().st_mode
        if not current & stat.S_IXUSR:
            path.chmod(current | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass


def find_binary(name: str, root: Path | None = None) -> tuple[str | None, str]:
    """按优先级查找单个可执行文件，返回 (路径, 来源)。"""
    exe = _binary_name(name)

    # 1) 环境变量
    override = os.environ.get(f"COT_{name.upper()}")
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_file():
            _make_executable(candidate)
            return str(candidate), "env"

    # 2) 项目内的 bin/（同时支持 bin/ffmpeg 与 bin/<platform>/ffmpeg）
    if root is not None:
        for relative in (Path("bin") / exe, Path("bin") / name):
            candidate = Path(root) / relative
            if candidate.is_file():
                _make_executable(candidate)
                return str(candidate), "local"

    # 3) 系统 PATH
    located = shutil.which(name)
    if located:
        return located, "system"

    return None, "missing"


def detect_tools(root: Path | None = None, *, verify: bool = False) -> Tools:
    """探测 ffmpeg / ffprobe。

    @param verify 额外执行一次 `-version`，用于确认二进制真能跑起来
                  （例如 bin/ 里放错架构的文件时能及时发现）。
    """
    ffmpeg, ffmpeg_source = find_binary("ffmpeg", root)
    ffprobe, ffprobe_source = find_binary("ffprobe", root)

    sources = [s for s, path in ((ffmpeg_source, ffmpeg), (ffprobe_source, ffprobe)) if path]
    source = min(sources, key=lambda s: SOURCE_RANK.get(s, 9)) if sources else "missing"

    version = ""
    if verify and ffmpeg:
        ok, output = ffmpeg_version(ffmpeg)
        if not ok:
            # 能定位到但跑不起来，视为不可用，避免上传时才报错
            return Tools(None, None, "missing", "")
        version = output

    return Tools(ffmpeg, ffprobe, source, version)


def ffmpeg_version(ffmpeg: str) -> tuple[bool, str]:
    """读取版本号首行，顺带验证二进制是否真的可执行。"""
    ok, output = _run([ffmpeg, "-version"], TIMEOUT_VERSION)
    if not ok:
        return False, output
    return True, output.splitlines()[0].strip() if output else ""


def _run(cmd: list[str], timeout: int) -> tuple[bool, str]:
    """执行外部命令，返回 (是否成功, stdout 或错误信息)。"""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)

    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "").strip()[-500:]
    return True, proc.stdout or ""


def probe_media(ffprobe: str, path: str | Path) -> dict[str, object]:
    """读取媒体信息：时长、分辨率、帧率、编码。

    返回的字段缺失时为空，不抛异常（探测失败不应阻断上传流程）。
    """
    info: dict[str, object] = {"duration": None, "resolution": "", "fps": "", "codec": ""}

    ok, out = _run(
        [
            ffprobe, "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
        ],
        TIMEOUT_PROBE,
    )
    if not ok:
        return info

    try:
        payload = json.loads(out)
    except json.JSONDecodeError:
        return info

    try:
        duration = float(payload.get("format", {}).get("duration") or 0)
        if duration > 0:
            info["duration"] = round(duration)
    except (TypeError, ValueError):
        pass

    stream = next(
        (s for s in payload.get("streams", []) if s.get("codec_type") == "video"), None
    )
    if stream:
        width, height = stream.get("width"), stream.get("height")
        if width and height:
            info["resolution"] = f"{width} × {height}"

        # 兼容 "30000/1001" 这种分式帧率
        rate = stream.get("avg_frame_rate") or stream.get("r_frame_rate") or ""
        if isinstance(rate, str) and "/" in rate:
            try:
                num, den = rate.split("/", 1)
                fps = float(num) / float(den) if float(den) else 0
                if fps > 0:
                    info["fps"] = f"{round(fps):g} fps"
            except (TypeError, ValueError, ZeroDivisionError):
                pass

        codec = stream.get("codec_name")
        if codec:
            info["codec"] = str(codec).upper()

    return info


def extract_frame(
    ffmpeg: str,
    video: str | Path,
    target: str | Path,
    *,
    at: float = 2.0,
    width: int = 1280,
) -> tuple[bool, str]:
    """从视频中抽一帧保存为 JPEG，用作封面。"""
    target_path = Path(target)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    return _run(
        [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{max(0.0, at):.3f}",   # 放在 -i 前，快速定位
            "-i", str(video),
            "-frames:v", "1",
            "-vf", f"scale={width}:-2",
            "-q:v", "3",
            str(target_path),
        ],
        TIMEOUT_TRANSCODE,
    )


def make_thumbnail(
    ffmpeg: str,
    image: str | Path,
    target: str | Path,
    *,
    width: int = 800,
) -> tuple[bool, str]:
    """用 ffmpeg 生成缩略图（避免为此引入 Pillow 依赖）。"""
    target_path = Path(target)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    return _run(
        [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(image),
            "-vf", f"scale='min({width},iw)':-2",
            "-q:v", "4",
            str(target_path),
        ],
        TIMEOUT_TRANSCODE,
    )
