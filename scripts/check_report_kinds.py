"""Render the daily report with a fake day and show the new intake block.

A template string that compiles is not the same as a template string that
prints what a human needs. This renders it once so the block can be read.
"""
from datetime import date

from src.bot.report import DayReport, format_report_md

r = DayReport(target_date=date(2026, 9, 23))
r.intake_by_source = {"work.ua": 12, "robota.ua": 2}
r.intake_by_kind = {
    "work.ua · надіслав резюме": 10,
    "work.ua · знайшла Єва": 2,
    "robota.ua · відгук": 2,
}

out = format_report_md(r)
start = out.find("Нові кандидати")
print(out[start - 4:start + 420] if start != -1 else out[:600])
