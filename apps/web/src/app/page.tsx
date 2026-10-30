/**
 * Holding page until the landing page is built here (src/components/landing/). It replaces the
 * create-next-app starter so nothing off-palette or placeholder ships; the words are the copy
 * deck's rail headline and sentence (.taste-skill/copy.md), no figures.
 */
export default function Home() {
  return (
    <main className="flex min-h-dvh flex-col justify-end gap-6 px-[var(--margin)] py-[var(--section)]">
      <h1 className="display max-w-[12ch]">BlindSpot counts unseen fires</h1>
      <p className="lead text-[var(--muted)]">Satellites look at 1:30. Fires start at 5.</p>
    </main>
  );
}
