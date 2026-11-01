'use client';

import { useSyncExternalStore } from 'react';

import { readTheme, setTheme, useTheme } from '@/lib/theme';

const S = { fill: 'none', stroke: 'currentColor', strokeWidth: 1.5, strokeLinecap: 'round', strokeLinejoin: 'round' } as const;

const never = () => () => {};
/** False in the server's markup and while React hydrates it, true from the render after. */
const useHydrated = () => useSyncExternalStore(never, () => true, () => false);

/**
 * The theme switch in the site nav: an icon button showing the theme it switches to (a sun on dark, a
 * moon on light) and named for that action.
 *
 * Both icons and both names are always in the markup, and the .dark-only / .light-only rule in
 * app/globals.css shows the right ones from [data-theme], so icon and accessible name are right from
 * the first paint, before React hydrates (a name read from useTheme() would say "Switch to light
 * theme" on a light page until then, since hydration reads 'dark'). The tooltip comes once React
 * knows the theme: before that there is none, rather than a wrong one.
 */
export function ThemeToggle() {
  const theme = useTheme();
  const hydrated = useHydrated();
  return (
    <button
      type="button"
      className="sn-theme"
      title={hydrated ? (theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme') : undefined}
      // read at click time: right after hydration the rendered value can trail the page by a frame
      onClick={() => setTheme(readTheme() === 'dark' ? 'light' : 'dark', { animate: true })}
    >
      <svg className="dark-only" width="20" height="20" viewBox="0 0 18 18" aria-hidden="true" focusable="false">
        <circle {...S} cx="9" cy="9" r="3.25" />
        <path {...S} d="M9 1.75v1.5M9 14.75v1.5M1.75 9h1.5M14.75 9h1.5M3.87 3.87l1.06 1.06M13.07 13.07l1.06 1.06M3.87 14.13l1.06-1.06M13.07 4.93l1.06-1.06" />
      </svg>
      <svg className="light-only" width="20" height="20" viewBox="0 0 18 18" aria-hidden="true" focusable="false">
        <path {...S} d="M15.3 9.55A6.3 6.3 0 1 1 8.45 2.7a4.9 4.9 0 0 0 6.85 6.85z" />
      </svg>
      <span className="sr-only dark-only">Switch to light theme</span>
      <span className="sr-only light-only">Switch to dark theme</span>
    </button>
  );
}
