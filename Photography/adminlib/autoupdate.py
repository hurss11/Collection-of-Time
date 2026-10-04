"""代码更新：把服务器上的**代码**换到最新，绝不碰内容。

服务器目录里混着两类东西：

- **代码**：仓库跟踪、应该随新版本整体替换 —— 由 `CODE_PATHS` 划定范围。注意
  `assets/css`、`assets/js` 也在这里面：`assets/` 是混合目录，别把它整个当内容；
- **本地状态**：`data/*.json`、`admin.config.json`、`bin/`、`.run/`，以及 `assets/` 里
  **上传的内容**（`assets/img/photos/**`、`assets/video/posters/**`、上传的视频文件）
  —— 这是站点的真实内容，任何更新都不允许覆盖它。

本目录**不一定就是仓库根**：`<仓库根>/Photography` 这种布局（本仓库自己的布局）里 git 根在
上一层。所以「是不是 git 检出」与「哪些文件算代码」都得按 git 报出来的仓库根对齐
（`work_tree_root()` / `app_prefix()`），不要写死 `ROOT/.git`。

所以更新不是 `git pull`，而是「**按路径**把代码换过去」：

    git checkout <新版本> -- <CODE_PATHS>

**更新默认是手动的**（`./run.sh update`）。这样安排是有意的：单人小站上，「推任何东西
服务器就自己换代码并重启」的收益只是省下一次登录，代价却是把 `git push` 从「记录代码」
变成「在服务器上执行代码」——手机上改一行、误推一次、合错分支，站点都会无声地变掉。
定时器（`./run.sh update-check on`）因此**只检查、只提醒，绝不换代码**；要更新还是
自己跑一条命令。

设计取舍（都写进了 DEPLOY.md「更新代码」一节）：

1. **只快进**：本地历史与远端分叉就拒绝，绝不自动 merge / rebase；
2. **只换代码路径**：范围外的文件一个字节都不动（新增不会带进来，删除也不会删）；
   代码路径内的删除会落实，避免留下没人再引用的旧模块；
3. **作者白名单**：新提交的作者必须在 `.run/autoupdate.authors` 里。首次安装时用
   当时的 `origin/main` 作者快照作为初始信任集，之后只增不减；
4. **远端固定**：地址与安装时记录的一致才继续（防误配 / 被换源）；
5. **可回滚**：`.run/autoupdate.state` 记录已应用版本与最近几个版本，回滚同样只换
   代码路径，数据不受影响；
6. **不打断上传**：`./run.sh update` 重启前先看 `.run/activity.json`（服务端写的
   正在上传计数），上传中默认推迟到下一轮。

安全边界要说清：**能往 `main` 推代码的人，等于能在这台服务器上执行代码**。
白名单、只快进、只换代码路径这些措施防的是「误配 / 意外覆盖 / 第三方扫到仓库」，
防不了仓库本身被入侵。所以更进一层的答案是：**别让更新自动发生**。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

BRANCH = "main"
REMOTE = "origin"

# 代码路径白名单：新版本会整体替换这些路径，其余一律视为站点内容 / 配置。
# 目录用相对路径，匹配时按前缀（`adminlib` 覆盖 `adminlib/**`）。
CODE_PATHS = (
    "admin.py",
    "serve.py",
    "run.sh",
    "index.html",
    "favicon.svg",
    "README.md",
    "DEPLOY.md",
    "admin",
    "adminlib",
    "tools",
    "assets/css",
    "assets/js",
)

# 只改这些文件就不重启：文档跟着代码一起更新，但没必要为一句 README 打断正在看
# 页面的人（重启会断掉正在进行的下载与上传，虽然上传中会推迟，见 busy_uploads）。
DOC_PATHS = ("README.md", "DEPLOY.md")

STATE_PATH = ROOT / ".run" / "autoupdate.state"
AUTHORS_PATH = ROOT / ".run" / "autoupdate.authors"
REMOTE_PATH = ROOT / ".run" / "autoupdate.remote"
ACTIVITY_PATH = ROOT / ".run" / "activity.json"
PENDING_PATH = ROOT / ".run" / "update-pending.json"   # 定时检查发现的新版本（只提醒用）

GIT_TIMEOUT = 300
STATE_KEEP = 5               # 状态文件里保留最近几个已应用版本（供连续回滚）
DEFAULT_INTERVAL = "5min"
SERVICE_UNIT = "photography-admin.service"      # 要重启的那个服务
UPDATE_CHECK_SERVICE = "photography-update-check.service"   # 定时检查（只提醒，不执行）
UPDATE_CHECK_TIMER = "photography-update-check.timer"
# 早期版本装过一次的单元名（当时还会自动应用），卸载时一并清掉
LEGACY_UNITS = ("photography-update.service", "photography-update.timer")
UPDATE_SERVICE = UPDATE_CHECK_SERVICE           # 兼容旧引用
UPDATE_TIMER = UPDATE_CHECK_TIMER


class GitError(RuntimeError):
    """git 调用失败。"""


# ---------------------------------------------------------------- git 封装


def git_available() -> bool:
    return shutil.which("git") is not None


def _git(*args: str, check: bool = True, timeout: int = GIT_TIMEOUT) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )
    if check and proc.returncode != 0:
        lines = [line.strip() for line in (proc.stderr or proc.stdout or "").splitlines() if line.strip()]
        raise GitError(f"git {' '.join(args)} 失败：{lines[-1] if lines else f'退出码 {proc.returncode}'}")
    return proc.stdout


def _git_lines(*args: str, **kwargs) -> list[str]:
    return [line for line in _git(*args, **kwargs).splitlines() if line.strip()]


def work_tree_root() -> Path | None:
    """git 工作树根；ROOT 不在任何仓库里时返回 None。

    仓库根可能在本目录的**上一层**（`<仓库根>/Photography`，本仓库自己的布局），
    git 会自己向上找到它 —— 判定「是不是 git 检出」只问 git，别看 `ROOT/.git`
    （那个目录只在这种布局下存在）。
    """
    if not git_available():
        return None
    try:
        top = _git("rev-parse", "--show-toplevel").strip()
    except (GitError, OSError):
        return None
    root = Path(top) if top else None
    return root if root and root.is_dir() else None


def app_prefix() -> str:
    """ROOT 相对工作树根的路径前缀（`""` 表示 ROOT 就是仓库根，例如 `"Photography"`）。

    有些 git 命令给的是**相对仓库根**的路径（`diff --name-status`、`status --porcelain`），
    而这里一律按「相对 ROOT」理解：不削掉这层前缀，代码文件会被当成内容 ——
    更新看着成功，其实一个文件都没换。
    """
    if not git_available():
        return ""
    try:
        return _git("rev-parse", "--show-prefix").strip().strip("/")
    except (GitError, OSError):
        return ""


def is_repo() -> bool:
    return work_tree_root() is not None


def remote_url() -> str:
    try:
        return _git("remote", "get-url", REMOTE).strip()
    except GitError:
        return ""


def head_sha() -> str:
    try:
        return _git("rev-parse", "HEAD").strip()
    except GitError:
        return ""


# ---------------------------------------------------------------- 状态文件


def _read_json(path: Path, fallback):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback
    return data if isinstance(data, type(fallback)) else fallback


def _write_json(path: Path, payload) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def state() -> dict:
    data = _read_json(STATE_PATH, {})
    return {
        "applied": str(data.get("applied") or ""),
        "history": [str(item) for item in (data.get("history") or []) if item][:STATE_KEEP],
        "at": data.get("at") or "",
        "branch": data.get("branch") or BRANCH,
        "remote": data.get("remote") or "",
    }


def save_state(*, applied: str, history: list[str], branch: str, remote: str) -> dict:
    payload = {
        "applied": applied,
        "history": history[:STATE_KEEP],
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "branch": branch,
        "remote": remote,
    }
    _write_json(STATE_PATH, payload)
    return state()


def trusted_authors() -> set[str]:
    data = _read_json(AUTHORS_PATH, {})
    emails = data.get("emails") or []
    return {str(email).strip().lower() for email in emails if str(email).strip()}


def remember_authors(emails) -> set[str]:
    known = trusted_authors() | {str(email).strip().lower() for email in emails if str(email).strip()}
    _write_json(AUTHORS_PATH, {"emails": sorted(known), "at": time.strftime("%Y-%m-%d %H:%M:%S")})
    return known


def locked_remote() -> str:
    return str(_read_json(REMOTE_PATH, {}).get("url") or "")


def lock_remote(url: str) -> None:
    if url:
        _write_json(REMOTE_PATH, {"url": url, "at": time.strftime("%Y-%m-%d %H:%M:%S")})


# ---------------------------------------------------------------- 待更新提醒


def pending() -> dict:
    """上次定时检查发现的待更新版本（没有就是空字典）。"""
    return _read_json(PENDING_PATH, {})


def clear_pending() -> None:
    try:
        PENDING_PATH.unlink()
    except OSError:
        pass


def record_pending(info: dict) -> dict:
    """把「有新版本」写到 `.run/update-pending.json`，供 `./run.sh status` 提醒；没有就清掉。"""
    behind = info.get("behind") or 0
    if info.get("state") in ("update-available", "blocked") and behind:
        payload = {
            "behind": behind,
            "head": info.get("head", ""),
            "remote": info.get("remote_sha", ""),
            "remote_short": info.get("remote_short", ""),
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "commits": [
                {key: commit.get(key) for key in ("short", "date", "author", "subject")}
                for commit in (info.get("commits") or [])[:10]
            ],
            "blockers": list(info.get("blockers") or []),
        }
        _write_json(PENDING_PATH, payload)
        return payload
    clear_pending()
    return {}


# ---------------------------------------------------------------- 上传活动


def _pid_alive(pid: int) -> bool:
    """进程是否还在。Windows 上不能用 os.kill(pid, 0)（会真的杀掉进程）。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def activity() -> dict:
    """服务端写下的「正在上传」快照。文件过期（进程已换）就当空闲。"""
    data = _read_json(ACTIVITY_PATH, {})
    if not data:
        return {}
    pid = data.get("pid")
    if isinstance(pid, int) and not _pid_alive(pid):
        return {}
    return data


