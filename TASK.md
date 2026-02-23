# TASK.md

## TASK_NAME
Print "hey" in the runtime environment

## GOAL
Create the smallest possible runnable program that outputs exactly `hey` (lowercase, no extra text) in the current runtime environment. The implementation should be placed at `src/say_hey.py`. The agent should be able to run the program from the repository root and produce the expected output reliably.

### Goal Details
- Primary objective: Implement a script that prints `hey`
- In-scope: Create/update `src/say_hey.py`, create minimal supporting folders if missing
- Out-of-scope: Any unrelated refactor, dependency installation, framework setup
- Expected output artifacts:
  - `artifacts/hey_output.txt`
- Target files to modify (if known):
  - `src/say_hey.py`

## ACCEPTANCE
The task is complete only when **all** checks below pass.

### Acceptance Criteria (Human-readable)
- [ ] Running `python src/say_hey.py` succeeds without errors
- [ ] Program output is exactly `hey`
- [ ] Output is captured to `artifacts/hey_output.txt`
- [ ] No extra dependencies are introduced

### Acceptance Commands (Machine-runnable)
Run all commands from the repository root. All commands below must exit with code 0.

```bash
# 1) Prepare folders
mkdir -p src artifacts logs

# 2) Main execution command (capture output)
python src/say_hey.py > artifacts/hey_output.txt

# 3) Output assertions
python - <<'PY'
from pathlib import Path

p = Path("artifacts/hey_output.txt")
assert p.exists(), "Missing artifacts/hey_output.txt"
content = p.read_text(encoding="utf-8")
# Allow trailing newline, but the content should be exactly 'hey' after stripping line endings
assert content.strip("\r\n") == "hey", f"Unexpected output: {content!r}"
print("acceptance-ok")
PY
```

### Expected Acceptance Evidence
When completed, the agent should be able to show:
- Successful command output containing `acceptance-ok`
- Generated file `artifacts/hey_output.txt`
- A brief summary of created/modified file(s)

## GUIDANCE_FOR_AGENT
Follow the guidance below while implementing this task.

### Implementation Guidance
1. Read this `TASK.md` fully before making changes.
2. Create `src/say_hey.py` if it does not exist.
3. Keep the implementation minimal (one file is enough).
4. Output exactly `hey` and nothing else.
5. Run the acceptance commands after implementation.
6. If acceptance fails, fix the issue and rerun checks.
7. Do not add dependencies or unrelated files.

### Suggested Work Loop
1. Inspect whether `src/say_hey.py` exists
2. Implement minimal script to print `hey`
3. Run acceptance commands
4. Fix any mismatch (case sensitivity, extra spaces, encoding)
5. Repeat until all checks pass

### Common Pitfalls to Avoid
- Printing `Hey` / `HEY` (wrong case)
- Printing extra spaces or punctuation (e.g. `hey!`)
- Adding debug output
- Forgetting to create `artifacts/` directory
- Claiming completion before running acceptance commands

## CONTEXT (Optional but Recommended)
### Background
This is a smoke test task used to validate the agent loop (edit -> run -> acceptance) inside the temporary runtime environment.

### Inputs
- Input files/data:
  - None
- Input format assumptions:
  - None

### Outputs
- Required outputs:
  - `artifacts/hey_output.txt`
- Output format expectations:
  - UTF-8 text file containing `hey` (newline allowed)

## CONSTRAINTS
- Runtime environment: Python in Colab (or equivalent)
- Dependency policy: Standard library only (no installs)
- Code style constraints: Keep implementation minimal and readable
- Safety constraints: Do not modify unrelated files

## DELIVERABLES
At the end of the task, provide:
1. Status (`DONE` or `CONTINUE`)
2. Summary of changes
3. Files modified
4. Validation commands run
5. Remaining issues (if any)

## PLACEHOLDER CHECKLIST (Edit Before Running)
- [x] This task has no placeholders
- [x] Target file path is correct (`src/say_hey.py`)
- [x] Acceptance commands are runnable in Colab
- [x] Expected output artifact path is listed
- [x] Constraints reflect this task
