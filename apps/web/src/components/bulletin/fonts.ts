import { Noto_Sans_Devanagari, Noto_Sans_Gurmukhi } from 'next/font/google';

/**
 * The two Indian scripts the bulletin is written in, self-hosted by next/font. This module is only
 * imported by the bulletin, so no other route downloads them; each loads only its own script subset.
 * Latin digits and markers inside the Hindi and Punjabi text fall back to Work Sans.
 *
 * Not preloaded: their @font-face rules can land in a stylesheet every page shares, and a preload
 * would then fetch both faces (about 150 KB) on every page and warn that they went unused. Without
 * it the browser downloads a face only when text on screen needs its script (each face's
 * unicode-range covers that script only), which is the bulletin in Hindi or Punjabi.
 */
export const devanagari = Noto_Sans_Devanagari({
  weight: ['400', '600'],
  subsets: ['devanagari'],
  display: 'swap',
  preload: false,
  variable: '--font-devanagari',
});

export const gurmukhi = Noto_Sans_Gurmukhi({
  weight: ['400', '600'],
  subsets: ['gurmukhi'],
  display: 'swap',
  preload: false,
  variable: '--font-gurmukhi',
});
