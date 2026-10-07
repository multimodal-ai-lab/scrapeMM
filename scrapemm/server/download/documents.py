"""Documents behind a URL: spreadsheets, PDFs, office files, archives.

A browser that opens such a URL shows no page: it downloads the file (often only after a
bot check, e.g. a "Verifying your browser..." page, has passed), or shows a PDF in its
viewer. What is cheap to read is turned into text -- spreadsheets into tables, one per
sheet; PDFs into their text, page by page. The rest is reported as a document type
scrapeMM does not extract, never as the bot check that preceded it.

`to_content()` is the one place that turns a downloaded document into content.
"""

import csv
import hashlib
import html
import logging
from io import BytesIO
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

logger = logging.getLogger("scrapeMM")

SPREADSHEET_EXTENSIONS = (".xlsx", ".xlsm")
TEXT_TABLE_EXTENSIONS = (".csv",)
# Everything a URL may serve as a file rather than a page
DOCUMENT_EXTENSIONS = (SPREADSHEET_EXTENSIONS + TEXT_TABLE_EXTENSIONS
                       + (".xls", ".ods", ".pdf", ".doc", ".docx", ".odt", ".rtf", ".ppt",
                          ".pptx", ".odp", ".zip", ".rar", ".7z", ".gz", ".tar"))
DOCUMENT_CONTENT_TYPES = ("application/pdf", "application/vnd.openxmlformats-officedocument",
                          "application/vnd.ms-", "application/vnd.oasis.opendocument",
                          "application/msword", "application/zip", "application/x-zip",
                          "application/x-rar", "application/x-7z", "application/gzip",
                          "text/csv", "application/rtf")

# A sheet of a statistics office runs to a few hundred rows; this bounds pathological ones
MAX_ROWS_PER_SHEET = 2000
MAX_COLUMNS = 100


def document_extension(url_or_name: str) -> Optional[str]:
    """The document extension of a URL's path or a file name, if it has one."""
    path = urlsplit(url_or_name).path if "://" in url_or_name else url_or_name
    suffix = Path(path).suffix.lower()
    if suffix in DOCUMENT_EXTENSIONS:
        return suffix
    if "://" in url_or_name and "pdf" in path.lower().split("/"):
        return ".pdf"  # E.g. eur-lex.europa.eu/legal-content/EN/TXT/PDF/?uri=...
    return None


def is_document_content_type(content_type: Optional[str]) -> bool:
    content_type = (content_type or "").split(";")[0].strip().lower()
    return any(content_type.startswith(t) for t in DOCUMENT_CONTENT_TYPES)


# How the files of each type begin. xlsx, docx and their kin are zip files.
_SIGNATURES = {".xlsx": (b"PK",), ".xlsm": (b"PK",), ".docx": (b"PK",), ".pptx": (b"PK",),
               ".odt": (b"PK",), ".ods": (b"PK",), ".odp": (b"PK",), ".zip": (b"PK",),
               ".pdf": (b"%PDF",), ".xls": (bytes.fromhex("d0cf11e0"),),
               ".doc": (bytes.fromhex("d0cf11e0"),), ".ppt": (bytes.fromhex("d0cf11e0"),),
               ".gz": (bytes.fromhex("1f8b"),), ".7z": (b"7z",), ".rar": (b"Rar!",)}


def is_file_of_type(data: bytes, kind: Optional[str], content_type: Optional[str] = None) -> bool:
    """Whether `data` is a file of the type `kind` (a document extension) or, without
    one, of `content_type` -- rather than, say, the HTML page of a bot check that was
    served in its place."""
    kind = kind or _kind("", content_type)
    head = data[:1024].lstrip()
    if kind in _SIGNATURES:
        return head.startswith(_SIGNATURES[kind])
    return not head[:15].lower().startswith((b"<!doctype html", b"<html"))


def describe(data: Optional[bytes], content_type: Optional[str] = None) -> str:
    """A short description of what came instead of a file, for the log."""
    if not data:
        return "nothing"
    head = " ".join(data[:60].decode("utf-8", errors="replace").split())
    return f"{len(data)} bytes{f' of {content_type}' if content_type else ''} starting {head!r}"


def to_content(data: bytes, name: str, content_type: Optional[str] = None):
    """The `ScrapedContent` of a downloaded document (`name` gives its type, as does
    `content_type`), or None if scrapeMM does not read documents of its type. Blocking:
    call it in a thread."""
    from ezmm import MultimodalSequence
    from scrapemm.common.scraping_response import ScrapedContent
    text = to_text(data, name, content_type)
    if text is None:
        return None
    html_text, markdown = text
    content = ScrapedContent(html=html_text, markdown=markdown,
                             multimodal=MultimodalSequence(markdown))
    if _kind(name, content_type) == ".pdf":
        # TODO: Once ezMM has a PDF item, make the kept file one and put its reference
        #  into the multimodal sequence (beside the text), so that results carry the PDF
        #  like they carry images. `keep_pdf()` already stores it for good.
        content.pdf_path = keep_pdf(data)
    return content


def to_text(data: bytes, name: str, content_type: Optional[str] = None) -> Optional[tuple[str, str]]:
    """(HTML, Markdown) of a document (`name` and `content_type` tell its type), or None
    if scrapeMM does not read documents of its type."""
    extension = _kind(name, content_type)
    if extension in SPREADSHEET_EXTENSIONS:
        tables = _spreadsheet_tables(data, name)
    elif extension in TEXT_TABLE_EXTENSIONS:
        tables = _csv_tables(data, name)
    elif extension == ".pdf":
        return _pdf_text(data, name)
    else:
        return None
    if tables is None:
        return None
    return _render(name, tables)


