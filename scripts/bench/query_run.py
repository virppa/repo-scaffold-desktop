import sqlite3
import sys
from pathlib import Path

db = Path(r"C:\Users\Antti\AppData\Roaming\repo-scaffold\bench.db")
run_prefix = sys.argv[1] if len(sys.argv) > 1 else "run_20260427_205008"

conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row

rows = conn.execute(
    "SELECT context_size, COUNT(*) as n, ROUND(AVG(throughput_tok_s),1) as avg_tok_s, "
    "ROUND(AVG(ttft_s),2) as avg_ttft, "
    "SUM(quality_task_success) as passed, "
    "SUM(CASE WHEN quality_task_success IS NOT NULL THEN 1 ELSE 0 END) as graded "
    "FROM bench_run WHERE run_id LIKE ? GROUP BY context_size ORDER BY context_size",
    (run_prefix + "%",),
).fetchall()

total = conn.execute(
    "SELECT COUNT(*) FROM bench_run WHERE run_id LIKE ?", (run_prefix + "%",)
).fetchone()[0]

print(f"Run {run_prefix} -- {total} rows")
for r in rows:
    passed = r["passed"] if r["passed"] is not None else 0
    print(
        f"  ctx={r['context_size']:>7}  n={r['n']:>2}  {str(r['avg_tok_s']):>6} tok/s"
        f"  ttft={r['avg_ttft']}s  {passed}/{r['graded']} pass"
    )

conn.close()
