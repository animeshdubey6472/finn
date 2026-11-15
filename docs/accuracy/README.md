# Labelling FINN's decisions

1. Open `labels.csv` in a spreadsheet (Excel, Numbers, Google Sheets).
2. **Filing rows:** open the `url`, then set `human_important` to `yes` if a shareholder should see it today, otherwise `no`. Optionally set `human_category`, using one of FINN's category keys (e.g. `results`, `m_and_a`, `fund_raise`, `unclassified`).
3. **Guidance rows:** open the `url` (it jumps to the transcript page), check the quote and `verdict_detail`, then set `human_verdict_correct` to `yes` or `no`. Use `notes` for anything odd.
4. Save as CSV, keeping the same file name and columns.
5. Run `.venv/bin/python docs/accuracy/score.py`. It prints the metrics and writes `RESULTS.md`. Re-run `make_sheet.py` only if you want a fresh sample, because it overwrites `labels.csv`.
