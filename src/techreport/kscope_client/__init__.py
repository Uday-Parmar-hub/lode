"""Vendored Kaleidoscope (Kscope) client — copied verbatim from MarketWatch
(prm.fetchers.kscope*) so this project is self-contained. Encodes the v4.6 transport
lessons (key-in-URL, no Content-Type on GETs, per-endpoint rate limiting, retries,
zero-byte handling, body fallback chain). Do not hand-edit; re-vendor from MarketWatch
if the upstream client changes.
"""
from .kscope import KscopeClient  # noqa: F401
from .kscope_models import DocumentRecord, BodyResult, KscopeError  # noqa: F401
