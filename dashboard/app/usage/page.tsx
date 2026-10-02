import { notFound } from "next/navigation";

import {
  getDailyViews,
  getPathViews,
  getUsageSummary,
  getViewerActivity,
} from "@/lib/access-log";
import { canSeeUsage } from "@/lib/viewer";

/**
 * Unlisted usage analytics. Nothing in the UI links here — it is reachable only by typing the path.
 *
 * Obscurity is not the gate though: LODE sits behind org-wide sign-in, so any colleague who guessed
 * the path would already be authenticated. canSeeUsage() (LODE_USAGE_EMAILS) is the real check, and
 * it returns 404 rather than 403 so the page does not advertise that it exists.
 */
export const dynamic = "force-dynamic";

function fmtDateTime(v: string | null): string {
  if (!v) return "—";
  const d = new Date(v);
  return isNaN(d.getTime()) ? "—" : d.toLocaleString("en-US", {
    month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZone: "America/New_York",
  });
}

function fmtDay(v: string): string {
  const [y, m, d] = v.split("-").map(Number);
  return y ? new Date(y, m - 1, d).toLocaleDateString("en-US", { month: "short", day: "numeric" }) : v;
}

function Tile({ n, label, sub }: { n: number; label: string; sub: string }) {
  return (
    <div className="utile">
      <div className="un">{n}</div>
      <div className="ul">{label}</div>
      <div className="us">{sub}</div>
    </div>
  );
}

export default async function UsagePage() {
  if (!(await canSeeUsage())) notFound();

  const [summary, viewers, daily, paths] = await Promise.all([
    getUsageSummary(), getViewerActivity(), getDailyViews(21), getPathViews(),
  ]);
  const maxDay = Math.max(1, ...daily.map((d) => d.views));
  const empty = summary.views === 0;

  return (
    <main className="usage">
      <header>
        <h1>Usage</h1>
        <p className="sub">
          Who has opened LODE, and when. Unlisted — nothing links here.
          {summary.first_at && <> Logging since {fmtDateTime(summary.first_at)}.</>}
        </p>
      </header>

      {empty ? (
        <div className="ucard uempty">
          <strong>No page views recorded yet.</strong>
          <p>
            The log starts the first time someone opens the dashboard while signed in. If you have
            opened it yourself since this shipped and still see nothing, the write is failing — check
            the container logs for <code>access-log write failed</code>.
          </p>
        </div>
      ) : (
        <>
          <section className="utiles">
            <Tile n={summary.views} label="Page views" sub="all time" />
            <Tile n={summary.viewers} label="People" sub="distinct signed-in users" />
            <Tile n={summary.views_7d} label="Views" sub="last 7 days" />
            <Tile n={summary.viewers_7d} label="People" sub="last 7 days" />
          </section>

          <section className="ucard">
            <h2>Who</h2>
            <div className="uscroll"><table className="utable">
              <thead>
                <tr><th>Person</th><th className="r">Views</th><th className="r">Days active</th>
                  <th>First seen</th><th>Last seen</th></tr>
              </thead>
              <tbody>
                {viewers.map((v) => (
                  <tr key={v.email}>
                    <td>
                      <div className="uname">{v.name ?? v.email.split("@")[0]}</div>
                      <div className="umail">{v.email}</div>
                    </td>
                    <td className="r num">{v.views}</td>
                    <td className="r num">{v.days_active}</td>
                    <td className="udim">{fmtDateTime(v.first_at)}</td>
                    <td className="udim">{fmtDateTime(v.last_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table></div>
          </section>

          <section className="ucard">
            <h2>Last {daily.length} active {daily.length === 1 ? "day" : "days"}</h2>
            <div className="ubars">
              {daily.map((d) => (
                <div key={d.day} className="ubar" title={`${d.views} views by ${d.viewers} on ${fmtDay(d.day)}`}>
                  <div className="ubarfill" style={{ height: `${Math.round((d.views / maxDay) * 100)}%` }} />
                  <div className="ubarlabel">{fmtDay(d.day)}</div>
                </div>
              ))}
            </div>
          </section>

          {paths.length > 1 && (
            <section className="ucard">
              <h2>Pages</h2>
              <div className="uscroll"><table className="utable">
                <thead><tr><th>Path</th><th className="r">Views</th></tr></thead>
                <tbody>
                  {paths.map((p) => (
                    <tr key={p.path}><td><code>{p.path}</code></td><td className="r num">{p.views}</td></tr>
                  ))}
                </tbody>
              </table></div>
            </section>
          )}
        </>
      )}

      <footer className="ufoot">
        Visible to the addresses in <code>LODE_USAGE_EMAILS</code>. Everyone else gets a 404.
      </footer>
    </main>
  );
}
