"""Postman — downloads documents discovered by Hunter.

Reads PENDING rows from the documents table, fetches the bytes from each
source_url, saves them to data/raw/, computes a SHA-256 content hash, and
flips parse_status to FETCHED. On any failure, sets FETCH_FAILED with the
error message — the run continues.

Implementation notes:
  * Streaming downloads (chunked I/O) so large 10-Ks never load fully into RAM.
  * Atomic writes (temp file + rename) so a crash mid-download never leaves
    a corrupted file at the final path.
  * Content-hash dedup: if two URLs serve byte-identical files, both rows can
    point to the same raw_path. (We don't currently re-link; we just record
    the hash so future analysis can detect this.)
  * One transaction per document, so partial progress is durable across crashes.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from app.config import settings
from app.db import Document, ParseStatus, get_session

logger = logging.getLogger(__name__)

CHUNK_SIZE = 8192
MAX_FILE_SIZE_BYTES = 100 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 60.0
KNOWN_EXTENSIONS = {".htm", ".html", ".pdf", ".txt", ".xml"}

_BSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bseindia.com/corporates/ann.html",
    "Accept": "application/pdf,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _pick_headers(url: str, default_user_agent: str) -> dict[str, str]:
    """BSE rejects the SEC-style UA (and vice-versa). Pick by host."""
    if "bseindia.com" in url:
        return _BSE_HEADERS
    return {"User-Agent": default_user_agent}


def _extract_extension(url: str) -> str:
    """Return the file extension from a URL path, lowercased and including the dot."""
    path = urlparse(url).path
    if "." not in path.rsplit("/", 1)[-1]:
        return ".bin"
    ext = "." + path.rsplit(".", 1)[-1].lower()
    if ext not in KNOWN_EXTENSIONS:
        logger.warning("Unrecognized extension %s for URL %s", ext, url)
    return ext


def _build_local_filename(
    ticker: str, period: str, doc_type: str, content_hash: str, ext: str
) -> str:
    """Construct a stable filename from doc metadata + first 8 chars of hash."""
    safe_period = period.replace(" ", "_")
    short_hash = content_hash[:8]
    return f"{ticker}_{safe_period}_{doc_type}_{short_hash}{ext}"


@retry(
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=1, min=1, max=3),
    reraise=True,
    retry=retry_if_exception_type(
        (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError)
    ),
)
def _do_download(url: str, dest_path: Path, headers: dict) -> str:
    """Single streaming download. Retries ONLY on network glitches.
    HTTP errors (404, 403, etc.) raise immediately — no retry.
    """
    tmp_path = dest_path.with_suffix(dest_path.suffix + ".tmp")
    hasher = hashlib.sha256()
    bytes_written = 0
    try:
        with httpx.stream(
            "GET", url, headers=headers,
            timeout=DOWNLOAD_TIMEOUT_SECONDS,
            follow_redirects=True,
        ) as response:
            response.raise_for_status()
            with open(tmp_path, "wb") as f:
                for chunk in response.iter_bytes(chunk_size=CHUNK_SIZE):
                    bytes_written += len(chunk)
                    if bytes_written > MAX_FILE_SIZE_BYTES:
                        raise ValueError(f"File too large: {url}")
                    hasher.update(chunk)
                    f.write(chunk)
        tmp_path.replace(dest_path)
        return hasher.hexdigest()
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


def _download_to_file(url: str, dest_path: Path, user_agent: str) -> str:
    """Try the URL. For BSE AttachLive URLs, fall back to AttachHis on 404.
    No retry decorator here — retry lives in _do_download for network errors only.
    """
    headers = _pick_headers(url, user_agent)

    # Build candidate URL list
    candidates = [url]
    if "AttachLive" in url:
        candidates.append(url.replace("AttachLive", "AttachHis"))

    last_error: Exception | None = None
    for i, candidate in enumerate(candidates):
        try:
            return _do_download(candidate, dest_path, headers)
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 404:
                raise  # 403/500/etc — give up immediately
            last_error = e
            if i < len(candidates) - 1:
                logger.info(
                    "404 on AttachLive, trying AttachHis: %s",
                    candidates[i + 1].rsplit("/", 1)[-1],
                )
            # else: this was the last candidate, fall through to raise
    raise last_error  # both AttachLive and AttachHis 404'd

def _process_one_document(doc_id: int, user_agent: str) -> tuple[bool, str | None]:
    """Fetch one document. Returns (success, error_message_or_None).

    Opens its own session so each doc commits independently.
    """
    with get_session() as s:
        doc = s.get(Document, doc_id)
        if doc is None:
            return False, f"Document {doc_id} not found"
        if doc.parse_status != ParseStatus.PENDING:
            return True, None

        company = doc.company
        ticker = company.ticker
        ext = _extract_extension(doc.source_url)

        temp_filename = (
            f"{ticker}_{doc.period.replace(' ', '_')}_{doc.document_type}_pending{ext}"
        )
        temp_dest = settings.raw_docs_dir / temp_filename

        try:
            content_hash = _download_to_file(doc.source_url, temp_dest, user_agent)
        except Exception as exc:
            doc.parse_status = ParseStatus.FETCH_FAILED
            doc.error_message = str(exc)[:500]
            logger.warning("FETCH_FAILED %s: %s", doc.source_url, exc)
            return False, str(exc)

        final_filename = _build_local_filename(
            ticker, doc.period, doc.document_type, content_hash, ext
        )
        final_dest = settings.raw_docs_dir / final_filename
        if final_dest != temp_dest:
            temp_dest.rename(final_dest)

        doc.content_hash = content_hash
        doc.raw_path = str(final_dest)
        doc.parse_status = ParseStatus.FETCHED
        doc.fetched_at = datetime.utcnow()

    return True, None


def run_postman() -> dict[str, int]:
    """Fetch every PENDING document. Returns a summary {key: count}."""
    if not settings.sec_user_agent:
        raise RuntimeError(
            "SEC_USER_AGENT must be set in .env. Required by SEC for all requests."
        )

    settings.ensure_dirs()
    user_agent = settings.sec_user_agent

    with get_session() as s:
        pending_ids = [
            row[0]
            for row in s.query(Document.id)
            .filter(Document.parse_status == ParseStatus.PENDING)
            .all()
        ]

    if not pending_ids:
        logger.info("Postman: no pending documents to fetch.")
        return {"pending": 0, "fetched": 0, "failed": 0}

    logger.info("Postman: %d pending documents to fetch", len(pending_ids))

    fetched = 0
    failed = 0
    for doc_id in pending_ids:
        success, _err = _process_one_document(doc_id, user_agent)
        if success:
            fetched += 1
        else:
            failed += 1

    summary = {"pending": len(pending_ids), "fetched": fetched, "failed": failed}
    logger.info("Postman complete: %s", summary)
    return summary