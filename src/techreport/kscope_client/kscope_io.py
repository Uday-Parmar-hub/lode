"""Filesystem helpers for the Kaleidoscope fetcher.

Pure disk/path concerns, deliberately kept out of the network client in
:mod:`prm.fetchers.kscope`: magic-byte extension sniffing (v4.6 lesson
L9), content hashing, the collision-proof relative-path policy, and a
non-clobbering save helper. **Nothing in this module makes a network
call**, which keeps both it and the client independently testable.

The path policy here is the structural fix for the legacy downloader's
body-collision bug (``scripts/legacy/kscope_archive_downloader.py``): by
embedding the content SHA-256 in every filename, two different document
bodies can never overwrite each other on disk, regardless of any
``source_doc_id`` collision.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from pathlib import Path

# Map the fetcher's ``source`` value to the on-disk directory segment, so the
# live fetcher's layout matches the existing ``kscope_archive/`` tree
# (``AEM/pr/...``, ``AEM/sedar/...``, ``AEM/edgar/...``).
_SOURCE_DIR: dict[str, str] = {
    "kscope_pr": "pr",
    "kscope_sedar": "sedar",
    "kscope_edgar": "edgar",
}

# Characters allowed in a single path segment; everything else collapses to "_".
_SAFE_SEGMENT_RE = re.compile(r"[^A-Za-z0-9._-]+")


def sha256_hex(content: bytes) -> str:
    """Return the full hex SHA-256 digest of ``content``."""
    return hashlib.sha256(content).hexdigest()


def detect_extension(content: bytes) -> str:
    """Guess a file extension from leading magic bytes (v4.6 lesson L9).

    Kscope document bodies do not always carry a reliable ``Content-Type``
    header, so we sniff the content itself rather than trusting the
    response. Recognises PDF, HTML/XML, and ZIP containers; falls back to
    ``.txt`` for decodable text and ``.bin`` for anything else.
    """
    head = content[:8]
    if head.startswith(b"%PDF"):
        return ".pdf"
    if head.startswith(
        (b"<!DOCTYPE", b"<!doctype", b"<html", b"<HTML", b"<?xml", b"<div", b"<head", b"<body")
    ):
        return ".html"
    if head.startswith(b"PK"):
        return ".zip"
    try:
        sample = content[:500].decode("utf-8")
    except UnicodeDecodeError:
        return ".bin"
    lowered = sample.lower()
    if "<html" in lowered or "<div" in lowered:
        return ".html"
    return ".txt"


def _safe_segment(value: str, *, max_len: int = 80) -> str:
    """Make ``value`` safe to use as a single filesystem path segment."""
    cleaned = _SAFE_SEGMENT_RE.sub("_", value.strip()).strip("_")
    return cleaned[:max_len] or "unknown"


def build_relative_path(
    *,
    ticker: str,
    source: str,
    doc_type: str,
    doc_date: dt.date,
    sha256: str,
    ext: str,
) -> str:
    """Build the collision-proof archive-relative path for a document body.

    Form: ``{TICKER}/{source_dir}/{doc_type}/{YYYY-MM-DD}_{sha12}{ext}``.

    The 12-character content hash in the filename guarantees that two
    different bodies can never overwrite each other on disk, even when they
    share a synthesized ``source_doc_id`` — the structural fix for the
    legacy press-release body-collision bug.

    Args:
        ticker: Issuer ticker (becomes the top-level directory).
        source: One of ``kscope_pr`` / ``kscope_sedar`` / ``kscope_edgar``.
        doc_type: Normalized document type (second directory level).
        doc_date: Date used as the filename prefix (publish date, or the
            caller's fallback when the document had no publish date).
        sha256: Full hex content digest; the first 12 chars go in the name.
        ext: File extension including the leading dot (from
            :func:`detect_extension`).

    Returns:
        A POSIX-style relative path string (forward slashes).
    """
    source_dir = _SOURCE_DIR.get(source, _safe_segment(source))
    filename = f"{doc_date.isoformat()}_{sha256[:12]}{ext}"
    return "/".join(
        (
            _safe_segment(ticker),
            source_dir,
            _safe_segment(doc_type),
            filename,
        )
    )


def save_document_safe(target: Path, content: bytes) -> Path:
    """Write ``content`` to ``target`` without ever clobbering differing bytes.

    Behaviour:

    * **Target absent** — parent directories are created and the bytes are
      written.
    * **Target present, identical content** — no-op (idempotent re-fetch);
      the existing file is left untouched.
    * **Target present, different content** — :class:`FileExistsError` is
      raised, quoting both SHA-256 digests, and nothing is written.

    This is the deliberate inverse of the legacy downloader's
    ``open(path, "wb")`` truncate-write, which silently overwrote ~2,229
    press-release bodies down to 30 files on disk.

    Args:
        target: Absolute path to write to.
        content: The document bytes.

    Returns:
        The ``target`` path (written, or pre-existing and identical).

    Raises:
        FileExistsError: If ``target`` exists with different content.
    """
    new_sha = sha256_hex(content)
    if target.exists():
        existing_sha = sha256_hex(target.read_bytes())
        if existing_sha == new_sha:
            return target
        raise FileExistsError(
            f"refusing to overwrite {target}: existing sha256={existing_sha} "
            f"differs from new sha256={new_sha}"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return target


__all__ = [
    "build_relative_path",
    "detect_extension",
    "save_document_safe",
    "sha256_hex",
]
