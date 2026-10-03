#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 FFmpeg 静态构建装进项目的 bin/ 目录，方便随项目一起打包到服务器。

装好之后后台会自动优先使用 bin/ 里的版本（查找顺序：环境变量 → bin/ → PATH），
服务器上就不需要再 `apt install ffmpeg` 了。

用法：
    python tools/fetch_ffmpeg.py                   # 自动识别平台并下载安装
    python tools/fetch_ffmpeg.py --check           # 只看当前状态，不下载
    python tools/fetch_ffmpeg.py --file ffmpeg.tar.xz   # 从本地压缩包安装（离线/内网）
    python tools/fetch_ffmpeg.py --url <地址>      # 从指定地址下载（内网镜像）
    python tools/fetch_ffmpeg.py --sha256 <摘要>   # 校验下载内容（推荐）
    python tools/fetch_ffmpeg.py --platform linux-arm64
    python tools/fetch_ffmpeg.py --force           # 覆盖已存在的 bin/ffmpeg

安全提示：默认地址指向第三方静态构建（johnvansickle / BtbN / evermeet），
无法保证其完整性与可信度。生产环境建议自行准备二进制并用 --file 安装，
或先用 --sha256 校验官方公布的摘要。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adminlib import media  # noqa: E402

# 平台 → 默认下载地址（静态构建）
BUILD_URLS: dict[str, str] = {
    "linux-x64": "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz",
    "linux-arm64": "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-arm64-static.tar.xz",
    "linux-armhf": "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-armhf-static.tar.xz",
    "windows-x64": "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
    "windows-arm64": "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-winarm64-gpl.zip",
    "darwin-x64": "https://evermeet.cx/ffmpeg/getrelease/zip",
}

WANTED_NAMES = {"ffmpeg", "ffprobe", "ffmpeg.exe", "ffprobe.exe"}
TIMEOUT = 60
CHUNK = 256 * 1024


def detect_platform() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()

    if machine in ("x86_64", "amd64"):
        arch = "x64"
    elif machine in ("aarch64", "arm64"):
        arch = "arm64"
    elif machine.startswith("armv7") or machine.startswith("armv6"):
        arch = "armhf"
    else:
        arch = machine or "unknown"

    name = {"darwin": "darwin", "linux": "linux", "windows": "windows"}.get(system, system)
    return f"{name}-{arch}"


def human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GB"


def display_path(path: Path) -> str:
    """尽量显示相对项目根的路径；--dest 指向项目外时回退为绝对路径。"""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def archive_name_for(url: str) -> str:
    """从下载地址推出一个合理的文件名（保留压缩包后缀）。"""
    name = Path(urlparse(url).path).name
    if name and name.lower().endswith((".tar.xz", ".tar.gz", ".tgz", ".xz", ".zip", ".gz", ".bz2")):
        return name
    return "ffmpeg-download"


def download(url: str, target: Path) -> None:
    """下载到目标文件，带进度输出。支持 file:// 便于离线测试。"""
    print(f"下载：{url}", flush=True)
    request = urllib.request.Request(url, headers={"User-Agent": "collection-of-time/1.0"})

    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        total = int(response.headers.get("Content-Length") or 0)
        received = 0
        with target.open("wb") as fh:
            while True:
                chunk = response.read(CHUNK)
                if not chunk:
                    break
                fh.write(chunk)
                received += len(chunk)
                if total:
                    percent = received * 100 / total
                    sys.stderr.write(f"\r  {percent:5.1f}%  {human(received)} / {human(total)}")
                else:
                    sys.stderr.write(f"\r  {human(received)}")
                sys.stderr.flush()

    sys.stderr.write("\r" + " " * 40 + "\r")
    print(f"完成：{human(target.stat().st_size)}", flush=True)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def extract_binaries(archive: Path, dest: Path, *, force: bool) -> list[Path]:
    """从压缩包里挑出 ffmpeg / ffprobe 并写入 dest。

    只按成员名精确提取，不做整包解压，天然避免路径穿越与无谓的文件落地。
    """
    def is_wanted(name: str) -> bool:
        # 同时接受 ffmpeg 与 ffmpeg.exe：这样在 Linux 上也能为 Windows 目标准备分发包
        base = Path(name).name.lower()
        return base in WANTED_NAMES

    written: list[Path] = []

    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                if info.is_dir() or not is_wanted(info.filename):
                    continue
                target = _target_path(dest, Path(info.filename).name, force=force)
                if target is None:
                    continue
                with zf.open(info) as src, target.open("wb") as out:
                    shutil.copyfileobj(src, out)
                written.append(target)
    else:
        try:
            with tarfile.open(archive) as tf:
                for member in tf.getmembers():
                    if not member.isfile() or not is_wanted(member.name):
                        continue
                    target = _target_path(dest, Path(member.name).name, force=force)
                    if target is None:
                        continue
                    extracted = tf.extractfile(member)
                    if extracted is None:
                        continue
                    with extracted, target.open("wb") as out:
                        shutil.copyfileobj(extracted, out)
                    written.append(target)
        except tarfile.TarError as exc:
            raise SystemExit(f"无法解压 {archive.name}：{exc}") from exc

    return written


