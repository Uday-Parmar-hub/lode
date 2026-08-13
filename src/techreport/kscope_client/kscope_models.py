"""Value types, exceptions, and constants for the Kaleidoscope fetcher.

Leaf module (standard library only) shared by :mod:`prm.fetchers.kscope`
and :mod:`prm.fetchers.kscope_transport`: the :class:`DocumentRecord` /
:class:`BodyResult` / :class:`FetchAttempt` records, the exception
hierarchy, the doc-type vocabulary constants, and the default body-fetch
chain. It imports nothing else from the package, so neither sibling can
create a circular dependency through it.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

BASE_URL = "https://api.kscope.io"

# ---------------------------------------------------------------------
# Document-type vocabulary (moved here from prm.jobs.ingest_archive so the
# fetcher and the archive ingester share one source of truth; ingest_archive
# now imports DOC_TYPE_NORMALIZATION from this module).
#
# Maps the archive/category vocabulary -> the small, stable normalized
# ``documents.doc_type`` set that Phase 4 signal rules match against. The raw
# value is preserved in ``documents.raw_doc_type`` / ``DocumentRecord.raw_doc_type``.
# ---------------------------------------------------------------------
DOC_TYPE_NORMALIZATION: dict[str, str] = {
    "press_release": "press_release",
    "news_releases": "press_release",  # SEDAR news releases are PRs in practice
    "mda": "mda",
    "interim_financials": "interim_financials",
    "annual_financials": "annual_financials",
    "annual_reports": "annual_report",
    "ni43101": "ni43101",
    "ni43101_supporting": "ni43101_supporting",
    "edgar_6k": "edgar_6k",
    "sk1300": "sk1300",
}

# SEDAR ``document_kdesc`` -> archive category key. Keyed to the
# DOC_TYPE_NORMALIZATION vocabulary above so the two compose cleanly:
# kdesc -> category -> normalized doc_type.
SEDAR_KDESC_PATTERNS: dict[str, tuple[str, ...]] = {
    "ni43101": (
        "technical report (ni 43-101)",
        "amended & restated technical report (ni 43-101)",
    ),
    "ni43101_supporting": (
        "consent of qualified person (ni 43-101)",
        "certificate of qualified person (ni 43-101)",
    ),
    "news_releases": ("news release",),
    "mda": ("management's discussion and analysis", "md&a"),
    "interim_financials": (
        "interim financial statements/report",
        "interim financial statement",
    ),
    "annual_financials": (
        "audited annual financial statements",
        "annual financial statements",
    ),
    "annual_reports": ("annual report", "annual information form"),
}


# =====================================================================
# EXCEPTIONS
# =====================================================================


class KscopeError(Exception):
    """Base class for every error raised by this module."""


class KscopeFetchError(KscopeError):
    """A request failed permanently (after retries / all body candidates).

    Carries structured detail so callers can react without re-parsing
    strings: the URL attempted (API key redacted), the HTTP status if any,
    every candidate URL tried (for body-fallback chains), and the full
    per-attempt log.
    """

    def __init__(
        self,
        message: str,
        *,
        url: str | None = None,
        http_status: int | None = None,
        candidate_urls: Sequence[str] | None = None,
        attempts: Sequence[FetchAttempt] | None = None,
    ) -> None:
        super().__init__(message)
        self.url = url
        self.http_status = http_status
        self.candidate_urls: list[str] = list(candidate_urls or [])
        self.attempts: list[FetchAttempt] = list(attempts or [])


class KscopeAuthError(KscopeFetchError):
    """Authentication or permission failure (HTTP 401/403). Not transient."""


class KscopeRateLimitError(KscopeFetchError):
    """Rate limit exceeded — a server 429 beyond the max wait, or the local budget."""


class KscopeNotFoundError(KscopeFetchError):
    """HTTP 404 — unknown identifier or no data. Frequently expected (e.g. a
    ticker that simply isn't present on a given endpoint)."""


class _RetryableServerError(KscopeError):
    """Internal sentinel: a 5xx response that tenacity should retry."""

    def __init__(self, status: int) -> None:
        super().__init__(f"server error {status}")
        self.status = status


class _RetryableRateLimitError(KscopeError):
    """Internal sentinel: a 429 that tenacity should retry after ``reset_in`` seconds."""

    def __init__(self, reset_in: int) -> None:
        super().__init__(f"rate limited, reset_in={reset_in}")
        self.reset_in = reset_in


# =====================================================================
# DATA RECORDS
# =====================================================================


@dataclass(frozen=True, slots=True)
class FetchAttempt:
    """One rung of a fetch, for observability and structured error payloads."""

    strategy: str
    url: str  # API key redacted
    http_status: int | None = None
    bytes: int | None = None
    elapsed_ms: float | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class DocumentRecord:
    """Listing-time metadata for one document, aligned to the ``documents`` schema.

    Field names mirror ``documents`` columns (``source_doc_id``,
    ``doc_type``, ``raw_doc_type``, ``published_at``, ``body_url_html`` …),
    so ingestion is a near-direct mapping.

    ``source_doc_id`` is final at listing time for every record **except**
    EDGAR exhibits that share a SEC accession number: those are marked
    ``raw_metadata['source_docid_shared'] = True`` and their final id is
    computed by :meth:`KscopeClient.fetch_body` from the body hash and
    surfaced via ``BodyResult.divergences['source_doc_id_final']``.

    ``raw_metadata['source_docid_raw']`` always holds Kscope's original
    ``docid`` (even when empty or synthesized) — the bridge back to Kscope
    for later re-fetch.
    """

    source: str
    source_doc_id: str
    doc_type: str
    raw_doc_type: str
    ticker: str
    title: str | None = None
    published_at: dt.datetime | None = None
    body_url_html: str | None = None
    body_url_pdf: str | None = None
    distributor: str | None = None
    issuer_name: str | None = None
    language: str = "en"
    raw_metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BodyResult:
    """The outcome of fetching a document body.

    ``relative_path`` is *recommended* — :meth:`KscopeClient.fetch_body`
    computes it but never writes to disk; persistence (via
    :func:`prm.fetchers.kscope_io.save_document_safe`) is the caller's
    choice. ``divergences`` records anything that differed from the source
    :class:`DocumentRecord`, e.g. ``fallback_used`` when a non-primary body
    URL won, or ``source_doc_id_final`` for shared-accession EDGAR exhibits.
    """

    content: bytes
    sha256: str
    content_type: str
    relative_path: str
    body_url_used: str  # API key redacted
    size_bytes: int
    divergences: dict[str, object] = field(default_factory=dict)
    attempts: tuple[FetchAttempt, ...] = ()


@dataclass(frozen=True, slots=True)
class BodyStrategy:
    """One rung of the configurable body-fetch fallback chain (v4.6 lesson L11).

    ``build_url`` returns a candidate URL for a given record, or ``None``
    when the strategy does not apply to that record.
    """

    label: str
    build_url: Callable[[DocumentRecord], str | None]


# ----- default body-fetch chain (order matches what the trial found worked) ---


def _strategy_listing_html(doc: DocumentRecord) -> str | None:
    """Primary: the ``html`` body URL the listing handed us."""
    return doc.body_url_html


def _strategy_pr_viewer(doc: DocumentRecord) -> str | None:
    """Fallback: construct the modern PR viewer URL from the raw docid."""
    raw = doc.raw_metadata.get("source_docid_raw")
    if doc.source != "kscope_pr" or not raw:
        return None
    return f"{BASE_URL}/v2/documents/viewer?docid={raw}&content=prnews&format=html"


def _strategy_ks_doc_view_legacy(doc: DocumentRecord) -> str | None:
    """Fallback: the legacy ``/ks-doc-view`` path (works when the modern viewer 403s)."""
    raw = doc.raw_metadata.get("source_docid_raw")
    if doc.source != "kscope_pr" or not raw:
        return None
    return f"{BASE_URL}/ks-doc-view?docid={raw}&content=prnews&format=html"


DEFAULT_BODY_CHAIN: tuple[BodyStrategy, ...] = (
    BodyStrategy("listing_html", _strategy_listing_html),
    BodyStrategy("pr_viewer_constructed", _strategy_pr_viewer),
    BodyStrategy("ks_doc_view_legacy", _strategy_ks_doc_view_legacy),
)


__all__ = [
    "BASE_URL",
    "DEFAULT_BODY_CHAIN",
    "DOC_TYPE_NORMALIZATION",
    "SEDAR_KDESC_PATTERNS",
    "BodyResult",
    "BodyStrategy",
    "DocumentRecord",
    "FetchAttempt",
    "KscopeAuthError",
    "KscopeError",
    "KscopeFetchError",
    "KscopeNotFoundError",
    "KscopeRateLimitError",
]