def busy_uploads() -> int:
    try:
        return max(0, int(activity().get("uploads") or 0))
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------- 路径与差异


def _in_code(path: str) -> bool:
    path = path.strip()
    return any(path == entry or path.startswith(entry + "/") for entry in CODE_PATHS)


def _code_status_lines(*extra: str) -> list[str]:
    """`git status --porcelain` 里与代码路径有关的行，路径统一削成「相对 ROOT」。

    porcelain 给的是相对仓库根的路径，而调用方（包括打印给用户照抄的
    `git checkout -- <路径>`）都按相对 ROOT 来理解，所以在这里削掉前缀。
    """
    lines = _git_lines("status", "--porcelain", *extra, "--", *CODE_PATHS)
    prefix = app_prefix()
    if not prefix:
        return lines
    return [line[:3] + line[3:][len(prefix) + 1:]
            for line in lines if line[3:].startswith(prefix + "/")]


def _split_changes(base: str, target: str) -> dict:
    """base → target 的改动，按代码 / 内容分开（路径相对 ROOT）。"""
    code: list[dict] = []
    content: list[dict] = []
    deleted: list[str] = []
    # --relative：路径按当前目录（ROOT）给，顺带排除 ROOT 之外的改动 ——
    # 「ROOT 就是仓库根」与「仓库根在上一层」两种情况因此得到同一套路径
    for line in _git_lines("diff", "--name-status", "--relative", base, target):
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status, path = parts[0].strip(), parts[-1].strip()
        if _in_code(path):
            code.append({"status": status, "path": path})
            if status.startswith("D"):
                deleted.append(path)
        else:
            content.append({"status": status, "path": path})
    return {"code": code, "content": content, "deleted": deleted}


