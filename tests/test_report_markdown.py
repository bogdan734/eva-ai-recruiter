"""The daily digest has to survive its own text.

On 2026-08-22 the 09:00 report did not arrive. The report itself was fine; a
single underscore in it was not. It is sent as legacy Markdown, where one
unpaired `_` opens an italic that never closes, so Telegram answered 400 and
the digest was lost — and then the handler meant to log that failure raised a
TypeError of its own and took the whole job down with it, hiding the cause.

Two rules come out of that: text reaching Telegram must carry no unpaired
Markdown, and formatting must never be able to cost the whole message. The
first is tested here; the second lives in the send path, which falls back to
plain text.

What counts is not how many markers remain in the string but how many Telegram
will still read as formatting — an escaped one is inert.
"""
import re

import pytest

from src.bot.report import markdown_safe


def live_markers(text: str, marker: str) -> int:
    """Markers Telegram still treats as toggles: the ones without a backslash."""
    return len(re.findall(r"(?<!\\)" + re.escape(marker), text))


class TestUnpairedMarkers:
    @pytest.mark.parametrize(
        "text",
        [
            "Новий job_id завести",
            "поле vacancy_id порожнє",
            "a_b_c_d",
            "trailing underscore_",
            "_leading underscore",
        ],
    )
    def test_underscores_end_up_inert(self, text):
        assert live_markers(markdown_safe(text), "_") % 2 == 0

    def test_paired_emphasis_is_left_alone(self):
        """Deliberate formatting must survive — the digest is full of it."""
        assert markdown_safe("*Час*") == "*Час*"

    def test_stray_star_is_defused(self):
        assert live_markers(markdown_safe("5 * 3 = 15"), "*") % 2 == 0

    def test_text_is_still_readable(self):
        out = markdown_safe("Новий job_id завести")
        assert "job" in out and "id" in out and "завести" in out


class TestRealReportLine:
    """The exact line that broke it, and the shape of the whole digest."""

    def test_the_liveness_line_is_safe(self):
        line = (
            "└ Клієнту треба перепублікувати. Новий job_id завести: "
            "/menu → Параметри вакансії → Збір і обдзвін"
        )
        assert live_markers(markdown_safe(line), "_") % 2 == 0

    def test_a_digest_sized_body_stays_balanced(self):
        body = "\n".join(
            [
                "📋 *work.ua — оголошення*",
                "├ 🔴 «sales» — жодного оголошення",
                "└ Новий job_id завести: /menu → Параметри",
            ]
        )
        safe = markdown_safe(body)
        assert live_markers(safe, "_") % 2 == 0
        assert live_markers(safe, "*") % 2 == 0


class TestEdgeCases:
    def test_empty_string(self):
        assert markdown_safe("") == ""

    def test_no_markers_is_unchanged(self):
        assert markdown_safe("звичайний текст") == "звичайний текст"
