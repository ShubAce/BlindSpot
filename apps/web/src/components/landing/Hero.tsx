import { REFERENCE_SITE } from '@/lib/aoi';
import { dateIST, metricText, number, percent } from '@/lib/format';

import { BlindClock } from './BlindClock';
import { clockModel, livePasses } from './clock';
import { getDiurnal, getHeadline, getPasses } from './data';
import { s } from './styles';

/**
 * The first screen's two data blocks besides the pictures: the blind-spot clock and the key figure.
 * Each awaits only its own reads inside its own <Suspense> (page.tsx), and says in one line what
 * failed instead of guessing.
 */

export async function HeroClock() {
  const [passes, diurnal] = await Promise.all([getPasses(), getDiurnal()]);
  const d = diurnal.ok ? diurnal.data : null;
  // the dial needs either the API's polar windows (diurnal) or today's passes; the live line needs the passes
  if (!passes.ok && !d?.polar_windows_ist?.length) {
    return <p className={`${s.clockMissing} ${s.honest}`}>{`The satellites’ pass times did not load, so the clock cannot be drawn. ${passes.error.message}`}</p>;
  }
  const stamp = passes.ok ? passes.data.generated_at : d?.generated_at;
  const at = Date.parse(stamp ?? '');
  const model = Number.isFinite(at) ? clockModel(passes.ok ? passes.data.passes : undefined, d, at) : null;
  if (!model) return <p className={`${s.clockMissing} ${s.honest}`}>The pass list came back without a usable time, so the clock cannot be drawn.</p>;
  return (
    <BlindClock
      model={model}
      live={passes.ok ? livePasses(passes.data.passes) : []}
      meta={{
        site: REFERENCE_SITE.name,
        at,
        days: diurnal.ok ? (diurnal.data.days ?? null) : null,
        share: diurnal.ok ? (diurnal.data.share_outside_polar_windows ?? null) : null,
        peak: diurnal.ok ? (diurnal.data.peak_ist ?? null) : null,
        diurnalError: diurnal.ok ? null : diurnal.error.message,
        passesError: passes.ok ? null : passes.error.message,
      }}
    />
  );
}

export async function HeroFigure() {
  const r = await getHeadline();
  if (!r.ok) return <p className={s.honest}>{`The burn scar totals did not load. ${r.error.message}`}</p>;
  const u = r.data.unseen_burns;
  if (!u?.available || u.unseen_share == null) {
    return <p className={s.honest}>{u?.reason ?? 'No burn scar has been checked against the fire alerts yet, so there is no unseen share to show.'}</p>;
  }
  const ci = u.precision_ci95;
  return (
    <>
      <p className={s.figure}>
        <span className={s.figureNum}>{percent(u.unseen_share)}</span>
        <span className={s.figureText}>
          {`of the burnt area Sentinel-2 has mapped${u.first_pass ? ` since ${dateIST(u.first_pass)}` : ''} had no fire alert: ${metricText('area_ha', u.unseen_area_ha)} in ${number(u.unseen_clusters)} burn scars, the ${u.label ?? 'candidate unseen burns'}.`}
        </span>
      </p>
      <p className={s.figureAudit}>
        {u.precision != null
          ? `Audited precision ${percent(u.precision)} (95% CI ${ci ? `${percent(ci[0])}–${percent(ci[1])}` : 'not given'}${u.precision_n != null ? `, n = ${number(u.precision_n)}` : ''}). Some candidates are tilled or flooded fields, so read the share as an upper bound.`
          : 'This share has not been audited yet: read it as an upper bound.'}
      </p>
    </>
  );
}
