import { browse } from "@/lib/repos";

export const dynamic = "force-dynamic";

export function GET(req: Request) {
  return Response.json(browse(new URL(req.url).searchParams.get("at")));
}
