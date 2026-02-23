import os
import re
import sys
import shlex
import json
import time
import base64
import subprocess
from pathlib import Path
from datetime import datetime

# =========================
# Config
# =========================
REPO_DIR = Path("/content/work/agent-workspace")
LOG_DIR = REPO_DIR / "logs"
ARTIFACT_DIR = REPO_DIR / "artifacts"

LOG_DIR.mkdir(parents=True, exist_ok=True)
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

MAX_ROUNDS = 8
ROUND_TIMEOUT_SEC = 1800  # per codex/acceptance phase
SLEEP_BETWEEN_ROUNDS = 2

# 建議預設關閉自動 git 同步，避免每輪覆蓋模型剛做的修改
AUTO_GIT_SYNC = False
SYNC_EACH_ROUND = False

# 關鍵：不要用 --full-auto（它會走 Linux sandbox / Landlock）
# 直接使用無 approvals / 無 sandbox 模式，避免 Colab 的 Landlock 問題
DEFAULT_CODEX_FLAGS = "--dangerously-bypass-approvals-and-sandbox --ephemeral --json"
CODEX_FLAGS = shlex.split(DEFAULT_CODEX_FLAGS)
USE_CODEX_JSON = True

# 啟動時先做 Codex shell 自檢（建議開啟）
ENABLE_STARTUP_SELFCHECK = True

# =========================
# Git Auto Push Config
# =========================
AUTO_PUSH_ON_DONE = True
GIT_REMOTE_URL = "https://github.com/31182112Ask/agent-workspace.git"
GIT_BRANCH = "main"

# Git commit author（你提供的資訊）
GIT_AUTHOR_NAME = "021097xxx"
GIT_AUTHOR_EMAIL = "021097xxx@gmail.com"

# 直接寫死 token（請替換為你的實際 GitHub PAT）
GITHUB_TOKEN = "REPLACE_WITH_YOUR_GITHUB_PAT"

# 提交訊息前綴
GIT_COMMIT_PREFIX = "auto(task)"

# 不想自動 push 的路徑（避免把 logs/artifacts 一起推上去）
PUSH_EXCLUDE_PATHS = ["logs", "artifacts"]

TASK_FILE = REPO_DIR / "TASK.md"
AGENTS_FILE = REPO_DIR / "AGENTS.md"
STOP_FILE = REPO_DIR / ".stop"

SANDBOX_BLOCK_PATTERNS = [
    "Sandbox(LandlockRestrict)",
    "legacy Linux sandbox restrictions",
    "error applying legacy Linux sandbox restrictions",
]

# =========================
# Utility
# =========================
def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def run_capture(cmd, cwd=None, timeout=None):
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def run_stream(cmd, cwd=None, log_path=None, prefix="", env=None, shell=False, timeout=None):
    """
    即時串流輸出到 Colab，同步寫 log。
    回傳: (returncode, combined_output)
    """
    merged_env = os.environ.copy()
    merged_env["PYTHONUNBUFFERED"] = "1"
    if env:
        merged_env.update(env)

    if shell:
        popen_args = {
            "args": ["bash", "-lc", cmd],
            "cwd": str(cwd) if cwd else None,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "text": True,
            "bufsize": 1,
            "env": merged_env,
        }
    else:
        popen_args = {
            "args": cmd,
            "cwd": str(cwd) if cwd else None,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "text": True,
            "bufsize": 1,
            "env": merged_env,
        }

    p = subprocess.Popen(**popen_args)
    f = open(log_path, "w", encoding="utf-8") if log_path else None
    lines = []
    start_ts = time.time()

    try:
        while True:
            line = p.stdout.readline()
            if line:
                lines.append(line)
                sys.stdout.write(f"{prefix}{line}")
                sys.stdout.flush()
                if f:
                    f.write(line)
                    f.flush()

            if p.poll() is not None:
                remainder = p.stdout.read()
                if remainder:
                    for extra in remainder.splitlines(True):
                        lines.append(extra)
                        sys.stdout.write(f"{prefix}{extra}")
                        sys.stdout.flush()
                        if f:
                            f.write(extra)
                            f.flush()
                break

            if timeout is not None and (time.time() - start_ts) > timeout:
                sys.stdout.write(f"\n{prefix}[runner] timeout reached, terminating process...\n")
                sys.stdout.flush()
                try:
                    p.terminate()
                    p.wait(timeout=5)
                except Exception:
                    try:
                        p.kill()
                    except Exception:
                        pass
                break

    except KeyboardInterrupt:
        sys.stdout.write(f"\n{prefix}[runner] KeyboardInterrupt received, terminating child process...\n")
        sys.stdout.flush()
        try:
            p.terminate()
            p.wait(timeout=5)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
        raise
    finally:
        if f:
            f.close()

    return p.returncode if p.returncode is not None else 130, "".join(lines)


