#!/usr/bin/env bash
#
# Collection of Time · Photography —— 一键运行脚本
#
# 用法：
#   ./run.sh                 启动（等价于 start）
#   ./run.sh setup           首次部署一条龙：装 FFmpeg + 建账号 + 启动
#   ./run.sh start           启动后台服务（后台运行，日志写入 .run/admin.log）
#   ./run.sh stop           停止
#   ./run.sh restart        重启
#   ./run.sh status         查看运行状态
#   ./run.sh logs [-f]      查看日志（-f 持续跟踪）
#   ./run.sh doctor         环境自检（Python / FFmpeg / 目录权限 / JSON / 端口）
#   ./run.sh install-ffmpeg 下载 FFmpeg 静态构建到 bin/（随项目打包到服务器）
#   ./run.sh package        打成可迁移到服务器的 tar.gz（含 FFmpeg，默认 linux-x64）
#   ./run.sh create-user    创建管理员账号
#   ./run.sh reset-password 重置管理员密码
#   ./run.sh systemd        生成 systemd 单元文件（可用 sudo 安装）
#   ./run.sh help           显示本帮助
#
# 可用环境变量：
#   PORT=8080  HOST=127.0.0.1  SESSION_HOURS=12  PYTHON=python3
#   ADMIN_PASSWORD=...     非交互创建账号时的密码（至少 8 位）
#   ADMIN_USERNAME=...     非交互创建账号时的用户名（默认 admin）
#   FFMPEG_URL=...         指定 FFmpeg 下载地址（内网镜像）
#
# 非交互部署示例：
#   ADMIN_USERNAME=me ADMIN_PASSWORD='你的密码' ./run.sh setup
#
# 也支持命令行覆盖： ./run.sh start --port 9000 --host 0.0.0.0
#
set -uo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"

RUN_DIR="$APP_DIR/.run"
PID_FILE="$RUN_DIR/admin.pid"
LOG_FILE="$RUN_DIR/admin.log"

MIN_PY_MAJOR=3
MIN_PY_MINOR=9

# ---------- 输出 ----------

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'
  C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'; C_BLUE=$'\033[36m'
else
  C_RESET=''; C_BOLD=''; C_DIM=''; C_GREEN=''; C_YELLOW=''; C_RED=''; C_BLUE=''
fi

info()  { printf '%s\n' "$*"; }
ok()    { printf '%s✓%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
warn()  { printf '%s!%s %s\n' "$C_YELLOW" "$C_RESET" "$*"; }
err()   { printf '%s✗%s %s\n' "$C_RED" "$C_RESET" "$*" >&2; }
step()  { printf '%s▸%s %s\n' "$C_BLUE" "$C_RESET" "$*"; }
die()   { err "$*"; exit 1; }

banner() {
  printf '%s%s%s\n' "$C_BOLD" "Collection of Time · Photography" "$C_RESET"
  printf '%s%s%s\n' "$C_DIM" "目录：$APP_DIR" "$C_RESET"
}

# ---------- Python ----------

PY_BIN=""

find_python() {
  local candidate
  for candidate in "${PYTHON:-}" python3 python; do
    [ -n "$candidate" ] || continue
    if command -v "$candidate" >/dev/null 2>&1; then
      if "$candidate" -c "import sys; sys.exit(0 if sys.version_info >= ($MIN_PY_MAJOR, $MIN_PY_MINOR) else 1)" 2>/dev/null; then
        # 存绝对路径：nohup / systemd 这类外部命令无法调用 shell 函数
        PY_BIN="$(command -v "$candidate")" || PY_BIN="$candidate"
        return 0
      fi
    fi
  done
  return 1
}

require_python() {
  if [ -z "$PY_BIN" ] && ! find_python; then
    if command -v python3 >/dev/null 2>&1; then
      die "Python 版本过低：需要 ${MIN_PY_MAJOR}.${MIN_PY_MINOR}+，当前为 $("python3" --version 2>&1)"
    fi
    die "找不到 python3，请先安装 Python ${MIN_PY_MAJOR}.${MIN_PY_MINOR}+"
  fi
}

py() { "$PY_BIN" "$@"; }

# ---------- 进程 ----------

read_pid() {
  [ -f "$PID_FILE" ] || return 1
  local pid
  pid="$(cat "$PID_FILE" 2>/dev/null | tr -d '[:space:]')"
  [ -n "$pid" ] || return 1
  printf '%s' "$pid"
}

is_running() {
  local pid
  pid="$(read_pid)" || return 1
  kill -0 "$pid" 2>/dev/null
}

# 端口被谁占了（尽力而为，缺少工具时返回空）
port_owner() {
  local port="$1"
  if command -v ss >/dev/null 2>&1; then
    ss -ltnp 2>/dev/null | awk -v p=":$port" '$4 ~ p {print; exit}'
  elif command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | tail -n +2 | head -1
  fi
}

# HTTP 健康检查：优先 curl / wget，最后回落到 python
http_ok() {
  local url="$1"
  if command -v curl >/dev/null 2>&1; then
    curl -fsS --max-time 3 "$url" >/dev/null 2>&1
  elif command -v wget >/dev/null 2>&1; then
    wget -q -T 3 -O /dev/null "$url" 2>/dev/null
  else
    py - "$url" <<'PY' 2>/dev/null
import sys, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1], timeout=3) as r:
        sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)
