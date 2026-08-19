"use client";

import { useMemo, useState } from "react";
import type { Royalty, Kpis } from "@/lib/queries";

const M: Record<string, string> = {
  Au: "#e8b45a", Ag: "#c9cfd8", Cu: "#cd7d4c", Mo: "#6e8ba6",
  Ni: "#8fb3a0", Zn: "#9aa3b2", PGE: "#b39cd0",
};
const METALS = ["Au", "Ag", "Cu", "Ni", "Zn", "Mo", "PGE"];
const STAGES = ["Producing", "Development", "Construction", "Resource", "Exploration", "PEA", "PFS", "FS"];
const REGIMES = ["NI 43-101", "S-K 1300", "JORC"];
const cap = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : s);

// Which structured features are present on a row → compact chips (the thing Matt tracks).
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
  { k: "asset", t: "Asset", w: 15 }, { k: "operator", t: "Operator", w: 12 }, { k: "juris", t: "Jurisdiction", w: 8 },
  { k: "commodity", t: "Commodity", w: 9, nosort: true }, { k: "stage", t: "Stage", w: 7 },
  { k: "rate_pct", t: "Royalty", w: 9 }, { k: "type", t: "Type", w: 6 }, { k: "holder", t: "Held by", w: 14 },
  { k: "features", t: "Features", w: 12, nosort: true }, { k: "source_label", t: "Source", w: 8 },
];

function Commodity({ c }: { c: string[] }) {
  return (
    <div className="comm">
      {(c || []).map((x, i) => (
        <span key={i} style={{ ["--cc" as string]: M[x] || "#5f584c" }}>{x}</span>
      ))}
    </div>
  );
}

