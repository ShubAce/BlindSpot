'use client';

import { createElement, useEffect, useLayoutEffect, useSyncExternalStore } from 'react';

/**
 * The colour theme: dark, the default and the brand (warm black, straw gold), or light (bone paper).
 *
 * One attribute carries it: <html data-theme="dark" | "light">. ThemeScript sets it in <head> before
 * the first paint: ?theme=light|dark in the address wins and is saved, else the choice saved under
 * localStorage 'bs-theme', else dark. A choice made with the switch takes ?theme= out of the address,
 * so a reload keeps that choice. Dark stays the default whatever the operating system prefers: it is
 * the brand, and the site was designed on it.
 *
 * CSS keys every light value off :root[data-theme="light"] (the app surface off
 * :root[data-theme="light"] [data-surface="app"]), so stylesheets need nothing from here. Code that
 * paints colours itself (the map, deck.gl layers, a canvas) calls useTheme() and re-renders when the
 * theme changes; code outside React subscribes with onThemeChange(). The theme changes only through
 * setTheme(), which the toggle in the site nav calls.
 *
 * A client module: the hook needs the browser, and the root layout reaches ThemeScript as a Client
 * Component reference, which keeps the boot script and the runtime reading one set of constants.
 */

export type Theme = 'dark' | 'light';

/** Dispatched on window after every theme change; `detail` is the new Theme. */
export const THEME_EVENT = 'bs-themechange';

// public/sw.js repeats these three and cannot import this module, so keep them equal there: pageKey
// drops PARAM from a saved page's address, and FALLBACK_HTML's script reads PARAM and STORAGE_KEY and
// sets ATTRIBUTE.
const STORAGE_KEY = 'bs-theme';
const PARAM = 'theme';
const ATTRIBUTE = 'data-theme';

// The browser's toolbar colour on phones: the nav bar's ground (--sn-ground), which every page opens
// with. The layout's viewport export sends the dark one; light adds its own meta ahead of it (the
// first theme-color in the document wins), marked so the toggle can find it again.
const THEME_COLOR: Readonly<Record<Theme, string>> = { dark: '#0c0a08', light: '#efeeec' };
const OWN_META = 'data-bs-theme-color';

const isTheme = (value: unknown): value is Theme => value === 'dark' || value === 'light';

// Runs while the parser is still in <head>, before anything paints. Plain ES5 on purpose: it runs
// before any polyfill, on every browser that loads the page. Storage can throw (blocked cookies,
// some private windows); the theme then holds for this page only.
const BOOT = `(function(){var d=document.documentElement,t="dark";try{var q=new URLSearchParams(location.search).get(${JSON.stringify(PARAM)});if(q==="light"||q==="dark"){t=q;localStorage.setItem(${JSON.stringify(STORAGE_KEY)},q)}else if(localStorage.getItem(${JSON.stringify(STORAGE_KEY)})==="light")t="light"}catch(e){}d.setAttribute(${JSON.stringify(ATTRIBUTE)},t);d.style.colorScheme=t;if(t==="light"){var h=document.head,m=document.createElement("meta");m.name="theme-color";m.content=${JSON.stringify(THEME_COLOR.light)};m.setAttribute(${JSON.stringify(OWN_META)},"");h.insertBefore(m,h.querySelector('meta[name="theme-color"]'))}})()`;

/**
 * The boot script, for the root layout's <head>. The server sends it runnable; once in the browser it
 * has done its work, so the client copy is typed as data, which keeps React from warning about a
 * script it would create but never run (the type differs from the server's on purpose, hence
 * suppressHydrationWarning). The root layout renders it once per document, so it also keeps the
 * theme right for the rest of the visit (useThemeSync), on every page, with or without the site nav.
 */
export function ThemeScript() {
  useThemeSync();
  return createElement('script', {
    type: typeof window === 'undefined' ? undefined : 'text/plain',
    suppressHydrationWarning: true,
    dangerouslySetInnerHTML: { __html: BOOT },
  });
}

/** The theme on screen now ('dark' on the server, and before the boot script has run). */
export function readTheme(): Theme {
  if (typeof document === 'undefined') return 'dark';
  return document.documentElement.getAttribute(ATTRIBUTE) === 'light' ? 'light' : 'dark';
}

// The theme this page last showed, kept apart from the attribute so it can be put back exactly (even
// with storage refused) if React rebuilds <html> without it. The boot script ran before this module.
let shown: Theme | null =
  typeof document !== 'undefined' && document.documentElement.hasAttribute(ATTRIBUTE) ? readTheme() : null;

