import Link from 'next/link';
import { Suspense, type ReactNode } from 'react';

import { Freshness, Skeleton } from '@/components/ui/states';
import { PATHS } from '@/lib/api';
import { serverGet } from '@/lib/api-server';
import { CADENCE } from '@/lib/cadence';
import { dateIST, dateTimeIST, metric, metricText, MISSING, number, placeName, timeIST } from '@/lib/format';

import { auditPhrase, caseState, platformName, shortUrl, type CaseState } from './audit';
import { EvidenceChips } from './EvidenceChips';
import { Locator } from './Locator';
import type { CaseFile, CaseFire, HeadlineAudit } from './types';
import { WeatherChart } from './WeatherChart';

/**
 * The case file of one Sentinel-2 burn scar (/case/[id], the app's "detail" screen).
 *
 * Its question: did this field burn while no fire satellite counted it, and what proves it? The
 * answer leads (the state, in the API's own label, with the finding sentence), then the evidence the
 * reader can check by eye (the pictures with the scar outlined), the figures, the weather between the
 * two passes, the fire alerts near it, why the rule calls it a burn and how often that rule is right,
 * and the provenance of every input. Only an 'unseen' scar is called a candidate unseen burn; an
 * alerted or unchecked scar says what it is instead.
 */

const STATE_WORDS: Record<CaseState, { pill: string; tone: string }> = {
  unseen: { pill: 'Candidate unseen burn', tone: 'unseen' },
  alerted: { pill: 'Seen by a fire satellite', tone: 'alerted' },
  unchecked: { pill: 'Not yet checked', tone: 'unchecked' },
};

/** A distance as a value and its unit: metres below a kilometre, kilometres with one decimal above. */
const dist = (m: number | null | undefined): { value: string; unit: string } =>
  m == null ? { value: MISSING, unit: '' } : m >= 1000 ? { value: number(m / 1000, { decimals: 1 }), unit: 'km' } : { value: number(m), unit: 'm' };
const kmPrecise = (m: number | null | undefined) => {
  const d = dist(m);
  return d.unit ? `${d.value} ${d.unit}` : d.value;
};

/** A field-sized area keeps two decimals (0.72 ha, not 0.7); larger ones follow the house metric. */
const areaOf = (ha: number | null | undefined): { value: string; unit: string } =>
  ha != null && Number.isFinite(ha) && ha < 10 ? { value: number(ha, { decimals: 2 }), unit: 'ha' } : metric('area_ha', ha);
const areaText = (ha: number | null | undefined) => {
  const a = areaOf(ha);
  return a.unit ? `${a.value} ${a.unit}` : a.value;
};

function passText(itemId: string | null | undefined, at: string | null | undefined) {
  if (!at) return MISSING;
  return `${platformName(itemId) ?? 'Sentinel-2'}, ${dateIST(at, { weekday: true })}, ${timeIST(at, { suffix: true })}`;
}

function Card({ title, aside, children, className = '', id }: { title: string; aside?: ReactNode; children: ReactNode; className?: string; id?: string }) {
  const hid = id ? `${id}-title` : undefined;
  return (
    <section className={`card ${className}`} aria-labelledby={hid}>
      <header className="card__head">
        <h2 id={hid} className="card__title">
          {title}
        </h2>
        {aside ? <div className="card__aside">{aside}</div> : null}
      </header>
      {children}
    </section>
  );
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="figs__row">
      <dt>{label}</dt>
      <dd className="nums">{children}</dd>
    </div>
  );
}

function Kpi({ label, value, unit, sub }: { label: string; value: string; unit?: string; sub?: ReactNode }) {
  return (
    <div className="kpi">
      <span className="kpi__label">{label}</span>
      <span className={`kpi__value nums${value === MISSING ? ' missing' : ''}`}>
        {value}
        {unit && value !== MISSING ? <span className="kpi__unit"> {unit}</span> : null}
      </span>
      {sub ? <span className="kpi__sub nums">{sub}</span> : null}
    </div>
  );
}

function fireVerdict(f: CaseFire): string {
  if (f.explains_scar) return 'Explains the scar';
  if (!f.in_window) return 'Outside the burn window';
  if (f.distance_m > f.match_radius_m) return 'Beyond the match radius';
  return 'Does not explain it';
}