def file_contains_any(text: str, patterns) -> bool:
    t = text or ""
    return any(p in t for p in patterns)


# =========================
# Git helpers (non-destructive)
# =========================
def git_is_repo() -> bool:
    return (REPO_DIR / ".git").exists()


def git_has_uncommitted_changes() -> bool:
    if not git_is_repo():
        return False
    r = run_capture(["git", "status", "--porcelain"], cwd=REPO_DIR, timeout=30)
    return bool((r.stdout or "").strip())


def git_sync_non_destructive():
    """
    非破壞性同步：只在乾淨工作區執行 fetch + pull --ff-only
    """
    if not git_is_repo():
        return True, "No git repo; skip sync."

    if git_has_uncommitted_changes():
        return False, "Repo has uncommitted changes; skip auto-sync to avoid overwriting local progress."

    logs = []
    for cmd in [["git", "fetch", "origin"], ["git", "pull", "--ff-only", "origin", "main"]]:
        r = run_capture(cmd, cwd=REPO_DIR, timeout=120)
        logs.append(f"$ {' '.join(cmd)}\n{r.stdout}\n{r.stderr}")
        if r.returncode != 0:
            return False, "\n\n".join(logs)
    return True, "\n\n".join(logs)


def save_git_patch(round_idx: int):
    if not git_is_repo():
        return None
    r = run_capture(["git", "diff"], cwd=REPO_DIR, timeout=60)
    patch_path = LOG_DIR / f"round{round_idx:02d}-changes-{timestamp()}.patch"
    patch_path.write_text(r.stdout or "", encoding="utf-8")
    return patch_path


def print_repo_changes():
    """
    兼容新檔案（untracked）與已追蹤修改。
    """
    if not git_is_repo():
        print("[runner] not a git repo; skipping git change summary")
        return

    r_status = run_capture(["git", "status", "--short"], cwd=REPO_DIR, timeout=30)
    r_tracked = run_capture(["git", "diff", "--name-only"], cwd=REPO_DIR, timeout=30)
    r_untracked = run_capture(["git", "ls-files", "--others", "--exclude-standard"], cwd=REPO_DIR, timeout=30)

    print("[runner] git status --short")
    print((r_status.stdout or "").strip() or "(clean)")

    tracked = [x for x in (r_tracked.stdout or "").splitlines() if x.strip()]
    untracked = [x for x in (r_untracked.stdout or "").splitlines() if x.strip()]
    all_changed = []
    seen = set()
    for p in tracked + untracked:
        if p not in seen:
            seen.add(p)
            all_changed.append(p)

    print("[runner] changed files (tracked + untracked)")
    if all_changed:
        for p in all_changed:
            print(p)
    else:
        print("(none)")


