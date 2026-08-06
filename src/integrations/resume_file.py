"""Pull a phone number out of a CV that came as an attached file.

About 8% of robota.ua responses are `resumeType = AttachedFile`: the candidate
uploaded a document instead of filling the site's resume form. Those applies have
`resumeId = 0` and an empty `phone`, so before this module they were unreachable —
the number sits inside the file and nowhere else.

Format support is deliberately shallow: we are looking for a phone number, not
rendering the document.
  * PDF   — `pypdf` (pure Python, no system libraries)
  * DOCX  — a zip of XML; unpacked with the stdlib, no dependency
  * DOC / RTF / anything else — best-effort scan of the raw bytes
Whatever the format, the candidate number is validated by `normalize_phone`, so a
false positive from a byte scan cannot enter the funnel as a real number.
"""
from __future__ import annotations

import io
import re
import zipfile

import structlog

from src.common.phone import normalize_phone

log = structlog.get_logger()

# Ukrainian mobile numbers as people actually type them in a CV:
# +380 67 123 45 67 / 380671234567 / 0(67)123-45-67 / 067 123 45 67.
_PHONE_RE = re.compile(
    r"(?:\+?38)?[\s\-()]*0\s*\(?\d{2}\)?[\s\-]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}"
)

# How much text we keep from a document. Generous: in an old binary .doc the
# extracted stream is NOT in document order — a real CV had its contact line at
# offset 81 485 — so "contacts are at the top" is only true for real text.
_MAX_SCAN_CHARS = 400_000


def _from_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        log.warning("resume_file.pypdf_missing")
        return ""
    try:
        reader = PdfReader(io.BytesIO(data))
        out: list[str] = []
        for page in reader.pages[:5]:  # contacts live on page one
            out.append(page.extract_text() or "")
            if sum(len(x) for x in out) > _MAX_SCAN_CHARS:
                break
        return "\n".join(out)
    except Exception as e:  # noqa: BLE001 — a broken PDF is not our problem
        log.warning("resume_file.pdf_failed", error=str(e))
        return ""


def _from_docx(data: bytes) -> str:
    """A .docx is a zip; word/document.xml holds the text. No dependency needed."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
        # Drop tags but keep their boundaries as spaces, so "067" and "1234567"
        # split across runs do not fuse into a wrong number.
        return re.sub(r"<[^>]+>", " ", xml)[:_MAX_SCAN_CHARS]
    except Exception as e:  # noqa: BLE001
        log.warning("resume_file.docx_failed", error=str(e))
        return ""


def _from_raw(data: bytes) -> str:
    """Last resort for .doc/.rtf: decode loosely and let the regex hunt.

    Old binary Word stores text in both latin-1 and utf-16; try both and
    concatenate rather than guessing which one this file uses.
    """
    head = data[:4_000_000]
    try:
        latin = head.decode("latin-1", "replace")
    except Exception:  # noqa: BLE001
        latin = ""
    try:
        utf16 = head.decode("utf-16-le", "replace")
    except Exception:  # noqa: BLE001
        utf16 = ""
    return latin + "\n" + utf16


def extract_text(data: bytes, file_name: str = "") -> str:
    """Best-effort text of a CV file. Empty string when nothing can be read."""
    if not data:
        return ""
    name = (file_name or "").lower()
    # Sniff the real type: robota.ua filenames lie often enough (a .doc that is
    # really a docx is common), and the magic bytes never do.
    if data[:4] == b"%PDF":
        return _from_pdf(data)
    if data[:2] == b"PK":  # zip container → docx/odt
        text = _from_docx(data)
        if text:
            return text
    if name.endswith(".pdf"):
        return _from_pdf(data)
    if name.endswith(".docx"):
        return _from_docx(data)
    return _from_raw(data)


def find_phone(text: str) -> str | None:
    """First string in `text` that normalizes to a real Ukrainian number.

    Scans all of it, not a prefix: see _MAX_SCAN_CHARS on why the contact line
    can sit deep inside an extracted .doc stream.
    """
    if not text:
        return None
    for match in _PHONE_RE.finditer(text):
        try:
            phone = normalize_phone(match.group(0))
        except Exception:  # noqa: BLE001
            continue
        if phone:
            return phone
    return None


def phone_from_file(data: bytes, file_name: str = "") -> tuple[str | None, str]:
    """(phone, extracted_text). Text is returned so it can go on the CRM card."""
    text = extract_text(data, file_name)
    return find_phone(text), text
