'use client';

import type { Layer, PickingInfo } from '@deck.gl/core';
import { MapboxOverlay } from '@deck.gl/mapbox';
import { useEffect } from 'react';
import { useControl, useMap } from 'react-map-gl/maplibre';

import { mapClock, type ClockFrame } from './clock';
import { layerOrder } from './registry';
import { DATA_ANCHOR, isAnimated, type MapLayerInput } from './types';

export type TooltipContent = null | string | { text?: string; html?: string; className?: string; style?: Partial<CSSStyleDeclaration> };

export type DeckOverlayProps = {
  layers: readonly MapLayerInput[];
  getTooltip?: (info: PickingInfo) => TooltipContent;
  onClick?: (info: PickingInfo) => void;
  onHover?: (info: PickingInfo) => void;
};

function collect(input: MapLayerInput, frame: ClockFrame, out: Layer[]): void {
  if (!input) return;
  if (Array.isArray(input)) {
    for (const x of input as readonly MapLayerInput[]) collect(x, frame, out);
    return;
  }
  if (isAnimated(input)) {
    const layer = input.render(frame);
    if (layer) out.push(layer);
    return;
  }
  out.push(input as Layer);
}

/** deck.gl's interleaved mode places each layer before the map layer named by its `beforeId`
 *  prop, which deck's own Layer types do not declare; this is the one place that sets it. */
function anchored(layer: Layer): Layer {
  const props = layer.props as unknown as { beforeId?: string };
  return props.beforeId ? layer : layer.clone({ beforeId: DATA_ANCHOR } as unknown as Partial<Layer['props']>);
}

/** Flatten, evaluate animated layers at `frame`, order by the registry's draw order and anchor
 *  every layer under the place labels. */
export function resolveLayers(inputs: readonly MapLayerInput[], frame: ClockFrame): Layer[] {
  const out: Layer[] = [];
  for (const x of inputs) collect(x, frame, out);
  return out
    .map((layer, i) => ({ layer, i, rank: layerOrder(layer.id) }))
    .sort((a, b) => a.rank - b.rank || a.i - b.i)
    .map((x) => anchored(x.layer));
}

const hasAnimated = (inputs: readonly MapLayerInput[]): boolean =>
  inputs.some((x) => (Array.isArray(x) ? hasAnimated(x as readonly MapLayerInput[]) : isAnimated(x)));

/**
 * deck.gl inside MapLibre (interleaved: one WebGL context, layers in the map's own stack). Static
 * layers are set once per change; when any layer is animated, the shared clock re-renders the layer
 * list every frame without touching React.
 */
export function DeckOverlay({ layers, getTooltip, onClick, onHover }: DeckOverlayProps) {
  // `_reuseDevices`: React's development double-mount (and a map reused by `reuseMaps`) can attach a
  // second overlay to the map's WebGL context while the first one's device is still attached; reuse
  // that device instead of throwing "WebGL context already attached".
  const overlay = useControl<MapboxOverlay>(() => new MapboxOverlay({ interleaved: true, deviceProps: { _reuseDevices: true } }));
  const { current: map } = useMap();

  useEffect(() => {
    const canvas = map?.getCanvas();
    const hover = (info: PickingInfo) => {
      // In interleaved mode the canvas is MapLibre's: set the pointer there.
      if (canvas) canvas.style.cursor = info.picked && info.object ? 'pointer' : '';
      onHover?.(info);
    };
    const apply = (frame: ClockFrame) => overlay.setProps({ layers: resolveLayers(layers, frame), getTooltip, onClick, onHover: hover });
    if (!hasAnimated(layers)) {
      apply(mapClock.frame());
      return;
    }
    return mapClock.subscribe(apply);
  }, [overlay, map, layers, getTooltip, onClick, onHover]);

  return null;
}
