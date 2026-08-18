import { listStands, latestStand } from "@/lib/ledger";

export const dynamic = "force-dynamic";

export function GET() {
  return Response.json({ stands: listStands(), latest: latestStand() });
}