PY
  fi
}

wait_health() {
  local url="$1" tries="${2:-40}" i=0
  while [ "$i" -lt "$tries" ]; do
    if http_ok "$url"; then return 0; fi
    # 进程已经挂了就不用再等了
    if [ -n "${START_PID:-}" ] && ! kill -0 "$START_PID" 2>/dev/null; then return 1; fi
    sleep 0.5
    i=$((i + 1))
  done
  return 1
}

# ---------- FFmpeg ----------

ffmpeg_state() {
  py - <<'PY' 2>/dev/null
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))
from adminlib import media
tools = media.detect_tools(Path.cwd(), verify=True)
if not tools.available:
    print("missing|未安装|")
    sys.exit(1)
print(f"{tools.source}|{tools.source_label}|{tools.version}")
PY
}

install_ffmpeg() {
  require_python
  step "安装 FFmpeg 到 bin/"
  local args=()
  [ -n "${FFMPEG_URL:-}" ] && args+=(--url "$FFMPEG_URL")
  py tools/fetch_ffmpeg.py "${args[@]+"${args[@]}"}" "$@" || die "FFmpeg 安装失败"
}

# ---------- 服务 ----------

start() {
  local port="${PORT:-8080}" host="${HOST:-127.0.0.1}" hours="${SESSION_HOURS:-12}"
  local foreground=0

  while [ "$#" -gt 0 ]; do
    case "$1" in
      --port) port="${2:-}"; shift 2 ;;
      --port=*) port="${1#*=}"; shift ;;
      --host) host="${2:-}"; shift 2 ;;
      --host=*) host="${1#*=}"; shift ;;
      --session-hours) hours="${2:-}"; shift 2 ;;
      --session-hours=*) hours="${1#*=}"; shift ;;
      --foreground|-f) foreground=1; shift ;;
      *) die "未知参数：$1（用 ./run.sh help 查看用法）" ;;
    esac
  done

  require_python
  banner

  if is_running; then
    warn "服务已在运行（PID $(read_pid)），如需重启请执行： ./run.sh restart"
    return 0
  fi

  # 端口占用检查：避免起来后才发现绑定失败
  local occupied
  occupied="$(port_owner "$port")"
  if [ -n "$occupied" ]; then
    warn "端口 $port 已被占用：$occupied"
    die "请换端口： ./run.sh start --port 8081"
  fi

  # 首次使用：没有账号时引导创建，否则登录页无法通过
  if [ ! -f admin.config.json ]; then
    info ""
    warn "还没有管理员账号，需要先创建。"
    # 给了 ADMIN_PASSWORD 就走非交互建号（systemd / 容器 / 一键部署）
    if [ -n "${ADMIN_PASSWORD:-}" ]; then
      step "使用 ADMIN_PASSWORD 环境变量创建账号"
      py admin.py --create-user || die "创建账号失败（ADMIN_PASSWORD 至少 8 位）"
    elif [ -t 0 ]; then
      py admin.py --create-user || die "创建账号失败"
    else
      info "  非交互环境，请执行： ADMIN_PASSWORD='...' ./run.sh create-user"
      info "  或先启动服务，打开后台页面按引导创建。"
      info "  也可以现在先启动，登录页会提示首次建号。"
    fi
  fi

  mkdir -p "$RUN_DIR"

  if [ "$foreground" -eq 1 ]; then
    step "前台启动（Ctrl+C 停止）"
    exec py admin.py --host "$host" --port "$port" --session-hours "$hours" --no-browser
  fi

  step "启动服务：$host:$port"
  nohup "$PY_BIN" "$APP_DIR/admin.py" --host "$host" --port "$port" --session-hours "$hours" --no-browser \
    >>"$LOG_FILE" 2>&1 &
  START_PID=$!
  printf '%s' "$START_PID" >"$PID_FILE"

  # 没有登录态也能访问 /api/health，用它判断是否真正起来了
  if wait_health "http://127.0.0.1:$port/api/health"; then
    info ""
    ok "已启动（PID $START_PID）"
    info ""
    if [ "$host" = "127.0.0.1" ] || [ "$host" = "localhost" ]; then
      info "  管理后台 : ${C_BOLD}http://127.0.0.1:$port/admin/${C_RESET}"
      info "  站点预览 : http://127.0.0.1:$port/"
      info ""
      info "  若部署在远程服务器，本地建立隧道后访问："
      info "    ${C_DIM}ssh -N -L $port:127.0.0.1:$port 用户名@服务器${C_RESET}"
    else
      info "  管理后台 : ${C_BOLD}http://$host:$port/admin/${C_RESET}"
    fi
    info "  日志     : ${C_DIM}./run.sh logs -f${C_RESET}"
    info "  停止     : ${C_DIM}./run.sh stop${C_RESET}"

    # 顺带把 FFmpeg 状态说清楚，避免上传视频后才发现没有封面
    local state
    if state="$(ffmpeg_state)"; then
      local src
      src="$(printf '%s' "$state" | cut -d'|' -f2)"
      local ver
      ver="$(printf '%s' "$state" | cut -d'|' -f3)"
      info "  FFmpeg   : $src"
      [ -n "$ver" ] && info "             ${C_DIM}$ver${C_RESET}"
    else
      info ""
      warn "未检测到 FFmpeg：上传仍可用，但不会生成缩略图与视频封面。"
      info "  启用： ${C_DIM}./run.sh install-ffmpeg${C_RESET}"
    fi
    info ""
  else
    err "启动失败，最近日志："
    tail -n 20 "$LOG_FILE" >&2 || true
    rm -f "$PID_FILE"
    return 1
  fi
}

