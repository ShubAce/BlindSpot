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
