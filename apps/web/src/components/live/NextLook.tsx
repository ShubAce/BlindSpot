'use client';

import { Skeleton } from '@/components/ui/states';
import { duration, timeIST } from '@/lib/format';

import { useLiveHeadline } from './data';
import styles from './styles';
import { useWatch } from './watch';

/**
 * The title row's countdown: the next fire satellite to look at Punjab, or the one watching now.
 * Computed every second in the browser from the CelesTrak elements; until they are read it shows
 * the headline's own prediction (the same elements, propagated by the API).
 */
export function NextLook() {
  const { site, now, status, tle } = useWatch(1_000);
  const head = useLiveHeadline();
  const place = site?.place ?? 'Punjab';
  const watching = status?.watching[0];
  const fallback = head.data?.passes?.next;

  let name: string | null = null;
  let at: Date | null = null;
  if (status?.next) {
    name = status.next.name;
    at = status.next.at;
  } else if (!status && fallback) {
    name = fallback.satellite;
    at = new Date(fallback.at_utc);
  }

  return (
    <div className={styles.nextLook} aria-live="off">
      <p className={styles.figLabel}>{watching ? `Watching ${place} now` : `Next fire satellite over ${place}`}</p>
      {watching && now ? (
        <>
          <p className={styles.nextValue}>{watching.name}</p>
          <p className={styles.figSub}>
            leaves in <span className="nums">{duration(watching.until.getTime() - now.getTime())}</span>
          </p>
        </>
      ) : name && at ? (
        <>
          <p className={styles.nextValue}>
            {name} <span className="nums">{timeIST(at)}</span> <span className={styles.unit}>IST</span>
          </p>
          <p className={styles.figSub}>{now ? <>in <span className="nums">{duration(at.getTime() - now.getTime())}</span>; nobody is looking until then</> : <Skeleton width={120} height={12} />}</p>
        </>
      ) : head.data?.passes?.available === false ? (
        <p className={styles.figSub}>No pass prediction: {head.data.passes.reason ?? 'orbital elements missing'}.</p>
      ) : tle.error && !tle.data && !fallback && (head.error || head.data) ? (
        // neither the elements nor the headline's own prediction came: say so, never a skeleton forever
        <p className={styles.figSub}>No pass time right now: the satellites’ orbital elements did not load.</p>
      ) : (
        <>
          <Skeleton width={150} height={22} />
          <Skeleton width={110} height={12} style={{ marginTop: 4 }} />
        </>
      )}
    </div>
  );
}