/** The audit precision of candidate unseen burns, read live from /api/stats/headline. */
async function AuditLine() {
  const r = await serverGet<HeadlineAudit>(PATHS.headline);
  if (!r.ok) return <p className="card__note">The audit figures could not be loaded: {r.error.message}</p>;
  const phrase = auditPhrase(r.data.unseen_burns);
  if (!phrase) return <p className="card__note">The API states no audit for candidate unseen burns yet{r.data.unseen_burns?.reason ? `: ${r.data.unseen_burns.reason}` : '.'}</p>;
  return (
    <p className="audit-line">
      How often this rule is right: when people checked a random sample of candidate unseen burns by eye, the {phrase}.
    </p>
  );
}

/** The same audit, one short sentence under the finding of a candidate unseen burn. */
async function AuditFinding() {
  const r = await serverGet<HeadlineAudit>(PATHS.headline);
  const phrase = r.ok ? auditPhrase(r.data.unseen_burns) : null;
  if (!phrase) return <p className="finding-audit">{r.ok ? 'The API states no audit of candidate unseen burns yet.' : 'The audit of candidate unseen burns could not be loaded.'}</p>;
  return <p className="finding-audit">{`Candidate unseen burns as a whole: ${phrase}, checked by eye.`}</p>;
}

export function caseQuestion(c: CaseFile['case']): string {
  const where = placeName(c.village ? `near ${c.village}` : null, c.district ? `${c.district} district` : null);
  return `Tell me about burn scar case ${c.id}${where ? ` ${where}` : ''}: did any fire satellite see this field burn, and where did its smoke go?`;
}

