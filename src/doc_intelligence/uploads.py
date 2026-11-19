"""Upload validation.

The format is detected from the file content, not from the filename or the client's
Content-Type. Office files are ZIP archives and are checked for decompression bombs (total
uncompressed size, entry count, compression ratio) before any parser opens them.
"""

import hashlib
import io
import zipfile
from dataclasses import dataclass

from pypdf import PdfReader
from pypdf.errors import PdfReadError

FORMATS = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "text/csv",
    "md": "text/markdown",
    "txt": "text/plain",
}
SUPPORTED_TEXT = "PDF, Word (.docx), PowerPoint (.pptx), Excel (.xlsx), CSV, Markdown or plain text"

MAX_ZIP_ENTRIES = 5000
MAX_UNCOMPRESSED_BYTES = 300 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200


class UploadRejected(ValueError):
    """Raised when a file fails validation. The message is safe to return to the client."""


@dataclass(frozen=True)
class ValidatedFile:
    data: bytes
    file_format: str
    media_type: str
    sha256: str


def _extension(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _check_zip(data: bytes) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise UploadRejected("The file looks like an Office document but is damaged.") from exc
    infos = archive.infolist()
    if len(infos) > MAX_ZIP_ENTRIES:
        raise UploadRejected("The document has too many internal parts.")
    total = sum(i.file_size for i in infos)
    if total > MAX_UNCOMPRESSED_BYTES:
        raise UploadRejected("The document expands to more than 300 MB; it may be a zip bomb.")
    for info in infos:
        ratio = info.file_size / info.compress_size if info.compress_size else 0
        if ratio > MAX_COMPRESSION_RATIO and info.file_size > 1_000_000:
            raise UploadRejected("The document is compressed suspiciously; it may be a zip bomb.")
    return archive


def detect_format(data: bytes, filename: str) -> str:
    head = data[:8]
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        names = set(_check_zip(data).namelist())
        if "word/document.xml" in names:
            return "docx"
        if "ppt/presentation.xml" in names:
            return "pptx"
        if "xl/workbook.xml" in names:
            return "xlsx"
        raise UploadRejected(f"This archive is not a supported document. Upload {SUPPORTED_TEXT}.")
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        raise UploadRejected("Old Office formats (.doc, .xls, .ppt) are not supported. Save as .docx/.xlsx/.pptx.")
    # Text formats must be valid UTF-8 and contain no NUL bytes.
    if b"\x00" in data[:8192]:
        raise UploadRejected(f"Unsupported file type. Upload {SUPPORTED_TEXT}.")
    try:
        data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise UploadRejected("Text files must be UTF-8 encoded.") from exc
    ext = _extension(filename)
    return ext if ext in ("csv", "md") else "txt"


def validate_upload(data: bytes, filename: str, *, max_bytes: int, max_pages: int) -> ValidatedFile:
    if not data:
        raise UploadRejected("The file is empty.")
    if len(data) > max_bytes:
        raise UploadRejected(f"The file is larger than {max_bytes // (1024 * 1024)} MB.")

    file_format = detect_format(data, filename)
    if file_format == "pdf":
        try:
            reader = PdfReader(io.BytesIO(data), strict=False)
            if reader.is_encrypted:
                raise UploadRejected("Password-protected PDFs are not supported.")
            pages = len(reader.pages)
        except UploadRejected:
            raise
        except (PdfReadError, ValueError, KeyError, TypeError, OSError) as exc:
            raise UploadRejected("The PDF could not be read; it may be damaged.") from exc
        if pages == 0:
            raise UploadRejected("The PDF has no pages.")
        if pages > max_pages:
            raise UploadRejected(f"The PDF has more than {max_pages} pages.")

    return ValidatedFile(data=data, file_format=file_format, media_type=FORMATS[file_format],
                         sha256=hashlib.sha256(data).hexdigest())


def safe_display_name(filename: str | None) -> str:
    """Returns a filename for display only, without directories or control characters."""
    name = (filename or "document").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch.isprintable()).strip()
    return name[:255] or "document"


def title_from_filename(filename: str) -> str:
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    return stem.replace("_", " ").replace("-", " ").strip()[:200] or "Untitled"
