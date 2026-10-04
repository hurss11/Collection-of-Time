# -*- coding: utf-8 -*-
"""把 MP4/MOV 的索引挪到文件开头（faststart）：让浏览器「边下边播」。

背景：MP4 的播放索引（`moov`）可以放在文件开头或末尾。相机 / 剪辑软件默认常常
放在**末尾**，这时浏览器必须先下完整份文件才能起播 —— 一段 128MB 的延时片，
移动网络上就是「点了播放，半天没动静」。把索引挪到开头（`-movflags +faststart`）
不需要重新编码（`-c copy`），几秒钟就能做完。

用法：
    python tools/faststart.py --check            # 只检查，列出需要处理的文件（有问题时退出码 1）
    python tools/faststart.py                   # 逐个就地改造（会先写到临时文件再原子替换）
    python tools/faststart.py --path some/dir   # 换目录（默认 assets/video）
    python tools/faststart.py --force           # 磁盘余量不足时也继续（默认会拒绝）

安全性：只用 `-c copy`（不重新编码，画面与音轨逐字节保留）；先写临时文件、校验新文件
的索引确实在开头且时长没变，再原子替换；任何一步失败都保留原文件。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIR = PROJECT_ROOT / "assets" / "video"

# 只处理这几类：`-c copy -movflags +faststart` 的输出容器是 MP4，
# MKV/WebM 没有 moov（它们的索引结构不同），AVI 也不适用。
ISO_SUFFIXES = {".mp4", ".m4v", ".mov"}
# 这些子目录里放的是原始素材 / 未压缩导出 / 封面，不是对外播放的那份
SKIP_DIRS = {"originals", "raw", "posters", "thumbs"}

sys.path.insert(0, str(PROJECT_ROOT))
from adminlib import videometa                     # noqa: E402
from adminlib.media import find_binary             # noqa: E402

TIMEOUT = 1800


def require_ffmpeg() -> str:
    path, _source = find_binary("ffmpeg", PROJECT_ROOT)
    if not path:
        sys.exit(
            "未找到 ffmpeg。请先安装（或在项目里装静态构建）：\n"
            "  ./run.sh install-ffmpeg\n"
            "  Windows: winget install Gyan.FFmpeg\n"
            "  macOS:   brew install ffmpeg\n"
            "  Linux:   sudo apt install ffmpeg"
        )
    return path


def display(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


def candidates(target: Path) -> list[Path]:
    files = sorted(
        path for path in target.rglob("*")
        if path.is_file()
        and path.suffix.lower() in ISO_SUFFIXES
        and not (SKIP_DIRS & set(path.relative_to(target).parts[:-1]))
        and not path.name.startswith(".")
    )
    return files


def needs_faststart(path: Path) -> bool:
    return videometa.faststart_state(path) == videometa.FASTSTART_SLOW


def convert(ffmpeg: str, source: Path, *, force: bool) -> tuple[bool, str]:
    """就地改造一个文件。返回 (是否成功, 说明)。"""
    size = source.stat().st_size
    free = shutil.disk_usage(source.parent).free
    if free < size * 2 and not force:
        return False, (f"磁盘余量不足（需要约 {human(size * 2)}，可用 {human(free)}）；"
                       f"腾出空间或加 --force")

    before = videometa.probe(source)
    with tempfile.TemporaryDirectory(prefix="cot-faststart-", dir=str(source.parent)) as tmp:
        out = Path(tmp) / f"{source.stem}.faststart{source.suffix.lower()}"
        cmd = [
            ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
            "-i", str(source), "-c", "copy", "-movflags", "+faststart", str(out),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
        if proc.returncode != 0 or not out.is_file() or not out.stat().st_size:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            return False, f"ffmpeg 失败：{detail[-1] if detail else '原因未知'}"

        if videometa.faststart_state(out) != videometa.FASTSTART_OK:
            return False, "改造后的文件索引仍然不在开头，已放弃（原文件未动）"
        after = videometa.probe(out)
        if before.get("duration") and after.get("duration") != before.get("duration"):
            return False, (f"时长对不上（{before.get('duration')}s → {after.get('duration')}s），"
                           f"已放弃（原文件未动）")

        # 原子替换：同一目录内的 os.replace，中途断电也不会留下半个文件
        os.replace(out, source)

    return True, f"{human(size)} → {human(source.stat().st_size)}"


def main() -> int:
    parser = argparse.ArgumentParser(description="把 MP4/MOV 改成 faststart（索引前置）")
    parser.add_argument("--path", default=str(DEFAULT_DIR), help="扫描目录（默认 assets/video）")
    parser.add_argument("--check", action="store_true", help="只检查，不修改任何文件")
    parser.add_argument("--force", action="store_true", help="磁盘余量不足时也继续")
    args = parser.parse_args()

    target = Path(args.path).resolve()
    if not target.is_dir():
        print(f"目录不存在：{target}", file=sys.stderr)
        return 2

    files = candidates(target)
    slow = [path for path in files if needs_faststart(path)]
    scanned = len(files)

    print(f"扫描 {display(target)}：{scanned} 个 MP4/MOV"
          f"（跳过 {', '.join(sorted(SKIP_DIRS))} 等目录）")
    if not slow:
        print("全部已是 faststart（索引在文件开头），无需处理。")
        return 0

    print(f"需要处理 {len(slow)} 个（索引 moov 在文件末尾，浏览器要下完整份才能起播）：")
    for path in slow:
        print(f"  · {display(path)}  {human(path.stat().st_size)}")

    if args.check:
        print("\n这是 --check：没有改动任何文件。执行 `./run.sh faststart` 就地改造。")
        return 1

    ffmpeg = require_ffmpeg()
    failed = 0
    for index, path in enumerate(slow, start=1):
        print(f"\n[{index}/{len(slow)}] {display(path)}")
        try:
            ok, note = convert(ffmpeg, path, force=args.force)
        except subprocess.TimeoutExpired:
            ok, note = False, f"ffmpeg 超过 {TIMEOUT} 秒，已放弃"
        except OSError as exc:
            ok, note = False, f"文件操作失败：{exc}"
        print(f"      {'✓' if ok else '✗'} {note}")
        failed += 0 if ok else 1

    print()
    if failed:
        print(f"完成：{len(slow) - failed} 个成功，{failed} 个失败（失败的原文件保持原样）")
        return 1
    print(f"完成：{len(slow)} 个已改成 faststart。")
    print("提示：文件内容变了，浏览器会重新下载一次；封面与 data/videos.json 不受影响。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