stop() {
  banner
  if ! is_running; then
    warn "服务未在运行"
    rm -f "$PID_FILE"
    return 0
  fi

  local pid
  pid="$(read_pid)"
  step "停止服务（PID $pid）"
  kill "$pid" 2>/dev/null || true

  local i=0
  while [ "$i" -lt 20 ]; do
    if ! kill -0 "$pid" 2>/dev/null; then
      rm -f "$PID_FILE"
      ok "已停止"
      return 0
    fi
    sleep 0.5
    i=$((i + 1))
  done

  warn "正常退出超时，强制结束"
  kill -9 "$pid" 2>/dev/null || true
  sleep 0.5
  rm -f "$PID_FILE"
  ok "已强制停止"
}

restart() {
  stop || true
  info ""
  start "$@"
}

status() {
  banner
  require_python
  info ""

  if is_running; then
    local pid
    pid="$(read_pid)"
    ok "运行中（PID $pid）"

    if command -v ps >/dev/null 2>&1; then
      local elapsed
      elapsed="$(ps -o etime= -p "$pid" 2>/dev/null | tr -d ' ')"
      [ -n "$elapsed" ] && info "  已运行   : $elapsed"
    fi
  else
    warn "未在运行"
    rm -f "$PID_FILE" 2>/dev/null || true
  fi

  # 日志与运行时长
  if [ -f "$LOG_FILE" ]; then
    info "  日志     : $LOG_FILE（$(wc -l <"$LOG_FILE" | tr -d ' ') 行）"
  fi

  # FFmpeg
  local state
  if state="$(ffmpeg_state)"; then
    info "  FFmpeg   : ${state#*|}" | sed 's/|/  /g'
  else
    info "  FFmpeg   : 未安装（不影响使用，仅跳过缩略图与封面）"
  fi

  # 账号
  if [ -f admin.config.json ]; then
    local user
    user="$(py - <<'PY' 2>/dev/null
import json, pathlib
data = json.loads(pathlib.Path("admin.config.json").read_text(encoding="utf-8"))
print((data.get("account") or {}).get("username") or "—")
PY
)"
    info "  管理员   : ${user:-—}"
  else
    info "  管理员   : 尚未创建"
  fi

  # 数据
  py - <<'PY' 2>/dev/null
