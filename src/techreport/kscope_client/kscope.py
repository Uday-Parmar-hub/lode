"""Production Kaleidoscope/Kscope API fetcher.

Refactor of ``scripts/legacy/kscope_archive_downloader.py`` into a
stateless, thread-safe client that feeds the Phase 2+ data model. The
trial-era lessons (catalogued in ``specs/design_kscope_fetcher.md``) are
preserved as code across this package:

* **L1** — the API key is appended to the URL string (never via httpx
  ``params=``, which 0.28 fails to merge with a pre-existing query
  string), and a request event hook re-asserts it on the wire.
* **L2** — no ``Content-Type`` header on GETs.
* **L3** — 429 responses honour the server's ``reset_in`` / ``Retry-After``.
* **L4** — an internal per-endpoint rate limiter (pace + hourly budget).
* **L5** — SEDAR identifiers try ``{ticker}:CA`` then bare; PR uses bare.
* **L6** — the press-release endpoint is paginated at 50 items/page.
* **L8** — a zero-byte ``200`` is treated as a failed fetch.
* **L9** — file extensions are sniffed from magic bytes (see
  :mod:`prm.fetchers.kscope_io`).
* **L10** — date windows use ``sd``/``ed`` Unix timestamps (the broken
  ``year`` filter is never sent); client-side filtering is authoritative.
* **L11** — body fetching is an ordered, configurable fallback chain.
* **L12** — transient errors retry with exponential backoff (via tenacity).

It also fixes the legacy body-collision bug at the source: every
``source_doc_id`` is stable even when Kscope's ``docid`` is empty
(bug-fix #1), and every body path embeds the content hash (bug-fix #2,
in :mod:`prm.fetchers.kscope_io`).

This module is the orchestration layer (:class:`KscopeClient`); value
types, exceptions, and constants live in :mod:`prm.fetchers.kscope_models`
and the HTTP transport/rate-limiter in :mod:`prm.fetchers.kscope_transport`.
Those names are re-exported here so the public import surface is unchanged.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Sequence

import httpx
import structlog

from .kscope_io import build_relative_path, detect_extension, sha256_hex
from .kscope_models import (
    BASE_URL,
    DEFAULT_BODY_CHAIN,
    DOC_TYPE_NORMALIZATION,
    SEDAR_KDESC_PATTERNS,
    BodyResult,
    BodyStrategy,
    DocumentRecord,
    FetchAttempt,
    KscopeAuthError,
    KscopeError,
    KscopeFetchError,
    KscopeNotFoundError,
    KscopeRateLimitError,
)
from .kscope_transport import _RateLimiter, _redact_key, _RequestExecutor

log = structlog.get_logger(__name__)

# =====================================================================
# CONFIG / CONSTANTS
# =====================================================================

DEFAULT_PER_ENDPOINT_HOURLY_BUDGET = 3000  # trial-key value; production TBD
DEFAULT_MIN_INTERVAL_S = 0.3
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE_S = 1.0
DEFAULT_BACKOFF_MAX_S = 30.0
DEFAULT_MAX_RATE_WAIT_S = 3600
DEFAULT_TIMEOUT_S = 60.0

# Exchange tokens the wire feed may include in a release's `stocks` list. Used to
# detect ticker collisions (same symbol, different issuer on another exchange).
_PR_EXCHANGES: frozenset[str] = frozenset({
    "NASDAQ", "NYSE", "NYSEAMERICAN", "NYSE AMERICAN", "AMEX", "OTC", "OTCMKTS",
    "OTCQB", "OTCQX", "TSX", "TSXV", "TSX-V", "TSX VENTURE", "CSE", "NEO",
    "LSE", "ASX", "JSE", "HKEX",
})
PR_PAGE_SIZE = 50  # L6: the PR endpoint silently caps page size at 50
SEDAR_PAGE_SIZE = 100  # downloader pattern: start/limit, 100 per page
SEC_PAGE_LIMIT = 25

# Safety caps so an unbounded full-history pull (no ``since``) can't page forever.
# SEDAR_MAX_PAGES is env-overridable so a "fast floor" count can peek just the first
# pages (the SEDAR feed is roughly newest-first; it ignores server-side date/sort).
PR_MAX_PAGES = 40
SEDAR_MAX_PAGES_DEFAULT = 30


def _sedar_max_pages() -> int:
    """SEDAR page-peek cap, read at call time so the env override actually applies — a
    low value (e.g. the daily fetch's 3) peeks just the newest pages instead of paging
    a ticker's full SEDAR history every run."""
    return int(os.getenv("SEDAR_MAX_PAGES", str(SEDAR_MAX_PAGES_DEFAULT)))

# Logical listing endpoints. These names double as rate-limit bucket keys (L4).
EP_PR_LISTING = "pr_listing"
EP_SEDAR_LISTING = "sedar_listing"
EP_SEC_LISTING = "sec_listing"
EP_BODY = "body"

# Normalized doc_type -> the listing endpoint(s) that can produce it. Routing
# activates every endpoint whose produced set intersects the requested
# doc_types (or all three when doc_types is None).
_PR_DOC_TYPES = frozenset({"press_release"})
# NOTE: press_release deliberately NOT in _SEDAR_DOC_TYPES even though SEDAR
# news releases normalize to press_release. Including it caused full-archive
# SEDAR pagination on incremental fetches (no server-side date filter on
# SEDAR). See specs/design_kscope_fetcher.md §4 production finding 2026-06-03.
# Universe mode (doc_types=None) still gets SEDAR news releases via natural
# endpoint inclusion; the targeted press_release filter now means "wire PRs
# only" per the routing layer.
_SEDAR_DOC_TYPES = frozenset(
    {
        "mda",
        "interim_financials",
        "annual_financials",
        "annual_report",
        "ni43101",
        "ni43101_supporting",
    }
)
_SEC_DOC_TYPES = frozenset({"edgar_6k", "sk1300"})

# SEC query specs by normalized doc_type. The S-K 1300 exhibit search omits the
# enterprise-tier ``exp`` parameter (L10): it is best-effort until the plan is
# confirmed, and the historical S-K 1300 corpus already lives in the archive.
_SEC_QUERY_SPECS: dict[str, dict[str, str]] = {
    "edgar_6k": {"content": "sec", "form": "6-K"},
    "sk1300": {"content": "exhibits", "form": "10-K;10-Q;8-K"},
}

# Press-release distributor canonicalization (lifted from the v4.6 evaluator),
# used to populate ``DocumentRecord.distributor`` from the PR ``meta.author``.
_DISTRIBUTOR_MAP: dict[str, str] = {
    "globenewswire": "GlobeNewswire",
    "globenewswireinc": "GlobeNewswire",
    "accessnewswire": "Access Newswire",
    "accesswire": "ACCESSWIRE",
    "prnewswire": "PRNewswire",
    "prnewswireus": "PRNewswire",
    "prnewswireassociation": "PRNewswire",
    "thenewswire": "TheNewswire",
    "thenewswireca": "TheNewswire",
    "businesswire": "Business Wire",
    "newsfile": "Newsfile",
    "newsfilecorp": "Newsfile",
    "einpresswire": "EIN Presswire",
    "cnw": "CNW Group",
    "cnwgroup": "CNW Group",
    "canadanewswire": "CNW Group",
    "prweb": "PRWeb",
    "cisionprweb": "PRWeb",
    "cision": "Cision",
    "abdigital": "AB Digital",
    "abdigitalinc": "AB Digital",
    "comserve": "COMSERVE",
    "prcom": "PR.com",
    "webwire": "WebWire",
}

_SLUG_RE = re.compile(r"[^a-z0-9]+")


# =====================================================================
# MODULE HELPERS
# =====================================================================


def normalize_source_name(name: str) -> str:
    """Canonicalize a press-release distributor name (e.g. ``PR Newswire`` -> ``PRNewswire``)."""
    collapsed = re.sub(r"\s+", " ", name.strip())
    key = re.sub(r"[^a-z0-9]", "", collapsed.lower())
    return _DISTRIBUTOR_MAP.get(key, collapsed)


def _slug(value: str) -> str:
    """Lowercase slug safe for use as a doc_type fallback key."""
    return _SLUG_RE.sub("_", value.lower()).strip("_")


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _from_numeric(value: float) -> dt.datetime | None:
    if value > 1e12:  # milliseconds
        value /= 1000.0
    try:
        return dt.datetime.fromtimestamp(value, tz=dt.UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _parse_kscope_timestamp(value: object) -> dt.datetime | None:
    """Parse the timestamp shapes Kscope returns into a tz-aware UTC datetime.

    Handles Unix seconds/millis (int/float/numeric string), 8-digit SEDAR
    dates (``YYYYMMDD``), and ISO-8601 strings. Returns ``None`` (logging a
    warning) for anything unrecognised.
    """
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    if isinstance(value, bool):  # guard: bool is an int subclass
        return None
    if isinstance(value, (int, float)):
        iv = int(value)
        # SEDAR date_filed arrives as an 8-digit YYYYMMDD integer (e.g. 20260611),
        # which is NOT a Unix timestamp — real Unix ts are ~1.7e9, far outside this
        # range. Parse it as a calendar date; otherwise treat as Unix seconds/millis.
        if 19000101 <= iv <= 21001231:
            try:
                return dt.datetime.strptime(str(iv), "%Y%m%d").replace(tzinfo=dt.UTC)
            except ValueError:
                return None
        return _from_numeric(float(value))
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if s.isdigit():
            if len(s) == 8:
                try:
                    return dt.datetime.strptime(s, "%Y%m%d").replace(tzinfo=dt.UTC)
                except ValueError:
                    return None
            return _from_numeric(float(s))
        try:
            parsed = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            log.warning("kscope_timestamp_unparseable", value=s)
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
    return None


def _synthesize_source_doc_id(
    *,
    raw_docid: str,
    html_url: str | None,
    published: dt.datetime | None,
    teaser: str,
) -> str:
    """Produce a stable ``source_doc_id`` from listing metadata (bug-fix #1).

    Fallback order matches the synthesis contract in
    ``src/prm/jobs/ingest_archive.py``: the raw docid when present, else a
    hash of the body URL, else a hash of ``published + teaser``, else a
    last-resort UUID (which is logged because it is not reproducible).
    """
    raw = raw_docid or ""
    if raw:
        return raw
    if html_url:
        return f"sha:{sha256_hex(html_url.encode('utf-8'))[:16]}"
    basis = f"{published.isoformat() if published else ''}{teaser[:100]}"
    if basis:
        return f"sha:{sha256_hex(basis.encode('utf-8'))[:16]}"
    new_id = f"uuid:{uuid.uuid4()}"
    log.warning("source_doc_id_uuid_fallback", source_doc_id=new_id)
    return new_id


def _sedar_category(kdesc: str) -> str:
    """Map a SEDAR ``document_kdesc`` to its archive category key."""
    low = kdesc.lower()
    for category, patterns in SEDAR_KDESC_PATTERNS.items():
        if any(pattern in low for pattern in patterns):
            return category
    return _slug(kdesc) or "sedar_other"


def _shared_raw_docids(items: Iterable[dict], *, key: str) -> set[str]:
    """Return the non-empty values of ``key`` that appear more than once."""
    counts: Counter[str] = Counter((item.get(key) or "") for item in items)
    return {value for value, count in counts.items() if value and count > 1}


# =====================================================================
# PUBLIC CLIENT
# =====================================================================


class KscopeClient:
    """Stateless (per-call) Kaleidoscope/Kscope API fetcher.

    The caller supplies a ticker / timestamp / record and gets results
    back; the client keeps no cross-call cursor of its own. The Phase 6
    scheduler drives incremental polling by passing a stored "last-seen"
    timestamp into :meth:`iter_documents_since`.

    **Ticker universe.** Kscope has no global "all issuers since T"
    endpoint, so listing fans out over a known ticker list. That universe
    is injected at construction (``tickers=...``) and used whenever a
    method is called with ``tickers=None``. **The client never reads the
    database** — the watchlist is supplied by the scheduler/CLI.

    **Threading.** This client is intended to be *shared* across threads
    (e.g. concurrent APScheduler jobs). Do not instantiate one per thread,
    as that would bypass the shared per-endpoint rate limiting that keeps
    us within Kscope's ~3,600/hour-per-endpoint budget.

    The API key is read from the ``KSCOPE_API_KEY`` environment variable
    unless passed explicitly; it never appears in source.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        tickers: Sequence[str] | None = None,
        base_url: str = BASE_URL,
        per_endpoint_hourly_budget: int = DEFAULT_PER_ENDPOINT_HOURLY_BUDGET,
        min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base_s: float = DEFAULT_BACKOFF_BASE_S,
        backoff_max_s: float = DEFAULT_BACKOFF_MAX_S,
        max_rate_wait_s: int = DEFAULT_MAX_RATE_WAIT_S,
        body_chain: Sequence[BodyStrategy] | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        key = api_key or os.getenv("KSCOPE_API_KEY") or ""
        if not key:
            raise KscopeAuthError("KSCOPE_API_KEY is not set (pass api_key= or set the env var)")
        self._default_tickers: tuple[str, ...] = tuple(tickers or ())
        self._body_chain: tuple[BodyStrategy, ...] = (
            tuple(body_chain) if body_chain is not None else DEFAULT_BODY_CHAIN
        )
        self._limiter = _RateLimiter(
            hourly_budget=per_endpoint_hourly_budget,
            min_interval_s=min_interval_s,
            clock=clock,
            sleeper=sleeper,
        )
        self._executor = _RequestExecutor(
            api_key=key,
            base_url=base_url,
            limiter=self._limiter,
            max_retries=max_retries,
            backoff_base_s=backoff_base_s,
            backoff_max_s=backoff_max_s,
            max_rate_wait_s=max_rate_wait_s,
            timeout_s=timeout_s,
            sleeper=sleeper,
            transport=transport,
        )

    # ----- lifecycle -----

    def __enter__(self) -> KscopeClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        self._executor.close()

    # ----- public API -----

    def iter_documents_since(
        self,
        timestamp: dt.datetime,
        tickers: list[str] | None = None,
        doc_types: list[str] | None = None,
        on_listing_error: Callable[[str, str, Exception], None] | None = None,
    ) -> Iterator[DocumentRecord]:
        """Yield documents published at or after ``timestamp`` across the universe.

        The primitive the Phase 6 scheduler calls with a stored per-source
        cursor. Filtering on ``timestamp`` is client-side and authoritative
        (``published_at >= timestamp``); server-side ``sd``/``ed`` windows
        are only a payload optimisation. Records with no publish date are
        always yielded (we cannot prove they are old). Results are
        de-duplicated by ``(source, source_doc_id)`` within a single call.

        **Boundary documents are re-emitted on consecutive calls by
        design.** Delivery is at-least-once: a document whose
        ``published_at`` equals the cursor will appear again on the next
        call (always with an identical, stable ``source_doc_id``). Callers
        must use idempotent writes (``on_conflict_do_nothing`` or
        equivalent) so the boundary document is persisted exactly once. This
        overlap is what keeps the cursor gap-free under same-second
        publishing bursts and clock skew.

        Args:
            timestamp: Inclusive lower bound, compared against
                ``published_at`` (tz-aware UTC recommended).
            tickers: Tickers to sweep. ``None`` uses the universe injected
                at construction; an empty universe raises.
            doc_types: Normalized doc types to include. ``None`` means all;
                otherwise only the matching endpoints are queried.

        Yields:
            :class:`DocumentRecord` listing-time metadata.

        Raises:
            KscopeFetchError: If no ticker universe is available.
        """
        universe = tickers if tickers is not None else list(self._default_tickers)
        if not universe:
            raise KscopeFetchError(
                "no tickers to query: pass tickers=[...] or construct "
                "KscopeClient(tickers=[...])"
            )
        requested = self._normalize_doc_types(doc_types)
        seen: set[tuple[str, str]] = set()
        for ticker in universe:
            yield from self._iter_for_ticker(ticker, requested, timestamp, seen, on_listing_error)

    def iter_documents_for_ticker(
        self,
        ticker: str,
        doc_types: list[str] | None = None,
        since: dt.datetime | None = None,
        on_listing_error: Callable[[str, str, Exception], None] | None = None,
    ) -> Iterator[DocumentRecord]:
        """Yield documents for a single ticker, optionally bounded by ``since``.

        The single-ticker primitive that :meth:`iter_documents_since` fans
        out over. With ``since=None`` it returns full available history
        (bounded by internal pagination safety caps).

        Args:
            ticker: Issuer ticker (bare; the SEDAR path also tries the
                ``:CA`` suffix internally).
            doc_types: Normalized doc types to include (``None`` = all).
            since: Optional inclusive ``published_at`` lower bound.

        Yields:
            :class:`DocumentRecord` listing-time metadata, de-duplicated by
            ``(source, source_doc_id)`` within the call.
        """
        requested = self._normalize_doc_types(doc_types)
        yield from self._iter_for_ticker(ticker, requested, since, set(), on_listing_error)

    def fetch_body(self, doc: DocumentRecord) -> BodyResult:
        """Fetch the body bytes for a previously listed document.

        Walks the configurable fallback chain (v4.6 lesson L11) in order,
        treating a zero-byte ``200`` as a failure (L8) and continuing to the
        next candidate. Returns the first non-empty body as a
        :class:`BodyResult` (computing — but **not** writing — the
        recommended on-disk path). For EDGAR exhibits that shared a SEC
        accession number, the final ``source_doc_id`` is computed here from
        the body hash and reported in ``divergences['source_doc_id_final']``.

        Args:
            doc: A record previously produced by one of the ``iter_*`` methods.

        Returns:
            :class:`BodyResult` with content, hash, content type, recommended
            relative path, the URL that succeeded, and any divergences.

        Raises:
            KscopeFetchError: If every candidate in the chain fails; the
                error carries ``candidate_urls`` and the full ``attempts`` log.
        """
        candidate_urls: list[str] = []
        all_attempts: list[FetchAttempt] = []
        last_error: KscopeFetchError | None = None

        for strategy in self._body_chain:
            url = strategy.build_url(doc)
            if not url:
                continue
            candidate_urls.append(_redact_key(url))
            try:
                resp, attempts = self._executor.request_bytes(
                    url, bucket=EP_BODY, label=strategy.label
                )
            except KscopeFetchError as exc:
                all_attempts.extend(exc.attempts)
                last_error = exc
                continue
            all_attempts.extend(attempts)
            content = resp.content or b""
            if not content:  # L8: zero-byte 200 is not a usable body
                last_error = KscopeFetchError(
                    "zero-byte 200 response",
                    url=_redact_key(url),
                    http_status=200,
                    attempts=attempts,
                )
                continue
            return self._build_body_result(doc, resp, content, strategy.label, all_attempts)

        raise KscopeFetchError(
            f"all body candidates failed for {doc.source}:{doc.source_doc_id}",
            url=candidate_urls[-1] if candidate_urls else None,
            http_status=getattr(last_error, "http_status", None),
            candidate_urls=candidate_urls,
            attempts=all_attempts,
        ) from last_error

    # ----- internal: routing + iteration -----

    @staticmethod
    def _normalize_doc_types(doc_types: list[str] | None) -> frozenset[str] | None:
        return None if doc_types is None else frozenset(doc_types)

    @staticmethod
    def _endpoints_for(requested: frozenset[str] | None) -> set[str]:
        if requested is None:
            return {EP_PR_LISTING, EP_SEDAR_LISTING, EP_SEC_LISTING}
        endpoints: set[str] = set()
        if requested & _PR_DOC_TYPES:
            endpoints.add(EP_PR_LISTING)
        if requested & _SEDAR_DOC_TYPES:
            endpoints.add(EP_SEDAR_LISTING)
        if requested & _SEC_DOC_TYPES:
            endpoints.add(EP_SEC_LISTING)
        return endpoints

    def _iter_for_ticker(
        self,
        ticker: str,
        requested: frozenset[str] | None,
        since: dt.datetime | None,
        seen: set[tuple[str, str]],
        on_listing_error: Callable[[str, str, Exception], None] | None = None,
    ) -> Iterator[DocumentRecord]:
        # Defensive: compare like with like. A naive `since` is treated as UTC
        # so it never raises against tz-aware ``published_at`` values.
        if since is not None and since.tzinfo is None:
            since = since.replace(tzinfo=dt.UTC)
        endpoints = self._endpoints_for(requested)
        sources: list[tuple[str, Iterator[DocumentRecord]]] = []
        if EP_PR_LISTING in endpoints:
            sources.append((EP_PR_LISTING, self._iter_pr_listing(ticker, since)))
        if EP_SEDAR_LISTING in endpoints:
            sources.append((EP_SEDAR_LISTING, self._iter_sedar_listing(ticker)))
        if EP_SEC_LISTING in endpoints:
            sources.append((EP_SEC_LISTING, self._iter_sec_listing(ticker, requested)))

        # Each endpoint is isolated: a listing failure on one (e.g. a SEDAR timeout for an
        # ambiguous bare ticker) is logged + reported via on_listing_error and skipped, so
        # it never discards the records another endpoint already returned for this ticker
        # (e.g. losing First Majestic's working SEC/EDGAR filings because SEDAR timed out).
        # Auth failures are global misconfiguration, so they propagate.
        for endpoint, source_iter in sources:
            try:
                for rec in source_iter:
                    if requested is not None and rec.doc_type not in requested:
                        continue
                    if since is not None and rec.published_at is not None and rec.published_at < since:
                        continue
                    key = (rec.source, rec.source_doc_id)
                    if key in seen:
                        continue
                    seen.add(key)
                    yield rec
            except KscopeAuthError:
                raise
            except KscopeFetchError as exc:
                log.warning("endpoint_listing_failed", ticker=ticker, endpoint=endpoint,
                            error=str(exc)[:200])
                if on_listing_error is not None:
                    on_listing_error(ticker, endpoint, exc)
                continue

    # ----- internal: per-endpoint listing -----

    def _iter_pr_listing(self, ticker: str, since: dt.datetime | None) -> Iterator[DocumentRecord]:
        """Paginate the press-release endpoint (bare ticker, 50/page — L5, L6)."""
        start = 0
        for _ in range(PR_MAX_PAGES):
            # NOTE: sd/ed deliberately NOT sent to PR endpoint.
            # Trial tier accepted these params; production tier returns 404 with sd present
            # (verified via httpx diagnostic on 2026-06-03 against api.kscope.io PR endpoint).
            # Client-side filtering (published_at >= since) is the authoritative date gate.
            # See specs/design_kscope_fetcher.md §L10 for the production finding.
            params: dict[str, object] = {"start": start, "limit": PR_PAGE_SIZE, "sort": "desc"}
            try:
                data = self._executor.request_json(
                    f"/v2/news/press-releases/{ticker}", params, bucket=EP_PR_LISTING
                )
            except KscopeNotFoundError:
                return  # bare ticker not present on the PR feed
            items = data.get("data") or []
            if not items:
                return
            page_oldest: dt.datetime | None = None
            for item in items:
                rec = self._pr_record(ticker, item)
                if rec.published_at is not None:
                    page_oldest = (
                        rec.published_at
                        if page_oldest is None
                        else min(page_oldest, rec.published_at)
                    )
                yield rec
            if len(items) < PR_PAGE_SIZE:
                return
            if since is not None and page_oldest is not None and page_oldest < since:
                return  # desc order: nothing newer than `since` remains
            start += PR_PAGE_SIZE

    def _pr_record(self, ticker: str, item: dict) -> DocumentRecord:
        meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
        raw_docid = item.get("docid") or ""
        html_url = item.get("html")
        published = _parse_kscope_timestamp(meta.get("published"))
        teaser = str(meta.get("teaser") or "")
        title = meta.get("title") or item.get("title") or (teaser[:200] or None)
        author = str(meta.get("author") or "")
        distributor = normalize_source_name(author) if author else None
        source_doc_id = _synthesize_source_doc_id(
            raw_docid=raw_docid, html_url=html_url, published=published, teaser=teaser
        )
        raw_metadata: dict[str, object] = {"source_docid_raw": raw_docid}
        if teaser:
            raw_metadata["teaser"] = teaser[:200]
        # The wire feed tags the listing exchange in `stocks` (e.g. ['BZ','NASDAQ']).
        # Captured so callers can catch ticker collisions (a TSXV ticker that also
        # exists on NASDAQ returns the wrong issuer's release — see disambiguation).
        stocks = meta.get("stocks") if isinstance(meta.get("stocks"), list) else []
        pr_exchange = next(
            (str(s).upper() for s in stocks if str(s).upper() in _PR_EXCHANGES), None
        )
        if pr_exchange:
            raw_metadata["pr_exchange"] = pr_exchange
        return DocumentRecord(
            source="kscope_pr",
            source_doc_id=source_doc_id,
            doc_type="press_release",
            raw_doc_type="press_release",
            ticker=ticker,
            title=title,
            published_at=published,
            body_url_html=html_url,
            body_url_pdf=item.get("pdf"),
            distributor=distributor,
            issuer_name=author or None,
            raw_metadata=raw_metadata,
        )

    def _iter_sedar_listing(self, ticker: str) -> Iterator[DocumentRecord]:
        """Paginate SEDAR, trying the ``:CA`` identifier then bare (L5)."""
        for identifier in (f"{ticker}:CA", ticker):
            try:
                yield from self._paginate_sedar(identifier, ticker)
                return  # first identifier that responds wins
            except KscopeNotFoundError:
                continue

    def _paginate_sedar(self, identifier: str, ticker: str) -> Iterator[DocumentRecord]:
        start = 0
        first_page = True
        for _ in range(_sedar_max_pages()):
            params: dict[str, object] = {"start": start, "limit": SEDAR_PAGE_SIZE}
            try:
                data = self._executor.request_json(
                    f"/v2/sedar/{identifier}", params, bucket=EP_SEDAR_LISTING
                )
            except KscopeNotFoundError:
                if first_page:
                    raise  # let the caller try the next identifier
                return  # 404 on a later page == end of data
            first_page = False
            items = data.get("data") or []
            if not items:
                return
            for item in items:
                yield self._sedar_record(ticker, item)
            total = data.get("total")
            if len(items) < SEDAR_PAGE_SIZE:
                return
            start += SEDAR_PAGE_SIZE
            if isinstance(total, int) and start >= total:
                return

    def _sedar_record(self, ticker: str, item: dict) -> DocumentRecord:
        kdesc = str(item.get("document_kdesc") or "").strip()
        raw_doc_type = _sedar_category(kdesc)
        doc_type = DOC_TYPE_NORMALIZATION.get(raw_doc_type, raw_doc_type)
        raw_docid = item.get("docid") or ""
        html_url = item.get("html")
        published = _parse_kscope_timestamp(item.get("date_filed") or item.get("accepted"))
        issuer = ((item.get("entities") or {}).get("issuer") or {}).get("name_e")
        source_doc_id = _synthesize_source_doc_id(
            raw_docid=raw_docid, html_url=html_url, published=published, teaser=kdesc
        )
        raw_metadata: dict[str, object] = {"source_docid_raw": raw_docid}
        if kdesc:
            raw_metadata["document_kdesc"] = kdesc
        return DocumentRecord(
            source="kscope_sedar",
            source_doc_id=source_doc_id,
            doc_type=doc_type,
            raw_doc_type=raw_doc_type,
            ticker=ticker,
            title=kdesc or None,
            published_at=published,
            body_url_html=html_url,
            body_url_pdf=item.get("pdf"),
            issuer_name=issuer,
            raw_metadata=raw_metadata,
        )

    def _iter_sec_listing(
        self, ticker: str, requested: frozenset[str] | None
    ) -> Iterator[DocumentRecord]:
        """Query EDGAR per requested SEC doc type via ``/v3/sec/search/{ticker}``."""
        wanted = _SEC_DOC_TYPES if requested is None else (requested & _SEC_DOC_TYPES)
        for doc_type_key in sorted(wanted):
            spec = _SEC_QUERY_SPECS.get(doc_type_key)
            if spec is None:
                continue
            params: dict[str, object] = {**spec, "limit": SEC_PAGE_LIMIT}
            try:
                data = self._executor.request_json(
                    f"/v3/sec/search/{ticker}", params, bucket=EP_SEC_LISTING
                )
            except KscopeNotFoundError:
                continue
            items = data.get("data") or []
            shared = _shared_raw_docids(items, key="acc")
            for item in items:
                yield self._sec_record(ticker, item, doc_type_key, shared)

    def _sec_record(
        self, ticker: str, item: dict, doc_type_key: str, shared: set[str]
    ) -> DocumentRecord:
        acc = item.get("acc") or ""
        html_url = item.get("html")
        published = _parse_kscope_timestamp(item.get("date"))
        is_shared = acc in shared
        if acc and not is_shared:
            source_doc_id = acc
        else:
            # Empty acc, or a *shared* accession whose provisional id must be
            # unique at listing time so dedup keeps every exhibit: synthesize
            # from the body URL -> published+teaser -> uuid. fetch_body later
            # finalizes shared records to "{acc}#{sha12}" via the body hash.
            source_doc_id = _synthesize_source_doc_id(
                raw_docid="",
                html_url=html_url,
                published=published,
                teaser=str(item.get("title") or ""),
            )
        raw_metadata: dict[str, object] = {"source_docid_raw": acc}
        if is_shared:
            raw_metadata["source_docid_shared"] = True
        doc_type = DOC_TYPE_NORMALIZATION.get(doc_type_key, doc_type_key)
        return DocumentRecord(
            source="kscope_edgar",
            source_doc_id=source_doc_id,
            doc_type=doc_type,
            raw_doc_type=doc_type_key,
            ticker=ticker,
            title=item.get("title"),
            published_at=published,
            body_url_html=html_url,
            body_url_pdf=item.get("pdf"),
            issuer_name=item.get("company_name"),
            raw_metadata=raw_metadata,
        )

    # ----- internal: body result assembly -----

    def _build_body_result(
        self,
        doc: DocumentRecord,
        resp: httpx.Response,
        content: bytes,
        label: str,
        attempts: list[FetchAttempt],
    ) -> BodyResult:
        sha = sha256_hex(content)
        ext = detect_extension(content)
        divergences: dict[str, object] = {}

        if doc.published_at is not None:
            doc_date = doc.published_at.date()
        else:
            doc_date = _utcnow().date()
            divergences["published_at_missing_used_fetch_date"] = True

        if label != self._body_chain[0].label:
            divergences["fallback_used"] = label

        # Two-phase finalization for shared-accession EDGAR exhibits (bug-fix #1).
        if doc.raw_metadata.get("source_docid_shared"):
            raw = str(doc.raw_metadata.get("source_docid_raw") or "")
            divergences["source_doc_id_final"] = f"{raw}#{sha[:12]}"

        relative_path = build_relative_path(
            ticker=doc.ticker,
            source=doc.source,
            doc_type=doc.doc_type,
            doc_date=doc_date,
            sha256=sha,
            ext=ext,
        )
        return BodyResult(
            content=content,
            sha256=sha,
            content_type=resp.headers.get("content-type", ""),
            relative_path=relative_path,
            body_url_used=_redact_key(str(resp.request.url)),
            size_bytes=len(content),
            divergences=divergences,
            attempts=tuple(attempts),
        )


__all__ = [
    "DOC_TYPE_NORMALIZATION",
    "BodyResult",
    "DocumentRecord",
    "FetchAttempt",
    "KscopeAuthError",
    "KscopeClient",
    "KscopeError",
    "KscopeFetchError",
    "KscopeNotFoundError",
    "KscopeRateLimitError",
]
