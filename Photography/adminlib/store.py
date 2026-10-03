# -*- coding: utf-8 -*-
"""JSON 数据文件的读写、校验、自动备份与原子写入。

设计目标：后台的任何一次保存都不应该把数据写坏。
   - 写入前先把现有文件复制到 data/.backups/；
   - 通过「写临时文件 + os.replace」保证原子性，进程中断也不会留下半截 JSON；
   - 保存前做结构校验，明显错误的提交直接拒绝。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

# 集合名 → 文件名
COLLECTIONS: dict[str, str] = {
    "albums": "albums.json",
    "photos": "photos.json",
    "videos": "videos.json",
}

# 集合名 → 生成新 id 时的前缀
ID_PREFIX: dict[str, str] = {
    "albums": "album",
    "photos": "p",
    "videos": "v",
}

BACKUP_KEEP = 40
MAX_ITEMS = 20000


class StoreError(ValueError):
    """数据校验或写入失败。"""


@dataclass(frozen=True)
class BackupInfo:
    name: str
    collection: str
    size: int
    mtime: float

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "collection": self.collection,
            "size": self.size,
            "mtime": self.mtime,
            "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.mtime)),
        }


# ============================================================
# 路径工具
# ============================================================

_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_SLUG_KEEP = re.compile(r"[^\w.\-]+", re.UNICODE)


def safe_filename(name: str, *, fallback: str = "file") -> str:
    """把用户上传的文件名清洗成安全的文件名（保留扩展名）。"""
    raw = unicodedata.normalize("NFKC", str(name or "")).strip()
    raw = raw.replace("\\", "/").split("/")[-1]           # 去掉任何路径成分
    raw = _UNSAFE_CHARS.sub("", raw)
    raw = _SLUG_KEEP.sub("-", raw).strip("-. ")
    if not raw:
        return fallback
    # 限制长度，同时保住扩展名
    stem, dot, suffix = raw.rpartition(".")
    if dot and len(stem) > 60:
        raw = f"{stem[:60]}.{suffix[:10]}"
    elif not dot and len(raw) > 80:
        raw = raw[:80]
    return raw


def unique_path(directory: Path, filename: str) -> Path:
    """在目录中生成不冲突的路径，例如 sunset.jpg → sunset-1.jpg。"""
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / filename
    if not candidate.exists():
        return candidate

    stem = candidate.stem
    suffix = candidate.suffix
    for index in range(1, 1000):
        candidate = directory / f"{stem}-{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise StoreError(f"目录中同名文件过多：{filename}")


def ensure_relative(root: Path, target: Path) -> str:
    """确认 target 位于 root 之内，返回相对 POSIX 路径（防止路径穿越）。"""
    root_resolved = root.resolve()
    try:
        relative = target.resolve().relative_to(root_resolved)
    except ValueError as exc:
        raise StoreError(f"路径越界：{target}") from exc
    return relative.as_posix()


# ============================================================
# 存储
# ============================================================

class Store:
    """负责 data/*.json 的读写。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.data_dir = self.root / "data"
        self.backup_dir = self.data_dir / ".backups"
        self.data_dir.mkdir(parents=True, exist_ok=True)

    # ---------- 路径 ----------

    def path_of(self, collection: str) -> Path:
        try:
            return self.data_dir / COLLECTIONS[collection]
        except KeyError as exc:
            raise StoreError(f"未知的数据集合：{collection}") from exc

    # ---------- 读取 ----------

    def load(self, collection: str) -> list[dict]:
        path = self.path_of(collection)
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise StoreError(f"{path.name} 不是合法 JSON：{exc}") from exc
        if not isinstance(payload, list):
            raise StoreError(f"{path.name} 的顶层结构必须是数组")
        return [item for item in payload if isinstance(item, dict)]

    def load_all(self) -> dict[str, list[dict]]:
        return {name: self.load(name) for name in COLLECTIONS}

    # ---------- 校验 ----------

    def validate(self, collection: str, items: object) -> list[str]:
        """校验待写入的数据，返回 warning 列表；致命问题直接抛 StoreError。"""
        if not isinstance(items, list):
            raise StoreError(f"{collection} 必须是数组")
        if len(items) > MAX_ITEMS:
            raise StoreError(f"{collection} 条目过多（{len(items)} > {MAX_ITEMS}）")

        warnings: list[str] = []
        seen: set[str] = set()

        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise StoreError(f"{collection}[{index}] 不是对象")

            item_id = item.get("id")
            if not isinstance(item_id, str) or not item_id.strip():
                raise StoreError(f"{collection}[{index}] 缺少 id")
            if item_id in seen:
                raise StoreError(f"{collection} 中 id 重复：{item_id}")
            seen.add(item_id)

            if collection in ("photos", "videos") and not item.get("title"):
                warnings.append(f"{item_id} 缺少 title")
            if collection == "videos":
                provider = str(item.get("provider", "file")).lower()
                if provider == "file" and not item.get("src"):
                    warnings.append(f"{item_id} 是本地视频但没有填 src")
                if provider != "file" and not item.get("poster"):
                    warnings.append(f"{item_id} 是外链视频但没有封面（poster）")

        # 相册引用检查（只提示，不阻断：允许先建条目后建相册）
        if collection in ("photos", "videos"):
            album_ids = {str(a.get("id")) for a in self.load("albums")}
            if album_ids:
                for item in items:
                    album = str(item.get("album") or "")
                    if album and album not in album_ids:
                        warnings.append(f"{item.get('id')} 引用了不存在的相册：{album}")

        return warnings

    # ---------- 写入 ----------

    def save(self, collection: str, items: list[dict], *, backup: bool = True) -> list[str]:
        warnings = self.validate(collection, items)
        path = self.path_of(collection)

        if backup and path.exists():
            self._backup(collection)

        payload = json.dumps(items, ensure_ascii=False, indent=2) + "\n"
        self._atomic_write(path, payload)
        return warnings

    def upsert(self, collection: str, item: dict) -> tuple[dict, list[str]]:
        """按 id 新增或覆盖单条记录。"""
        if not isinstance(item, dict):
            raise StoreError("条目必须是对象")

        items = self.load(collection)
        item_id = str(item.get("id") or "").strip()
        if not item_id:
            item_id = self.next_id(collection)
            item = {**item, "id": item_id}

        for index, existing in enumerate(items):
            if str(existing.get("id")) == item_id:
                items[index] = {**existing, **item}
                break
        else:
            items.append(item)

        warnings = self.save(collection, items)
        saved = next(i for i in items if str(i.get("id")) == item_id)
        return saved, warnings

    def delete(self, collection: str, item_id: str) -> bool:
        items = self.load(collection)
        remaining = [i for i in items if str(i.get("id")) != item_id]
        if len(remaining) == len(items):
            return False
        self.save(collection, remaining)
        return True

    def next_id(self, collection: str) -> str:
        """生成下一个可用 id，例如 photos → p-009。"""
        prefix = ID_PREFIX.get(collection, collection[:1] or "x")
        pattern = re.compile(rf"^{re.escape(prefix)}-(\d+)$")

        used: set[int] = set()
        for item in self.load(collection):
            matched = pattern.match(str(item.get("id") or ""))
            if matched:
                used.add(int(matched.group(1)))

        index = 1
        while index in used:
            index += 1
        return f"{prefix}-{index:03d}"

    # ---------- 备份 ----------

    def _backup(self, collection: str) -> Path | None:
        source = self.path_of(collection)
        if not source.exists():
            return None

        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = self.backup_dir / f"{collection}-{stamp}.json"

        # 同一秒内连续写入时避免覆盖
        counter = 0
        while target.exists():
            counter += 1
            target = self.backup_dir / f"{collection}-{stamp}-{counter}.json"

        shutil.copy2(source, target)
        self.prune_backups()
        return target

    def list_backups(self) -> list[BackupInfo]:
        if not self.backup_dir.is_dir():
            return []

        infos: list[BackupInfo] = []
        for path in self.backup_dir.glob("*.json"):
            collection = path.stem.split("-", 1)[0]
            stat = path.stat()
            infos.append(BackupInfo(path.name, collection, stat.st_size, stat.st_mtime))
        return sorted(infos, key=lambda i: i.mtime, reverse=True)

    def restore_backup(self, name: str) -> str:
        """用备份覆盖当前数据，返回被恢复的集合名。"""
        safe_name = safe_filename(name)
        source = self.backup_dir / safe_name
        if not source.is_file():
            raise StoreError(f"备份不存在：{safe_name}")

        collection = safe_name.split("-", 1)[0]
        if collection not in COLLECTIONS:
            raise StoreError(f"无法从备份名推断集合：{safe_name}")

        target = self.path_of(collection)
        if target.exists():
            self._backup(collection)   # 恢复前再存一份现状，可反复回滚

        shutil.copy2(source, target)
        return collection

    def prune_backups(self, keep: int = BACKUP_KEEP) -> int:
        infos = self.list_backups()
        removed = 0
        for info in infos[keep:]:
            try:
                (self.backup_dir / info.name).unlink()
                removed += 1
            except OSError:
                pass
        return removed

    # ---------- 底层 ----------

    @staticmethod
    def _atomic_write(path: Path, payload: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        try:
            with temp.open("w", encoding="utf-8", newline="\n") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temp, path)   # 原子替换
        finally:
            if temp.exists():
                try:
                    temp.unlink()
                except OSError:
                    pass

    # ---------- 导出 / 导入 ----------

    def export_bundle(self) -> dict[str, object]:
        return {
            "format": "collection-of-time/photography",
            "version": 1,
            "exportedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
            "albums": self.load("albums"),
            "photos": self.load("photos"),
            "videos": self.load("videos"),
        }

    def import_bundle(self, payload: object, *, replace: bool = True) -> dict[str, object]:
        """导入导出包。replace=False 时按 id 合并（同 id 覆盖）。"""
        if not isinstance(payload, dict):
            raise StoreError("导入内容必须是 JSON 对象")

        summary: dict[str, object] = {}
        for collection in COLLECTIONS:
            incoming = payload.get(collection)
            if incoming is None:
                continue
            if not isinstance(incoming, list):
                raise StoreError(f"导入内容中的 {collection} 必须是数组")

            if replace:
                merged = incoming
            else:
                current = {str(i.get("id")): i for i in self.load(collection)}
                for item in incoming:
                    if isinstance(item, dict) and item.get("id"):
                        current[str(item["id"])] = item
                merged = list(current.values())

            self.save(collection, merged)
            summary[collection] = len(merged)

        return summary