def pending_commits(base: str, target: str) -> list[dict]:
    fmt = "%H%x09%an%x09%ae%x09%ad%x09%s"
    commits = []
    for line in _git_lines("log", f"--format={fmt}", "--date=short", f"{base}..{target}"):
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        commits.append({
            "sha": parts[0], "short": parts[0][:8], "author": parts[1],
            "email": parts[2], "date": parts[3], "subject": "\t".join(parts[4:]),
        })
    commits.reverse()          # 按提交时间从旧到新，方便阅读
    return commits


# ---------------------------------------------------------------- 检查


def inspect(*, fetch: bool = True, branch: str = BRANCH) -> dict:
    """检查有没有新版本，返回结构化结果（不修改任何东西）。"""
    info: dict = {
        "root": str(ROOT), "branch": branch, "state": "unknown", "blockers": [],
        "remote": "", "head": "", "head_short": "", "remote_sha": "", "remote_short": "",
        "behind": 0, "ahead": 0, "commits": [], "code": [], "content": [], "deleted": [],
        "new_authors": [], "dirty_code": [], "untracked_code": [], "busy": busy_uploads(),
        "activity": activity(),
    }

    if not git_available():
        info["state"] = "no-git"
        info["blockers"].append("系统里没有 git")
        return info
    if not is_repo():
        info["state"] = "not-a-repo"
        info["blockers"].append(f"{ROOT} 不是 git 检出（用迁移包解压的部署就是这种）")
        return info

    info["remote"] = remote_url()
    if not info["remote"]:
        info["state"] = "no-remote"
        info["blockers"].append(f"没有名为 {REMOTE} 的远端")
        return info

    expected = locked_remote()
    if expected and expected != info["remote"]:
        info["state"] = "blocked"
        info["blockers"].append(f"远端地址与安装时记录的不一致：现在是 {info['remote']}，记录的是 {expected}")
        return info

    if fetch:
        try:
            _git("fetch", "--prune", "--quiet", REMOTE, branch)
        except (GitError, OSError) as exc:
            info["state"] = "fetch-failed"
            info["blockers"].append(f"拉取远端失败：{exc}")
            return info

    info["head"] = head_sha()
    try:
        info["remote_sha"] = _git("rev-parse", f"{REMOTE}/{branch}").strip()
    except GitError:
        info["state"] = "no-branch"
        info["blockers"].append(f"远端没有分支 {branch}")
        return info
    info["head_short"] = info["head"][:8]
    info["remote_short"] = info["remote_sha"][:8]

    if not info["head"]:
        info["state"] = "blocked"
        info["blockers"].append("本地还没有提交（HEAD 为空）")
        return info

    info["behind"] = int(_git("rev-list", "--count", f"HEAD..{REMOTE}/{branch}").strip() or 0)
    info["ahead"] = int(_git("rev-list", "--count", f"{REMOTE}/{branch}..HEAD").strip() or 0)

    if info["ahead"]:
        info["state"] = "diverged"
        info["blockers"].append(f"本地有 {info['ahead']} 个远端没有的提交（分叉），不自动处理")
        return info
    if info["behind"] == 0:
        info["state"] = "unchanged"
        return info

    info["commits"] = pending_commits("HEAD", f"{REMOTE}/{branch}")
    changes = _split_changes("HEAD", f"{REMOTE}/{branch}")
    info["code"] = changes["code"]
    info["content"] = changes["content"]
    info["deleted"] = changes["deleted"]

    known = trusted_authors()
    info["new_authors"] = sorted({commit["email"].lower() for commit in info["commits"]} - known)

    # 只看**已跟踪**文件的改动：未跟踪的多半是运行时产物（`__pycache__` 之类），
    # 更新根本不会碰它们；把它们也算成「本地改过」会让更新永远被自己拦下。
    info["dirty_code"] = _code_status_lines("--untracked-files=no")
    info["untracked_code"] = [
        line[3:] for line in _code_status_lines()
        if line.startswith("??") and "__pycache__" not in line and not line.endswith(".pyc")
    ]

    if info["new_authors"]:
        info["blockers"].append("有未受信任的作者：" + ", ".join(info["new_authors"]))
    if info["dirty_code"]:
        info["blockers"].append(f"服务器上有 {len(info['dirty_code'])} 个代码文件被本地改过，会被更新覆盖")

    info["state"] = "blocked" if info["blockers"] else "update-available"
    return info


