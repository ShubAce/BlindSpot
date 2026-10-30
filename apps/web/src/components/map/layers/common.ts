/** The text face for deck.gl labels. next/font registers Work Sans under its own family name, so
 *  read it from the CSS variable rather than guessing; fall back to the system sans. */
export function mapLabelFont(): string {
  if (typeof document === 'undefined') return 'sans-serif';
  const v = getComputedStyle(document.documentElement).getPropertyValue('--font-work-sans').trim();
  return v ? `${v}, sans-serif` : 'sans-serif';
}
