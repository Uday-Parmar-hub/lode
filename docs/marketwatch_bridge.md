# The MarketWatch → LODE bridge (as built)

**Status: built, tested against real press releases, running locally only. Not in production.**
Nothing in production has changed. This describes the feature as it actually is, what is waiting on
a decision, and what it would take to deploy.

*(The older `marketwatch_lode_integration.md` is the Phase‑1 investigation that preceded this work.
It predates the implementation and describes an earlier design — keep it as history, don't follow it.)*

---

## What it does

An analyst reading the MarketWatch feed sees an **"Add to LODE"** button on royalty signals. Clicking
it reads that press release, extracts any third‑party royalties with Claude, and stages them in LODE
as **pending** rows for human review.

Three properties matter, and they are deliberate:

- **Human‑gated.** Nothing is ever added automatically. A person decides, one release at a time.
- **Staged, not published.** Rows land `status='pending'`, `ingested_from='marketwatch'`,
  `origin='marketwatch'` — they are candidates awaiting review, not facts.
- **Reversible.** Everything the bridge wrote can be found and removed with
  `WHERE ingested_from = 'marketwatch'`.

It exists because royalty news arrives as press releases long before it appears in a technical
report. The bridge is the path from "we saw it" to "it's in the origination database".

---

## Decisions waiting on a person

These are the reason the feature isn't finished. None of them is an engineering problem.

| # | Decision | Why it can't be defaulted |
|---|---|---|
| 1 | **Announced vs closed** | A press release often announces a deal that hasn't closed. A row staged from a real Brixton Metals release carries the note *"contingent on Brixton exercising the Option."* Should a contingent royalty be staged at all, and if so how is it marked? This also governs royalties being *bought back* or extinguished, which the `royalty_change` tier includes. Already flagged as Matt's call in the Phase‑1 doc. |
| 2 | **The corroboration count is wrong on live data** | 53 instruments in production show "N reports for this royalty" where N is one document read several times, not independent sources agreeing. The desk reads that as confidence. Predates the bridge; affects what people see today. |
| 3 | **35 duplicate groups / ~91 redundant rows** | Caused by the same NULL‑blind unique constraint (below). Cleaning them means deleting real rows — a judgement call — and it blocks migration `005`. |
| 4 | **Prod commodity backfill** | 174 rows, file generated and guarded, ready for Cloud Shell. Low risk, but it changes live data. |
| 5 | **App‑to‑app auth** (see Deployment) | Shared secret, or Entra. A trade‑off, not a technical blocker. |
| 6 | **Sign‑off / merge to `main`** | The work sits on two feature branches. |

Decision 1 also blocks improving the extraction quality gate, because you cannot grade extraction
until you agree what the right answer is.

---

## What it would take to deploy

**It is not blocked on networking, and it does not need a VNet.** `mw-dashboard`, `lode-dashboard`
and `mw-pipeline` are all in the same Container Apps environment (`mw-env`), so they can already
reach each other internally.

What is missing is that the local prototype **shells out to a Python script on the developer's
machine**. In production the button must instead call a small LODE ingestion endpoint. That endpoint
would wrap the same `add_one_to_lode` logic; the extraction, the insert and the linking all stay as
they are.

The only genuine question is how the two apps authenticate:

- **Shared secret** — a header both apps hold, stored as a Container App secret. Needs nothing from
  anyone. Fastest.
- **Entra (client credentials)** — cleaner and auditable, but needs tenant **admin consent**, which
  is the one thing IT must grant. The same consent would also let LODE move from its shared
  basic‑auth password to real Microsoft sign‑in.

Until then the feature is **fail‑closed**: without `LODE_BRIDGE_ENABLED=1` and full configuration,
the button is not rendered and the server action refuses. Deploying these branches as‑is is safe —
the feature simply stays invisible.

---

## What an analyst sees

| State | Meaning |
|---|---|
| **Add to LODE** | Not yet sent. |
| **Adding…** | Extracting — about 6–9 seconds, because it is a Claude call. |
| **Added to LODE** | Staged. Shown on reload too, so re‑clicking can't double‑add. |
| **Added 1 of 2** (amber) | Some royalties were stored and some were refused. Deliberately not a plain success — the release is now marked as sent, so a silent partial would be unrecoverable. |
| **Retry** | Either a real failure, or the correct answer "no royalty in this release". Hover for which. |

A second click on the same release is a no‑op that costs nothing (~0.8s, no Claude call): the
release is identified by document, and an existing row short‑circuits before extraction.

---

## For whoever picks up the code

### Where it lives

| Repo | File | Role |
|---|---|---|
| MarketWatch | `dashboard/app/board.tsx` | the button and its states |
| MarketWatch | `dashboard/app/actions.ts` | `addToLode` server action — reads the story, calls LODE |
| MarketWatch | `dashboard/lib/lode.ts` | the feature gate + the "already added" lookup |
| LODE | `scripts/add_one_to_lode.py` | extract one release, insert, link |
| LODE | `src/techreport/chain.py` | **shared** row identity, instrument linking, primary selection |
| LODE | `src/techreport/commodity.py` | **shared** commodity‑string parser |
| LODE | `tests/test_bridge.py` | 30 cases, no Claude calls, DB tests roll back |
| LODE | `db/migrations/005_royalty_identity.sql` | written, **not applied** — see Known limitations |

### Configuration

The bridge is off unless **all** of these are set (fail‑closed, checked in `lib/lode.ts`):

```
LODE_BRIDGE_ENABLED=1
LODE_TEST_DATABASE_URL=postgresql://…      # the LODE database to write to
LODE_PYTHON=/path/to/python                # interpreter with the LODE deps
LODE_SCRIPT=/path/to/add_one_to_lode.py
LODE_DIR=/path/to/tech_report_db           # cwd for the child process
LODE_LD_LIBRARY_PATH=…                     # optional, conda builds need it
ANTHROPIC_API_KEY=…                        # read by the child, not by the dashboard
```

`ANTHROPIC_API_KEY` is the non‑obvious one: no dashboard code uses it. It exists solely to be passed
to the spawned Python, which reads `os.environ` and loads no `.env`. The child gets an explicit
minimal environment, not the dashboard's whole one.

### Running it locally

```bash
conda run -n mining_ai bash scripts/pg_local.sh          # LODE postgres on :5433
docker start prm-postgres                                 # MarketWatch postgres on :5432
# LODE dashboard  :3010   (standalone build: node .next/standalone/server.js)
# MarketWatch     :3000   (next start)
```

Set `LOCAL_NO_AUTH=1` for MarketWatch locally — it bypasses sign‑in, and the bridge records the
actor as `local` instead of refusing. Production never sets it.

Two things that will waste your time otherwise: the feed defaults to **"today"**, and the default
lookback is 90 days, so historical test stories are invisible until you widen
`DASHBOARD_LOOKBACK_DAYS` *and* pick a wider range in the UI.

### The result contract

`add_one_to_lode.py` prints one JSON object:

```
inserted              rows actually stored (cur.rowcount, never "how many we tried")
extracted             royalties the model found
skipped, skipped_detail   found but refused by the database — surfaced, never silent
repeats_collapsed     identical royalties stated twice in one release
already               this release is already in LODE; nothing re-extracted
instruments, joined_existing, flagged_revalidation   what the linking did
reason                why nothing was inserted (e.g. no royalty passage)
```

### Tests

```bash
python -m pytest tests/test_bridge.py          # 30 cases, no Claude calls, safe in CI
python scripts/eval_extraction.py              # extraction quality (costs API calls)
```

The DB‑backed tests run inside transactions that are rolled back, and skip cleanly with no database.
Several exist specifically to pin bugs that were fixed — `test_edit_duplicates_when_instrument_id_is_missing`
asserts the *broken* behaviour on purpose, so a regression is loud.

---

## Invariants that are easy to break

These cost real debugging. Please don't undo them.

**Send the press release, never the summary.** `stories.angle` is Claude's own one‑paragraph summary.
On real releases the body is **18–198× larger** — for one Agnico release, 297 characters versus
58,659. Feeding the summary silently lost royalties, and because `quote_verified` compares the quote
against whatever text it is given, it compared Claude's quote to Claude's own prose and **passed by
construction** — LODE then badged those rows "source‑verified". On real releases the old path falsely
badged **4 of 4** extracted rows.

**Pass `issuer_hint`, never `operator_hint`, for news.** The extraction prompt was written for
technical reports, where a third‑party royalty is one "held by a party other than the operator".
Telling it the *issuer* is the operator inverts that rule: on a royalty company's release it excludes
the royalty the release is about. A real Orogen Royalties release extracted **zero** royalties that
way. Likewise, take `operator` from the extraction, not from the issuer — the same release recorded
Orogen as operating First Majestic's mine.

**Every row must be linked into the memory chain.** `chain.link()` assigns `dup_key`,
`instrument_id` and `is_primary`. Without an `instrument_id` the dashboard's edit path demotes the
previous version with `WHERE instrument_id = <null>`, which matches nothing, so the analyst's first
correction leaves **two current versions** of one royalty.

**The surfaced row is chosen per source lineage.** A lineage is one source document plus the edits
appended to it. Standing (whether it was ever validated) belongs to the *lineage*, not the row,
because an edit is written `status='pending'` — rank on the row's own status and correcting a
validated row demotes it out of the running and hands the instrument to whatever unreviewed row is
newest. Both directions are pinned by tests.

**Identify releases by `documents.id`, not `stories.id`.** Story ids are per‑prompt‑version; a
prompt fork mints new rows for the same release. The repo's own `reflag_to_version.sql` documents
this for flags. Keying on the story id would re‑offer a release already in LODE — and since the two
rows share a `dup_key`, it would inflate the corroboration badge.

---

## Known limitations

- **Migration `005` is written but not applied.** Row identity is
  `UNIQUE (source_docid, project_name, holder, royalty_type)` — the **rate is missing**, so two
  different royalties from one release that share project/holder/type collide and the second is
  dropped (now reported as `skipped` rather than lost silently). Postgres also compares NULLs as
  distinct, so when `holder` is NULL the constraint doesn't apply at all — which is how one Salares
  Norte royalty ended up in the corpus **14 times from a single document**. The migration fixes
  both, and refuses to run until the existing duplicates are resolved (decision 3).
- **A truncated body degrades to a false negative.** About a quarter of live RSS documents carry
  only a ~500‑character feed blurb (Business Wire refuses the article fetch). The bridge then finds
  no royalty passage and reports "no royalty in this release" — wrong, but it stages nothing and
  invents nothing, and a later click re‑extracts. In production Business Wire arrives via the shared
  mailbox with full text, so this mostly doesn't apply.
- **`needs_revalidation` is raised but barely visible** — it appears in the row drawer, not the grid
  or the filters.
- **Bridged rows are `is_primary` and `quote_verified`**, so each one nudges the dashboard's
  `verified_pct` KPI upward.
- **The AI query feature doesn't know these rows exist** — its schema description omits
  `ingested_from`, `origin` and the `MarketWatch` regime, so unreviewed rows blend into answers.
- **The extraction quality gate grades the wrong thing.** `eval_extraction.py` runs the
  *technical‑report* prompt against synthetic fixtures. There are now 24 real `royalty_change`
  stories to relabel against — but that needs decision 1 first.

---

## How it was validated

Everything above was checked against real press releases, not fixtures: 36 real SEDAR news releases
from the archive plus 100 live RSS documents fetched from the production feeds, scored into 24 real `royalty_change` stories
(39 in the local database in total, the other 15 being the original synthetic seeds).

The end‑to‑end check was a live, same‑day release: Brixton Metals → staged the Silvergruvan property
in Sweden, a 2.0% NSR (reducible to 1.0%) held by McKnight, with a verified verbatim quote and the
contingency captured in the notes.

This matters because the feature was previously only tested against seeded stories whose body text
had been written *from* the angle — which is exactly why the false "source‑verified" badge looked
fine in testing and failed on the first real release.
