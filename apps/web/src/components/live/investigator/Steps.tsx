'use client';

import { number } from '@/lib/format';

import { callCode, describeCall, describeMap, modelName, thoughtBody, thoughtTitle, tidySummary } from './describe';
import type { Investigation, Step } from './model';

/**
 * The reasoning, one row per step as the events arrive: what it checked in plain words, the exact
 * call as code, what came back and how long it took (live runs only), its brief thoughts, the map
 * moves it made (with "Show again"), and any switch to another model.
 */

function seconds(ms: number | null | undefined): string | null {
  if (ms == null || !Number.isFinite(ms)) return null;
  return ms < 1000 ? `${number(ms)} ms` : `${number(ms / 1000, { decimals: 1 })} s`;
}

function Mark({ state }: { state: 'run' | 'ok' | 'fail' | 'think' | 'map' }) {
  return (
    <span className="iv-mark" data-state={state} aria-hidden="true">
      {state === 'map' ? (
        <svg width="14" height="14" viewBox="0 0 14 14">
          <path d="M7 12.5s4-3.6 4-6.6a4 4 0 10-8 0c0 3 4 6.6 4 6.6z" fill="none" stroke="currentColor" strokeWidth="1.3" />
          <circle cx="7" cy="5.9" r="1.4" fill="currentColor" />
        </svg>
      ) : state === 'fail' ? (
        <svg width="14" height="14" viewBox="0 0 14 14">
          <path d="M4 4l6 6M10 4l-6 6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
        </svg>
      ) : state === 'ok' ? (
        <svg width="14" height="14" viewBox="0 0 14 14">
          <circle cx="7" cy="7" r="3.5" fill="currentColor" />
        </svg>
      ) : state === 'think' ? (
        <svg width="14" height="14" viewBox="0 0 14 14">
          <circle cx="3.5" cy="7" r="1.1" fill="currentColor" />
          <circle cx="7" cy="7" r="1.1" fill="currentColor" />
          <circle cx="10.5" cy="7" r="1.1" fill="currentColor" />
        </svg>
      ) : (
        <svg width="14" height="14" viewBox="0 0 14 14" className="iv-spin">
          <circle cx="7" cy="7" r="4.5" fill="none" stroke="currentColor" strokeOpacity="0.3" strokeWidth="1.5" />
          <path d="M7 2.5a4.5 4.5 0 014.5 4.5" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
        </svg>
      )}
    </span>
  );
}

function StepRow({ step, timed, running, onReapply }: { step: Step; timed: boolean; running: boolean; onReapply: (action: string, payload: Record<string, unknown>) => boolean }) {
  if (step.kind === 'thought') {
    const titles = step.texts.map(thoughtTitle).filter(Boolean);
    return (
      <li className="iv-step" data-kind="thought">
        <Mark state="think" />
        <details className="iv-thought">
          <summary>
            <span className="iv-thoughtLabel">Thought</span> {titles.join(' · ')}
          </summary>
          {step.texts.map((t, i) => (
            <p key={i}>{thoughtBody(t) || t}</p>
          ))}
        </details>
      </li>
    );
  }
  if (step.kind === 'model') {
    return (
      <li className="iv-step" data-kind="model">
        <Mark state="ok" />
        <p className="iv-stepText">
          {step.from ? `${modelName(step.from)} was unavailable, so the run switched to ${modelName(step.model)}` : `${modelName(step.model)} took over`}
        </p>
      </li>
    );
  }
  if (step.kind === 'map') {
    return (
      <li className="iv-step" data-kind="map">
        <Mark state="map" />
        <div className="iv-stepMain">
          <p className="iv-stepText">
            {describeMap(step.action, step.payload)}
            {step.payload.auto === true ? <span className="iv-faint"> (the layer of its main evidence)</span> : null}
          </p>
          <MapAgain applied={step.applied} onClick={() => onReapply(step.action, step.payload)} />
        </div>
      </li>
    );
  }
  // a tool call; show_on_map reads as a map move
  const isMap = step.name === 'show_on_map';
  const state = step.result ? (step.result.ok ? (isMap ? 'map' : 'ok') : 'fail') : running ? 'run' : 'fail';
  const time = timed ? seconds(step.result?.ms) : null;
  const sources = step.result?.sources.length ?? 0;
  return (
    <li className="iv-step" data-kind={isMap ? 'map' : 'tool'} data-state={state}>
      <Mark state={state} />
      <div className="iv-stepMain">
        <div className="iv-stepTop">
          <p className="iv-stepText">{isMap && step.map ? describeMap(step.map.action, step.map.payload) : describeCall(step.name, step.args)}</p>
          {time ? <span className="iv-time">{time}</span> : null}
        </div>
        <code className="iv-code" translate="no">{callCode(step.name, step.args)}</code>
        {step.result && !isMap ? (
          <p className="iv-result" data-ok={step.result.ok ? undefined : 'false'}>
            {tidySummary(step.result.summary)}
            {sources ? <span className="iv-faint"> · {sources === 1 ? '1 source' : `${number(sources)} sources`}</span> : null}
          </p>
        ) : null}
        {step.result && isMap && !step.result.ok ? <p className="iv-result" data-ok="false">{tidySummary(step.result.summary)}</p> : null}
        {isMap && step.map ? <MapAgain applied={step.map.applied} onClick={() => onReapply(step.map!.action, step.map!.payload)} /> : null}
        {!step.result && !running ? <p className="iv-result iv-faint">No result: the run ended first.</p> : null}
      </div>
    </li>
  );
}

function MapAgain({ applied, onClick }: { applied: boolean; onClick: () => void }) {
  if (!applied) return <p className="iv-result iv-faint">This screen has no layer for it.</p>;
  return (
    <button type="button" className="iv-again lv-linkish" onClick={onClick}>
      Show again
    </button>
  );
}

export function Steps({ inv, onReapply }: { inv: Investigation; onReapply: (action: string, payload: Record<string, unknown>) => boolean }) {
  const running = inv.status === 'running';
  const waiting = running && !inv.steps.length;
  return (
    <ol className="iv-steps" role="log" aria-label="Steps of the investigation">
      {inv.steps.map((st) => (
        <StepRow key={st.key} step={st} timed={inv.mode === 'live'} running={running} onReapply={onReapply} />
      ))}
      {waiting ? (
        <li className="iv-step" data-kind="wait">
          <Mark state="run" />
          <p className="iv-stepText iv-faint">{inv.mode === 'stored' ? 'Loading the stored run…' : 'Asking Gemini…'}</p>
        </li>
      ) : null}
    </ol>
  );
}
