"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import type { Royalty, Kpis } from "@/lib/queries";
import { saveReview, saveFactEdit, getInstrumentHistory, aiQuery, type ReviewPatch, type FactEdit, type Version } from "./actions";

const M: Record<string, string> = {
  Au: "#e8b45a", Ag: "#c9cfd8", Cu: "#cd7d4c", Mo: "#6e8ba6",
  Ni: "#8fb3a0", Zn: "#9aa3b2", PGE: "#b39cd0",
};
const METALS = ["Au", "Ag", "Cu", "Ni", "Zn", "Mo", "PGE"];
const REGIMES = ["NI 43-101", "S-K 1300", "JORC"];
const CONTINENTS = ["North America", "South America", "Africa", "Asia", "Oceania", "Europe"];
const cap = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : s);
const originLabel = (o: string | null): string =>
  o === "claude_human_edited" ? "human-edited" : o === "marketwatch" ? "MarketWatch" : o === "human" ? "human" : "AI";

// Canonical feature taxonomy — the detail view shows ALL of these (dash when absent) for a consistent,
// scannable checklist (Matt's ask). Grid/card chips still use featureList() below = present ones only.
// (Two more — commodity-specific, area-of-interest — pending Matt's confirm + a re-extraction.)
const FEATURES: { k: string; desc?: string; get: (r: Royalty) => string | null }[] = [
  { k: "partial", get: (r) => (r.partial_coverage ? "burdens part of the property" : null) },
  { k: "buy-down", get: (r) => r.buyback },
  { k: "sliding", get: (r) => r.step_down },
  // "cap" is general (Matt): the royalty/stream stops once a cumulative cap is met — a production
  // volume OR a cumulative-revenue threshold. Stored in production_cap; extraction will widen to
  // revenue caps in the next prompt version (with the producing flag).
  { k: "cap", desc: "royalty/stream stops once a cumulative cap is reached (production volume or revenue)", get: (r) => r.production_cap },
  { k: "threshold", desc: "royalty applies only once a minimum threshold is reached", get: (r) => r.production_threshold },
  { k: "advance", get: (r) => r.advance_payments },
  { k: "ROFR", get: (r) => (r.rofr ? "right of first refusal / offer" : null) },
];

function featureList(r: Royalty): { k: string; v: string }[] {
  const out: { k: string; v: string }[] = [];
  if (r.buyback) out.push({ k: "buy-down", v: r.buyback });
  if (r.step_down) out.push({ k: "sliding", v: r.step_down });
  if (r.production_cap) out.push({ k: "cap", v: r.production_cap });
  if (r.production_threshold) out.push({ k: "threshold", v: r.production_threshold });
  if (r.advance_payments) out.push({ k: "advance", v: r.advance_payments });
  if (r.partial_coverage) out.push({ k: "partial", v: "burdens part of the property" });
  if (r.rofr) out.push({ k: "ROFR", v: "right of first refusal / offer" });
  return out;
}

const COLS: { k: keyof Royalty | "features"; t: string; w: number; nosort?: boolean }[] = [
  { k: "asset", t: "Property", w: 15 }, { k: "operator", t: "Operator", w: 12 }, { k: "juris", t: "Jurisdiction", w: 8 },
  { k: "commodity", t: "Commodity", w: 9, nosort: true }, { k: "stage", t: "Stage", w: 7 },
  { k: "rate_pct", t: "Rate", w: 9 }, { k: "type", t: "Type", w: 6 }, { k: "holder", t: "Held by", w: 14 },
  { k: "features", t: "Features", w: 12, nosort: true }, { k: "source_label", t: "Source", w: 8 },
];

function Commodity({ c }: { c: string[] }) {
  return <div className="comm">{(c || []).map((x, i) => <span key={i} style={{ ["--cc" as string]: M[x] || "#5f584c" }}>{x}</span>)}</div>;
}
function StatusDot({ s }: { s: string }) {
  const col = s === "validated" ? "var(--ok)" : s === "rejected" ? "var(--alert,#e0715a)" : s === "needs_info" ? "var(--warn)" : "var(--dim)";
  return <span title={s} style={{ width: 7, height: 7, borderRadius: "50%", background: col, flex: "none", display: "inline-block" }} />;
}