import json, pathlib
parts = []
for name in ("albums", "photos", "videos"):
    path = pathlib.Path("data") / f"{name}.json"
    try:
        parts.append(f"{name}={len(json.loads(path.read_text(encoding='utf-8')))}")
    except Exception:
        parts.append(f"{name}=?")
print("  数据     : " + "  ".join(parts))
PY

  # 健康检查
  if is_running; then
    local found=0
    for candidate in 8080 8081 8000 9000; do
      if http_ok "http://127.0.0.1:$candidate/api/health"; then
        ok "健康检查 : http://127.0.0.1:$candidate/api/health"
        found=1
        break
      fi
    done
    [ "$found" -eq 0 ] && warn "健康检查 : 未响应（进程在，但端口可能不是默认值）"
  fi
  info ""
}

logs() {
  require_python
  if [ ! -f "$LOG_FILE" ]; then
    warn "还没有日志文件（服务尚未启动过）"
    return 0
  fi
  if [ "${1:-}" = "-f" ] || [ "${1:-}" = "--follow" ]; then
    tail -n 50 -f "$LOG_FILE"
  else
    tail -n "${1:-80}" "$LOG_FILE"
  fi
}

# ---------- 自检 ----------

doctor() {
  banner
  info ""
  local problems=0

  # Python
  if find_python; then
    ok "Python   : $("$PY_BIN" --version 2>&1)（$PY_BIN）"
  else
    err "Python   : 需要 ${MIN_PY_MAJOR}.${MIN_PY_MINOR}+，未找到可用的 python3"
    problems=$((problems + 1))
  fi

  # 必需文件
  local missing=0
  for f in admin.py index.html admin/index.html adminlib/store.py adminlib/auth.py; do
    [ -e "$f" ] || { err "缺少文件 : $f"; missing=1; }
  done
  [ "$missing" -eq 0 ] && ok "项目文件 : 完整"

  # 写权限
  if [ -w . ] && [ -w data ] 2>/dev/null; then
    ok "写权限   : 正常（可写 data/ 与 assets/）"
  else
    err "写权限   : 当前用户无法写入项目目录，保存与上传会失败"
    problems=$((problems + 1))
  fi

  # JSON 合法性
  if [ -n "$PY_BIN" ]; then
    if py - <<'PY'
import json, pathlib, sys
bad = []
for name in ("albums", "photos", "videos"):
    path = pathlib.Path("data") / f"{name}.json"
    if not path.exists():
        continue
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            bad.append(f"{name} 顶层不是数组")
    except json.JSONDecodeError as exc:
        bad.append(f"{name}: {exc}")
if bad:
    print("; ".join(bad))
    sys.exit(1)
PY
    then
      ok "数据文件 : JSON 合法"
    else
      err "数据文件 : 存在问题（见上）"
      problems=$((problems + 1))
    fi
  fi

  # FFmpeg
  local state
  if state="$(ffmpeg_state)"; then
    local src="${state%%|*}"
    ok "FFmpeg   : 已就绪（来源：$(echo "$state" | cut -d'|' -f2)）"
    [ "$src" = "system" ] && info "           提示：想要随项目打包，可执行 ./run.sh install-ffmpeg 装到 bin/"
  else
    warn "FFmpeg   : 未安装 —— 后台可用，但跳过缩略图与视频封面"
    info "           启用： ./run.sh install-ffmpeg"
  fi

  # 账号
  if [ -f admin.config.json ]; then
    local mode
    mode="$(stat -c '%a' admin.config.json 2>/dev/null || stat -f '%Lp' admin.config.json 2>/dev/null || echo '?')"
    ok "管理员   : 已配置（admin.config.json 权限 $mode）"
    [ "$mode" != "600" ] && [ "$mode" != "?" ] && warn "           建议收紧权限： chmod 600 admin.config.json"
  else
    warn "管理员   : 尚未创建 —— 首次启动会自动引导，或执行 ./run.sh create-user"
  fi

  # 端口
  local port="${PORT:-8080}"
  local occupied
  occupied="$(port_owner "$port")"
  if [ -n "$occupied" ]; then
    if is_running; then
      ok "端口     : $port 已被本服务占用（正常）"
    else
      warn "端口     : $port 被其它进程占用：$occupied"
      problems=$((problems + 1))
    fi
  else
    ok "端口     : $port 可用"
  fi

  info ""
  if [ "$problems" -eq 0 ]; then
    ok "自检通过，可以执行： ./run.sh setup"
  else
    err "发现 $problems 个问题，建议先处理后启动"
    return 1
  fi
}

