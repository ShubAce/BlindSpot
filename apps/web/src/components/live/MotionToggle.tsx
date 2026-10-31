'use client';

import { useAnimationClock } from '@/components/map/clock';

import styles from './styles';

/**
 * Pause and play the moving layers (smoke trails, wind): a loop that runs for minutes beside the
 * data needs a stop (WCAG 2.2.2). Under prefers-reduced-motion they are already still, so this
 * says so instead.
 */
export function MotionToggle() {
  // a slow snapshot: this control re-renders when play state changes, not every frame
  const clock = useAnimationClock(0.2);
  if (clock.reduced) return <span className={styles.motionNote}>Motion off (system setting)</span>;
  return (
    <button type="button" className={styles.ctrl} aria-pressed={!clock.playing} onClick={clock.toggle}>
      {clock.playing ? (
        <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
          <path d="M3 2v8M9 2v8" stroke="currentColor" strokeWidth="2" />
        </svg>
      ) : (
        <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
          <path d="M3 1.5l7 4.5-7 4.5z" fill="currentColor" />
        </svg>
      )}
      <span>{clock.playing ? 'Pause motion' : 'Play motion'}</span>
    </button>
  );
}
