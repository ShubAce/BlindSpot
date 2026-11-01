'use client';

import { useState } from 'react';

import { isOffline, useConnectivity, type Connectivity } from './connectivity';
import { savedAgo, savedStamp, useClock } from './time';

/**
 * The small fixed notice that says a page is showing saved data: "Offline: showing the last data this
 * browser saw, from 22:41 IST (14 min ago)." It appears when the device reports no network, or when
 * the service worker answered this page (its load, a payload or an API answer) from a saved copy; it
 * goes when fresh answers arrive again. "From" is the oldest saved copy on the page, or, if nothing
 * came from a saved copy yet, the last time the network answered it.
 *
 * Bottom left on wide screens; on a phone it spans the width just above the bottom tab bar (and above
 * mission control's list sheet). "Hide" folds it to a small pill until the next time it appears.
 * App colour tokens only (data-surface="app"), so the light theme recolours it.
 */

type Notice = { key: string; tone: 'warning' | 'neutral'; lead: string; text: string; at: number | null; pill: string };

function noticeOf(c: Connectivity): Notice | null {
  if (c.screen) return null;
  if (c.switchedOff) {
    return {
      key: 'off',
      tone: 'neutral',
      lead: 'Offline copies off:',
      text: 'this browser’s saved pages are deleted and nothing new is saved. Open any page with ?sw=on to turn them back on',
      at: null,
      pill: 'Offline copies off',
    };
  }
  const offline = isOffline(c);
  const slow = !offline && (c.doc?.reason === 'slow' || c.saved?.reason === 'slow');
  if (!offline && !slow) return null;
  const known = [c.doc?.savedAt, c.saved?.oldest].filter((t): t is number => typeof t === 'number' && t > 0);
  const at = known.length ? Math.min(...known) : c.liveAt;
  return offline
    ? { key: 'offline', tone: 'warning', lead: 'Offline:', text: 'showing the last data this browser saw', at, pill: 'Offline' }
    : // a slow network or a cold API: either way the answer is late, so the words do not guess which
      { key: 'slow', tone: 'warning', lead: 'No fresh answer yet:', text: 'showing the last data this browser saw', at, pill: 'Saved data' };
}

export function OfflineNotice() {
  const notice = noticeOf(useConnectivity());
  // the clock only runs while a time is on screen
  const now = useClock(notice?.at != null);
  const age = notice?.at && now ? { stamp: savedStamp(notice.at, now), ago: savedAgo(notice.at, now) } : null;
  const key = notice?.key ?? null;
  // "Hide" lasts until this notice goes away; the next one shows in full
  const [hidden, setHidden] = useState<string | null>(null);
  const [shownKey, setShownKey] = useState<string | null>(null);
  if (shownKey !== key) {
    setShownKey(key);
    if (key == null) setHidden(null);
  }

  return (
    <div className="pwa-dock" data-surface="app" role="status" aria-live="polite">
      {notice && hidden !== notice.key ? (
        <div className="pwa-notice" data-tone={notice.tone}>
          <span className="pwa-notice__dot" aria-hidden="true" />
          <p className="pwa-notice__text">
            <strong>{notice.lead}</strong> {notice.text}
            {age ? `, from ${age.stamp}` : ''}
            {/* the minutes count up as the clock ticks: shown, not re-announced */}
            {age ? <span className="pwa-notice__age" aria-hidden="true">{` (${age.ago})`}</span> : null}.
          </p>
          <button type="button" className="pwa-notice__hide" onClick={() => setHidden(notice.key)} aria-label="Hide this notice">
            Hide
          </button>
        </div>
      ) : notice ? (
        <button type="button" className="pwa-pill" data-tone={notice.tone} onClick={() => setHidden(null)} aria-label={`${notice.pill}: show the notice`}>
          <span className="pwa-notice__dot" aria-hidden="true" />
          {notice.pill}
        </button>
      ) : null}
    </div>
  );
}