# ---------- 账号 ----------

create_user() {
  require_python
  banner
  info ""
  py admin.py --create-user "$@"
}

reset_password() {
  require_python
  banner
  info ""
  py admin.py --reset-password "$@"
}

# ---------- systemd ----------

systemd() {
  local port="${PORT:-8080}"
  local unit_name="photography-admin"

  if [ "${1:-}" = "--install" ]; then
    [ "$(id -u)" -eq 0 ] || die "安装需要 root： sudo ./run.sh systemd --install"
    require_python
    py admin.py --port "$port" --print-systemd >"/etc/systemd/system/$unit_name.service"
    systemctl daemon-reload
    systemctl enable --now "$unit_name"
    ok "已安装并启动：$unit_name"
    info "  查看状态： systemctl status $unit_name"
    info "  查看日志： journalctl -u $unit_name -f"
    return 0
  fi

  require_python
  py admin.py --port "$port" --print-systemd
  info ""
  info "安装为常驻服务："
  info "  sudo ./run.sh systemd --install"
  info "  # 等价的手工步骤："
  info "  sudo cp 上面输出 > /etc/systemd/system/$unit_name.service"
  info "  sudo systemctl daemon-reload && sudo systemctl enable --now $unit_name"
}

# ---------- 打包：准备迁移到服务器 ----------

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | cut -d' ' -f1
  else
    py - "$1" <<'PY'
import hashlib, sys
with open(sys.argv[1], "rb") as fh:
    print(hashlib.sha256(fh.read()).hexdigest())
PY
  fi
}

json_count() {
  py - "$1" <<'PY' 2>/dev/null
import json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    print(len(json.load(fh)))
PY
}