def ensure_git_identity_and_remote():
    """
    設定 git 作者資訊與 origin remote（不把 token 寫進 remote URL）。
    """
    if not git_is_repo():
        return False, "Not a git repo"

    logs = []

    # 設定作者資訊
    for cmd in [
        ["git", "config", "user.name", GIT_AUTHOR_NAME],
        ["git", "config", "user.email", GIT_AUTHOR_EMAIL],
    ]:
        r = run_capture(cmd, cwd=REPO_DIR, timeout=30)
        logs.append(f"$ {' '.join(cmd)}\n{r.stdout}\n{r.stderr}")
        if r.returncode != 0:
            return False, "\n\n".join(logs)

    # 確保 origin 指向指定 repo
    r = run_capture(["git", "remote", "get-url", "origin"], cwd=REPO_DIR, timeout=30)
    if r.returncode != 0:
        r2 = run_capture(["git", "remote", "add", "origin", GIT_REMOTE_URL], cwd=REPO_DIR, timeout=30)
        logs.append(f"$ git remote add origin <repo-url>\n{r2.stdout}\n{r2.stderr}")
        if r2.returncode != 0:
            return False, "\n\n".join(logs)
    else:
        current = (r.stdout or "").strip()
        if current != GIT_REMOTE_URL:
            r2 = run_capture(["git", "remote", "set-url", "origin", GIT_REMOTE_URL], cwd=REPO_DIR, timeout=30)
            logs.append(f"$ git remote set-url origin <repo-url>\n{r2.stdout}\n{r2.stderr}")
            if r2.returncode != 0:
                return False, "\n\n".join(logs)

    return True, "\n\n".join(logs)


def git_commit_and_push(round_idx: int):
    """
    自動 commit 並 push 到 GitHub。
    token 直接來自常量 GITHUB_TOKEN。
    """
    if not git_is_repo():
        return False, "Not a git repo"

    ok, prep_log = ensure_git_identity_and_remote()
    push_log_path = LOG_DIR / f"round{round_idx:02d}-gitpush-{timestamp()}.log"
    if not ok:
        push_log_path.write_text(prep_log, encoding="utf-8")
        return False, f"Git prepare failed. See {push_log_path.name}"

    token = (GITHUB_TOKEN or "").strip()
    if not token or token == "REPLACE_WITH_YOUR_GITHUB_PAT":
        push_log_path.write_text(prep_log + "\n\nMissing/placeholder GITHUB_TOKEN", encoding="utf-8")
        return False, f"GITHUB_TOKEN is not set to a real value. See {push_log_path.name}"

    logs = [prep_log]

    # stage 所有變更
    r = run_capture(["git", "add", "-A"], cwd=REPO_DIR, timeout=60)
    logs.append(f"$ git add -A\n{r.stdout}\n{r.stderr}")
    if r.returncode != 0:
        push_log_path.write_text("\n\n".join(logs), encoding="utf-8")
        return False, f"git add failed. See {push_log_path.name}"

    # 取消 staging 不想推的目錄（logs/artifacts）
    for p in PUSH_EXCLUDE_PATHS:
        r = run_capture(["git", "reset", "-q", "HEAD", "--", p], cwd=REPO_DIR, timeout=30)
        if r.returncode != 0:
            r2 = run_capture(["git", "restore", "--staged", "--", p], cwd=REPO_DIR, timeout=30)
            logs.append(f"$ git restore --staged -- {p}\n{r2.stdout}\n{r2.stderr}")
        else:
            logs.append(f"$ git reset -q HEAD -- {p}\n{r.stdout}\n{r.stderr}")

    # 檢查 staged 變更
    r = run_capture(["git", "diff", "--cached", "--name-only"], cwd=REPO_DIR, timeout=30)
    staged_files = [x for x in (r.stdout or "").splitlines() if x.strip()]
    logs.append(f"$ git diff --cached --name-only\n{r.stdout}\n{r.stderr}")

    if not staged_files:
        push_log_path.write_text("\n\n".join(logs), encoding="utf-8")
        return True, f"No staged changes to push (logs/artifacts excluded). See {push_log_path.name}"

    # commit
    commit_msg = f"{GIT_COMMIT_PREFIX}: round {round_idx} done ({timestamp()})"
    r = run_capture(["git", "commit", "-m", commit_msg], cwd=REPO_DIR, timeout=120)
    logs.append(f"$ git commit -m <msg>\n{r.stdout}\n{r.stderr}")
    if r.returncode != 0:
        push_log_path.write_text("\n\n".join(logs), encoding="utf-8")
        return False, f"git commit failed. See {push_log_path.name}"

    # push（用 extraheader，不把 token 寫入 remote URL）
    auth_raw = f"x-access-token:{token}".encode("utf-8")
    auth_b64 = base64.b64encode(auth_raw).decode("utf-8")
    push_cmd = [
        "git",
        "-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {auth_b64}",
        "push",
        "-u", "origin", f"HEAD:{GIT_BRANCH}",
    ]
    r = run_capture(push_cmd, cwd=REPO_DIR, timeout=180)

    logs.append("[push] git push via http.extraheader (token hidden)")
    logs.append(f"[push stdout]\n{r.stdout}")
    logs.append(f"[push stderr]\n{r.stderr}")

    push_log_path.write_text("\n\n".join(logs), encoding="utf-8")

    if r.returncode != 0:
        return False, f"git push failed. See {push_log_path.name}"

    return True, f"Pushed to {GIT_REMOTE_URL} ({GIT_BRANCH}). See {push_log_path.name}"