// The theme a fresh load of this address would choose, by the boot script's order.
function savedTheme(): Theme {
  try {
    const asked = new URLSearchParams(window.location.search).get(PARAM);
    if (isTheme(asked)) return asked;
    return localStorage.getItem(STORAGE_KEY) === 'light' ? 'light' : 'dark';
  } catch {
    return 'dark';
  }
}

function paintToolbar(theme: Theme) {
  const head = document.head;
  let meta = head.querySelector<HTMLMetaElement>(`meta[${OWN_META}]`);
  if (!meta) {
    meta = document.createElement('meta');
    meta.name = 'theme-color';
    meta.setAttribute(OWN_META, '');
    head.insertBefore(meta, head.querySelector('meta[name="theme-color"]'));
  }
  meta.content = THEME_COLOR[theme];
}

function apply(theme: Theme) {
  shown = theme;
  const root = document.documentElement;
  // Colours change in one step: with transitions on, every hover-animated link and button would
  // fade on its own clock after the page had already switched.
  const still = document.createElement('style');
  still.textContent = '*,*::before,*::after{transition:none!important}';
  document.head.appendChild(still);
  root.setAttribute(ATTRIBUTE, theme);
  root.style.colorScheme = theme;
  paintToolbar(theme);
  void getComputedStyle(root).color; // settle the new colours while transitions are off
  requestAnimationFrame(() => requestAnimationFrame(() => still.remove()));
  window.dispatchEvent(new CustomEvent<Theme>(THEME_EVENT, { detail: theme }));
}

export type SetThemeOptions = {
  /** Cross-fade the whole page into the new theme where the browser has view transitions. */
  animate?: boolean;
  /** Save the choice for the next visit (default true). */
  persist?: boolean;
};

/** Switch the theme: the attribute, the form controls' colour scheme, the toolbar colour, then THEME_EVENT. */
export function setTheme(theme: Theme, { animate = false, persist = true }: SetThemeOptions = {}): void {
  if (typeof document === 'undefined') return;
  if (persist) {
    try {
      localStorage.setItem(STORAGE_KEY, theme);
    } catch {
      // storage refused: the theme holds for this page only
    }
    // A ?theme= in the address would win again on reload (and save itself over this choice), so the
    // choice takes it out. Only here, on a visitor's choice: the router is up by then, and it keeps
    // its state across a native replaceState (the boot script runs before it exists).
    const url = new URL(window.location.href);
    if (url.searchParams.has(PARAM)) {
      url.searchParams.delete(PARAM);
      window.history.replaceState(null, '', url);
    }
  }
  if (readTheme() === theme && document.documentElement.hasAttribute(ATTRIBUTE)) return;
  // The browser's own quarter-second cross-fade instead of a full-screen flash. It stays under reduced
  // motion too: the house rule keeps opacity changes there, and a fade is the gentler switch.
  if (animate && typeof document.startViewTransition === 'function' && document.visibilityState === 'visible') {
    document.startViewTransition(() => apply(theme));
  } else {
    apply(theme);
  }
}

/** Calls `onChange` after every theme change (THEME_EVENT), until the returned function is called. */
export function onThemeChange(onChange: () => void): () => void {
  window.addEventListener(THEME_EVENT, onChange);
  return () => window.removeEventListener(THEME_EVENT, onChange);
}

const onServer = (): Theme => 'dark';

/**
 * The current theme, re-rendering the caller when it changes. On the server and during hydration it
 * reads 'dark' (the markup React hydrates against); React then re-renders with the browser's value,
 * so a light page never meets a hydration mismatch. Things that must be right in the very first
 * paint (before hydration) belong in CSS under [data-theme], not here.
 */
export function useTheme(): Theme {
  return useSyncExternalStore(onThemeChange, readTheme, onServer);
}

/**
 * Keeps the page's theme right while the caller is mounted; ThemeScript calls it, so every page has
 * it once. It puts the attribute back if React rebuilt <html> without it (after a failed hydration,
 * and after some dev remounts), before anything paints, and it follows a choice made in another tab.
 */
function useThemeSync(): void {
  useLayoutEffect(() => {
    if (!document.documentElement.hasAttribute(ATTRIBUTE)) setTheme(shown ?? savedTheme(), { persist: false });
  }, []);
  useEffect(() => {
    const onStorage = (event: StorageEvent) => {
      if (event.key === STORAGE_KEY && isTheme(event.newValue)) setTheme(event.newValue, { persist: false });
    };
    window.addEventListener('storage', onStorage);
    return () => window.removeEventListener('storage', onStorage);
  }, []);
}