# 把项目打成可迁移到服务器的 tar.gz
package() {
  local platform="linux-x64"
  local out_dir="$APP_DIR/dist"
  local with_config=0
  local with_ffmpeg=1
  local keep_stage=0

  while [ "$#" -gt 0 ]; do
    case "$1" in
      --platform)    platform="${2:-}"; shift 2 ;;
      --platform=*)  platform="${1#*=}"; shift ;;
      --out)         out_dir="${2:-}"; shift 2 ;;
      --out=*)       out_dir="${1#*=}"; shift ;;
      --with-config) with_config=1; shift ;;
      --no-ffmpeg)   with_ffmpeg=0; shift ;;
      --keep-stage)  keep_stage=1; shift ;;
      *) die "未知参数：$1（./run.sh help 查看用法）" ;;
    esac
  done

  require_python
  banner
  info ""

  # ---- 1/4 必需文件 ----
  step "1/4 检查必需文件"
  local missing="" f
  for f in admin.py run.sh serve.py index.html favicon.svg README.md DEPLOY.md \
           admin/index.html admin/js/app.js admin/css/admin.css \
           adminlib/store.py adminlib/auth.py adminlib/media.py adminlib/exifread.py \
           tools/fetch_ffmpeg.py tools/make_posters.py \
           data/albums.json data/photos.json data/videos.json; do
    [ -e "$f" ] || missing="$missing $f"
  done
  if [ -n "$missing" ]; then
    err "缺少文件：$missing"
    return 1
  fi
  ok "必需文件齐全"

  # ---- 2/4 目标平台的 FFmpeg ----
  step "2/4 准备 FFmpeg（目标平台 $platform）"
  local ffmpeg_name="ffmpeg" ffprobe_name="ffprobe"
  case "$platform" in
    windows-*) ffmpeg_name="ffmpeg.exe"; ffprobe_name="ffprobe.exe" ;;
  esac

  # 另一种命名约定的二进制属于别的平台，排除掉，免得白白占几十 MB
  local foreign_bins=""
  if [ "$ffmpeg_name" = "ffmpeg" ]; then
    [ -f bin/ffmpeg.exe ] && foreign_bins="ffmpeg.exe ffprobe.exe"
  else
    [ -f bin/ffmpeg ] && foreign_bins="ffmpeg ffprobe"
  fi

  local ffmpeg_desc="未包含"
  if [ "$with_ffmpeg" -eq 0 ]; then
    info "已指定 --no-ffmpeg，包内不含二进制（服务器需自行提供）"
    ffmpeg_desc="未包含（已指定 --no-ffmpeg）"
  elif [ -f "bin/$ffmpeg_name" ] && [ -f "bin/$ffprobe_name" ]; then
    ok "bin/ 里已有 $platform 的二进制，跳过下载"
    ffmpeg_desc="已包含（bin/$ffmpeg_name、bin/$ffprobe_name）"
    # 尽力核对架构，避免把 ARM 的二进制装到 x86_64 服务器上
    if command -v file >/dev/null 2>&1; then
      local desc
      desc="$(file -b "bin/$ffmpeg_name" 2>/dev/null)"
      case "$platform:$desc" in
        linux-x64:*aarch64*|linux-x64:*ARM*)
          warn "但 bin/$ffmpeg_name 看起来是 ARM 架构：$desc"
          info "           如需替换： ./run.sh install-ffmpeg --force --platform $platform" ;;
        linux-arm64:*x86-64*|linux-arm64:*x86_64*)
          warn "但 bin/$ffmpeg_name 看起来是 x86-64：$desc"
          info "           如需替换： ./run.sh install-ffmpeg --force --platform $platform" ;;
      esac
    fi
  else
    info "bin/ 里没有 $platform 的二进制，尝试下载（离线请用 --no-ffmpeg 或手工放到 bin/）"
    if py tools/fetch_ffmpeg.py --platform "$platform" --force; then
      ffmpeg_desc="已包含（bin/$ffmpeg_name、bin/$ffprobe_name）"
    else
      warn "下载失败，包内将不含 FFmpeg"
      info "           服务器上可执行 ./run.sh install-ffmpeg 补装"
      ffmpeg_desc="未包含（下载失败）"
    fi
  fi
  if [ -n "$foreign_bins" ]; then
    warn "bin/ 里另有其它平台的二进制（$foreign_bins），已从包中排除"
  fi

  # ---- 3/4 组装 ----
  step "3/4 组装包内容"
  local stamp pkg_name stage root
  stamp="$(date +%Y%m%d-%H%M)"
  pkg_name="photography-${platform}-${stamp}"
  stage="$(mktemp -d "${TMPDIR:-/tmp}/cot-package-XXXXXX")" || die "无法创建临时目录"
  root="$stage/$pkg_name"
  mkdir -p "$root"

  # 先列出候选文件，再按规则剔除；比 tar --exclude 更好控制（不受 ./ 前缀影响）
  local list="$stage/files.txt"
  find . \
    \( -name '.git' -o -name '.venv' -o -name 'venv' -o -name 'node_modules' \
       -o -name '__pycache__' -o -name '.run' -o -name 'dist' \
       -o -path './data/.backups' \) -prune -o \
    -type f -print >"$list"

  local skip_re='\.(pyc|pyo|log|swp)$|(^|/)(\.DS_Store|Thumbs\.db|desktop\.ini)$'
  [ "$with_config" -eq 0 ] && skip_re="$skip_re|^\./admin\.config\.json$"
  [ "$with_ffmpeg" -eq 0 ] && skip_re="$skip_re|^\./bin/(ffmpeg|ffprobe)(\.exe)?$"
  local b
  for b in $foreign_bins; do
    skip_re="$skip_re|^\./bin/${b}$"
  done

  local count=0 line rel
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    if printf '%s' "$line" | grep -Eq "$skip_re"; then
      continue
    fi
    rel="${line#./}"
    mkdir -p "$root/$(dirname "$rel")"
    cp -p "$line" "$root/$rel" || die "复制失败：$line"
    count=$((count + 1))
  done <"$list"
  ok "已收集 $count 个文件"

  # 附一份清单，方便在服务器上核对包内容
  local data_lines="" n c h
  for n in albums photos videos; do
    if [ -f "$root/data/$n.json" ]; then
      c="$(json_count "$root/data/$n.json")"
      h="$(sha256_of "$root/data/$n.json")"
      data_lines="${data_lines}  ${n}.json  ${c} 条  sha256=${h}
