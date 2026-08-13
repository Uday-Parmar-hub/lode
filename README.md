# Technical Report Database (Tool 2)

A structured, auto-filled database of **OR Royalties' portfolio technical reports** (NI 43-101 /
S-K 1300 / JORC), with a filterable dashboard. Replaces the stale-and-rigid Excel tracker.

Sister project to MarketWatch + the impairment tool — same stack. **See `CLAUDE.md` for the plan,
the source reality, and the open questions.**

## Status: corpus layer (in progress)

Currently only the **fetch-and-archive** layer is being built — the one piece that doesn't depend on
Matt's (still-open) field list. Extraction, schema, and dashboard come after the fields land.

## Setup

```bash
conda activate mining_ai        # or: python -m venv .venv && pip install -r requirements.txt
# .env holds KSCOPE_API_KEY + ANTHROPIC_API_KEY (already copied locally)
# the portfolio xlsx is read from ~/OR_Portfolio_List_2026-08-04.xlsx (override: PORTFOLIO_XLSX=...)
```

## Layout

```
src/techreport/config.py          settings + .env
src/techreport/portfolio.py       load the portfolio (asset / operator / jurisdiction)
src/techreport/kscope_client/     vendored Kscope client (do not hand-edit)
scripts/                          probes + the archiver (WIP)
corpus/                           downloaded reports (gitignored — big + confidential)
```

## Confidentiality

Local, internal, **no remote yet**. Portfolio data + the corpus are gitignored. Review before
pushing anywhere.
