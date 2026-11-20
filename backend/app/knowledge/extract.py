"""Turn uploaded files into plain text (no network, no external converters)."""

from __future__ import annotations

import io
import json
import re
import unicodedata

from app.core.errors import ValidationFailedError

TEXT_TYPES = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".log": "text/plain",
    ".csv": "text/csv",
    ".json": "application/json",
    ".pdf": "application/pdf",
}
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
MAX_PDF_PAGES = 500


def extension(filename: str) -> str:
    name = filename.lower().strip()
    return name[name.rfind(".") :] if "." in name else ""


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL.sub("", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{4,}", "\n\n\n", text).strip()


def _decode(data: bytes) -> str:
    encodings = ["utf-16"] if data[:2] in (b"\xff\xfe", b"\xfe\xff") else []
    for encoding in [*encodings, "utf-8-sig"]:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _pdf(data: bytes) -> str:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ValidationFailedError("Encrypted PDFs are not supported")
        pages = []
        for number, page in enumerate(reader.pages[:MAX_PDF_PAGES], start=1):
            text = (page.extract_text() or "").strip()
            if text:
                pages.append(f"## Page {number}\n\n{text}")
    except PdfReadError as exc:
        raise ValidationFailedError(f"Could not read the PDF: {exc}") from exc
    if not pages:
        raise ValidationFailedError(
            "The PDF contains no extractable text (scanned images need OCR first)"
        )
    return "\n\n".join(pages)


def extract_text(filename: str, data: bytes) -> tuple[str, str]:
    """Return (normalised text, mime type) or raise ``ValidationFailedError``."""
    ext = extension(filename)
    if ext not in TEXT_TYPES:
        allowed = ", ".join(sorted(TEXT_TYPES))
        raise ValidationFailedError(
            f"Unsupported file type '{ext or filename}'. Allowed: {allowed}"
        )
    mime = TEXT_TYPES[ext]
    if ext == ".pdf":
        text = _pdf(data)
    else:
        text = _decode(data)
        if ext == ".json":
            try:
                text = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
            except json.JSONDecodeError as exc:
                raise ValidationFailedError(f"Invalid JSON: {exc.msg}") from exc
    text = normalise(text)
    if not text:
        raise ValidationFailedError("The document is empty")
    return text, mime