# ---------------------------------------------------------------- 应用


def _sync_code(target: str, *, base: str | None = None) -> list[str]:
    """把代码路径换成 target 版本；返回实际改动的路径。"""
    base = base or head_sha()
    changed = sorted({entry["path"] for entry in _split_changes(base, target)["code"]})

    to_delete = [path for path in changed if not _tree_has(target, path)]
    if to_delete:
        _git("rm", "-q", "-f", "--ignore-unmatch", "--", *to_delete)
    # 代码路径里不存在的条目不能直接交给 checkout（pathspec 不匹配会整体失败）
    existing = [path for path in _tree_files(target) if _in_code(path)]
    if existing:
        _git("checkout", target, "--", *existing)
    return changed


def _tree_files(target: str) -> list[str]:
    return _git_lines("ls-tree", "-r", "--name-only", target)


def _tree_has(target: str, path: str) -> bool:
    return bool(_git("ls-tree", "-r", "--name-only", target, "--", path).strip())


def protect_local_state() -> int:
    """把代码路径之外、仓库里跟踪的文件标记成「本地状态」：更新一律不看、不动。"""
    tracked = _git_lines("ls-files")
    protected = [path for path in tracked if not _in_code(path)]
    if not protected:
        return 0
    _git("update-index", "--skip-worktree", "--", *protected)
    return len(protected)


