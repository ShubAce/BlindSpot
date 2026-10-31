import { Noto_Sans_Devanagari, Noto_Sans_Gurmukhi } from 'next/font/google';

/**
 * The two Indian scripts the bulletin is written in, self-hosted by next/font. This module is only
 * imported by the bulletin, so no other route downloads them; each loads only its own script subset.
 * Latin digits and markers inside the Hindi and Punjabi text fall back to Work Sans.
 */
export const devanagari = Noto_Sans_Devanagari({
  weight: ['400', '600'],
  subsets: ['devanagari'],
  display: 'swap',
  variable: '--font-devanagari',
});

export const gurmukhi = Noto_Sans_Gurmukhi({
  weight: ['400', '600'],
  subsets: ['gurmukhi'],
  display: 'swap',
  variable: '--font-gurmukhi',
});
