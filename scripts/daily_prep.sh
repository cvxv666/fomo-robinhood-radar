#!/usr/bin/env bash
# The morning's raw material, written to /opt/fomoradar/daily/<date>/ by the radar-daily timer:
# the day in numbers (daily_report.py as JSON lines), the unscored wallets exported for a verdict,
# and their digest. Whatever reads them - the desktop summary, a person over ssh - finds them
# ready; nothing here needs the app to be open.
set -euo pipefail
APP=/opt/fomoradar/app
PY=/opt/fomoradar/venv/bin/python
DAY=$(date -u +%F)
OUT=/opt/fomoradar/daily/$DAY
mkdir -p "$OUT"
cd "$APP"
HOURS=${HOURS:-24} "$PY" scripts/daily_report.py > "$OUT/report.jsonl" 2> "$OUT/report.err" || echo "report failed, see $OUT/report.err"
"$PY" -m fomo_agent.cli score --export "$OUT/pending.json" --unscored --digest 200 > "$OUT/digest.txt" 2>&1 || true
"$PY" -m fomo_agent.cli health > "$OUT/health.txt" 2>&1 || true
# fourteen mornings kept; the rest is in git history and the journal anyway
ls -1dt /opt/fomoradar/daily/*/ 2>/dev/null | tail -n +15 | xargs -r rm -rf
echo "prepared $OUT: $(wc -l < "$OUT/report.jsonl") report lines, $(grep -c . "$OUT/digest.txt" || true) digest lines"
