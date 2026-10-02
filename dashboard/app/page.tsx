import { recordAccess } from "@/lib/access-log";
import { getRoyalties, getKpis } from "@/lib/queries";
import { currentViewer } from "@/lib/viewer";
import Board from "./board";

// Server component: read live Postgres, hand the rows to the client Board. No caching while we iterate.
export const dynamic = "force-dynamic";

export default async function Page() {
  // Fire-and-forget adoption logging. Never awaited and never allowed to throw — see /usage.
  void currentViewer().then((v) => v && recordAccess(v.email, v.name, "/"));
  const [royalties, kpis] = await Promise.all([getRoyalties(), getKpis()]);
  return <Board royalties={royalties} kpis={kpis} />;
}
