#!/usr/bin/env bash
# 全仓快照 / 回滚锚点。为什么这么写见 DEPLOY.md「备份」一节。
#
# 用法（在部署目录 Photography/ 下）：
#   sudo bash tools/cot-backup.sh                       # 做一份快照，只保留最近 2 份
#   sudo COT_BACKUP_KEEP=5 bash tools/cot-backup.sh     # 改保留份数
#   COT_BACKUP_ROOT=/mnt/backup bash tools/cot-backup.sh
#
# 与「内容备份」（tar data/ assets/ admin.config.json）的分工：这条是**整仓**快照，
# 代码、内容、配置、以及"本地改过什么"的记录一起留，出问题时能整仓退回。
#
# 做四件事：
#   1. 先查剩余磁盘、并检查仓库是不是浅克隆（浅克隆会让「更新失败自动回滚」失去目标版本）；
#   2. 打一份整仓 tar（含 .git 完整历史 + 上传内容），排除 __pycache__ / *.pyc 这类可再生垃圾；
#   3. 单独留一份 admin.config.json（600），并记录 HEAD / git status / 本地改动补丁；
#   4. 只保留最近 N 份，删之前先确认目录名长得像自己的快照。
set -euo pipefail

# 部署目录不一定是仓库根（<仓库根>/Photography 也是常见布局），所以仓库根交给 git 判定；
# 不是 git 检出时（迁移包解压部署）退回到上一级目录。
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR_DEFAULT="$(dirname -- "$SCRIPT_DIR")"
REPO_DIR="${COT_REPO_DIR:-}"
if [ -z "$REPO_DIR" ]; then
  REPO_DIR="$(git -C "$APP_DIR_DEFAULT" rev-parse --show-toplevel 2>/dev/null || true)"
  [ -n "$REPO_DIR" ] || REPO_DIR="$(dirname -- "$APP_DIR_DEFAULT")"
fi
BACKUP_ROOT="${COT_BACKUP_ROOT:-/root/cot-backup}"
KEEP="${COT_BACKUP_KEEP:-2}"
APP_DIR="$REPO_DIR/Photography"
STAMP="$(date +%Y%m%d-%H%M%S)"
DEST="$BACKUP_ROOT/cot-full.$STAMP"
# 同一秒内重跑会撞名：撞了就带 pid 后缀。绝不复用已存在的目录，
# 否则失败清理会把上一次的快照当成"自己的半成品"删掉。
[ ! -e "$DEST" ] || DEST="$DEST-$$"
CREATED=0

info() { printf '  %s\n' "$*"; }
die() { printf '✗ %s\n' "$*" >&2; exit 1; }

# 失败时别留下半成品快照（否则它会占掉一个保留位、还冒充成锚点）。
# 只删**本次真的创建了**的那个目录 —— 判定依据是 CREATED，不是"目录存在"。
on_exit() {
  local rc=$?
  trap - EXIT
  if [ "$rc" -ne 0 ] && [ "${CREATED:-0}" -eq 1 ] && [ -d "$DEST" ]; then
    printf '  失败（退出码 %s），清掉本次的半成品快照：%s\n' "$rc" "$DEST" >&2
    rm -rf -- "$DEST"
  fi
  exit "$rc"
}
trap on_exit EXIT

[ -d "$REPO_DIR" ] || die "找不到仓库目录：$REPO_DIR"
[ -f "$APP_DIR/admin.py" ] || die "看起来不是本项目的仓库（缺 $APP_DIR/admin.py）：$REPO_DIR"
case "$KEEP" in ''|*[!0-9]*) die "COT_BACKUP_KEEP 要是正整数：$KEEP" ;; esac
[ "$KEEP" -ge 1 ] || die "至少保留 1 份"

# 1) 前置检查：一份快照接近 900 MB，留够两倍再动手
mkdir -p "$BACKUP_ROOT"
chmod 700 "$BACKUP_ROOT"
free_kb="$(df -Pk "$BACKUP_ROOT" | awk 'NR==2 {print $4}')"
[ "${free_kb:-0}" -ge $((2 * 1024 * 1024)) ] \
  || die "剩余空间不足 2 GB（当前 $(( ${free_kb:-0} / 1024 )) MB），先清理旧快照"
