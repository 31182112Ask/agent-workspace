# runner.py（把原本的 run / run_shell / codex_round / run_acceptance 換成這版）
import os
import re
import shlex
import sys
import time
import json
import subprocess
from pathlib import Path
from datetime import datetime

REPO_DIR = Path("/content/work/agent-workspace")
LOG_DIR = REPO_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

MAX_ROUNDS = int(os.environ.get("MAX_ROUNDS", "8"))
AUTO_GIT_SYNC = os.environ.get("AUTO_GIT_SYNC", "1") == "1"

# 你可以切換這個：1 = 用 Codex JSON 事件流（建議）
USE_CODEX_JSON = os.environ.get("USE_CODEX_JSON", "1") == "1"

# 這裡保留 full-auto / ephemeral；若你有 sandbox 需求可自行加 flags
BASE_CODEX_FLAGS = shlex.split(os.environ.get("CODEX_FLAGS", "--full-auto --ephemeral"))
if USE_CODEX_JSON and "--json" not in BASE_CODEX_FLAGS:
    BASE_CODEX_FLAGS = BASE_CODEX_FLAGS + ["--json"]

TASK_FILE = REPO_DIR / "TASK.md"
AGENTS_FILE = REPO_DIR / "AGENTS.md"
STOP_FILE = REPO_DIR / ".stop"

def timestamp():
    return datetime.now().strftime("%Y%m%d-%H%M%S")

def run_capture(cmd, cwd=None, timeout=None):
    return subprocess.run(
        cmd, cwd=str(cwd) if cwd else None,
        capture_output=True, text=True, timeout=timeout
    )

def run_stream(cmd, cwd=None, log_path=None, prefix="", env=None, shell=False):
    """
    串流執行：即時印到 Colab 輸出，同時寫入 log 檔
    回傳 (returncode, combined_output)
    """
    merged_env = os.environ.copy()
    merged_env["PYTHONUNBUFFERED"] = "1"
    if env:
        merged_env.update(env)

    if shell:
        p = subprocess.Popen(
            ["bash", "-lc", cmd],
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=merged_env,
        )
    else:
        p = subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=merged_env,
        )

    lines = []
    f = open(log_path, "w", encoding="utf-8") if log_path else None
    try:
        for line in p.stdout:
            lines.append(line)
            # 即時輸出到 Colab
            sys.stdout.write(f"{prefix}{line}")
            sys.stdout.flush()
            # 同步寫檔
            if f:
                f.write(line)
                f.flush()
        rc = p.wait()
    finally:
        if f:
            f.close()

    return rc, "".join(lines)

def parse_coding_events_from_jsonl(jsonl_text: str):
    """
    把 codex --json 的事件做簡單摘要，避免你只看到生硬 JSON。
    這裡故意寫得很保守，字段不存在也不會炸。
    """
    status_tag = None
    event_lines = []

    for raw in jsonl_text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            # 若不是 JSON（例如 shell 層輸出），仍保留
            if "STATUS:" in raw:
                m = re.search(r"STATUS:\s*(DONE|CONTINUE)", raw, flags=re.I)
                if m:
                    status_tag = m.group(1).upper()
            continue

        t = obj.get("type", "")
        if t in ("thread.started", "turn.started", "turn.completed", "turn.failed", "error"):
            event_lines.append(f"[{t}]")
            continue

        if t.startswith("item."):
            item = obj.get("item", {}) or {}
            item_type = item.get("type", "unknown")
            item_status = item.get("status", "")
            # 常見可觀測項：命令、檔案變更、agent message
            if item_type == "command_execution":
                cmd = item.get("command", "<no-command>")
                event_lines.append(f"[{t}] command_execution ({item_status}) :: {cmd}")
            elif item_type in ("file_change", "file_changes"):
                # 不假設固定 schema，盡量抓可能的欄位
                paths = item.get("paths") or item.get("files") or item.get("file_paths") or []
                if isinstance(paths, list):
                    paths_text = ", ".join(map(str, paths)) if paths else "<paths-unknown>"
                else:
                    paths_text = str(paths)
                event_lines.append(f"[{t}] file_change ({item_status}) :: {paths_text}")
            elif item_type == "agent_message":
                text = (item.get("text") or "").replace("\n", " ")
                if "STATUS:" in text:
                    m = re.search(r"STATUS:\s*(DONE|CONTINUE)", text, flags=re.I)
                    if m:
                        status_tag = m.group(1).upper()
                event_lines.append(f"[{t}] agent_message :: {text[:200]}")
            else:
                event_lines.append(f"[{t}] {item_type} ({item_status})")

    return status_tag, event_lines

