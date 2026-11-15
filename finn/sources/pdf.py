"""Filing PDFs: download, page-level text, and a scanned-document check.

Text is kept per page so every extracted fact can cite the page it came from. Results
filings are often scanned images; `is_scanned` tells callers to send the PDF itself to a
multimodal model instead of the (empty) text layer.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import re

from ..config import CACHE_DIR
from ..net import Fetcher, FetchError, fetcher

logging.getLogger("pypdf").setLevel(logging.ERROR)

TEXT_CACHE = CACHE_DIR / "pdftext"


_BSE_VIEWER = re.compile(r"^https?://www\.bseindia\.com/stockinfo/AnnPdfOpen\.aspx\?Pname=([\w\-]+\.pdf)$", re.IGNORECASE)


def direct_url(url: str) -> str | None:
    """BSE's AnnPdfOpen.aspx only redirects to the archived file; going there directly halves requests."""
    m = _BSE_VIEWER.match(url or "")
    return f"https://www.bseindia.com/xml-data/corpfiling/AttachHis/{m.group(1)}" if m else None


def download(url: str, *, net: Fetcher | None = None, max_bytes: int = 40_000_000, budget_s: float = 120) -> bytes:
    net = net or fetcher()
    kwargs = dict(max_age=None, max_bytes=max_bytes, budget_s=budget_s, timeout=45)
    if net.cached(url, None):  # downloaded before under its original link
        return net.get(url, **kwargs).content
    direct = direct_url(url)
    if direct:
        try:
            return net.get(direct, **kwargs).content
        except FetchError as exc:
            if exc.status not in (404, 403):  # very new filings may not be archived yet
                raise
    return net.get(url, **kwargs).content


def is_pdf(data: bytes) -> bool:
    return data[:1024].lstrip().startswith(b"%PDF")


def page_texts(data: bytes, max_pages: int | None = None) -> list[str]:
    """Text of each page ('' where a page has no text layer). Cached by content hash."""
    from pypdf import PdfReader

    TEXT_CACHE.mkdir(parents=True, exist_ok=True)
    cache = TEXT_CACHE / f"{hashlib.sha1(data).hexdigest()}-{max_pages or 'all'}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    texts: list[str] = []
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        for index, page in enumerate(reader.pages):
            if max_pages is not None and index >= max_pages:
                break
            try:
                texts.append(" ".join((page.extract_text() or "").split()))
            except Exception:  # noqa: BLE001 - one malformed page must not lose the document
                texts.append("")
    except Exception:  # noqa: BLE001 - encrypted or corrupt file
        texts = []
    cache.write_text(json.dumps(texts, ensure_ascii=False), encoding="utf-8")
    return texts


def page_count(data: bytes) -> int:
    from pypdf import PdfReader

    try:
        return len(PdfReader(io.BytesIO(data), strict=False).pages)
    except Exception:  # noqa: BLE001
        return 0


def is_scanned(texts: list[str]) -> bool:
    """True when the document has (almost) no text layer."""
    if not texts:
        return True
    return sum(len(t) for t in texts) / len(texts) < 150


def head(data: bytes, pages: int) -> bytes:
    """A new PDF containing only the first `pages` pages (keeps multimodal calls small)."""
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(io.BytesIO(data), strict=False)
    if len(reader.pages) <= pages:
        return data
    writer = PdfWriter()
    for page in reader.pages[:pages]:
        writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def with_page_marks(texts: list[str], start: int = 0, budget_chars: int | None = None) -> str:
    """Join pages as '[p.N] text' so a model can cite page numbers. Truncates at `budget_chars`."""
    out, used = [], 0
    for offset, text in enumerate(texts):
        if not text:
            continue
        chunk = f"[p.{start + offset + 1}] {text}"
        if budget_chars is not None and used + len(chunk) > budget_chars:
            remaining = budget_chars - used
            if remaining > 500:
                out.append(chunk[:remaining])
            break
        out.append(chunk)
        used += len(chunk)
    return "\n\n".join(out)