export default function Board({ royalties, kpis }: { royalties: Royalty[]; kpis: Kpis }) {
  const [data, setData] = useState<Royalty[]>(royalties);
  const [q, setQ] = useState("");
  const [comm, setComm] = useState<Set<string>>(new Set());
  const [regime, setRegime] = useState<Set<string>>(new Set());
  const [cont, setCont] = useState<Set<string>>(new Set());
  const [tier1, setTier1] = useState(false);
  const [compOnly, setCompOnly] = useState(false);
  const [producing, setProducing] = useState(false);
  const [sort, setSort] = useState<string>("rate_pct");
  const [dir, setDir] = useState(-1);
  const [view, setView] = useState<"table" | "cards">("table");
  const [selId, setSelId] = useState<string | null>(null);
  // AI text-to-SQL. rows mode: aiIds (+ order) restricts the grid to Claude's SELECT. table mode: aiTable
  // holds an aggregate result rendered in place of the grid. aiInfo drives the banner (+ the generated SQL).
  const [aiIds, setAiIds] = useState<Set<string> | null>(null);
  const [aiOrder, setAiOrder] = useState<Map<string, number> | null>(null);
  const [aiTable, setAiTable] = useState<{ fields: string[]; rows: string[][] } | null>(null);
  const [aiInfo, setAiInfo] = useState<{ explanation: string; sql: string; kind: "rows" | "table" | "error" | "reject" } | null>(null);
  const [aiBusy, setAiBusy] = useState(false);
  const [showSql, setShowSql] = useState(false);
  const aiActive = aiIds !== null || aiTable !== null || aiInfo !== null;

  const toggle = (set: Set<string>, setter: (s: Set<string>) => void, v: string) => {
    const n = new Set(set); n.has(v) ? n.delete(v) : n.add(v); setter(n);
  };

  const clearAi = () => { setAiIds(null); setAiOrder(null); setAiTable(null); setAiInfo(null); setShowSql(false); };

  const runAi = async () => {
    const nl = q.trim();
    if (!nl) { clearAi(); return; }
    setAiBusy(true); setShowSql(false);
    try {
      const res = await aiQuery(nl);
      if (!res.ok) {
        setAiIds(null); setAiOrder(null); setAiTable(null);
        setAiInfo({ explanation: res.error || "Search failed — try rephrasing.", sql: res.sql || "", kind: "error" });
      } else if (res.mode === "reject") {
        setAiIds(null); setAiOrder(null); setAiTable(null);
        setAiInfo({ explanation: res.explanation, sql: "", kind: "reject" });
      } else if (res.mode === "table") {
        setAiIds(null); setAiOrder(null);
        setAiTable({ fields: res.fields || [], rows: res.rows || [] });
        setAiInfo({ explanation: res.explanation, sql: res.sql, kind: "table" }); setSelId(null);
      } else {
        const ids = res.ids || [];
        setAiTable(null);
        setAiIds(new Set(ids)); setAiOrder(new Map(ids.map((id, i) => [id, i])));
        setAiInfo({ explanation: res.explanation, sql: res.sql, kind: "rows" });
        setSort("__ai__"); setDir(1); setSelId(null);
      }
    } catch {
      clearAi(); setAiInfo({ explanation: "Search failed — try rephrasing.", sql: "", kind: "error" });
    } finally { setAiBusy(false); }
  };

  const rows = useMemo(() => {
    const aiMode = aiIds !== null;
    const ql = q.trim().toLowerCase();
    const sig = ql.split(/[^a-z0-9.%]+/).filter((w) => w.length > 1 && !["under", "with", "and", "the", "an", "or", "in", "a"].includes(w));
    // Live keyword pass only for short, keyword-like queries (1–2 words). In AI mode the id set already
    // reflects the query; a longer sentence is an AI question that waits for Enter (never blanks the grid).
    const words = aiMode || sig.length > 2 ? [] : sig;
    const out = data.filter((r) => {
      if (aiMode && !aiIds!.has(r.id)) return false;
      if (comm.size) {
        const cs = r.commodity || [];
        // "Other" matches any metal not in the standard set (U, Li, V, graphite, REE, …)
        const hit = cs.some((c) => comm.has(c)) || (comm.has("Other") && cs.some((c) => !METALS.includes(c)));
        if (!hit) return false;
      }
      if (regime.size && !(r.regime && regime.has(r.regime))) return false;
      if (cont.size && !(r.continent && cont.has(r.continent))) return false;
      if (tier1 && r.jurisdiction_tier !== 1) return false;
      if (compOnly && !r.competitor_holder) return false;
      if (producing && r.is_producing !== true) return false;
      if (words.length) {
        const blob = `${r.asset} ${r.operator ?? ""} ${r.juris ?? ""} ${r.continent ?? ""} ${r.country ?? ""} ${r.holder ?? ""} ${(r.commodity || []).join(" ")} ${r.stage ?? ""} ${r.type ?? ""} ${r.rate ?? ""} ${r.features_note ?? ""}`.toLowerCase();
        if (!words.every((w) => blob.includes(w))) return false;
      }
      return true;
    });
    if (sort === "__ai__" && aiOrder) {
      out.sort((a, b) => (aiOrder.get(a.id) ?? 0) - (aiOrder.get(b.id) ?? 0)); // preserve the SQL's ORDER BY
    } else {
      const sv = (r: Royalty) => { const v = (r as unknown as Record<string, unknown>)[sort]; return typeof v === "number" ? v : (v ?? "").toString().toLowerCase(); };
      out.sort((a, b) => { const x = sv(a), y = sv(b); if (x === y) return 0; if (x === "" || x === null) return 1; if (y === "" || y === null) return -1; return (x < y ? -1 : 1) * dir; });
    }
    return out;
  }, [data, q, comm, regime, cont, tier1, compOnly, producing, sort, dir, aiIds, aiOrder]);

  const selIdx = selId ? rows.findIndex((r) => r.id === selId) : -1;
  const sel = selIdx >= 0 ? rows[selIdx] : null;

  // keyboard nav in review mode (skip when typing)
  useEffect(() => {
    if (!sel) return;
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      if (t && (t.tagName === "TEXTAREA" || t.tagName === "INPUT")) return;
      if (e.key === "Escape") setSelId(null);
      else if (e.key === "ArrowDown") { e.preventDefault(); setSelId(rows[Math.min(rows.length - 1, selIdx + 1)]?.id ?? null); }
      else if (e.key === "ArrowUp") { e.preventDefault(); setSelId(rows[Math.max(0, selIdx - 1)]?.id ?? null); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [sel, selIdx, rows]);

  const applyReview = (id: string, patch: Partial<Royalty>) => {
    setData((d) => d.map((r) => (r.id === id ? { ...r, ...patch } : r)));
  };

  const Chip = ({ label, on, color, onClick }: { label: string; on: boolean; color?: string; onClick: () => void }) => (
    <span className={`chip${on ? " on" : ""}`} style={color ? { ["--c" as string]: color } : undefined} onClick={onClick}>
      {color && <span className="sw" />}{label}
    </span>
  );

  return (
    <main>
      <div className="cmd">
        <div className="brand"><div className="mark" /><div><div className="word">LODE</div><div className="tag">Royalty Origination · OR Royalties</div></div></div>
        <div className={`search${aiBusy ? " busy" : ""}${(aiIds !== null || aiTable) ? " aion" : ""}`}>
          {aiBusy ? <span className="spin" aria-label="Thinking" />
            : (aiIds !== null || aiTable) ? <span className="aidot">✦</span>
            : <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="11" cy="11" r="7" /><path d="m21 21-4.3-4.3" /></svg>}
          <input
            value={q}
            onChange={(e) => { setQ(e.target.value); if (aiActive) clearAi(); }}
            onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); runAi(); } else if (e.key === "Escape" && aiActive) clearAi(); }}
            placeholder="Ask anything — “producing gold in Nevada or Quebec under 2%”, “top 10 holders by number of instruments”"
          />
          {q.trim() && !aiBusy && !aiActive && <kbd className="askhint" onClick={runAi} title="Ask AI (Enter)">✦ Ask&nbsp;↵</kbd>}
        </div>
        <div className="kstats">
          <div className="kstat"><div className="n au">{kpis.royalties.toLocaleString()}</div><div className="l">Instruments</div></div>
          <div className="kstat"><div className="n">{kpis.assets}</div><div className="l">Assets</div></div>
          <div className="kstat"><div className="n">{kpis.pending}</div><div className="l">Pending</div></div>
          <div className="kstat"><div className="n">{kpis.verified_pct}%</div><div className="l">Verified</div></div>
        </div>
        <div className="live"><span className="d" />LIVE</div>
      </div>

      {aiInfo && (<>
        <div className={`aibar${aiInfo.kind === "error" ? " err" : ""}${aiInfo.kind === "reject" ? " info" : ""}`}>
          <span className="aimark">{aiInfo.kind === "reject" ? "◍" : "✦"}</span>
          <span className="aitext">{aiInfo.explanation}</span>
          {aiInfo.kind === "table" && <span className="aichip">aggregate</span>}
          <span style={{ flex: 1 }} />
          {aiInfo.kind === "rows" && <span className="aicount">{rows.length} match{rows.length === 1 ? "" : "es"}</span>}
          {aiInfo.sql && <button className="aiclear" onClick={() => setShowSql((s) => !s)}>{showSql ? "hide SQL" : "‹ › SQL"}</button>}
          <button className="aiclear" onClick={clearAi} title="Clear AI search (Esc)"><span aria-hidden>✕</span> Clear</button>
        </div>
        {showSql && aiInfo.sql && <pre className="aisql">{aiInfo.sql}</pre>}
      </>)}

      {!aiTable && <div className="toolbar">
        <span className="glabel">Metal</span>
        {METALS.map((m) => <Chip key={m} label={m} color={M[m]} on={comm.has(m)} onClick={() => toggle(comm, setComm, m)} />)}
        <Chip label="Other" color="#8a8172" on={comm.has("Other")} onClick={() => toggle(comm, setComm, "Other")} />
        <span className="glabel" style={{ marginLeft: 6 }}>Regime</span>
        {REGIMES.map((r) => <Chip key={r} label={r} on={regime.has(r)} onClick={() => toggle(regime, setRegime, r)} />)}
        <span className="glabel" style={{ marginLeft: 6 }}>Region</span>
        {CONTINENTS.map((c) => <Chip key={c} label={c} on={cont.has(c)} onClick={() => toggle(cont, setCont, c)} />)}
        <Chip label="Tier 1" color="#f5b23e" on={tier1} onClick={() => setTier1((v) => !v)} />
        <Chip label="Producing" color="#5fae7a" on={producing} onClick={() => setProducing((v) => !v)} />
        <Chip label="Competitor-held" color="#d98a7a" on={compOnly} onClick={() => setCompOnly((v) => !v)} />
        <div className="rt">
          {!sel && <div className="toggle">
            <button className={view === "table" ? "on" : ""} onClick={() => setView("table")}>▤ Table</button>
            <button className={view === "cards" ? "on" : ""} onClick={() => setView("cards")}>▦ Cards</button>
          </div>}
          <span className="count"><b>{rows.length}</b> / {data.length}</span>
        </div>
      </div>}

      {aiTable ? (
        <div className="respanel">
          <div className="reswrap">
            <table className="restable">
              <thead><tr>{aiTable.fields.map((f, i) => <th key={i}>{f}</th>)}</tr></thead>
              <tbody>
                {aiTable.rows.map((row, ri) => (
                  <tr key={ri}>{row.map((c, ci) => <td key={ci} className={ci === 0 ? "c0" : ""}>{c}</td>)}</tr>
                ))}
              </tbody>
            </table>
            {!aiTable.rows.length && <div className="resempty">No results.</div>}
          </div>
        </div>
      ) : sel ? (
        <div className="split">
          <div className="rail">
            {rows.map((r) => (
              <div key={r.id} className={`railrow${r.id === selId ? " active" : ""}`} onClick={() => setSelId(r.id)}>
                <span className="vein" style={{ background: M[(r.commodity || [])[0]] || "#5f584c" }} />
                <div className="rmid"><div className="rn">{r.asset}</div><div className="rsub">{r.operator ?? ""}</div></div>
                <div className="rend"><div className="rr">{r.rate ?? "—"}</div><StatusDot s={r.status} /></div>
              </div>
            ))}
          </div>
          <div className="detail-pane">
            <Detail key={sel.id} r={sel} idx={selIdx} total={rows.length}
              onNav={(d) => setSelId(rows[Math.min(rows.length - 1, Math.max(0, selIdx + d))]?.id ?? null)}
              onClose={() => setSelId(null)} onApply={(patch) => applyReview(sel.id, patch)} />
          </div>
        </div>
      ) : view === "table" ? (
        <div className="tablewrap">
          <table>
            <thead><tr>{COLS.map((c) => { const on = sort === c.k; return (
              <th key={c.k as string} className={on ? "sorted" : ""} style={{ width: `${c.w}%`, ...(c.nosort ? { cursor: "default" } : {}) }}
                onClick={() => { if (c.nosort) return; if (sort === c.k) setDir(-dir); else { setSort(c.k as string); setDir(1); } }}>
                {c.t}{!c.nosort && on && <span className="ar">{dir < 0 ? "↓" : "↑"}</span>}
              </th>); })}</tr></thead>
            <tbody>
              {rows.map((r) => { const vein = M[(r.commodity || [])[0]] || "#5f584c"; const feats = featureList(r); return (
                <tr key={r.id} onClick={() => setSelId(r.id)}>
                  <td className="asset" style={{ ["--vein" as string]: vein }}><span className="vein" /><span className="nm">{r.asset}</span></td>
                  <td className="op"><span className="cl">{r.operator}</span></td>
                  <td className="juris"><span className="cl">{r.juris}</span></td>
                  <td><Commodity c={r.commodity} /></td>
                  <td>{r.stage && <span className="stage" title={r.stage}>{r.stage.replace(/\s*\([^)]*\)/g, "")}</span>}</td>
                  <td className="rate"><div className="v">{r.rate ?? "—"}</div></td>
                  <td className="type"><span className="cl">{r.type}</span></td>
                  <td className="holder"><div className="h">{r.holder ?? "—"}</div>{r.competitor_holder && <span className="comptag" title={`Held by competitor: ${r.competitor_holder}`}>⚑ competitor</span>}{r.holder_note && <div className="hn">{r.holder_note}</div>}</td>
                  <td><div className="fchips">{feats.slice(0, 3).map((f, i) => <span key={i} className="fchip">{f.k}</span>)}{!feats.length && <span style={{ color: "var(--text-3)" }}>—</span>}</div></td>
                  <td className="src">{r.source_label} {r.quote_verified && <span className="verified">✓</span>}{r.report_count > 1 && <span className="repbadge" title={`Corroborated by ${r.report_count} source reports`}>×{r.report_count}</span>}</td>
                </tr>); })}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="cards">
          {rows.map((r) => { const vein = M[(r.commodity || [])[0]] || "#5f584c"; const feats = featureList(r); return (
            <div key={r.id} className="card" style={{ ["--vein" as string]: vein }} onClick={() => setSelId(r.id)}>
              <div className="ctop">
                <div className="cinfo"><div className="nm">{r.asset}</div><div className="op2">{r.operator} · {r.juris}</div></div>
                <div className="crate"><div className="rr">{r.rate ?? "—"}</div><div className="rt2">{r.type}</div></div>
              </div>
              <div className="mid"><Commodity c={r.commodity} />{r.stage && <span className="stage" title={r.stage}>{r.stage.replace(/\s*\([^)]*\)/g, "")}</span>}{feats.slice(0, 2).map((f, i) => <span key={i} className="fchip">{f.k}</span>)}</div>
              <div className="foot"><span className="hh">held by <b>{r.holder ?? "—"}</b></span>{r.quote_verified && <span className="verified" style={{ fontSize: 12, flex: "none" }}>✓ verified</span>}</div>
            </div>); })}
        </div>
      )}
      <div className="mockflag">LIVE DB · {kpis.royalties.toLocaleString()} instruments · {kpis.pending} pending review</div>
    </main>
  );
}

