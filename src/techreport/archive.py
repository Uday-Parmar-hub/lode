"""Archiver: download every report in the corpus inventory to disk — original document + extracted
text — turning the corpus MAP (pointers) into a local corpus we control and can run extraction over.

Layout (per operator):
    corpus/<operator-slug>/<date>__<regime>__<id>.pdf|.htm   original document
    corpus/<operator-slug>/<date>__<regime>__<id>.txt        extracted plain text
    corpus/_archive_manifest.json                            per-report status/paths (resume + report)

Retrieval by regime:
  - NI 43-101 / JORC (LSEG): docid -> DocumentText for the body, pdf_link -> stream the PDF; if the
    body is empty (image-only filing) we fall back to a fitz text-extract of the downloaded PDF.
  - S-K 1300 (SEC EDGAR): the report's archive URL *is* the document (HTML EX-96 or PDF); download it
    and extract text (bs4 for HTML, fitz for PDF).

Idempotent + resumable: a report already recorded done in the manifest is skipped, so a killed run
resumes cleanly. Errors are per-report (one bad document never aborts the batch). The LSEG bearer token
lasts ~5 min, so the client is refreshed periodically and once more on any auth failure mid-run.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import httpx

from . import config, edgar
from .lseg import LSEG

_INV = config.ROOT / "data" / "corpus_inventory.json"
_CORPUS = config.CORPUS_DIR
_MANIFEST = _CORPUS / "_archive_manifest.json"
_TOKEN_REFRESH_EVERY = 25   # recreate the LSEG client (fresh ~5-min token) every N reports
_EDGAR_POLITE_SECS = 0.15


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:60] or "unknown"


def _safe_id(docid: str) -> str:
    # EDGAR docids are "accession:filename.htm" — drop the trailing file extension so we don't end up
    # with a doubled ".htm.htm"/".htm.txt" when the real extension is appended.
    d = re.sub(r"\.(pdf|html?|txt)$", "", docid or "", flags=re.I)
    return re.sub(r"[^A-Za-z0-9._-]+", "_", d).strip("_")[:60] or "noid"


def _rel(path: str | None) -> str | None:
    return str(Path(path).relative_to(_CORPUS)) if path else None


def _pdf_text(path: str) -> str:
    """Extract text from a PDF via PyMuPDF (guarded — returns '' on any failure/image-only PDF)."""
    try:
        import fitz
    except Exception:  # noqa: BLE001
        return ""
    try:
        parts: list[str] = []
        with fitz.open(path) as doc:
            for page in doc:
                parts.append(page.get_text())
        return "\n".join(parts)
    except Exception:  # noqa: BLE001
        return ""


def _html_text(raw: bytes) -> str:
    try:
        from bs4 import BeautifulSoup
        return BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
    except Exception:  # noqa: BLE001
        return re.sub(r"<[^>]+>", " ", raw.decode("utf-8", "ignore"))


class Archiver:
    """Downloads the inventory to disk. Holds the LSEG client so it can be refreshed on token expiry."""

    def __init__(self) -> None:
        self.cli = LSEG()
        _CORPUS.mkdir(parents=True, exist_ok=True)
        self.manifest: list[dict] = (
            json.loads(_MANIFEST.read_text(encoding="utf-8")) if _MANIFEST.exists() else []
        )
        self.done = {m["key"] for m in self.manifest if m.get("status") in ("ok", "partial", "empty")}

    # -- LSEG call with one auth-refresh retry --------------------------------
    def _lseg(self, fn):
        try:
            return fn(self.cli)
        except Exception:  # noqa: BLE001 — refresh the token once and retry (idempotent read/download)
            self.cli = LSEG()
            return fn(self.cli)

    # -- per-regime retrieval -------------------------------------------------
    def _archive_lseg(self, operator: str, rep: dict, stem: Path) -> dict:
        docid = rep["docid"]
        text = self._lseg(lambda c: c.document_text(docid)) or ""
        pdf_path = None
        nbytes = 0
        link = self._lseg(lambda c: c.pdf_link(docid))
        if link:
            pdf_path = f"{stem}.pdf"
            self._lseg(lambda c: c.download(link, pdf_path))
            nbytes = os.path.getsize(pdf_path)
            if not text.strip():
                text = _pdf_text(pdf_path)
        return self._finish(operator, rep, stem, text, pdf_path, nbytes)

    def _archive_edgar(self, operator: str, rep: dict, stem: Path) -> dict:
        url = rep.get("url")
        if not url:
            return self._finish(operator, rep, stem, "", None, 0)
        r = httpx.get(url, headers=edgar._headers(), timeout=90.0, follow_redirects=True)  # noqa: SLF001
        r.raise_for_status()
        time.sleep(_EDGAR_POLITE_SECS)
        raw = r.content
        is_pdf = url.lower().endswith(".pdf") or raw[:5] == b"%PDF-"
        ext = "pdf" if is_pdf else "htm"
        doc_path = f"{stem}.{ext}"
        Path(doc_path).write_bytes(raw)
        text = _pdf_text(doc_path) if is_pdf else _html_text(raw)
        return self._finish(operator, rep, stem, text, doc_path, len(raw))

    def _finish(self, operator: str, rep: dict, stem: Path, text: str,
                doc_path: str | None, nbytes: int) -> dict:
        txt_path = None
        if text.strip():
            txt_path = f"{stem}.txt"
            Path(txt_path).write_text(text, encoding="utf-8")
        status = "ok" if (txt_path and doc_path) else ("partial" if (txt_path or doc_path) else "empty")
        return {
            "operator": operator, "date": rep.get("date"), "regime": rep.get("regime"),
            "source": rep.get("source"), "docid": rep.get("docid"), "title": rep.get("title"),
            "doc": _rel(doc_path), "txt": _rel(txt_path), "bytes": nbytes, "chars": len(text),
            "status": status,
        }

    # -- driver ---------------------------------------------------------------
    def run(self, *, limit: int | None = None, operators: set[str] | None = None,
            verbose: bool = True) -> list[dict]:
        inv = json.loads(_INV.read_text(encoding="utf-8"))
        flat = [(x["operator"], rep) for x in inv if "error" not in x
                for rep in x.get("reports", [])
                if not operators or x["operator"] in operators]
        n_new = 0
        for operator, rep in flat:
            key = f"{rep.get('regime')}|{rep.get('docid')}"
            if key in self.done:
                continue
            if limit is not None and n_new >= limit:
                break
            if n_new and n_new % _TOKEN_REFRESH_EVERY == 0:
                self.cli = LSEG()

            stem = _CORPUS / _slug(operator) / (
                f"{rep.get('date') or 'undated'}__{_slug(rep.get('regime') or '')}__{_safe_id(rep.get('docid') or '')}"
            )
            stem.parent.mkdir(parents=True, exist_ok=True)
            try:
                entry = (self._archive_edgar if rep.get("source") == "sec_edgar"
                         else self._archive_lseg)(operator, rep, stem)
            except Exception as exc:  # noqa: BLE001 — record + move on
                entry = {"operator": operator, "date": rep.get("date"), "regime": rep.get("regime"),
                         "docid": rep.get("docid"), "status": "error",
                         "error": f"{type(exc).__name__}: {exc}"[:200]}
            entry["key"] = key
            self.manifest.append(entry)
            self.done.add(key)
            n_new += 1
            if verbose:
                print(f"  [{n_new:4d}] {entry['status']:7s} {operator[:26]:26s} {rep.get('regime'):9s} "
                      f"{rep.get('date')}  {entry.get('chars', 0):>8} chars")
            if n_new % 10 == 0:
                self._save()
        self._save()
        return self.manifest

    def _save(self) -> None:
        _MANIFEST.write_text(json.dumps(self.manifest, indent=1), encoding="utf-8")


def archive_corpus(*, limit: int | None = None, operators: set[str] | None = None,
                   verbose: bool = True) -> list[dict]:
    """Download the corpus inventory to disk; return the archive manifest."""
    return Archiver().run(limit=limit, operators=operators, verbose=verbose)