def apply(*, branch: str = BRANCH, accept_authors: bool = False,
          allow_dirty: bool = False, fetch: bool = True) -> dict:
    """拉取并把代码换成新版本。返回结果字典（`applied` 表示是否有实际改动）。"""
    info = inspect(fetch=fetch, branch=branch)
    if info["state"] == "unchanged":
        return {**info, "applied": False, "restart_needed": False, "reason": "已是最新版本"}
    if info["state"] not in ("update-available", "blocked"):
        return {**info, "applied": False, "restart_needed": False, "reason": info["blockers"][0] if info["blockers"] else info["state"]}

    blockers = list(info["blockers"])
    if info["new_authors"] and accept_authors:
        blockers = [item for item in blockers if "未受信任的作者" not in item]
    if info["dirty_code"] and allow_dirty:
        blockers = [item for item in blockers if "被本地改过" not in item]
    if blockers:
        return {**info, "applied": False, "restart_needed": False, "reason": "；".join(blockers)}

    target = info["remote_sha"]
    previous = info["head"]
    try:
        changed = _sync_code(target, base=previous)
        _git("update-ref", f"refs/heads/{branch}", target)
    except (GitError, OSError) as exc:
        return {**info, "applied": False, "restart_needed": False, "reason": f"应用失败：{exc}"}

    protect_local_state()
    old = state()
    history = [previous, *[item for item in old["history"] if item not in (previous, target)]]
    save_state(applied=target, history=history, branch=branch, remote=info["remote"])
    lock_remote(info["remote"])
    clear_pending()               # 已经更新过了，提醒可以撤掉
    if info["new_authors"]:
        remember_authors(info["new_authors"])

    code_changed = any(entry["path"] not in DOC_PATHS for entry in info["code"])
    return {
        **info,
        "applied": True,
        "restart_needed": code_changed,
        "changed": changed,
        "reason": "" if code_changed else "本次发布只改了文档或数据，不需要重启",
    }


