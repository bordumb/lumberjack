import { Dashboard } from "@/components/dashboard";
import { RepoBrowser } from "@/components/repo-browser";

export const dynamic = "force-dynamic";

/**
 * One route, two subjects. Without a stand this is the repository; with one it is that
 * run. The nav decides which by what it links to, so the URL stays shareable either way.
 */
export default async function Page({
  searchParams,
}: {
  searchParams: Promise<{ stand?: string; repo?: string }>;
}) {
  const { stand, repo } = await searchParams;

  if (!stand) {
    return (
      <main className="h-full">
        <RepoBrowser repo={repo ?? null} />
      </main>
    );
  }

  return (
    <main>
      <Dashboard stand={stand} />
    </main>
  );
}
