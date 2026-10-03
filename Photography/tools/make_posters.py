# -*- coding: utf-8 -*-
"""批量生成视频封面（poster）。

抓帧需要 ffmpeg；时长 / 分辨率等元数据优先用 ffprobe，
没有 ffprobe 时自动退回纯标准库的容器解析（adminlib.videometa），所以
「只装了 ffmpeg」也能跑出完整的 videos.json 片段。

用法：
    python tools/make_posters.py                     # 扫描 assets/video/ 生成封面
    python tools/make_posters.py --time 5            # 指定抓帧时间点（秒）
    python tools/make_posters.py --width 1600        # 控制封面宽度
    python tools/make_posters.py --force             # 覆盖已存在的封面
    python tools/make_posters.py --json              # 额外输出可粘贴进 videos.json 的片段

生成的封面写入 assets/video/posters/<视频名>.jpg，
在 videos.json 中把 poster 指向该文件即可（比运行时抓帧更快、更稳定）。
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VIDEO_DIR = PROJECT_ROOT / "assets" / "video"
POSTER_DIR = VIDEO_DIR / "posters"

VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}

sys.path.insert(0, str(PROJECT_ROOT))
from adminlib import videometa                     # noqa: E402
from adminlib.media import find_binary             # noqa: E402


def require(tool: str) -> str:
    """确认外部工具可用，否则给出安装指引。

    查找顺序与后端一致：环境变量 COT_<TOOL> → 项目 bin/ → 系统 PATH。
    """
    path, _source = find_binary(tool, PROJECT_ROOT)
    if not path:
        sys.exit(
            f"未找到 {tool}。请先安装 FFmpeg 并确保它在 PATH 中：\n"
            "  Windows: winget install Gyan.FFmpeg\n"
            "  macOS:   brew install ffmpeg\n"
            "  Linux:   sudo apt install ffmpeg"
        )
    return path


def display_path(path: Path) -> str:
    """尽量输出相对项目根目录的路径，不在项目内时回退为绝对路径。"""
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def probe_duration(ffprobe: str, video: Path) -> float | None:
    """读取视频时长（秒）。"""
    try:
        out = subprocess.run(
            [
                ffprobe, "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1", str(video),
            ],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        return float(out)
    except (subprocess.CalledProcessError, ValueError):
        return None


def probe_resolution(ffprobe: str, video: Path) -> str | None:
    """读取视频分辨率，返回 "宽 × 高"。"""
    try:
        out = subprocess.run(
            [
                ffprobe, "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "csv=p=0:s=x", str(video),
            ],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        if "x" in out:
            width, height = out.split("x")[:2]
            return f"{width} × {height}"
    except subprocess.CalledProcessError:
        pass
    return None


def extract_poster(ffmpeg: str, video: Path, target: Path, at: float, width: int) -> bool:
    """从视频中抽一帧保存为 JPEG。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-ss", str(at),           # 放在 -i 之前，快速定位
        "-i", str(video),
        "-frames:v", "1",
        "-vf", f"scale={width}:-2",
        "-q:v", "3",
        str(target),
    ]
    return subprocess.run(cmd, capture_output=True).returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(description="批量生成视频封面")
    parser.add_argument("--time", type=float, default=2.0, help="抓帧时间点，单位秒（默认 2）")
    parser.add_argument("--width", type=int, default=1280, help="封面宽度像素（默认 1280）")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的封面")
    parser.add_argument("--json", action="store_true", help="输出可粘贴进 videos.json 的片段")
    args = parser.parse_args()

    ffmpeg = require("ffmpeg")
    ffprobe, _source = find_binary("ffprobe", PROJECT_ROOT)
    if not ffprobe:
        print("提示：未找到 ffprobe，时长 / 分辨率改用纯 Python 容器解析（结果同样可用）\n")

    if not VIDEO_DIR.is_dir():
        sys.exit(f"目录不存在：{VIDEO_DIR}")

    videos = sorted(
        path for path in VIDEO_DIR.iterdir()
        if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES
    )
    if not videos:
        print(f"在 {VIDEO_DIR} 中没有找到视频文件（支持：{', '.join(sorted(VIDEO_SUFFIXES))}）")
        return 0

    snippets = []
    ok = 0

    for video in videos:
        poster = POSTER_DIR / f"{video.stem}.jpg"
        # ffprobe 优先，缺了就用纯 Python 的容器解析兜底
        local = videometa.probe(video)
        duration = probe_duration(ffprobe, video) if ffprobe else None
        resolution = probe_resolution(ffprobe, video) if ffprobe else None
        if duration is None:
            duration = float(local.get("duration") or 0) or None
        if not resolution:
            resolution = str(local.get("resolution") or "") or None

        # 抓帧时间点不能超过时长
        at = args.time
        if duration is not None and duration > 1:
            at = min(at, max(0.5, duration * 0.1))

        if poster.exists() and not args.force:
            print(f"跳过（已存在）：{poster.name}")
        elif extract_poster(ffmpeg, video, poster, at, args.width):
            print(f"已生成：{display_path(poster)}  （抓帧于 {at:.1f}s）")
            ok += 1
        else:
            print(f"失败：{video.name}（请检查文件是否损坏）")
            continue

        snippets.append({
            "id": f"v-{video.stem}",
            "title": video.stem,
            "album": "uncategorized",
            "provider": "file",
            "src": f"assets/video/{video.name}",
            "poster": f"assets/video/posters/{poster.name}",
            "posterTime": round(at, 1),
            "duration": round(duration) if duration else None,
            "resolution": resolution,
            "exif": {key: value for key, value in {
                "camera": local.get("device"),
                "fps": local.get("fps"),
                "codec": local.get("codec"),
            }.items() if value},
        })

    print(f"\n完成：新增 / 更新 {ok} 个封面，输出目录 {display_path(POSTER_DIR)}")

    if args.json:
        print("\n--- 粘贴到 data/videos.json（记得补全 title / album / date / tags）---")
        print(json.dumps(snippets, ensure_ascii=False, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
