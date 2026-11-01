/**
 * The shared animation clock for moving map layers (smoke trails, wind), and the reduced-motion
 * preference.
 *
 * One requestAnimationFrame loop drives every animated deck.gl layer: the map overlay subscribes and
 * re-renders its layers each frame without re-rendering React. UI that shows the clock (a "+14 h"
 * label, a play button) uses `useAnimationClock`, which re-renders only a few times a second.
 * The loop runs only while something subscribes and the clock is playing; under
 * `prefers-reduced-motion: reduce` it stops and layers draw their still version.
 */

import { useCallback, useSyncExternalStore } from 'react';

export type ClockFrame = {
  /** Seconds of animation played so far (pauses stop it; speed scales it). */
  t: number;
  /** The viewer asked for reduced motion: draw the still version. */
  reduced: boolean;
  playing: boolean;
};

type Listener = (frame: ClockFrame) => void;

class AnimationClock {
  private t = 0;
  private playing = true;
  private reduced = false;
  private speed = 1;
  private last = 0;
  private raf = 0;
  private listeners = new Set<Listener>();
  private uiListeners = new Set<() => void>();

  frame(): ClockFrame {
    return { t: this.t, reduced: this.reduced, playing: this.playing && !this.reduced };
  }

  /** Called every frame while running, and once immediately. */
  subscribe(fn: Listener): () => void {
    this.listeners.add(fn);
    fn(this.frame());
    this.ensureLoop();
    return () => {
      this.listeners.delete(fn);
      if (!this.listeners.size) this.stopLoop();
    };
  }

  /** For useSyncExternalStore: notified on every frame and on state changes. */
  subscribeUi = (fn: () => void): (() => void) => {
    this.uiListeners.add(fn);
    return () => {
      this.uiListeners.delete(fn);
    };
  };

  get time(): number {
    return this.t;
  }
  get isPlaying(): boolean {
    return this.playing && !this.reduced;
  }
  get isReduced(): boolean {
    return this.reduced;
  }

  play(): void {
    this.playing = true;
    this.ensureLoop();
    this.emit();
  }
  pause(): void {
    this.playing = false;
    this.stopLoop();
    this.emit();
  }
  toggle(): void {
    if (this.playing) this.pause();
    else this.play();
  }
  setSpeed(speed: number): void {
    this.speed = Math.max(0, speed);
  }
  /** Jump to a moment (seconds of animation), e.g. from a scrubber. */
  seek(t: number): void {
    this.t = Math.max(0, t);
    this.emit();
  }
  setReduced(reduced: boolean): void {
    if (reduced === this.reduced) return;
    this.reduced = reduced;
    if (reduced) this.stopLoop();
    else this.ensureLoop();
    this.emit();
  }

  private emit(): void {
    const f = this.frame();
    for (const l of this.listeners) l(f);
    for (const l of this.uiListeners) l();
  }

  private ensureLoop(): void {
    if (this.raf || !this.playing || this.reduced || !this.listeners.size || typeof requestAnimationFrame === 'undefined') return;
    this.last = 0;
    const loop = (ts: number) => {
      // A hidden tab pauses rAF; clamp the step so the animation does not jump when it returns.
      const dt = this.last ? Math.min(0.1, (ts - this.last) / 1000) : 0;
      this.last = ts;
      this.t += dt * this.speed;
      this.emit();
      this.raf = requestAnimationFrame(loop);
    };
    this.raf = requestAnimationFrame(loop);
  }

  private stopLoop(): void {
    if (this.raf && typeof cancelAnimationFrame !== 'undefined') cancelAnimationFrame(this.raf);
    this.raf = 0;
  }
}

/** The one clock all maps share. */
export const mapClock = new AnimationClock();

/**
 * The clock for UI: time quantised to `fps` updates a second, with play/pause controls.
 *   const clock = useAnimationClock();  <button onClick={clock.toggle}>{clock.playing ? 'Pause' : 'Play'}</button>
 */
export function useAnimationClock(fps = 4) {
  const getSnapshot = useCallback(
    () => `${Math.floor(mapClock.time * fps)}|${mapClock.isPlaying ? 1 : 0}|${mapClock.isReduced ? 1 : 0}`,
    [fps],
  );
  const snap = useSyncExternalStore(mapClock.subscribeUi, getSnapshot, () => '0|0|0');
  const [tick, playing, reduced] = snap.split('|');
  return {
    t: Number(tick) / fps,
    playing: playing === '1',
    reduced: reduced === '1',
    play: () => mapClock.play(),
    pause: () => mapClock.pause(),
    toggle: () => mapClock.toggle(),
    seek: (t: number) => mapClock.seek(t),
  };
}

// ---------------------------------------------------------------------------------------------
// prefers-reduced-motion

const QUERY = '(prefers-reduced-motion: reduce)';

function subscribeReduced(cb: () => void): () => void {
  if (typeof window === 'undefined' || !window.matchMedia) return () => {};
  const mq = window.matchMedia(QUERY);
  mq.addEventListener('change', cb);
  return () => mq.removeEventListener('change', cb);
}
/** The preference read once, for a one-off choice outside render (a scroll's behaviour, a playback's pace). */
export const prefersReducedMotion = () => typeof window !== 'undefined' && !!window.matchMedia && window.matchMedia(QUERY).matches;
const getReduced = prefersReducedMotion;

/** True when the viewer asked for reduced motion. False during server rendering. */
export function usePrefersReducedMotion(): boolean {
  return useSyncExternalStore(subscribeReduced, getReduced, () => false);
}
