/**
 * The site's three languages, in the order the pages offer them: the bulletin's tabs and the field
 * brief's CSVs. `label` is the name in its own script (what a reader of that language looks for);
 * `english` names it in an English sentence.
 */

export type LangCode = 'en' | 'hi' | 'pa';

export const LANGS: readonly { code: LangCode; label: string; english: string }[] = [
  { code: 'en', label: 'English', english: 'English' },
  { code: 'hi', label: 'हिन्दी', english: 'Hindi' },
  { code: 'pa', label: 'ਪੰਜਾਬੀ', english: 'Punjabi' },
];

export const isLangCode = (v: unknown): v is LangCode => LANGS.some((l) => l.code === v);

/** The language named in an English sentence: "Hindi" for 'hi'. A code the site does not offer stays as it is. */
export const langEnglish = (code: string): string => LANGS.find((l) => l.code === code)?.english ?? code;
