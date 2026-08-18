import Link from "next/link";
import { Dashboard } from "@/components/dashboard";
import { latestStand, listStands } from "@/lib/ledger";

export const dynamic = "force-dynamic";

export default async function Page({
  searchParams,
}: {
  searchParams: Promise<{ stand?: string }>;
}) {
  const { stand: requested } = await searchParams;
  const stand = requested ?? latestStand();
  const stands = listStands();

  if (!stand) {
    return (
      <main className="mx-auto max-w-2xl p-16 text-center">
        <h1 className="font-sans text-xl font-semibold tracking-[-0.02em]">No stands yet</h1>
        <p className="mt-2 text-sm text-muted-foreground">
          Start one with{" "}
          <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-[12px]">uv run lj run</code>{" "}
          and this page will pick it up.
        </p>
      </main>
    );
  }

  return (
    <main>
      {stands.length > 1 && (
        <nav className="flex flex-wrap gap-2 border-b border-border/60 px-8 py-3">
          {stands.slice(0, 6).map((item) => (
            <Link
              key={item.stand}
              href={`/?stand=${item.stand}`}
              className={`rounded-md border px-2 py-1 font-mono text-[11px] transition-colors ${
                item.stand === stand
                  ? "border-primary/40 bg-primary/10 text-primary"
                  : "border-border text-muted-foreground hover:text-foreground"
              }`}
            >
              {item.stand}
            </Link>
          ))}
        </nav>
      )}
      <Dashboard stand={stand} />
    </main>
  );
}