if [ -f "$REPO_DIR/.git/shallow" ]; then
  info "注意：这是浅克隆（存在 .git/shallow）—— 回滚兜底可能失效"
  info "      一次性修好： git -C $REPO_DIR fetch --unshallow origin"
fi

# 2) 整仓快照（-C 到仓库根的上一级，tar 里的顶层目录名就是仓库目录名）
mkdir -p "$DEST"
CREATED=1
chmod 700 "$DEST"
info "打包 $REPO_DIR → $DEST/Collection-of-Time-full.tar"
# --force-local：让 GNU tar 别把 "C:/..." 当成 rsh 的 host:path（Linux 上是无害选项）
tar --force-local -C "$(dirname "$REPO_DIR")" \
    --exclude='__pycache__' --exclude='*/__pycache__' \
    --exclude='*.pyc' --exclude='*.pyo' \
    --exclude='.run/*.log' --exclude='.run/*.err' \
    --exclude='node_modules' --exclude='.venv' --exclude='dist' \
    -cf "$DEST/Collection-of-Time-full.tar" "$(basename "$REPO_DIR")"

# 3) 记录与配置（本地改动清单 + 补丁，回滚时不至于"不知道自己改过什么"）
{
  echo "repo:   $REPO_DIR"
  echo "at:     $(date -Is)"
  echo "keep:   $KEEP"
} > "$DEST/RECORD-README.txt"
if git -C "$REPO_DIR" rev-parse --git-dir >/dev/null 2>&1; then
  git -C "$REPO_DIR" rev-parse HEAD > "$DEST/RECORD-HEAD.txt"
  git -C "$REPO_DIR" status --short > "$DEST/RECORD-status.txt" 2>&1 || true
  git -C "$REPO_DIR" diff --stat > "$DEST/RECORD-diffstat.txt" 2>&1 || true
  git -C "$REPO_DIR" diff > "$DEST/RECORD-local-changes.patch" 2>&1 || true
else
  # 迁移包解压部署的目录没有 git：内容照样备份，只是没有版本锚点
  printf '(不是 git 检出，没有 HEAD)\n' > "$DEST/RECORD-HEAD.txt"
  info "注意：$REPO_DIR 不是 git 检出，快照不含版本历史"
fi
if [ -f "$APP_DIR/admin.config.json" ]; then
  if command -v install >/dev/null 2>&1; then
    install -m 600 "$APP_DIR/admin.config.json" "$DEST/admin.config.json"
  else
    cp -p "$APP_DIR/admin.config.json" "$DEST/admin.config.json" && chmod 600 "$DEST/admin.config.json"
  fi
  info "admin.config.json 已单独留存（md5 $(md5sum "$DEST/admin.config.json" | cut -d' ' -f1)）"
fi
printf '%s\n' "$DEST" > "$BACKUP_ROOT/LAST-FULL-BACKUP.txt"
chmod 600 "$DEST"/* 2>/dev/null || true
info "锚点记录：$BACKUP_ROOT/LAST-FULL-BACKUP.txt → $DEST"

# 4) 保留最近 N 份：只删自己的、且不删刚做的那份
old="$(ls -1dt "$BACKUP_ROOT"/cot-full.* 2>/dev/null | tail -n +$((KEEP + 1)) || true)"
while IFS= read -r d; do
  [ -n "$d" ] || continue
  case "$d" in
    "$BACKUP_ROOT"/cot-full.*) ;;
    *) info "跳过不像快照的路径：$d"; continue ;;
  esac
  [ "$d" != "$DEST" ] || continue
  info "删除旧快照：$d"
  rm -rf -- "$d"
done <<< "$old"

info "完成：$DEST （$(du -sh "$DEST" | cut -f1)）"
info "回滚锚点： $(cat "$DEST/RECORD-HEAD.txt")"