def _target_path(dest: Path, filename: str, *, force: bool) -> Path | None:
    """确定写入位置；已存在且未指定 --force 时跳过。"""
    target = dest / filename
    if target.exists() and not force:
        print(f"跳过（已存在）：{target.name}   如需覆盖请加 --force")
        return None
    return target


def make_executable(path: Path) -> None:
    if os.name == "nt":
        return
    current = path.stat().st_mode
    path.chmod(current | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def print_status() -> int:
    tools = media.detect_tools(ROOT, verify=True)
    print("当前 FFmpeg 状态")
    print(f"  来源    : {tools.source_label}")
    print(f"  ffmpeg  : {tools.ffmpeg or '未找到'}")
    print(f"  ffprobe : {tools.ffprobe or '未找到'}")
    if tools.version:
        print(f"  版本    : {tools.version}")
    elif tools.available:
        print("  版本    : 无法执行——二进制可能不完整或架构不匹配")
    else:
        print("\n  未安装不影响后台使用，只是跳过缩略图与视频封面生成。")
        print(f"  安装： python {Path('tools/fetch_ffmpeg.py')}")
    return 0 if tools.available else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="把 FFmpeg 静态构建安装到项目的 bin/ 目录",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--check", action="store_true", help="只显示当前状态，不下载")
    parser.add_argument("--platform", default=detect_platform(),
                        help=f"目标平台，默认自动识别为 {detect_platform()}")
    parser.add_argument("--url", help="自定义下载地址（内网镜像等）")
    parser.add_argument("--file", dest="archive", help="使用本地的压缩包，不联网下载")
    parser.add_argument("--dest", default=str(ROOT / "bin"), help="安装目录，默认 bin/")
    parser.add_argument("--sha256", help="校验压缩包的 sha256，不匹配则中止")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的文件")
    parser.add_argument("--keep-archive", action="store_true", help="保留下载的压缩包")
    args = parser.parse_args()

    if args.check:
        return print_status()

    dest = Path(args.dest).expanduser().resolve()
    dest.mkdir(parents=True, exist_ok=True)

    url = args.url or BUILD_URLS.get(args.platform, "")
    if not args.archive and not url:
        print(f"没有针对 {args.platform} 的默认下载地址。", file=sys.stderr)
        print("请手动下载静态构建后用 --file 指定，或用 --url 指向内网镜像。", file=sys.stderr)
        print(f"可用平台：{', '.join(sorted(BUILD_URLS))}", file=sys.stderr)
        return 2

    temp_dir = Path(tempfile.mkdtemp(prefix="ffmpeg-fetch-"))
    archive = (Path(args.archive).expanduser().resolve() if args.archive
               else temp_dir / archive_name_for(url or ""))
    downloaded = args.archive is None

    try:
        if downloaded:
            try:
                download(url, archive)
            except (urllib.error.URLError, OSError) as exc:
                sys.stdout.flush()
                print(f"\n下载失败：{exc}", file=sys.stderr)
                print("如果是内网环境，请用 --file 指定本地压缩包，或用 --url 指向可用镜像。",
                      file=sys.stderr)
                return 1

        if args.sha256:
            actual = sha256_of(archive)
            if actual.lower() != args.sha256.strip().lower():
                print("sha256 校验不通过，已中止。", file=sys.stderr)
                print(f"  期望：{args.sha256}", file=sys.stderr)
                print(f"  实际：{actual}", file=sys.stderr)
                return 1
            print("sha256 校验通过")

        written = extract_binaries(archive, dest, force=args.force)
        if not written:
            if not args.force and any((dest / n).exists() for n in WANTED_NAMES):
                print(f"\n{display_path(dest)} 里已经有 ffmpeg，未做改动。（加 --force 可覆盖）")
                return 0
            print(f"压缩包里没有找到 ffmpeg / ffprobe：{archive.name}", file=sys.stderr)
            return 1

        for path in written:
            make_executable(path)
            print(f"已安装：{display_path(path)}  ({human(path.stat().st_size)})")

        if args.keep_archive and downloaded:
            # 放在安装目录旁边，方便下次 --file 复用或分发给其它机器
            kept = dest / archive.name
            shutil.copyfile(archive, kept)
            print(f"压缩包已保留：{display_path(kept)}")

        if dest != (ROOT / "bin").resolve():
            # 装到别处时不要报项目 bin/ 的状态，免得误导
            print(f"\n已安装到自定义目录 {display_path(dest)}（后台只会读取 bin/）")
            return 0

        print()
        return print_status()
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
