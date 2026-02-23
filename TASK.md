# TASK.md

## TASK_NAME
<任務名稱，例如：Build CSV cleaner / Add login endpoint / Refactor parser module>

## GOAL
<用 2-5 句清楚描述最終要完成的功能或結果。>
<說明輸入是什麼、輸出是什麼、成功後使用者可以做什麼。>
<若有目標檔案/模組，請明確寫出路徑，例如 src/<module>.py 或 app/<feature>.ts>

### Goal Details
- Primary objective: <主要目標>
- In-scope: <本次要做的內容>
- Out-of-scope: <本次不做的內容，避免代理發散>
- Expected output artifacts:
  - <artifacts/<file1>>
  - <artifacts/<file2>>
- Target files to modify (if known):
  - <src/...>
  - <tests/...>

## ACCEPTANCE
The task is complete only when **all** checks below pass.

### Acceptance Criteria (Human-readable)
- [ ] <功能條件 1，例如：Script runs without error>
- [ ] <功能條件 2，例如：Output file is generated at artifacts/output.csv>
- [ ] <品質條件，例如：No failing tests>
- [ ] <格式/性能條件（可選）>

### Acceptance Commands (Machine-runnable)
Run all commands from the repository root. All commands below must exit with code 0.

```bash
# 1) Setup / install (if needed)
<安裝命令，例如：pip install -r requirements.txt>

# 2) Main execution command
<主執行命令，例如：python src/<script>.py>

# 3) Tests / validation
<測試命令，例如：pytest -q tests/<test_file>.py>

# 4) Output assertions (example using inline Python)
python - <<'PY'
from pathlib import Path
# Replace with real checks:
target = Path("<驗收輸出檔案路徑，例如 artifacts/output.csv>")
assert target.exists(), f"Missing file: {target}"
print("acceptance-ok")
PY
```

### Expected Acceptance Evidence
When completed, the agent should be able to show:
- Command outputs proving success (or logs under `logs/`)
- Generated files under `artifacts/`
- A brief summary of what changed

## GUIDANCE_FOR_AGENT
Follow the guidance below while implementing this task.

### Implementation Guidance
1. Read this `TASK.md` fully before making changes.
2. Start with the **smallest working change** that moves the task forward.
3. Prefer editing existing files over creating many new files unless necessary.
4. Run checks immediately after each meaningful change.
5. If a check fails, fix the root cause before moving on.
6. Keep changes localized and avoid unrelated refactors.
7. If blocked, state the blocker precisely and propose the next action.

### Suggested Work Loop
1. Understand goal and constraints
2. Inspect current codebase
3. Implement minimal change
4. Run acceptance commands
5. Fix failures
6. Repeat until all acceptance checks pass

### Common Pitfalls to Avoid
- Do not stop after partial implementation
- Do not claim success without running acceptance commands
- Do not introduce new dependencies unless explicitly allowed
- Do not modify unrelated modules/files

## CONTEXT (Optional but Recommended)
### Background
<補充背景資訊，例如：這個功能用於哪個流程、與哪些模組互動>

### Inputs
- Input files/data:
  - <<path/to/input1>>
  - <<path/to/input2>>
- Input format assumptions:
  - <例如：CSV has headers id,name,value>

### Outputs
- Required outputs:
  - <<path/to/output1>>
- Output format expectations:
  - <例如：UTF-8 CSV with header row>

## CONSTRAINTS
- Runtime environment: <例如：Colab Python 3.10 / Node 20>
- Dependency policy: <例如：Only stdlib + pandas>
- Performance limit (optional): <例如：Process 10k rows under 10s>
- Code style constraints (optional): <例如：Keep function names snake_case>
- Safety constraints (optional): <例如：Do not delete original input files>

## DELIVERABLES
At the end of the task, provide:
1. Status (`DONE` or `CONTINUE`)
2. Summary of changes
3. Files modified
4. Validation commands run
5. Remaining issues (if any)

## PLACEHOLDER CHECKLIST (Edit Before Running)
- [ ] Replace all `<佔位符>`
- [ ] Confirm target file paths are correct
- [ ] Confirm acceptance commands are runnable in Colab
- [ ] Confirm expected output artifact paths are listed
- [ ] Confirm constraints reflect this task