def _kind(name: str, content_type: Optional[str]) -> Optional[str]:
    """The document extension standing for the document's type."""
    if extension := document_extension(name):
        return extension
    content_type = (content_type or "").split(";")[0].strip().lower()
    return {"application/pdf": ".pdf", "text/csv": ".csv",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
            }.get(content_type)


def keep_pdf(data: bytes) -> Optional[Path]:
    """Stores a PDF for good, named by its content, beside the media in the media
    registry's directory (`pdf/<sha256>.pdf`), and returns where. Kept so it can become
    an ezMM item once ezMM supports PDFs, without downloading it again."""
    try:
        from ezmm.common.registry import item_registry
        directory = Path(item_registry.path) / "pdf"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{hashlib.sha256(data).hexdigest()}.pdf"
        if not path.exists():
            partial = path.with_suffix(".pdf.part")
            partial.write_bytes(data)
            partial.replace(path)
        return path
    except OSError:
        logger.info("Could not keep a downloaded PDF.", exc_info=True)
        return None


# A PDF's text beyond this many pages is left out: a report runs to dozens, a scanned
# archive to thousands
MAX_PDF_PAGES = 500


def _pdf_text(data: bytes, name: str) -> Optional[tuple[str, str]]:
    try:
        from pypdf import PdfReader
    except ImportError:
        logger.info("pypdf is not installed; PDFs cannot be read.")
        return None
    try:
        reader = PdfReader(BytesIO(data))
        pages = [(page.extract_text() or "").strip() for page in reader.pages[:MAX_PDF_PAGES]]
        total = len(reader.pages)
    except Exception as e:
        logger.info(f"Could not read the PDF {name}: {type(e).__name__}: {e}")
        return None
    if not any(pages):
        logger.info(f"The PDF {name} has no text layer (a scan?).")
        return None
    note = (f"(Only the first {MAX_PDF_PAGES} of {total} pages.)"
            if total > MAX_PDF_PAGES else "")
    paragraph, line = "\n\n", "\n"
    markdown = paragraph.join([f"# {name}"] + [p for p in pages if p] + ([note] if note else []))
    html_text = (f"<h1>{html.escape(name)}</h1>"
                 + "".join('<section class="pdf-page"><p>'
                           + html.escape(p).replace(paragraph, "</p><p>").replace(line, "<br>")
                           + "</p></section>" for p in pages if p)
                 + (f"<p>{note}</p>" if note else ""))
    return html_text, markdown


def _spreadsheet_tables(data: bytes, name: str) -> Optional[list[tuple[str, list[list[str]], bool]]]:
    try:
        import openpyxl
    except ImportError:
        logger.info("openpyxl is not installed; spreadsheets cannot be read.")
        return None
    try:
        workbook = openpyxl.load_workbook(BytesIO(data), read_only=True, data_only=True)
    except Exception as e:
        logger.info(f"Could not open the spreadsheet {name}: {type(e).__name__}: {e}")
        return None
    tables = []
    try:
        for sheet in workbook.worksheets:
            rows, truncated = [], False
            for row in sheet.iter_rows(values_only=True):
                if len(rows) >= MAX_ROWS_PER_SHEET:
                    truncated = True
                    break
                rows.append(["" if v is None else str(v) for v in row[:MAX_COLUMNS]])
            tables.append((sheet.title, _trim(rows), truncated))
    finally:
        workbook.close()
    return tables


def _csv_tables(data: bytes, name: str) -> list[tuple[str, list[list[str]], bool]]:
    text = data.decode("utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096])
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(text.splitlines(), dialect))
    truncated = len(rows) > MAX_ROWS_PER_SHEET
    return [(name, _trim([r[:MAX_COLUMNS] for r in rows[:MAX_ROWS_PER_SHEET]]), truncated)]


def _trim(rows: list[list[str]]) -> list[list[str]]:
    """Without empty rows, and without the columns that are empty in every row."""
    rows = [r for r in rows if any(cell.strip() for cell in r)]
    width = max((len(r) for r in rows), default=0)
    rows = [r + [""] * (width - len(r)) for r in rows]
    keep = [i for i in range(width) if any(r[i].strip() for r in rows)]
    return [[r[i] for i in keep] for r in rows]


def _render(name: str, tables: list[tuple[str, list[list[str]], bool]]) -> tuple[str, str]:
    md_parts, html_parts = [f"# {name}"], [f"<h1>{html.escape(name)}</h1>"]
    for title, rows, truncated in tables:
        md_parts.append(f"## {title}")
        html_parts.append(f"<h2>{html.escape(title)}</h2>")
        if not rows:
            md_parts.append("(empty)")
            html_parts.append("<p>(empty)</p>")
            continue

        def md_cell(value: str) -> str:
            return " ".join(value.split()).replace("|", "\\|")

        header, body = rows[0], rows[1:]
        md_parts.append("\n".join(
            ["| " + " | ".join(md_cell(c) for c in header) + " |",
             "|" + "---|" * len(header)]
            + ["| " + " | ".join(md_cell(c) for c in r) + " |" for r in body]))
        html_parts.append("<table>" + "".join(
            "<tr>" + "".join(f"<{tag}>{html.escape(c)}</{tag}>" for c in r) + "</tr>"
            for tag, r in [("th", header)] + [("td", r) for r in body]) + "</table>")
        if truncated:
            note = f"(Only the first {MAX_ROWS_PER_SHEET} rows of this sheet.)"
            md_parts.append(note)
            html_parts.append(f"<p>{note}</p>")
    return "\n".join(html_parts), "\n\n".join(md_parts)