"
    fi
  done

  local config_desc="未包含（到服务器上执行 ./run.sh create-user 重新建号）"
  [ "$with_config" -eq 1 ] && config_desc="已包含（沿用现有账号与密码）"

  cat >"$root/PACKAGE-INFO.txt" <<EOF
Collection of Time · Photography —— 迁移包

打包时间   : $(date '+%Y-%m-%d %H:%M:%S')
打包主机   : $(uname -srm 2>/dev/null || echo unknown)
目标平台   : $platform
包内 FFmpeg: $ffmpeg_desc
管理员配置 : $config_desc
文件数量   : $count
解压后大小 : $(du -sh "$root" 2>/dev/null | cut -f1)

数据统计（data/）：
${data_lines}
使用步骤见同目录 DEPLOY.md。
EOF
  ok "已写入 PACKAGE-INFO.txt"

  # ---- 4/4 压缩 ----
  step "4/4 生成压缩包"
  mkdir -p "$out_dir"
  local out="$out_dir/$pkg_name.tar.gz"
  tar -czf "$out" -C "$stage" "$pkg_name" || die "打包失败"
  local sum size
  sum="$(sha256_of "$out")"
  size="$(du -h "$out" | cut -f1)"
  printf '%s  %s\n' "$sum" "$(basename "$out")" >"$out.sha256"

  if [ "$keep_stage" -eq 1 ]; then
    info "临时目录已保留：$stage"
  else
    rm -rf "$stage"
  fi

  local base
  base="$(basename "$out")"
  info ""
  ok "打包完成"
  info ""
  info "  文件   : $out"
  info "  大小   : $size"
  info "  sha256 : $sum"
  info "  校验单 : $base.sha256"
  info ""
  info "${C_BOLD}上传到服务器${C_RESET}"
  info "  scp $out $out.sha256 user@服务器:/tmp/"
  info "  ${C_DIM}# 或： rsync -avP $out $out.sha256 user@服务器:/tmp/${C_RESET}"
  info ""
  info "${C_BOLD}服务器上${C_RESET}"
  info "  cd /tmp && sha256sum -c $base.sha256        # 可选，校验完整性"
  info "  sudo mkdir -p /opt && sudo tar -xzf $base -C /opt"
  info "  cd /opt/$pkg_name && sudo chown -R \"\$USER\" . && chmod +x run.sh"
  info "  cat PACKAGE-INFO.txt                        # 确认包内容"
  info "  ./run.sh doctor                             # 先自检"
  info "  ./run.sh setup                              # 建号 + 启动"
  info ""
  info "  非交互部署："
  info "    ADMIN_USERNAME=me ADMIN_PASSWORD='...' ./run.sh setup"
  info "  常驻服务："
  info "    sudo ./run.sh systemd --install"
  info "  通过 SSH 隧道访问："
  info "    ssh -N -L 8080:127.0.0.1:8080 user@服务器"
  info "    然后打开 http://127.0.0.1:8080/admin/"
  info ""
  info "  更完整的说明见包内 DEPLOY.md"
}

