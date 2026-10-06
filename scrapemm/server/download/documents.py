"""Documents behind a URL: spreadsheets, PDFs, office files, archives.

A browser that opens such a URL shows no page: it downloads the file (often only after a
bot check, e.g. a "Verifying your browser..." page, has passed). What is cheap to read is
turned into text -- spreadsheets into tables, one per sheet. The rest is reported as a
document type scrapeMM does not extract, never as the bot check that preceded it.
"""

import csv
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
    return suffix if suffix in DOCUMENT_EXTENSIONS else None


def is_document_content_type(content_type: Optional[str]) -> bool:
    content_type = (content_type or "").split(";")[0].strip().lower()
    return any(content_type.startswith(t) for t in DOCUMENT_CONTENT_TYPES)


def to_text(path: Path, name: str) -> Optional[tuple[str, str]]:
    """(HTML, Markdown) of the document at `path` (named `name`, for its type), or None
    if scrapeMM does not read documents of its type."""
    extension = document_extension(name) or Path(path).suffix.lower()
    if extension in SPREADSHEET_EXTENSIONS:
        tables = _spreadsheet_tables(path)
    elif extension in TEXT_TABLE_EXTENSIONS:
        tables = _csv_tables(path, name)
    else:
        return None
    if tables is None:
        return None
    return _render(name, tables)


def _spreadsheet_tables(path: Path) -> Optional[list[tuple[str, list[list[str]], bool]]]:
    try:
        import openpyxl
    except ImportError:
        logger.info("openpyxl is not installed; spreadsheets cannot be read.")
        return None
    try:
        # From a file object: a download's file has no extension, which openpyxl, given
        # a path, takes for an unsupported format
        workbook = openpyxl.load_workbook(BytesIO(Path(path).read_bytes()), read_only=True,
                                          data_only=True)
    except Exception as e:
        logger.info(f"Could not open the spreadsheet {path.name}: {type(e).__name__}: {e}")
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


def _csv_tables(path: Path, name: str) -> list[tuple[str, list[list[str]], bool]]:
    text = path.read_bytes().decode("utf-8-sig", errors="replace")
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
