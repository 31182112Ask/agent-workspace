import os
import re
import shlex
import time
import subprocess
from pathlib import Path
from datetime import datetime

REPO_DIR = Path("/content/work/agent-workspace")
LOG_DIR = REPO_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

MAX_ROUNDS = int(os.environ.get("MAX_ROUNDS", "8"))
ROUND_TIMEOUT_SEC = int(os.environ.get("ROUND_TIMEOUT_SEC", "1800"))  # 30 min per round
SLEEP_BETWEEN_ROUNDS = int(os.environ.get("SLEEP_BETWEEN_ROUNDS", "5"))
AUTO_GIT_SYNC = os.environ.get("AUTO_GIT_SYNC", "1") == "1"

# 默認允許 Codex 自動修改；如遇 sandbox 限制可改成:
# "--full-auto --sandbox danger-full-access --ephemeral"
CODEX_FLAGS = shlex.split(os.environ.get("CODEX_FLAGS", "--full-auto --ephemeral"))

TASK_FILE = REPO_DIR / "TASK.md"
AGENTS_FILE = REPO_DIR / "AGENTS.md"
STOP_FILE = REPO_DIR / ".stop"

def run(cmd, cwd=None, timeout=None):
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
    )

def run_shell(script, cwd=None, timeout=None):
    return subprocess.run(
        ["bash", "-lc", script],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
    )

def timestamp():
    return datetime.now().strftime("%Y%m%d-%H%M%S")

def git_sync():
    # 允許你在 GitHub Web 直接改 TASK.md / AGENTS.md，這裡每輪同步
    if not (REPO_DIR / ".git").exists():
        return True, "No git repo; skip sync."
    steps = [
        ["git", "fetch", "origin"],
        ["git", "reset", "--hard", "origin/main"],
        ["git", "clean", "-fd"],
    ]
    logs = []
    for cmd in steps:
        r = run(cmd, cwd=REPO_DIR, timeout=120)
        logs.append(f"$ {' '.join(cmd)}\n{r.stdout}\n{r.stderr}")
        if r.returncode != 0:
            return False, "\n\n".join(logs)
    return True, "\n\n".join(logs)

def extract_acceptance_commands(task_text: str):
    """
    從 TASK.md 的 ## ACCEPTANCE 區塊中抓出第一個 ```bash ... ``` 代碼塊
    """
    m = re.search(
        r"##\s*ACCEPTANCE\b.*?```bash\s*(.*?)```",
        task_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return None
    block = m.group(1).strip()
    return block if block else None

def load_task():
    if not TASK_FILE.exists():
        raise FileNotFoundError(f"{TASK_FILE} not found")
    return TASK_FILE.read_text(encoding="utf-8")

def codex_round(round_idx: int, feedback: str):
    prompt = f"""
Read TASK.md and complete ONE full implementation iteration.

Requirements:
- Read TASK.md carefully (goal + acceptance + constraints).
- Make code changes needed to move the task forward.
- Run relevant local checks/tests.
- Be proactive and continue until this round reaches a meaningful result.
- If acceptance is not fully passing yet, still provide the best progress and a precise diagnosis.

Important response format (exact tags required):
STATUS: DONE or CONTINUE
SUMMARY:
- ...
CHANGED_FILES:
- ...
VALIDATION_COMMANDS:
- ...

Previous feedback from orchestrator (if any):
{feedback or "(none)"}
""".strip()

    cmd = ["codex", "exec"] + CODEX_FLAGS + [prompt]
    r = run(cmd, cwd=REPO_DIR, timeout=ROUND_TIMEOUT_SEC)
    return r

def run_acceptance(task_text: str, round_idx: int):
    cmds = extract_acceptance_commands(task_text)
    if not cmds:
        return None, "No ACCEPTANCE bash block found in TASK.md"
    r = run_shell(cmds, cwd=REPO_DIR, timeout=ROUND_TIMEOUT_SEC)
    acc_log = LOG_DIR / f"round{round_idx:02d}-acceptance-{timestamp()}.log"
    acc_log.write_text(
        f"[ACCEPTANCE COMMANDS]\n{cmds}\n\n[STDOUT]\n{r.stdout}\n\n[STDERR]\n{r.stderr}",
        encoding="utf-8"
    )
    return r.returncode == 0, f"Acceptance log: {acc_log.name}\n{r.stdout[-4000:]}\n{r.stderr[-4000:]}"

def parse_status(stdout: str):
    m = re.search(r"STATUS:\s*(DONE|CONTINUE)", stdout, flags=re.IGNORECASE)
    if not m:
        return None
    return m.group(1).upper()

def main():
    if not AGENTS_FILE.exists():
        print("WARNING: AGENTS.md not found. Codex will run without project guidance.")
    feedback = ""
    for i in range(1, MAX_ROUNDS + 1):
        print(f"\n===== ROUND {i}/{MAX_ROUNDS} =====")

        if STOP_FILE.exists():
            print("STOP flag detected (.stop). Exiting.")
            break

        if AUTO_GIT_SYNC:
            ok, sync_log = git_sync()
            sync_log_file = LOG_DIR / f"round{i:02d}-gitsync-{timestamp()}.log"
            sync_log_file.write_text(sync_log, encoding="utf-8")
            if not ok:
                print("Git sync failed.")
                print(sync_log[-2000:])
                break

        task_text = load_task()

        # 跑一輪 Codex
        r = codex_round(i, feedback)
        codex_log = LOG_DIR / f"round{i:02d}-codex-{timestamp()}.log"
        codex_log.write_text(
            f"[RETURN CODE] {r.returncode}\n\n[STDOUT]\n{r.stdout}\n\n[STDERR]\n{r.stderr}",
            encoding="utf-8"
        )

        print(f"Codex rc={r.returncode}, log={codex_log.name}")
        status_tag = parse_status(r.stdout or "")
        if status_tag:
            print(f"Codex status tag: {status_tag}")
        else:
            print("Codex status tag not found; will rely on acceptance result.")

        # 跑客觀驗收（TASK.md 的 ACCEPTANCE 命令）
        acc_ok, acc_info = run_acceptance(task_text, i)
        if acc_ok is True:
            print("✅ ACCEPTANCE PASSED")
            done_mark = REPO_DIR / "artifacts" / "DONE.txt"
            done_mark.parent.mkdir(parents=True, exist_ok=True)
            done_mark.write_text(
                f"Completed at {datetime.now().isoformat()}\nRound={i}\n",
                encoding="utf-8"
            )
            break
        elif acc_ok is False:
            print("❌ ACCEPTANCE FAILED")
            feedback = (
                "Acceptance failed in the last round. Fix the issue and try again.\n"
                + acc_info[-5000:]
            )
        else:
            # 沒有 ACCEPTANCE bash block，退化為看 Codex STATUS 標記
            print("⚠️ No machine-runnable ACCEPTANCE block found.")
            if status_tag == "DONE":
                print("Codex reported DONE, but no objective acceptance command exists.")
                break
            feedback = "No ACCEPTANCE bash block found in TASK.md. Continue implementation and self-verify carefully."

        time.sleep(SLEEP_BETWEEN_ROUNDS)

    print("\nRunner finished.")

if __name__ == "__main__":
    main()