# ---------- setup：首次部署一条龙 ----------

setup() {
  banner
  require_python
  info ""

  step "1/3 环境自检"
  doctor || { info ""; warn "自检发现问题，请先处理"; return 1; }

  info ""
  step "2/3 FFmpeg"
  if ffmpeg_state >/dev/null 2>&1; then
    ok "已就绪，跳过"
  else
    info "未检测到，尝试下载静态构建到 bin/（离线环境请改用 tools/fetch_ffmpeg.py --file）"
    if ! install_ffmpeg; then
      warn "FFmpeg 安装失败，继续——后台仍可正常使用，只是没有缩略图与视频封面"
    fi
  fi

  info ""
  step "3/3 启动服务"
  start "$@"
}

# ---------- 入口 ----------

usage() {
  cat <<'USAGE'
Photography 一键运行脚本

  ./run.sh                 启动（等价于 start）
  ./run.sh setup           首次部署一条龙：自检 + 装 FFmpeg + 建账号 + 启动
  ./run.sh start           启动后台服务（后台运行，日志写入 .run/admin.log）
  ./run.sh stop            停止
  ./run.sh restart         重启
  ./run.sh status          查看运行状态
  ./run.sh logs [-f]       查看日志（-f 持续跟踪）
  ./run.sh doctor          环境自检（Python / FFmpeg / 权限 / JSON / 端口）
  ./run.sh install-ffmpeg  下载 FFmpeg 静态构建到 bin/（随项目打包到服务器）
  ./run.sh ffmpeg-status   查看当前使用的是哪个 ffmpeg
  ./run.sh package         打包迁移到服务器：dist/photography-<平台>-<时间>.tar.gz
  ./run.sh create-user     创建管理员账号
  ./run.sh reset-password  重置管理员密码
  ./run.sh systemd         [--install] 生成/安装 systemd 常驻服务
  ./run.sh help            显示本帮助

可选参数（start / restart）：
  --port 9000 --host 0.0.0.0 --session-hours 8 --foreground

打包参数（package）：
  --platform linux-x64     目标平台，默认 linux-x64
                           （可选 linux-arm64、linux-armhf、windows-x64、darwin-x64）
  --out DIR                输出目录，默认 dist/
  --no-ffmpeg              不打包 FFmpeg（服务器自行提供，包最小）
  --with-config            连 admin.config.json 一起打包（沿用现有账号密码）
  --keep-stage             保留组装用的临时目录，便于排查

可用环境变量：
  PORT=8080  HOST=127.0.0.1  SESSION_HOURS=12  PYTHON=python3
  ADMIN_PASSWORD=...   非交互创建账号时的密码（至少 8 位）
  ADMIN_USERNAME=...   非交互创建账号时的用户名（默认 admin）
  FFMPEG_URL=...       指定 FFmpeg 下载地址（内网镜像）

非交互部署：
  ADMIN_USERNAME=me ADMIN_PASSWORD='你的密码' ./run.sh setup
USAGE
}

main() {
  local command="${1:-start}"
  shift || true

  case "$command" in
    setup)          setup "$@" ;;
    start|run|"")   start "$@" ;;
    stop)           stop "$@" ;;
    restart)        restart "$@" ;;
    status|ps)      status "$@" ;;
    logs|log)       logs "$@" ;;
    doctor|check)   doctor "$@" ;;
    install-ffmpeg|ffmpeg) install_ffmpeg "$@" ;;
    ffmpeg-status)  require_python; py admin.py --ffmpeg-status ;;
    package|pack|dist) package "$@" ;;
    create-user)    create_user "$@" ;;
    reset-password) reset_password "$@" ;;
    systemd)        systemd "$@" ;;
    help|-h|--help) usage ;;
    *)              err "未知命令：$command"; info ""; usage; exit 2 ;;
  esac
}

main "$@"
