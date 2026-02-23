import os
import re
import sys
import shlex
import json
import time
import subprocess
from pathlib import Path
from datetime import datetime

# =========================
# Config
# =========================
REPO_DIR = Path(os.environ.get("REPO_DIR", "/content/work/agent-workspace"))
LOG_DIR = REPO_DIR / "logs"
ARTIFACT_DIR = REPO_DIR / "artifacts"

LOG_DIR.mkdir(parents=True, exist_ok=True)
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

MAX_ROUNDS = int(os.environ.get("MAX_ROUNDS", "8"))
ROUND_TIMEOUT_SEC = int(os.environ.get("ROUND_TIMEOUT_SEC", "1800"))  # per codex/acceptance phase
SLEEP_BETWEEN_ROUNDS = int(os.environ.get("SLEEP_BETWEEN_ROUNDS", "2"))

# 建議預設關閉自動 git 同步，避免每輪覆蓋模型剛做的修改
AUTO_GIT_SYNC = os.environ.get("AUTO_GIT_SYNC", "0") == "1"
SYNC_EACH_ROUND = os.environ.get("SYNC_EACH_ROUND", "0") == "1"

# 預設就開 JSON + full-auto + danger-full-access（避免 Colab Landlock 問題）
DEFAULT_CODEX_FLAGS = "--dangerously-bypass-approvals-and-sandbox --ephemeral --json"
CODEX_FLAGS = shlex.split(os.environ.get("CODEX_FLAGS", DEFAULT_CODEX_FLAGS))
USE_CODEX_JSON = ("--json" in CODEX_FLAGS) or (os.environ.get("USE_CODEX_JSON", "1") == "1")

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
                # process ended; flush remaining
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
    if not git_is_repo():
        print("[runner] not a git repo; skipping git change summary")
        return
    r1 = run_capture(["git", "status", "--short"], cwd=REPO_DIR, timeout=30)
    r2 = run_capture(["git", "diff", "--name-only"], cwd=REPO_DIR, timeout=30)

    print("[runner] git status --short")
    print((r1.stdout or "").strip() or "(clean)")
    print("[runner] changed files")
    print((r2.stdout or "").strip() or "(none)")


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
            # 非 JSON 行，盡量抓 STATUS 標記
            m = re.search(r"STATUS:\s*(DONE|CONTINUE)", raw, flags=re.I)
            if m:
                status_tag = m.group(1).upper()
            continue

        t = obj.get("type", "")

        # turn/thread lifecycle
        if t in {"thread.started", "thread.completed", "turn.started", "turn.completed", "turn.failed", "error"}:
            event_lines.append(f"[{t}]")
            continue

        # item events
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
                # 可以保留簡短摘要
                txt = (item.get("text") or "").replace("\n", " ")
                event_lines.append(f"[{t}] reasoning :: {txt[:160]}")

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

        # unknown event types
        event_lines.append(f"[{t or 'unknown'}]")

    return status_tag, event_lines


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

    # 檢測 sandbox 硬阻塞（避免浪費輪次）
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

    # 加 set -euo pipefail，避免前一個命令失敗還繼續跑後面驗證
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

    # 可選：啟動前同步一次（非破壞）
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

        # 可選：每輪同步（不建議，除非你確定 repo 乾淨）
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

        # 3) 若遇到 sandbox 硬阻塞，提前停止，避免浪費輪次
        if codex_result["sandbox_blocked"]:
            print("❌ Sandbox execution is blocked (Landlock).")
            print("[runner] Detected sandbox blocker in Codex output. Stopping early.")
            print("[runner] Suggested fix: use --sandbox danger-full-access (already set by default in this script).")
            print("[runner] If it still happens, run one direct test:")
            print("        codex exec --full-auto --sandbox danger-full-access --ephemeral --json \"pwd && ls -la && cat TASK.md\"")
            break

        # 4) 跑客觀驗收
        acc_ok, acc_info, acc_log = run_acceptance(task_text, i)
        if acc_ok is True:
            print("✅ ACCEPTANCE PASSED")
            done = create_done_marker(i)
            print(f"[runner] done marker -> {done}")
            break

        elif acc_ok is False:
            print("❌ ACCEPTANCE FAILED")
            feedback = (
                "Acceptance failed in the last round. Fix the issue and try again.\n"
                + acc_info
            )
        else:
            print("⚠️ No machine-runnable ACCEPTANCE block found.")
            # 沒有可執行驗收就退化為看 Codex 狀態
            if codex_result["status_tag"] == "DONE":
                print("[runner] Codex reported DONE, and there is no runnable acceptance block. Stopping.")
                break
            feedback = "No ACCEPTANCE bash block found in TASK.md. Continue implementation and self-verify carefully."

        time.sleep(SLEEP_BETWEEN_ROUNDS)

    print("\nRunner finished.")


if __name__ == "__main__":
    main()