function Cell({ k, v, gold }: { k: string; v: React.ReactNode; gold?: boolean }) {
  return <div className="cell"><div className="k">{k}</div><div className={`v${gold ? " au" : ""}`}>{v ?? "—"}</div></div>;
}

function Detail({ r, idx, total, onNav, onClose, onApply }: {
  r: Royalty; idx: number; total: number; onNav: (d: number) => void; onClose: () => void; onApply: (p: Partial<Royalty>) => void;
}) {
  const router = useRouter();
  const [draft, setDraft] = useState<ReviewPatch>({});
  const [saving, setSaving] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [fx, setFx] = useState<FactEdit>({});
  const [savingFact, setSavingFact] = useState(false);
  const [versions, setVersions] = useState<Version[] | null>(null);
  useEffect(() => { setDraft({}); setEditing(false); setFx({}); }, [r.id]);
  // load this instrument's version chain for the history panel
  useEffect(() => {
    let live = true;
    setVersions(null);
    if (r.instrument_id) getInstrumentHistory(r.instrument_id).then((v) => { if (live) setVersions(v); });
    return () => { live = false; };
  }, [r.instrument_id, r.id]);

  // Fact edit (memory-chain): append a new version with the analyst's corrections, then re-fetch.
  const FACT_KEYS: (keyof FactEdit)[] = ["royalty_type", "rate", "holder", "holder_note"];
  const fcur = <K extends keyof FactEdit>(k: K): FactEdit[K] =>
    (fx[k] !== undefined ? fx[k] : (r as unknown as Record<string, unknown>)[k === "royalty_type" ? "type" : k]) as FactEdit[K];
  const saveFacts = async () => {
    const edit: FactEdit = {};
    for (const k of FACT_KEYS) (edit as Record<string, unknown>)[k] = fcur(k);
    setSavingFact(true);
    const res = await saveFactEdit(r.id, edit);
    setSavingFact(false);
    if (res.ok) { setEditing(false); router.refresh(); }
  };
  // ReviewPatch keys mostly match Royalty fields; `availability` maps to the row's `avail` alias.
  const ROWKEY: Partial<Record<keyof ReviewPatch, string>> = { availability: "avail" };
  const cur = <K extends keyof ReviewPatch>(k: K): ReviewPatch[K] =>
    (draft[k] !== undefined ? draft[k] : (r as unknown as Record<string, unknown>)[ROWKEY[k] ?? k]) as ReviewPatch[K];

  // Send the *effective* value of every editable field (draft ?? current), so saving one field
  // (or just clicking Validate) never nulls out the others the analyst set earlier.
  const EDITABLE: (keyof ReviewPatch)[] = [
    "tier", "keep", "availability", "score_project_quality", "score_instrument_quality",
    "score_confidence", "score_actionable", "comments", "rank", "link",
  ];
  const persist = async (extra: ReviewPatch) => {
    const patch: ReviewPatch = { ...extra };
    for (const k of EDITABLE) if (!(k in patch)) (patch as Record<string, unknown>)[k] = cur(k);
    setSaving(extra.status ?? "save");
    const res = await saveReview(r.id, patch);
    setSaving(null);
    // map availability back onto the row's `avail` alias so the detail reflects it immediately
    if (res.ok) onApply({ ...patch, ...(patch.availability != null ? { avail: patch.availability } : {}) } as Partial<Royalty>);
  };
  const SCORES: [keyof ReviewPatch, string][] = [
    ["score_project_quality", "Project quality"], ["score_instrument_quality", "Instrument quality"],
    ["score_confidence", "Confidence"], ["score_actionable", "Actionable"],
  ];

  return (
    <div className="dpad">
      <div className="dhead">
        <button className="iconbtn" onClick={() => onNav(-1)} disabled={idx <= 0} title="Previous (↑)" aria-label="Previous">↑</button>
        <button className="iconbtn" onClick={() => onNav(1)} disabled={idx >= total - 1} title="Next (↓)" aria-label="Next">↓</button>
        <span className="pos">{idx + 1} <em>/ {total}</em></span>
        <span style={{ flex: 1 }} />
        <span className={`pill ${r.status}`}><StatusDot s={r.status} />{cap(r.status)}</span>
        <button className="closebtn" onClick={onClose} title="Close (Esc)"><span aria-hidden>✕</span> Close</button>
      </div>
      <div className="deye">{r.stage} · {r.juris}</div>
      <div className="dtitle">{r.asset}</div>
      <div className="dmeta">{r.operator}</div>

      <div className="sec">
        Instrument
        {r.origin === "claude_human_edited" && <span className="prov edited" title="A field was corrected by an analyst">✎ human-edited</span>}
        {r.needs_revalidation && <span className="prov reval" title="A new source or edit landed — needs re-validation">⟳ needs re-validation</span>}
        <span style={{ flex: 1 }} />
        {!editing && <button className="editbtn" onClick={() => { setFx({}); setEditing(true); }} title="Correct a fact — saves a new version">✎ Edit</button>}
      </div>
      {editing ? (
        <div className="factedit">
          <label>Rate<input value={(fcur("rate") as string) ?? ""} onChange={(e) => setFx((d) => ({ ...d, rate: e.target.value || null }))} placeholder="e.g. 2.00% NSR" /></label>
          <label>Type<input value={(fcur("royalty_type") as string) ?? ""} onChange={(e) => setFx((d) => ({ ...d, royalty_type: e.target.value || null }))} placeholder="e.g. NSR / NPI / metal stream" /></label>
          <label>Held by<input value={(fcur("holder") as string) ?? ""} onChange={(e) => setFx((d) => ({ ...d, holder: e.target.value || null }))} placeholder="counterparty (the seller)" /></label>
          <label>Held-by note<input value={(fcur("holder_note") as string) ?? ""} onChange={(e) => setFx((d) => ({ ...d, holder_note: e.target.value || null }))} placeholder="lineage / clarification (optional)" /></label>
          <div className="facthint">Saving keeps the original as history and adds a new, human-edited version (flagged for re-validation).</div>
          <div className="factact">
            <button className="btn primary" disabled={savingFact} onClick={saveFacts}>{savingFact ? "Saving…" : "Save as new version"}</button>
            <button className="btn ghost" disabled={savingFact} onClick={() => { setEditing(false); setFx({}); }}>Cancel</button>
          </div>
        </div>
      ) : (
      <div className="dgrid">
        <Cell k="Rate" v={`${r.rate ?? "—"} ${r.type ?? ""}`} gold />
        <Cell k="Held by" v={<>{r.holder ?? "—"}{r.competitor_holder && <span className="comptag" style={{ marginLeft: 6 }} title={`Competitor: ${r.competitor_holder}`}>⚑ competitor</span>}{r.holder_note && <div style={{ fontSize: 11, color: "var(--text-3)" }}>{r.holder_note}</div>}</>} />
        <Cell k="Available" v={<span className={`avail-${cur("availability")}`}>{cap((cur("availability") as string) || "unknown")}</span>} />
        <Cell k="Confidence" v={r.conf != null ? `${r.conf} / 5` : "—"} />
        <Cell k="Commodity" v={(r.commodity || []).join(" · ")} />
        <Cell k="Regime" v={r.regime} />
      </div>
      )}

      <div className="sec">Property</div>
      <div className="dgrid">
        <Cell k="Jurisdiction" v={r.juris} />
        <Cell k="Country" v={[r.country, r.state_province].filter(Boolean).join(" · ")} />
        <Cell k="Continent" v={r.continent} />
        <Cell k="Jurisdiction tier" v={r.jurisdiction_tier === 1 ? "Tier 1" : "Not tier 1"} />
        <Cell k="Stage / est. start" v={[r.stage, r.est_startup].filter(Boolean).join(" · ")} />
        <Cell k="Producing" v={r.is_producing == null ? "Unknown" : r.is_producing ? "Yes — in production" : "No — pre-production"} />
        <Cell k="S&P ID" v={r.sp_id} />
        <Cell k="Granted" v={r.royalty_created} />
        <Cell k="Info available" v={r.info_available} />
      </div>

      <div className="sec">Instrument description, as stated in source document</div>
      <div className="assay">
        <div className="q">“{r.quote}”</div>
        <div className="cite">{r.source_label} &nbsp;·&nbsp; {r.quote_verified ? <span className="verified">✓ source-verified</span> : <span>unverified</span>}{r.source_url && (<> &nbsp;·&nbsp; <a href={r.source_url} target="_blank" rel="noreferrer" style={{ color: "var(--gold-hi)" }}>open ↗</a></>)}{r.report_count > 1 && (<> &nbsp;·&nbsp; <span className="repcount">corroborated by {r.report_count} reports{r.report_from && r.report_to ? ` (${r.report_from === r.report_to ? r.report_from : `${r.report_from}–${r.report_to}`})` : ""}</span></>)}</div>
      </div>

      <div className="sec">Instrument features</div>
      <div className="feat">
        {FEATURES.map((f) => { const v = f.get(r); return (
          <div key={f.k} className={`frow${v ? "" : " empty"}`} title={f.desc}><span className="fk">{f.k}</span><span className="fv">{v ?? "—"}</span></div>
        ); })}
      </div>
      {r.features_note && <div className="fnote"><span className="fk">also check</span> {r.features_note}</div>}

      {versions && versions.length > 1 && (
        <>
          <div className="sec">Version history <span className="vcount">{versions.length} versions</span></div>
          <div className="vhist">
            {versions.map((v) => (
              <div key={v.id} className={`vrow${v.is_primary ? " cur" : ""}`}>
                <div className="vmeta">
                  <span className="vdate">{(v.source_date || v.created_at || "").slice(0, 10) || "—"}</span>
                  <span className={`vorigin o-${v.origin ?? "claude"}`}>{originLabel(v.origin)}</span>
                  {v.is_primary && <span className="vtag cur">current</span>}
                  {v.needs_revalidation && <span className="vtag reval">needs re-validation</span>}
                </div>
                <div className="vfacts">{[v.rate, v.royalty_type].filter(Boolean).join(" ") || "—"} &nbsp;·&nbsp; {v.holder ?? "—"}</div>
                <div className="vsrc">{v.source_label ?? "—"}{v.quote_verified && <span className="verified"> ✓</span>}</div>
              </div>
            ))}
          </div>
        </>
      )}

      <div className="sec">Review · analyst</div>
      <div className="review">
        <div className="rfield"><span className="rk">Available?</span>
          <div className="seg">{["available", "partial", "held", "unknown"].map((a) => <button key={a} className={cur("availability") === a ? "on" : ""} onClick={() => setDraft((d) => ({ ...d, availability: a }))}>{cap(a)}</button>)}</div>
        </div>
        <div className="rfield"><span className="rk">Tier</span>
          <div className="seg">{[1, 2, 3].map((t) => <button key={t} className={cur("tier") === t ? "on" : ""} onClick={() => setDraft((d) => ({ ...d, tier: d.tier === t ? null : t }))}>{t}</button>)}</div>
        </div>
        <div className="rfield"><span className="rk">Keep?</span>
          <div className="seg"><button className={cur("keep") === true ? "on" : ""} onClick={() => setDraft((d) => ({ ...d, keep: true }))}>Yes</button><button className={cur("keep") === false ? "on" : ""} onClick={() => setDraft((d) => ({ ...d, keep: false }))}>No</button></div>
        </div>
        <div className="rfield"><span className="rk">Rank</span>
          <input className="rnum" type="number" placeholder="—" defaultValue={r.rank ?? ""}
            onChange={(e) => setDraft((d) => ({ ...d, rank: e.target.value === "" ? null : Number(e.target.value) }))} />
        </div>
        {SCORES.map(([k, label]) => (
          <div className="rfield" key={k as string}><span className="rk">{label}</span>
            <div className="seg">{[1, 2, 3, 4, 5].map((n) => <button key={n} className={cur(k) === n ? "on" : ""} onClick={() => setDraft((d) => ({ ...d, [k]: d[k] === n ? null : n }))}>{n}</button>)}</div>
          </div>
        ))}
        <textarea placeholder="Comments — grounds the decision…" defaultValue={r.comments ?? ""} onChange={(e) => setDraft((d) => ({ ...d, comments: e.target.value }))} />
        <div className="rlinkrow">
          <input className="rlink" type="url" placeholder="Link (URL) — SEDAR+ / internal doc…" defaultValue={r.link ?? ""}
            onChange={(e) => setDraft((d) => ({ ...d, link: e.target.value.trim() || null }))} />
          {cur("link") && <a className="rlinkopen" href={cur("link") as string} target="_blank" rel="noreferrer">open ↗</a>}
        </div>
      </div>
      <div className="dact">
        <button className="btn primary" disabled={!!saving} onClick={() => persist({ status: "validated" })}>
          <span aria-hidden>✓</span> {saving === "validated" ? "Saving…" : "Validate"}
        </button>
        <button className="btn danger" disabled={!!saving} onClick={() => persist({ status: "rejected" })}>Reject</button>
        <button className="btn ghost" disabled={!!saving} onClick={() => persist({})}>{saving === "save" ? "Saving…" : "Save"}</button>
      </div>

      <div className="metastrip">
        <span><b>Entered</b> {r.created_at ? r.created_at.slice(0, 10) : "—"}</span>
        <span><b>Modified</b> {r.updated_at ? r.updated_at.slice(0, 10) : "—"}</span>
      </div>
    </div>
  );
}