def git_sync():
    if not (REPO_DIR / ".git").exists():
        return True, "No git repo; skip sync."
    logs = []
    for cmd in [["git","fetch","origin"], ["git","reset","--hard","origin/main"], ["git","clean","-fd"]]:
        r = run_capture(cmd, cwd=REPO_DIR, timeout=120)
        logs.append(f"$ {' '.join(cmd)}\n{r.stdout}\n{r.stderr}")
        if r.returncode != 0:
            return False, "\n\n".join(logs)
    return True, "\n\n".join(logs)

def extract_acceptance_commands(task_text: str):
    m = re.search(r"##\s*ACCEPTANCE\b.*?```bash\s*(.*?)```", task_text, flags=re.I | re.S)
    return m.group(1).strip() if m else None

def load_task():
    return TASK_FILE.read_text(encoding="utf-8")

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
    cmd = ["codex", "exec"] + BASE_CODEX_FLAGS + [prompt]
    print(f"[runner] starting Codex round {round_idx}, log={codex_log.name}")
    rc, out = run_stream(cmd, cwd=REPO_DIR, log_path=codex_log, prefix="[codex] ")

    status_tag = None
    # 1) JSON 模式：做事件摘要
    if USE_CODEX_JSON:
        status_tag, events = parse_coding_events_from_jsonl(out)
        events_log = LOG_DIR / f"round{round_idx:02d}-codex-events-{timestamp()}.log"
        events_log.write_text("\n".join(events), encoding="utf-8")
        print(f"[runner] codex events summary -> {events_log.name}")
        # 在畫面上也印一份精簡摘要
        for e in events[-30:]:
            print(f"[event] {e}")
    else:
        m = re.search(r"STATUS:\s*(DONE|CONTINUE)", out, flags=re.I)
        status_tag = m.group(1).upper() if m else None

    return rc, status_tag, codex_log

def run_acceptance(task_text: str, round_idx: int):
    cmds = extract_acceptance_commands(task_text)
    if not cmds:
        return None, "No ACCEPTANCE bash block found in TASK.md", None

    acc_log = LOG_DIR / f"round{round_idx:02d}-acceptance-{timestamp()}.log"
    print(f"[runner] running acceptance, log={acc_log.name}")
    rc, out = run_stream(cmds, cwd=REPO_DIR, log_path=acc_log, prefix="[acc] ", shell=True)

    ok = (rc == 0)
    # 回傳後段內容給下一輪 prompt
    tail = out[-5000:]
    return ok, f"Acceptance log: {acc_log.name}\n{tail}", acc_log

def print_repo_changes():
    # 讓你每輪都能看到模型改了哪些檔案（非常實用）
    r1 = run_capture(["git", "status", "--short"], cwd=REPO_DIR)
    r2 = run_capture(["git", "diff", "--name-only"], cwd=REPO_DIR)
    print("[runner] git status --short")
    print(r1.stdout if r1.stdout.strip() else "(clean)")
    print("[runner] changed files")
    print(r2.stdout if r2.stdout.strip() else "(none)")

def main():
    if not AGENTS_FILE.exists():
        print("WARNING: AGENTS.md not found")
    feedback = ""

    for i in range(1, MAX_ROUNDS + 1):
        print(f"\n===== ROUND {i}/{MAX_ROUNDS} =====", flush=True)
        if STOP_FILE.exists():
            print("STOP flag detected (.stop). Exiting.")
            break

        if AUTO_GIT_SYNC:
            ok, sync_log = git_sync()
            sync_log_file = LOG_DIR / f"round{i:02d}-gitsync-{timestamp()}.log"
            sync_log_file.write_text(sync_log, encoding="utf-8")
            if not ok:
                print("[runner] Git sync failed.")
                print(sync_log[-2000:])
                break

        task_text = load_task()

        codex_rc, status_tag, codex_log = codex_round(i, feedback)
        print(f"[runner] Codex rc={codex_rc}, status={status_tag}, log={codex_log.name}")

        print_repo_changes()

        acc_ok, acc_info, acc_log = run_acceptance(task_text, i)
        if acc_ok is True:
            print("✅ ACCEPTANCE PASSED")
            done_mark = REPO_DIR / "artifacts" / "DONE.txt"
            done_mark.parent.mkdir(parents=True, exist_ok=True)
            done_mark.write_text(f"Completed at {datetime.now().isoformat()}\nRound={i}\n", encoding="utf-8")
            break
        elif acc_ok is False:
            print("❌ ACCEPTANCE FAILED")
            feedback = "Acceptance failed. Fix and retry.\n" + acc_info
        else:
            print("⚠️ No machine-runnable ACCEPTANCE block found.")
            feedback = "No ACCEPTANCE bash block found. Continue and self-verify."

        time.sleep(2)

    print("\nRunner finished.")

if __name__ == "__main__":
    main()
