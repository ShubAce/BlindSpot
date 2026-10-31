'use client';

import { relative } from '@/lib/format';
import { useNow } from '@/lib/live';

/** "answered 3 h ago", computed in the browser (the server never guesses the reader's clock). */
export function AnswerAge({ at }: { at: string | null }) {
  const now = useNow(60_000);
  if (!at) return <>answer time not recorded</>;
  return <time dateTime={at}>{now ? `answered ${relative(at, now)}` : 'answered recently'}</time>;
}
