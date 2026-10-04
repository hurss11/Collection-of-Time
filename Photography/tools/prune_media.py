# -*- coding: utf-8 -*-
"""清理媒体目录里的「对不上」的东西：悬空引用与孤儿文件。

两类问题都出现过：

- **悬空引用**：`data/*.json` 里的条目的 `src` / `thumb` / `poster` 指向的文件不在了。
  手动 `rm` 过文件、上传被中断、或者从旧版本迁移漏带了文件，都会留下这种条目 ——
  后台「数据完整性」里的**缺失文件**就是这个数，前台卡片会显示「缺失」角标。
- **孤儿文件**：媒体目录里有、但没有任何条目的路径提到它的文件。常见于「上传到一半
  改主意」或手工拷进目录的散图。

用法：

    python tools/prune_media.py                        # 只报告，不改动任何东西
    python tools/prune_media.py --delete               # 删掉悬空**条目**与孤儿**文件**
    python tools/prune_media.py --delete --keep-days 7 # 孤儿文件只清 7 天没动过的（默认 1 天）
    python tools/prune_media.py --no-orphans           # 只看悬空引用，不碰孤儿文件

安全性：

- 删除条目走 `adminlib.store`，与后台保存同一条路径 —— **动手前自动备份**到 `data/.backups/`，
  删错了可以从后台「备份」页恢复；
- 只动 `assets/img/photos(/thumbs)` 与 `assets/video(/posters)` 里的文件，绝不越界；
- 默认跳过 `.md`、`README`、以及**被 git 跟踪的上游文件**（示例占位图、
  `landscape-sunrise.mp4` 这类）—— 它们在仓库里，不属于「散落在服务器上的内容」；
  没有 git 时退回到一张保守的跳过清单；
- 孤儿文件默认还要「超过 1 天没动过」才删，避免和正在进行的上传抢文件。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from adminlib import store                                          # noqa: E402
from adminlib.store import Store                                    # noqa: E402

# 条目里指向具体文件的字段
MEDIA_KEYS = ("src", "thumb", "poster")
# 允许清理的目录（相对项目根）：只在这几个目录里删文件
MEDIA_DIRS = (
    Path("assets/img/photos"),
    Path("assets/img/photos/thumbs"),
    Path("assets/video"),
    Path("assets/video/posters"),
)
# 没有 git 时保守跳过（这些是随仓库分发的东西，不该被当孤儿删掉）
FALLBACK_KEEP = {
    "assets/img/photos/README.md",
    "assets/img/photos/thumbs/README.md",
    "assets/video/README.md",
    "assets/video/landscape-sunrise.mp4",
    "assets/video/star-trails-timelapse.mp4",
    "assets/video/posters/v-001.jpg",
    "assets/video/posters/v-002.jpg",
    "assets/video/posters/v-003.svg",
}


def tracked_files() -> set[str]:
    """git 跟踪的文件（相对项目根的 POSIX 路径）；没有 git / 不是检出时返回空集合。"""
    try:
        proc = subprocess.run(["git", "ls-files"], cwd=str(PROJECT_ROOT),
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return set()
    if proc.returncode != 0:
        return set()
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


def media_dirs_present() -> list[Path]:
    return [PROJECT_ROOT / item for item in MEDIA_DIRS if (PROJECT_ROOT / item).is_dir()]


def referenced_paths(entries: dict[str, list[dict]]) -> set[str]:
    """所有条目提到过的文件路径（字符串直接比对，不做存在性判断）。"""
    mentioned: set[str] = set()
    for items in entries.values():
        for item in items:
            for key in MEDIA_KEYS:
                value = str(item.get(key) or "").strip()
                if value and not value.startswith(("http://", "https://")):
                    mentioned.add(value)
    return mentioned


def is_within_media(relative: str) -> bool:
    try:
        path = Path(relative)
    except (TypeError, ValueError):
        return False
    return any(path == item or item in path.parents for item in MEDIA_DIRS)


def find_dangling(entries: dict[str, list[dict]]) -> list[tuple[str, str, str]]:
    """返回 [(集合, 条目 id, 缺失的字段与路径)]，一个条目可能缺多个文件。"""
    found: list[tuple[str, str, str]] = []
    for collection, items in entries.items():
        for item in items:
            for key in MEDIA_KEYS:
                value = str(item.get(key) or "").strip()
                if not value or value.startswith(("http://", "https://")):
                    continue
                if not is_within_media(value):
                    continue
                if not (PROJECT_ROOT / value).is_file():
                    found.append((collection, str(item.get("id") or "?"), f"{key}={value}"))
    return found


def find_orphans(mentioned: set[str], tracked: set[str], keep_days: float) -> list[Path]:
    cutoff = time.time() - keep_days * 86400
    orphans: list[Path] = []
    for directory in media_dirs_present():
        for path in sorted(directory.rglob("*")):
            if not path.is_file() or path.name.startswith("."):
                continue
            relative = path.relative_to(PROJECT_ROOT).as_posix()
            if relative in mentioned:
                continue
            if path.suffix.lower() == ".md" or "README" in path.name.upper():
                continue
            if relative in tracked or relative in FALLBACK_KEEP:
                continue                     # 仓库里的东西，不是「服务器上的散落内容」
            try:
                if path.stat().st_mtime > cutoff:
                    continue                 # 太新：可能正是这次上传正在写的文件
            except OSError:
                continue
            orphans.append(path)
    return orphans


def main() -> int:
    parser = argparse.ArgumentParser(description="清理悬空引用与孤儿媒体文件")
    parser.add_argument("--delete", action="store_true", help="真的删（默认只报告）")
    parser.add_argument("--keep-days", type=float, default=1.0,
                        help="孤儿文件至少「这么久没动过」才删（默认 1 天）")
    parser.add_argument("--no-orphans", action="store_true", help="不处理孤儿文件，只看悬空引用")
    parser.add_argument("--no-dangling", action="store_true", help="不处理悬空引用")
    args = parser.parse_args()

    data_store = Store(PROJECT_ROOT)
    entries = {name: data_store.load(name) for name in store.COLLECTIONS}
    mentioned = referenced_paths(entries)
    tracked = tracked_files()

    print(f"项目目录 : {PROJECT_ROOT}")
    print(f"扫描目录 : {', '.join(item.as_posix() for item in MEDIA_DIRS)}")
    print(f"条目引用 : {len(mentioned)} 个文件路径"
          + (f"（其中 {len(tracked)} 个上游文件被 git 跟踪，会跳过）" if tracked else ""))
    print()

    dangling = [] if args.no_dangling else find_dangling(entries)
    orphans = [] if args.no_orphans else find_orphans(mentioned, tracked, args.keep_days)

    if dangling:
        print(f"悬空引用 {len(dangling)} 条（文件不在了，前台会显示「缺失」）：")
        for collection, item_id, detail in dangling:
            print(f"  · {collection} / {item_id}  {detail}")
    else:
        print("悬空引用：没有")

    print()
    if orphans:
        print(f"孤儿文件 {len(orphans)} 个（没有任何条目引用，且超过 {args.keep_days:g} 天没动过）：")
        for path in orphans:
            size = path.stat().st_size
            print(f"  · {path.relative_to(PROJECT_ROOT).as_posix()}  {size / 1024:.0f}KB")
    else:
        print("孤儿文件：没有")

    if not dangling and not orphans:
        return 0

    if not args.delete:
        print()
        print("这是演练（没有改动任何东西）。确认无误后加 --delete。")
        return 0

    print()
    if dangling:
        removed_ids: set[tuple[str, str]] = set()
        for collection, item_id, _detail in dangling:
            if (collection, item_id) in removed_ids:
                continue
            removed_ids.add((collection, item_id))
            data_store.delete(collection, item_id)
        print(f"已删除 {len(removed_ids)} 个悬空条目（删前已自动备份到 data/.backups/）")

    if orphans:
        freed = 0
        for path in orphans:
            try:
                freed += path.stat().st_size
                path.unlink()
            except OSError as exc:
                print(f"  ! 删不掉 {path.name}：{exc}")
        print(f"已删除 {len(orphans)} 个孤儿文件，释放 {freed / 1024 / 1024:.1f}MB")

    print("提示：后台「概览」里的「缺失文件」应已归零；改错了可从「备份」页恢复。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