# =========================
# TASK parsing
# =========================
def load_task() -> str:
    if not TASK_FILE.exists():
        raise FileNotFoundError(f"TASK.md not found: {TASK_FILE}")
    return TASK_FILE.read_text(encoding="utf-8")


def extract_acceptance_commands(task_text: str):
    """
    從 TASK.md 中抓 ## ACCEPTANCE 區塊內第一個 ```bash ... ``` code block
    """
    m = re.search(
        r"##\s*ACCEPTANCE\b.*?```bash\s*(.*?)```",
        task_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return None
    block = m.group(1).strip()
    return block or None


# =========================
# Codex event parsing
# =========================
def parse_coding_events_from_jsonl(jsonl_text: str):
    """
    解析 codex --json JSONL 事件流，輸出：
    - status_tag (DONE/CONTINUE/None)
    - event summary lines
    """
    status_tag = None
    event_lines = []

    for raw in (jsonl_text or "").splitlines():
        raw = raw.strip()
        if not raw:
            continue

        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            m = re.search(r"STATUS:\s*(DONE|CONTINUE)", raw, flags=re.I)
            if m:
                status_tag = m.group(1).upper()
            continue

        t = obj.get("type", "")

        if t in {"thread.started", "thread.completed", "turn.started", "turn.completed", "turn.failed", "error"}:
            event_lines.append(f"[{t}]")
            continue

        if t.startswith("item."):
            item = obj.get("item", {}) or {}
            item_type = item.get("type", "unknown")
            item_status = item.get("status", "")

            if item_type == "agent_message":
                text = (item.get("text") or "").replace("\n", " ")
                m = re.search(r"STATUS:\s*(DONE|CONTINUE)", text, flags=re.I)
                if m:
                    status_tag = m.group(1).upper()
                event_lines.append(f"[{t}] agent_message :: {text[:300]}")

            elif item_type == "reasoning":
                txt = (item.get("text") or "").replace("\n", " ")
                event_lines.append(f"[{t}] reasoning :: {txt[:180]}")

            elif item_type in {"command_execution", "command"}:
                cmd = item.get("command") or item.get("input") or "<command>"
                event_lines.append(f"[{t}] command_execution ({item_status}) :: {cmd}")

            elif item_type in {"file_change", "file_changes"}:
                paths = item.get("paths") or item.get("files") or item.get("file_paths") or []
                if isinstance(paths, list):
                    ptxt = ", ".join(map(str, paths)) if paths else "<paths-unknown>"
                else:
                    ptxt = str(paths)
                event_lines.append(f"[{t}] file_change ({item_status}) :: {ptxt}")

            else:
                event_lines.append(f"[{t}] {item_type} ({item_status})")

            continue

        event_lines.append(f"[{t or 'unknown'}]")

    return status_tag, event_lines


# =========================
# Codex startup self-check
# =========================
def codex_shell_selfcheck():
    """
    啟動前驗證 Codex 能在當前環境執行 shell 指令。
    """
    selfcheck_log = LOG_DIR / f"startup-codex-selfcheck-{timestamp()}.log"
    test_cmd = [
        "codex",
        "exec",
        "--dangerously-bypass-approvals-and-sandbox",
        "--ephemeral",
        "--json",
        "pwd && ls -la && test -f TASK.md && echo codex-shell-ok",
    ]
    print("[runner] startup self-check: codex shell access")
    rc, out = run_stream(
        test_cmd,
        cwd=REPO_DIR,
        log_path=selfcheck_log,
        prefix="[selfcheck] ",
        timeout=120,
    )
    ok = (rc == 0) and ("codex-shell-ok" in (out or ""))
    if not ok:
        print("[runner] Codex shell self-check failed. Stop early.")
        print(f"[runner] See log: {selfcheck_log.name}")
        sys.exit(1)
    print("[runner] startup self-check passed")


# =========================
# Core steps
# =========================
def codex_round(round_idx: int, feedback: str):
    prompt = f"""
Read TASK.md and complete ONE full implementation iteration.

Requirements:
- Read TASK.md carefully (goal + acceptance + constraints).
- Make code changes needed to move the task forward.
- Run relevant local checks/tests.
- If acceptance is not fully passing yet, provide precise diagnosis and continue progress.

Important response format (exact tags required):
STATUS: DONE or CONTINUE
SUMMARY:
- ...
CHANGED_FILES:
- ...
VALIDATION_COMMANDS:
- ...

Previous feedback from orchestrator:
{feedback or "(none)"}
""".strip()

    codex_log = LOG_DIR / f"round{round_idx:02d}-codex-{timestamp()}.log"
    cmd = ["codex", "exec"] + CODEX_FLAGS + [prompt]
    print(f"[runner] starting Codex round {round_idx}, log={codex_log.name}")

    rc, out = run_stream(
        cmd,
        cwd=REPO_DIR,
        log_path=codex_log,
        prefix="[codex] ",
        timeout=ROUND_TIMEOUT_SEC,
    )

    status_tag = None
    events = []
    events_log = None

    if USE_CODEX_JSON:
        status_tag, events = parse_coding_events_from_jsonl(out)
        events_log = LOG_DIR / f"round{round_idx:02d}-codex-events-{timestamp()}.log"
        events_log.write_text("\n".join(events), encoding="utf-8")
        print(f"[runner] codex events summary -> {events_log.name}")
        for e in events[-40:]:
            print(f"[event] {e}")
    else:
        m = re.search(r"STATUS:\s*(DONE|CONTINUE)", out, flags=re.I)
        if m:
            status_tag = m.group(1).upper()

    sandbox_blocked = file_contains_any(out, SANDBOX_BLOCK_PATTERNS)

    return {
        "rc": rc,
        "status_tag": status_tag,
        "raw_output": out,
        "codex_log": codex_log,
        "events_log": events_log,
        "sandbox_blocked": sandbox_blocked,
    }


def run_acceptance(task_text: str, round_idx: int):
    cmds = extract_acceptance_commands(task_text)
    if not cmds:
        return None, "No ACCEPTANCE bash block found in TASK.md", None

    wrapped_cmds = "set -euo pipefail\n" + cmds.strip() + "\n"

    acc_log = LOG_DIR / f"round{round_idx:02d}-acceptance-{timestamp()}.log"
    print(f"[runner] running acceptance, log={acc_log.name}")
    rc, out = run_stream(
        wrapped_cmds,
        cwd=REPO_DIR,
        log_path=acc_log,
        prefix="[acc] ",
        shell=True,
        timeout=ROUND_TIMEOUT_SEC,
    )

    ok = (rc == 0)
    tail = out[-5000:] if out else ""
    return ok, f"Acceptance log: {acc_log.name}\n{tail}", acc_log


def create_done_marker(round_idx: int):
    done_mark = ARTIFACT_DIR / "DONE.txt"
    done_mark.write_text(
        f"Completed at {datetime.now().isoformat()}\nRound={round_idx}\n",
        encoding="utf-8",
    )
    return done_mark


# =========================
# Main loop
# =========================
def main():
    print(f"[runner] REPO_DIR={REPO_DIR}")
    if not REPO_DIR.exists():
        print("[runner] ERROR: repo directory does not exist")
        sys.exit(1)

    if not TASK_FILE.exists():
        print("[runner] ERROR: TASK.md not found")
        sys.exit(1)

    if not AGENTS_FILE.exists():
        print("[runner] WARNING: AGENTS.md not found (Codex can still run, but behavior may be less stable)")

    print(f"[runner] CODEX_FLAGS={' '.join(CODEX_FLAGS)}")
    print(f"[runner] AUTO_GIT_SYNC={AUTO_GIT_SYNC} SYNC_EACH_ROUND={SYNC_EACH_ROUND}")
    print(f"[runner] AUTO_PUSH_ON_DONE={AUTO_PUSH_ON_DONE}")

    if ENABLE_STARTUP_SELFCHECK:
        codex_shell_selfcheck()

    if AUTO_GIT_SYNC:
        ok, sync_log = git_sync_non_destructive()
        sync_log_file = LOG_DIR / f"startup-gitsync-{timestamp()}.log"
        sync_log_file.write_text(sync_log, encoding="utf-8")
        if not ok:
            print(f"[runner] startup git sync skipped/failed: {sync_log_file.name}")
            print(sync_log)

    feedback = ""

    for i in range(1, MAX_ROUNDS + 1):
        print(f"\n===== ROUND {i}/{MAX_ROUNDS} =====", flush=True)

        if STOP_FILE.exists():
            print("[runner] STOP flag detected (.stop). Exiting.")
            break

        if AUTO_GIT_SYNC and SYNC_EACH_ROUND:
            ok, sync_log = git_sync_non_destructive()
            sync_log_file = LOG_DIR / f"round{i:02d}-gitsync-{timestamp()}.log"
            sync_log_file.write_text(sync_log, encoding="utf-8")
            if not ok:
                print(f"[runner] round git sync skipped/failed: {sync_log_file.name}")
                print(sync_log)

        task_text = load_task()

        # 1) Codex round (實時可觀測)
        codex_result = codex_round(i, feedback)
        print(
            f"[runner] Codex rc={codex_result['rc']}, "
            f"status={codex_result['status_tag']}, "
            f"log={codex_result['codex_log'].name}"
        )

        # 2) 看 repo 改動 + 存 patch
        print_repo_changes()
        patch_path = save_git_patch(i)
        if patch_path:
            print(f"[runner] patch saved -> {patch_path.name}")

        # 3) 若遇到 sandbox 硬阻塞，提前停止
        if codex_result["sandbox_blocked"]:
            print("❌ Sandbox execution is blocked (Landlock).")
            print("[runner] Detected sandbox blocker in Codex output. Stopping early.")
            print("[runner] Suggested direct test:")
            print('        codex exec --dangerously-bypass-approvals-and-sandbox --ephemeral --json "pwd && ls -la && cat TASK.md"')
            break

        # 4) 跑客觀驗收（最終裁決）
        acc_ok, acc_info, _ = run_acceptance(task_text, i)
        if acc_ok is True:
            print("✅ ACCEPTANCE PASSED")
            done = create_done_marker(i)
            print(f"[runner] done marker -> {done}")

            if AUTO_PUSH_ON_DONE:
                print("[runner] auto-push enabled, committing and pushing...")
                push_ok, push_msg = git_commit_and_push(i)
                if push_ok:
                    print(f"[runner] ✅ {push_msg}")
                else:
                    print(f"[runner] ❌ {push_msg}")

            break

        elif acc_ok is False:
            print("❌ ACCEPTANCE FAILED")
            feedback = (
                "Acceptance failed in the last round. Fix the issue and try again.\n"
                + acc_info
            )
        else:
            print("⚠️ No machine-runnable ACCEPTANCE block found.")
            if codex_result["status_tag"] == "DONE":
                print("[runner] Codex reported DONE, and there is no runnable acceptance block. Stopping.")
                break
            feedback = "No ACCEPTANCE bash block found in TASK.md. Continue implementation and self-verify carefully."

        time.sleep(SLEEP_BETWEEN_ROUNDS)

    print("\nRunner finished.")


if __name__ == "__main__":
    main()