def rollback(*, branch: str = BRANCH) -> dict:
    """回到上一个已应用的版本（同样只换代码路径）。"""
    current = state()
    if not is_repo():
        return {"ok": False, "reason": "不是 git 检出，无法回滚"}
    history = list(current["history"])
    if not history:
        return {"ok": False, "reason": "没有可回滚的版本记录"}
    target = history[0]
    if not _tree_has(target, "admin.py"):
        return {"ok": False, "reason": f"回滚目标 {target[:8]} 不在本地仓库里（历史被裁剪过？）"}
    try:
        changed = _sync_code(target, base=current["applied"] or head_sha())
        _git("update-ref", f"refs/heads/{branch}", target)
    except (GitError, OSError) as exc:
        return {"ok": False, "reason": f"回滚失败：{exc}"}
    protect_local_state()
    save_state(applied=target, history=history[1:], branch=branch, remote=current["remote"])
    return {"ok": True, "applied": target, "from": current["applied"], "changed": changed,
            "reason": f"已回滚到 {target[:8]}"}


def adopt(url: str, *, branch: str = BRANCH) -> dict:
    """把「解压迁移包」的部署目录就地变成 git 检出（不覆盖任何本地文件）。"""
    if not git_available():
        return {"ok": False, "reason": "系统里没有 git"}
    root = work_tree_root()
    if root is not None:
        where = "" if root == ROOT else f"（仓库根在上层：{root}）"
        return {"ok": False,
                "reason": f"{ROOT} 已经在 git 检出内{where}，不用再来一次；直接 ./run.sh update"}
    if not (ROOT / "admin.py").is_file() or not (ROOT / "run.sh").is_file():
        return {"ok": False, "reason": f"{ROOT} 看起来不是 Photography 项目目录（缺 admin.py / run.sh）"}

    try:
        _git("init", "--quiet")
        _git("remote", "add", REMOTE, url)
        _git("fetch", "--prune", "--quiet", REMOTE, branch)
        target = _git("rev-parse", f"{REMOTE}/{branch}").strip()
        # 只移动分支指针，不碰工作区：data/ 与上传内容保持原样
        _git("update-ref", f"refs/heads/{branch}", target)
        _git("symbolic-ref", "HEAD", f"refs/heads/{branch}")
        _git("reset", "--quiet")
        changed = _sync_code(target)
    except (GitError, OSError) as exc:
        return {"ok": False, "reason": f"接管失败：{exc}"}

    protected = protect_local_state()
    authors = sorted({commit["email"].lower() for commit in pending_commits("HEAD", target)} or
                     {email.strip().lower() for email in _git_lines("log", "--format=%ae", "-n", "50", target) if email.strip()})
    remember_authors(authors)
    lock_remote(url)
    save_state(applied=target, history=[], branch=branch, remote=url)
    return {"ok": True, "applied": target, "changed": changed, "protected": protected,
            "authors": authors, "remote": url,
            "reason": f"已接管：远端 {url}，代码对齐到 {target[:8]}，{protected} 个内容文件被标记为本地状态"}


# ---------------------------------------------------------------- systemd 单元

_INTERVAL_RE = re.compile(r"^(\d+)(s|sec|min|h|hour)?$")


def normalize_interval(text: str) -> str:
    """把 5 / 5min / 300s / 1h 归一成 systemd 认识的值。"""
    match = _INTERVAL_RE.match((text or "").strip().lower())
    if not match:
        raise ValueError(f"时间间隔写法不认识：{text!r}（可用 5min / 300s / 1h）")
    amount, unit = match.group(1), match.group(2) or "min"
    unit = {"sec": "s", "hour": "h"}.get(unit, unit)
    if int(amount) <= 0:
        raise ValueError("时间间隔要大于 0")
    return f"{amount}{unit}"


def update_condition_dir() -> Path:
    """定时检查单元的 ConditionPathIsDirectory：不在 git 检出里就跳过。

    条件是**仓库根**而不是 `ROOT/.git` —— 部署目录是仓库子目录时后者不存在，
    定时器会被 systemd 静默跳过（看着装了、其实永远不检查）。
    """
    return work_tree_root() or (ROOT / ".git")