export function CaseScreen({ data }: { data: CaseFile }) {
  const c = data.case;
  const st = caseState(c.unseen);
  const words = STATE_WORDS[st];
  const place = placeName(c.village ? `near ${c.village}` : null, c.district);
  const title = `Case ${c.id}${c.village ? `: ${c.village}` : ''}${c.district ? `, ${c.district}` : ''}`;
  const area = areaOf(c.area_ha);
  const pre = data.imagery?.pre ?? null;
  const post = data.imagery?.post ?? null;
  const fires = data.fires_nearby ?? [];
  const radius = fires[0]?.match_radius_m ?? null;
  const w = data.weather_at_burn;
  const ev = c.evidence ?? null;
  const ask = `/live?ask=${encodeURIComponent(caseQuestion(c))}`;
  const windowText = `${dateTimeIST(c.burn_after, { suffix: true })} and ${dateTimeIST(c.burn_before, { suffix: true })}`;

  const finding =
    st === 'unseen'
      ? `${areaText(c.area_ha)} burnt between ${windowText}, and no fire alert explains it. This is a candidate unseen burn.`
      : st === 'alerted'
        ? `${areaText(c.area_ha)} burnt between ${windowText}. ${c.fire_matches === 1 ? 'A fire alert explains' : `${number(c.fire_matches)} fire alerts explain`} it: a fire satellite counted this one.`
        : `${areaText(c.area_ha)} burnt between ${windowText}. It has not been checked against the fire alerts yet, so it is not called unseen.`;

  const sources = Array.from(new Map((data.sources ?? []).map((s) => [`${s.key}|${s.url}`, s])).values());

  return (
    <>
      <div className="title-row">
        <div className="title-row__text">
          <p className={`state-pill state-pill--${words.tone}`}>
            <span className="state-pill__mark" aria-hidden="true" />
            {words.pill}
            {c.label && c.label.toLowerCase() !== words.pill.toLowerCase() ? <span className="state-pill__label">{c.label}</span> : null}
          </p>
          <h1>{title}</h1>
          <p className="finding">{finding}</p>
          {st === 'unseen' ? (
            <Suspense fallback={<p className="finding-audit" aria-hidden="true">&nbsp;</p>}>
              <AuditFinding />
            </Suspense>
          ) : null}
        </div>
        <div className="title-row__actions" data-print="hide">
          <Link className="button-primary" href={ask} prefetch={false}>
            Ask the investigator about this case
          </Link>
          <Freshness resp={data} cadenceMs={CADENCE.scars} verb="case file built" />
        </div>
      </div>

      <div className="kpis" role="group" aria-label="Key figures">
        <Kpi label="Area burnt" value={area.value} unit={area.unit} sub={c.res_m ? `mapped from ${c.res_m} m pixels` : undefined} />
        <Kpi label="Between the two passes" value={c.gap_days == null ? MISSING : number(c.gap_days, { decimals: 1 })} unit="days" sub={`${dateIST(c.burn_after)} to ${dateIST(c.burn_before)}`} />
        <Kpi label="Fire alerts that explain it" value={st === 'unchecked' ? 'Not yet' : c.fire_matches == null ? MISSING : number(c.fire_matches)} sub={st === 'unchecked' ? 'not matched yet' : radius != null ? `within ${kmPrecise(radius)} and the window` : 'by the matching rule'} />
        <Kpi label="Nearest fire alert" value={c.nearest_fire_m == null ? 'None' : dist(c.nearest_fire_m).value} unit={c.nearest_fire_m == null ? undefined : dist(c.nearest_fire_m).unit} sub={c.nearest_fire_m == null ? 'none found near the scar' : 'from the scar'} />
        <Kpi label="Detection confidence" value={c.confidence == null ? MISSING : number(c.confidence, { decimals: 2 })} sub="score from 0 to 1" />
      </div>

      <div className="case-grid">
        <div className="case-col case-col--main">
        <Card title="The pictures" id="pictures" className="o-1" aside={<span>Copernicus Sentinel-2, rendered by the API</span>}>
          <EvidenceChips bbox={data.imagery?.bbox ?? null} pre={pre} post={post} geometry={c.geometry ?? null} fires={fires} state={st} place={place ? place : 'in this case'} />
        </Card>




        <Card title="Why the rule calls it a burn" id="rule" className="o-5" aside={c.model_version ? <span>{`Rule ${c.model_version}`}</span> : undefined}>
          {ev ? (
            <dl className="evidence-figs">
              <Row label="Pixels in the scar">{ev.n_px == null ? MISSING : number(ev.n_px)}</Row>
              <Row label="Near-infrared darkened (dB8A)">{ev.d_b8a == null ? MISSING : number(ev.d_b8a, { decimals: 3 })}</Row>
              <Row label="Short-wave infrared 1 darkened (dB11)">{ev.d_b11 == null ? MISSING : number(ev.d_b11, { decimals: 3 })}</Row>
              <Row label="Short-wave infrared 2 darkened (dB12)">{ev.d_b12 == null ? MISSING : number(ev.d_b12, { decimals: 3 })}</Row>
              <Row label="Burned-area index after (BAIS2)">{ev.bais2_post == null ? MISSING : number(ev.bais2_post, { decimals: 2 })}</Row>
              <Row label="Brightness after">{ev.tbi_post == null ? MISSING : number(ev.tbi_post, { decimals: 2 })}</Row>
              <Row label="Greenness before, after (NDVI)">{ev.ndvi_pre == null || ev.ndvi_post == null ? MISSING : `${number(ev.ndvi_pre, { decimals: 2 })}, ${number(ev.ndvi_post, { decimals: 2 })}`}</Row>
              <Row label="Burn ratio change (dNBR), NBR after">{`${c.dnbr == null ? MISSING : number(c.dnbr, { decimals: 2 })}, ${c.nbr_post == null ? MISSING : number(c.nbr_post, { decimals: 2 })}`}</Row>
            </dl>
          ) : null}
          {data.method?.summary ? (
            <div className="rule-text">
              <h3>The detection rule</h3>
              <p>{data.method.summary}</p>
            </div>
          ) : null}
          {data.method?.unseen_rule ? (
            <div className="rule-text">
              <h3>When a burn counts as unseen</h3>
              <p>{data.method.unseen_rule}</p>
            </div>
          ) : null}
          <Suspense fallback={<Skeleton height={20} width="80%" />}>
            <AuditLine />
          </Suspense>
          {data.method?.audit?.known_lookalikes ? <p className="card__note">Known look-alikes: {data.method.audit.known_lookalikes}.</p> : null}
          {c.hazy ? <p className="card__note">The pass before the burn was hazy, which can exaggerate the darkening.</p> : null}
        </Card>

        </div>
        <div className="case-col case-col--side">
        <Card title="Where and when" id="where" className="o-2">
          <dl className="figs">
            <Row label="Nearest village">{c.village ? `${c.village}${c.village_km != null ? `, ${number(c.village_km, { decimals: 1 })} km` : ''}` : <span className="missing">{MISSING}</span>}</Row>
            <Row label="Subdistrict">{c.subdistrict ?? <span className="missing">{MISSING}</span>}</Row>
            <Row label="District">{placeName(c.district, c.state) || <span className="missing">{MISSING}</span>}</Row>
            <Row label="Centre">{c.centroid ? `${number(c.centroid[1], { decimals: 4 })}° N, ${number(c.centroid[0], { decimals: 4 })}° E` : <span className="missing">{MISSING}</span>}</Row>
            <Row label="Clear pass before">{passText(pre?.item_id ?? c.scene_pre, c.burn_after)}</Row>
            <Row label="Pass that shows it">{passText(post?.item_id ?? c.scene_post, c.burn_before)}</Row>
            <Row label="Sentinel-2 tile">{c.mgrs_tile ?? <span className="missing">{MISSING}</span>}</Row>
            <Row label="Checked against alerts">{c.matched_at ? dateTimeIST(c.matched_at, { suffix: true }) : 'Not yet'}</Row>
          </dl>
          {c.centroid ? (
            <Suspense fallback={<Skeleton height={180} />}>
              <Locator lon={c.centroid[0]} lat={c.centroid[1]} state={c.state} />
            </Suspense>
          ) : null}
        </Card>
        <Card title="Fire alerts near the scar" id="alerts" className="o-3" aside={<span>NASA FIRMS</span>}>
          {fires.length ? (
            <ul className="alert-list">
              {fires.map((f) => (
                <li key={f.id} className="alert-list__item">
                  <div className="alert-list__line">
                    <span className="alert-list__sat">
                      {f.satellite} {f.instrument}
                    </span>
                    <span className="nums">{kmPrecise(f.distance_m)}</span>
                  </div>
                  <div className="alert-list__sub nums">
                    {dateIST(f.acq_utc, { weekday: true })}, {timeIST(f.acq_utc, { suffix: true })}
                    {f.frp_mw != null ? ` · ${metricText('frp_mw', f.frp_mw)}` : ''}
                    {f.confidence ? ` · ${f.confidence} confidence` : ''}
                  </div>
                  <div className={`alert-list__verdict${f.explains_scar ? ' is-match' : ''}`}>{fireVerdict(f)}</div>
                </li>
              ))}
            </ul>
          ) : (
            <p className="card__note">The API found no FIRMS fire detection near this scar.</p>
          )}
          {radius != null ? <p className="card__note">An alert explains a scar when it falls within {kmPrecise(radius)} of it, inside the burn window.</p> : null}
        </Card>
        <Card
          title="Weather between the passes"
          id="weather"
          className="o-4"
          aside={w?.available && w.model ? <span>{`Model ${w.model}${w.hours && w.covered_hours != null ? `, ${number(w.covered_hours)} of ${number(w.hours)} hours` : ''}`}</span> : undefined}
        >
          {w?.available && w.series?.length ? (
            <>
              <div className="mini-kpis">
                <Kpi label="Afternoon wind, mean" value={metric('wind_kmh', w.afternoon_wind_10m_kmh_mean).value} unit="km/h" />
                <Kpi label="Afternoon mixing height, mean" value={metric('blh_m', w.afternoon_blh_m_mean).value} unit="m" />
                <Kpi label="Rain in the window" value={metric('rain_mm', w.rain_mm_total).value} unit="mm" />
              </div>
              <WeatherChart series={w.series} from={w.window?.from ?? c.burn_after} to={w.window?.to ?? c.burn_before} />
              <p className="card__note">Arrows point where the wind blew. A low mixing height keeps smoke near the ground.</p>
            </>
          ) : (
            <p className="card__note">No weather for this window: {w?.reason ?? 'the API returned none.'}</p>
          )}
        </Card>

        </div>
        <Card title="Provenance" id="provenance" className="o-6 span-12" aside={<span>{`${sources.length} inputs`}</span>}>
          <ol className="prov prov--cols">
            {sources.map((s, i) => (
              <li key={`${s.key}-${i}`} className="prov__item">
                <span className="prov__label">{s.label === s.key ? (shortUrl(s.url, 40)?.split('/')[0] ?? s.key) : s.label}</span>
                <span className="prov__meta nums">
                  {s.fetched_at ? `fetched ${dateTimeIST(s.fetched_at, { suffix: true })}` : 'fetch time not recorded'}
                  {s.fetch_id != null ? ` · fetch ${s.fetch_id}` : ''}
                </span>
                {s.url ? (
                  <a className="prov__url" href={s.url} rel="noreferrer noopener" target="_blank" title={s.url}>
                    {shortUrl(s.url)}
                  </a>
                ) : null}
              </li>
            ))}
            {pre?.item_id || post?.item_id ? (
              <li className="prov__item">
                <span className="prov__label">Sentinel-2 scenes</span>
                <span className="prov__meta">{[pre?.item_id, post?.item_id].filter(Boolean).join(' and ')}</span>
              </li>
            ) : null}
          </ol>
        </Card>
      </div>
    </>
  );
}
