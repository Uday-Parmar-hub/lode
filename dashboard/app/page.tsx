import { getRoyalties, getKpis } from "@/lib/queries";
import Board from "./board";

// Server component: read live Postgres, hand the rows to the client Board. No caching while we iterate.
export const dynamic = "force-dynamic";

export default async function Page() {
  const [royalties, kpis] = await Promise.all([getRoyalties(), getKpis()]);
  return <Board royalties={royalties} kpis={kpis} />;
}
