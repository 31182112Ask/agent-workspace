# TASK.md

## GOAL
Implement a script that reads `data/input.csv`, cleans missing values, and writes `artifacts/output.csv`.
The script should be `src/clean_data.py`.

## ACCEPTANCE
The following commands must all pass:

```bash
python src/clean_data.py
python - <<'PY'
import pandas as pd
df = pd.read_csv("artifacts/output.csv")
assert "id" in df.columns
assert df["id"].isna().sum() == 0
print("acceptance-ok")
PY
CONSTRAINTS

Do not add external dependencies beyond the Python standard library and pandas.

Keep the script runnable in Colab.

Write logs to logs/.