def update_service_unit() -> str:
    return f"""[Unit]
Description=Collection of Time - 检查是否有新代码（只提醒，不执行更新）
After=network-online.target
Wants=network-online.target
# 不在 git 检出里（用迁移包解压的部署）就不必反复失败，直接跳过
ConditionPathIsDirectory={update_condition_dir()}

[Service]
Type=oneshot
WorkingDirectory={ROOT}
# 注意：这里是 --check —— 只拉取远端信息并写进 .run/update-pending.json，
# 不会替换任何代码、不会重启服务。要更新请手动执行 ./run.sh update
ExecStart={ROOT}/run.sh update --check --quiet
TimeoutStartSec=300
Nice=10
"""


def update_timer_unit(interval: str = DEFAULT_INTERVAL) -> str:
    every = normalize_interval(interval)
    return f"""[Unit]
Description=Collection of Time - 每 {every} 检查一次有没有新代码（只提醒）

[Timer]
# 开机 3 分钟后再开始（先把服务起好），之后每 {every} 检查一次
OnBootSec=3min
OnUnitActiveSec={every}
RandomizedDelaySec=60
AccuracySec=30
Persistent=true
Unit={UPDATE_CHECK_SERVICE}

[Install]
WantedBy=timers.target
"""


def write_units(out_dir: Path, interval: str = DEFAULT_INTERVAL) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, text in ((UPDATE_CHECK_SERVICE, update_service_unit()),
                       (UPDATE_CHECK_TIMER, update_timer_unit(interval))):
        path = out_dir / name
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return written


# ---------------------------------------------------------------- CLI


def _print_inspect(info: dict) -> None:
    state_label = {
        "unchanged": "已是最新版本",
        "update-available": "有新版本可更新",
        "not-a-repo": "不是 git 检出",
        "no-git": "系统里没有 git",
        "no-remote": "没有远端",
        "no-branch": "远端没有该分支",
        "diverged": "本地与远端分叉",
        "blocked": "被安全规则拦下",
        "fetch-failed": "拉取失败",
    }.get(info["state"], info["state"])

    print(f"目录     : {info['root']}")
    if info["remote"]:
        print(f"远端     : {info['remote']}")
    if info["head_short"]:
        print(f"本地版本 : {info['head_short']}")
    if info["remote_short"]:
        print(f"远端版本 : {info['remote_short']}")
    if info["state"] in ("unchanged", "update-available", "blocked"):
        print(f"落后     : {info['behind']} 个提交")
    print(f"状态     : {state_label}")
    if info["busy"]:
        print(f"正在上传 : {info['busy']} 个文件（更新会推迟到下一轮）")
    for commit in info["commits"]:
        print(f"  · {commit['short']} {commit['date']} {commit['author']} — {commit['subject']}")
    if info["code"]:
        print(f"代码改动 : {len(info['code'])} 个文件")
        for entry in info["code"]:
            print(f"    {entry['status']} {entry['path']}")
    if info["content"]:
        print(f"内容改动 : {len(info['content'])} 个（服务器上的是你的真实内容，更新会跳过这些路径）")
    if info["untracked_code"]:
        print(f"多出文件 : {len(info['untracked_code'])} 个（不拦更新，留意别和新版本重名）")
        for path in info["untracked_code"]:
            print(f"    ?? {path}")
    for blocker in info["blockers"]:
        print(f"拦下     : {blocker}")


def _print_check(info: dict) -> None:
    print("（本次只检查：只拉取远端信息，没有替换任何代码、没有重启服务）")
    _print_inspect(info)
    if info["state"] == "update-available":
        print("要更新就执行： ./run.sh update")
    elif info["state"] == "unchanged":
        print("无需操作。")


