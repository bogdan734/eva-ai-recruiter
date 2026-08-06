import io
import zipfile

from src.integrations.resume_file import extract_text, find_phone, phone_from_file


def _docx(body_text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(
            "word/document.xml",
            f"<w:document><w:body><w:p><w:r><w:t>{body_text}</w:t></w:r></w:p></w:body></w:document>",
        )
    return buf.getvalue()


def test_finds_phone_in_common_ua_formats():
    for raw in (
        "Телефон: +380 67 123 45 67",
        "тел. 380671234567",
        "Моб: 0(67)123-45-67",
        "067 123 45 67",
        "+38(067)123-45-67",
    ):
        assert find_phone(raw) == "+380671234567", raw


def test_rejects_things_that_are_not_phones():
    assert find_phone("") is None
    assert find_phone("Досвід 2011-2019, 15 років у галузі") is None
    assert find_phone("індекс 01001, вул. Баскетбольна 7") is None


def test_reads_a_docx_without_any_dependency():
    data = _docx("Іваненко Іван. Телефон: (096) 215-72-79. Київ")
    phone, text = phone_from_file(data, "Іваненко.docx")
    assert phone == "+380962157279"
    assert "Іваненко" in text


def test_docx_is_detected_by_magic_bytes_not_the_name():
    """robota.ua filenames lie — a .doc that is really a docx is common."""
    data = _docx("Контакти: 0501234567")
    phone, _ = phone_from_file(data, "Резюме_Ширіна_Ю.П2026.doc")
    assert phone == "+380501234567"


def test_tags_do_not_fuse_digits_across_runs():
    """A number split across two <w:t> runs must not glue into a wrong one."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(
            "word/document.xml",
            "<w:body><w:t>099</w:t><w:t>1112233</w:t></w:body>",
        )
    text = extract_text(buf.getvalue(), "x.docx")
    assert "099 1112233" in " ".join(text.split())


def test_phone_is_found_deep_inside_the_text():
    """Real .doc case: the contact line sat at offset 81 485, not near the top."""
    padding = "x" * 90_000
    assert find_phone(padding + " Телефон мобільний: (099)-733-13-10") == "+380997331310"


def test_parenthesised_leading_zero_form():
    assert find_phone("(099)-733-13-10") == "+380997331310"


def test_empty_and_garbage_are_survivable():
    assert phone_from_file(b"", "x.pdf") == (None, "")
    phone, _ = phone_from_file(b"\x00\x01\x02not a document", "x.doc")
    assert phone is None
