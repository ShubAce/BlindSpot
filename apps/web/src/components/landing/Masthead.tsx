import { s } from './styles';

/**
 * The landing's title row, just under the site bar: the h1 (the name with what it does) and the one
 * supporting sentence. The navigation and the one action live in the site bar (components/nav).
 *
 * The sentence is the thesis in words, no figure: the figures sit just below it (the share of burnt
 * area with no fire alert, and the clock of who looks when), each read from the API.
 */
export function Masthead() {
  return (
    <div className={s.rail}>
      <div className={s.railInner}>
        <h1 className={s.railTitle}>BlindSpot looks between the passes</h1>
        <p className={s.railSentence}>Fields are lit after the fire satellites pass over, so their fire counts miss most of the burning.</p>
      </div>
    </div>
  );
}