export default function Board({ royalties, kpis }: { royalties: Royalty[]; kpis: Kpis }) {
  const [q, setQ] = useState("");
  const [comm, setComm] = useState<Set<string>>(new Set());
  const [stage, setStage] = useState<Set<string>>(new Set());
  const [regime, setRegime] = useState<Set<string>>(new Set());
  const [sort, setSort] = useState<string>("rate_pct");
  const [dir, setDir] = useState(-1);
  const [view, setView] = useState<"table" | "cards">("table");
  const [sel, setSel] = useState<Royalty | null>(null);

  const toggle = (set: Set<string>, setter: (s: Set<string>) => void, v: string) => {
    const n = new Set(set); n.has(v) ? n.delete(v) : n.add(v); setter(n);
  };

  const rows = useMemo(() => {
    const ql = q.trim().toLowerCase();
    const words = ql.split(/[^a-z0-9.%]+/).filter((w) => w.length > 1 && !["under", "with", "and", "the", "an", "or", "in", "a"].includes(w));
    const out = royalties.filter((r) => {
      if (comm.size && !(r.commodity || []).some((c) => comm.has(c))) return false;
      if (stage.size && !(r.stage && [...stage].some((s) => r.stage!.toLowerCase().includes(s.toLowerCase())))) return false;
      if (regime.size && !(r.regime && regime.has(r.regime))) return false;
      if (words.length) {
        const blob = `${r.asset} ${r.operator ?? ""} ${r.juris ?? ""} ${r.holder ?? ""} ${(r.commodity || []).join(" ")} ${r.stage ?? ""} ${r.type ?? ""} ${r.rate ?? ""} ${r.features_note ?? ""}`.toLowerCase();
        if (!words.every((w) => blob.includes(w))) return false;
      }
      return true;
    });
    const sv = (r: Royalty) => {
      const v = (r as unknown as Record<string, unknown>)[sort];
      return typeof v === "number" ? v : (v ?? "").toString().toLowerCase();
    };
    out.sort((a, b) => {
      const x = sv(a), y = sv(b);
      if (x === y) return 0;
      // nulls/empties last regardless of dir
      if (x === "" || x === null) return 1;
      if (y === "" || y === null) return -1;
      return (x < y ? -1 : 1) * dir;
    });
    return out;
  }, [royalties, q, comm, stage, regime, sort, dir]);

  const Chip = ({ label, on, color, onClick }: { label: string; on: boolean; color?: string; onClick: () => void }) => (
    <span className={`chip${on ? " on" : ""}`} style={color ? { ["--c" as string]: color } : undefined} onClick={onClick}>
      {color && <span className="sw" />}{label}
    </span>
  );

  return (
    <main>
      <div className="cmd">
        <div className="brand">
          <div className="mark" />
          <div><div className="word">LODE</div><div className="tag">Royalty Origination · OR Royalties</div></div>
        </div>
        <div className="search">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="11" cy="11" r="7" /><path d="m21 21-4.3-4.3" /></svg>
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder='Search asset, operator, holder, jurisdiction…' />
        </div>
        <div className="kstats">
          <div className="kstat"><div className="n au">{kpis.royalties.toLocaleString()}</div><div className="l">Royalties</div></div>
          <div className="kstat"><div className="n">{kpis.assets}</div><div className="l">Assets</div></div>
          <div className="kstat"><div className="n">{kpis.pending}</div><div className="l">Pending</div></div>
          <div className="kstat"><div className="n">{kpis.verified_pct}%</div><div className="l">Verified</div></div>
        </div>
        <div className="live"><span className="d" />LIVE</div>
      </div>

      <div className="toolbar">
        <span className="glabel">Metal</span>
        {METALS.map((m) => <Chip key={m} label={m} color={M[m]} on={comm.has(m)} onClick={() => toggle(comm, setComm, m)} />)}
        <span className="glabel" style={{ marginLeft: 6 }}>Stage</span>
        {["Producing", "Development", "Resource", "Exploration"].map((s) => <Chip key={s} label={s} on={stage.has(s)} onClick={() => toggle(stage, setStage, s)} />)}
        <span className="glabel" style={{ marginLeft: 6 }}>Regime</span>
        {REGIMES.map((r) => <Chip key={r} label={r} on={regime.has(r)} onClick={() => toggle(regime, setRegime, r)} />)}
        <div className="rt">
          <div className="toggle">
            <button className={view === "table" ? "on" : ""} onClick={() => setView("table")}>▤ Table</button>
            <button className={view === "cards" ? "on" : ""} onClick={() => setView("cards")}>▦ Cards</button>
          </div>
          <span className="count"><b>{rows.length}</b> / {royalties.length}</span>
        </div>
      </div>

      {view === "table" ? (
        <div className="tablewrap">
          <table>
            <thead><tr>{COLS.map((c) => {
              const on = sort === c.k;
              return (
                <th key={c.k as string} className={on ? "sorted" : ""}
                  style={{ width: `${c.w}%`, ...(c.nosort ? { cursor: "default" } : {}) }}
                  onClick={() => { if (c.nosort) return; if (sort === c.k) setDir(-dir); else { setSort(c.k as string); setDir(1); } }}>
                  {c.t}{!c.nosort && on && <span className="ar">{dir < 0 ? "↓" : "↑"}</span>}
                </th>
              );
            })}</tr></thead>
            <tbody>
              {rows.map((r) => {
                const vein = M[(r.commodity || [])[0]] || "#5f584c";
                const feats = featureList(r);
                return (
                  <tr key={r.id} onClick={() => setSel(r)}>
                    <td className="asset" style={{ ["--vein" as string]: vein }}><span className="vein" /><span className="nm">{r.asset}</span></td>
                    <td className="op">{r.operator}</td>
                    <td className="juris">{r.juris}</td>
                    <td><Commodity c={r.commodity} /></td>
                    <td>{r.stage && <span className="stage">{r.stage}</span>}</td>
                    <td className="rate"><div className="v">{r.rate ?? "—"}</div></td>
                    <td className="type">{r.type}</td>
                    <td className="holder"><div className="h">{r.holder ?? "—"}</div>{r.holder_note && <div className="hn">{r.holder_note}</div>}</td>
                    <td><div className="comm">{feats.slice(0, 3).map((f, i) => <span key={i} style={{ ["--cc" as string]: "#2a2419", color: "var(--gold-hi)" }}>{f.k}</span>)}{!feats.length && <span style={{ color: "var(--text-3)" }}>—</span>}</div></td>
                    <td className="src">{r.source_label} {r.quote_verified && <span className="verified">✓</span>}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="cards">
          {rows.map((r) => {
            const vein = M[(r.commodity || [])[0]] || "#5f584c";
            return (
              <div key={r.id} className="card" style={{ ["--vein" as string]: vein }} onClick={() => setSel(r)}>
                <div className="ctop">
                  <div><div className="nm">{r.asset}</div><div className="op2">{r.operator} · {r.juris}</div></div>
                  <div><div className="rr">{r.rate ?? "—"}</div><div className="rt2">{r.type}</div></div>
                </div>
                <div className="mid"><Commodity c={r.commodity} />{r.stage && <span className="stage">{r.stage}</span>}</div>
                <div className="foot"><span className="hh">held by <b>{r.holder ?? "—"}</b></span>{r.quote_verified && <span className="verified" style={{ fontSize: 12 }}>✓ verified</span>}</div>
              </div>
            );
          })}
        </div>
      )}

      <div className={`backdrop${sel ? " open" : ""}`} onClick={() => setSel(null)} />
      <aside className={`drawer${sel ? " open" : ""}`}>
        {sel && <Detail r={sel} onClose={() => setSel(null)} />}
      </aside>
      <div className="mockflag">LIVE DB · {kpis.royalties.toLocaleString()} royalties · pending review</div>
    </main>
  );
}

function Detail({ r, onClose }: { r: Royalty; onClose: () => void }) {
  const feats = featureList(r);
  return (
    <div className="dpad">
      <button className="dclose" onClick={onClose}>✕</button>
      <div className="deye">{r.stage} · {r.juris}</div>
      <div className="dtitle">{r.asset}</div>
      <div className="dmeta">{r.operator}</div>
      <div className="dgrid">
        <div className="cell"><div className="k">Headline royalty</div><div className="v au">{r.rate} {r.type}</div></div>
        <div className="cell"><div className="k">Review</div><div className="v"><span className={`pill ${r.avail}`}><span className="d" />{cap(r.status)}</span></div></div>
        <div className="cell"><div className="k">Held by</div><div className="v">{r.holder ?? "—"}</div></div>
        <div className="cell"><div className="k">Commodities</div><div className="v">{(r.commodity || []).join(" · ")}</div></div>
      </div>
      <div className="sec">Verbatim from the technical report</div>
      <div className="assay">
        <div className="q">“{r.quote}”</div>
        <div className="cite">{r.source_label} &nbsp;·&nbsp; {r.quote_verified ? <span className="verified">✓ source-verified</span> : <span>unverified</span>}</div>
      </div>
      {feats.length > 0 && (
        <>
          <div className="sec">Royalty features</div>
          <div className="feat">{feats.map((f, i) => (
            <div key={i} className="frow"><span className="fk">{f.k}</span><span className="fv">{f.v}</span></div>
          ))}</div>
        </>
      )}
    </div>
  );
}