def _load(path: Path):
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m adminlib.autoupdate",
        description="代码更新：只换代码路径，不碰站点内容",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python -m adminlib.autoupdate check            只检查有没有新版本（会写提醒）\n"
               "  python -m adminlib.autoupdate inspect          检查（不写提醒）\n"
               "  python -m adminlib.autoupdate apply            应用更新（只改代码）\n"
               "  python -m adminlib.autoupdate rollback         回到上一个版本\n"
               "  python -m adminlib.autoupdate adopt --url URL  把迁移包部署接管成 git 检出\n"
               "  python -m adminlib.autoupdate units --out DIR  写出 systemd 单元文件（只检查用的）\n",
    )
    parser.add_argument("command",
                        choices=("inspect", "check", "apply", "rollback", "adopt", "units", "busy", "state", "show"),
                        help="要做的动作（check = 只检查并写提醒；show 读标准输入的 JSON 并渲染）")
    parser.add_argument("--json", action="store_true", help="输出 JSON（给脚本用）")
    parser.add_argument("--branch", default=BRANCH, help=f"分支，默认 {BRANCH}")
    parser.add_argument("--url", default="", help="adopt 用：远端仓库地址")
    parser.add_argument("--interval", default=DEFAULT_INTERVAL, help=f"units 用：检查间隔，默认 {DEFAULT_INTERVAL}")
    parser.add_argument("--out", default="", help="units 用：单元文件写到哪里（留空则打印到标准输出）")
    parser.add_argument("--accept-authors", action="store_true", help="apply 用：把本次提交的作者加入白名单")
    parser.add_argument("--allow-dirty", action="store_true", help="apply 用：允许覆盖被本地改过的代码文件")
    parser.add_argument("--no-fetch", action="store_true", help="inspect 用：不联网，只看本地已知的远端状态")
    args = parser.parse_args(argv)

    if args.command == "busy":
        count = busy_uploads()
        if args.json:
            print(json.dumps({"busy": count, "activity": activity()}, ensure_ascii=False))
        if count:
            print(f"正在上传 {count} 个文件", file=sys.stderr)
            return 0
        return 1

    if args.command == "state":
        print(json.dumps(state(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "show":
        try:
            payload = json.load(sys.stdin)
        except (ValueError, OSError):
            payload = None
        if not isinstance(payload, dict):
            print("show 需要从标准输入读一个 JSON 对象", file=sys.stderr)
            return 2
        _print_inspect(payload)
        return 0

    if args.command == "inspect":
        info = inspect(fetch=not args.no_fetch, branch=args.branch)
        if args.json:
            print(json.dumps(info, ensure_ascii=False))
        else:
            _print_inspect(info)
        if info["state"] in ("unchanged", "update-available"):
            return 0
        if info["state"] == "fetch-failed":
            return 1
        print(info["blockers"][0] if info["blockers"] else info["state"], file=sys.stderr)
        return 3

    if args.command == "check":
        # 只检查：除了 git fetch（写 .git 内部）之外不改任何东西，
        # 结果写进 .run/update-pending.json 供 ./run.sh status 提醒。
        info = inspect(fetch=not args.no_fetch, branch=args.branch)
        if args.json:
            print(json.dumps(info, ensure_ascii=False))
        else:
            _print_check(info)
        record_pending(info)
        return 1 if info["state"] == "fetch-failed" else 0

    if args.command == "apply":
        result = apply(branch=args.branch, accept_authors=args.accept_authors, allow_dirty=args.allow_dirty)
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            _print_inspect(result)
            print(f"结果     : {'已更新' if result['applied'] else '未更新'} — {result['reason']}")
        if result["applied"]:
            return 0
        return 0 if result["state"] == "unchanged" else 3

    if args.command == "rollback":
        result = rollback(branch=args.branch)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("ok") else 1

    if args.command == "adopt":
        if not args.url:
            print("adopt 需要 --url <仓库地址>", file=sys.stderr)
            return 2
        result = adopt(args.url, branch=args.branch)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("ok") else 1

    # units
    try:
        every = normalize_interval(args.interval)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.out:
        written = write_units(Path(args.out), every)
        print(json.dumps({"ok": True, "written": [str(path) for path in written], "interval": every},
                         ensure_ascii=False))
        return 0
    print(f"# ==== {UPDATE_SERVICE} ====")
    print(update_service_unit())
    print(f"# ==== {UPDATE_TIMER} ====")
    print(update_timer_unit(every))
    return 0


if __name__ == "__main__":
    sys.exit(main())
