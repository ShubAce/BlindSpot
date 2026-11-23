"""The haze correction of s2burn-rules-4h (analytics.burnscar.normalise_haze) on synthetic passes.

A 256 x 256 px block at 40 m (four 128 px correction cells) of harvested cropland with a 40 x 40 px
field that burns between two passes five days apart. The burn is made just strong enough to pass
every test on a clear post pass (dB8A 0.08 against a cut of 0.07, TBI 0.29 against 0.34), so a
smoke haze of the size seen in November 2026 (blue +0.05, NIR +0.02) hides it.
"""

from __future__ import annotations

import dataclasses

import numpy as np

from blindspot.analytics import burnscar
from blindspot.analytics.burnscar import RULE, BlockState

SHAPE = (256, 256)
RES = 40
DAYS = (2500, 2505)
BANDS = burnscar.NORM_BANDS
GROUND = dict(zip(BANDS, (0.08, 0.11, 0.14, 0.20, 0.22, 0.24, 0.25, 0.32, 0.26), strict=True))  # stubble
CHAR = dict(zip(BANDS, (0.07, 0.08, 0.09, 0.13, 0.15, 0.16, 0.17, 0.22, 0.20), strict=True))  # weak burn
HAZE = dict(zip(BANDS, (0.05, 0.04, 0.035, 0.025, 0.022, 0.02, 0.02, 0.005, 0.0), strict=True))
FIELD = (slice(100, 140), slice(60, 100))
RULES_2 = dataclasses.replace(RULE, haze_norm=False)


def _pass(seed: int, burnt: bool, haze: dict[str, float] | None = None, gain: float = 1.0) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(seed)
    out = {}
    for b in BANDS:
        x = np.full(SHAPE, GROUND[b], np.float32)
        if burnt:
            x[FIELD] = CHAR[b]
        x += rng.normal(0, 0.001, SHAPE).astype(np.float32)
        out[b] = x * gain + (haze or {}).get(b, 0.0)
    return out


def _run(post: dict[str, np.ndarray], rule: burnscar.BurnRule = RULE) -> tuple[BlockState, burnscar.PassResult]:
    state = BlockState.empty(SHAPE)
    scl = np.full(SHAPE, 4, np.uint8)
    scope = np.ones(SHAPE, bool)
    registry: list[str] = []
    idx = np.zeros(SHAPE, np.uint8)
    burnscar.apply_pass(state, _pass(1, False), scl, ["pre"], idx, DAYS[0], scope, registry, rule=rule, res_m=RES)
    out = burnscar.apply_pass(state, post, scl, ["post"], idx, DAYS[1], scope, registry, rule=rule, res_m=RES)
    return state, out


def _field_mask() -> np.ndarray:
    m = np.zeros(SHAPE, bool)
    m[FIELD] = True
    return m


def test_haze_hides_the_burn_and_the_correction_recovers_it() -> None:
    _, clear = _run(_pass(2, True))
    assert not clear.hazy and np.array_equal(clear.burn, _field_mask())  # the clear-pass decision
    hazy_post = _pass(2, True, HAZE)
    _, raw = _run(hazy_post, RULES_2)
    assert raw.hazy and raw.burn.sum() < 0.05 * _field_mask().sum()  # rules-2 loses it under haze
    _, fixed = _run(hazy_post)
    assert fixed.hazy and fixed.stats["haze_norm"]["applied"]
    assert np.array_equal(fixed.burn, clear.burn)  # every clear-pass decision recovered, no new one
    offsets = fixed.stats["haze_norm"]["offsets"]
    for b in BANDS:
        assert abs(offsets[b] - HAZE[b]) < 0.003, b
    assert fixed.stats["haze_norm"]["cells_fitted"] == 4


def test_haze_with_a_loss_of_contrast_still_recovers_the_burn() -> None:
    """Haze also dims the ground a little (gain 0.97): an offset-only fit, measured on the bright
    stubble, slightly under-corrects dark char, which keeps the correction conservative."""
    _, clear = _run(_pass(2, True))
    _, fixed = _run(_pass(2, True, HAZE, gain=0.97))
    assert fixed.stats["haze_norm"]["applied"]
    assert (fixed.burn & clear.burn).sum() >= 0.95 * clear.burn.sum()
    assert not (fixed.burn & ~clear.burn).any()


def test_a_clear_pair_is_left_as_it_is() -> None:
    post = _pass(2, True)
    s4, out4 = _run(post)
    s2, out2 = _run(post, RULES_2)
    assert not out4.hazy and "haze_norm" not in out4.stats
    assert np.array_equal(out4.burn, out2.burn)
    for k, v in out2.evidence.items():
        np.testing.assert_array_equal(out4.evidence[k], v)
    # And the fit itself finds no haze between two clear passes (within noise).
    state = BlockState.empty(SHAPE)
    scl = np.full(SHAPE, 4, np.uint8)
    burnscar.apply_pass(state, _pass(1, False), scl, ["pre"], np.zeros(SHAPE, np.uint8), DAYS[0],
                        np.ones(SHAPE, bool), [], res_m=RES)
    grid, stats = burnscar.normalise_haze(state.arrays, post, np.ones(SHAPE, bool), DAYS[1], RES)
    assert grid is not None and max(abs(v) for v in stats["offsets"].values()) < 0.002


def test_a_bad_fit_falls_back_to_the_raw_pass() -> None:
    # No unchanged ground: every pixel's SWIR-1 moved (e.g. a whole block irrigated).
    moved = _pass(2, True, HAZE)
    moved["B11"] = moved["B11"] - 0.05
    _, out = _run(moved)
    _, out2 = _run(moved, RULES_2)
    assert out.stats["haze_norm"] == {"cells": 4, "cells_fitted": 0, "cells_too_hazy": 0, "pif_px": 0,
                                      "applied": False}
    assert np.array_equal(out.burn, out2.burn)
    # An offset no haze produces (blue +0.2): a bad fit, the pass is used raw.
    wild = _pass(2, True, {**HAZE, "B02": 0.2})
    _, out = _run(wild)
    _, out2 = _run(wild, RULES_2)
    assert out.hazy and not out.stats["haze_norm"]["applied"]
    assert np.array_equal(out.burn, out2.burn)


def test_thick_haze_is_left_raw() -> None:
    thick = _pass(2, True, {b: v * 2 for b, v in HAZE.items()})  # blue +0.10, beyond norm_max_haze
    _, out = _run(thick)
    _, out2 = _run(thick, RULES_2)
    st = out.stats["haze_norm"]
    assert st["applied"] and st["cells_too_hazy"] == 4 and st["offsets"]["B02"] == 0.0
    assert np.array_equal(out.burn, out2.burn)


def test_the_memory_keeps_the_corrected_view_and_the_clear_reference() -> None:
    """After a corrected hazy pass the memory holds clear-equivalent values (so the next clear pass
    sees no false darkening), and the haze reference (clear_*) still holds the last clear pass."""
    state, out = _run(_pass(2, True, HAZE))
    a = state.arrays
    unchanged = ~_field_mask()
    assert abs(float(np.median(a["last_b8a"][unchanged])) - GROUND["B8A"]) < 0.003
    assert abs(float(np.median(a["clear_b02"][unchanged])) - GROUND["B02"]) < 0.003  # not the hazy blue
    raw_state, _ = _run(_pass(2, True, HAZE), RULES_2)
    assert float(np.median(raw_state.arrays["last_b8a"][unchanged])) > GROUND["B8A"] + 0.015


def test_offsets_never_grow_with_wavelength_or_go_negative() -> None:
    """Harvest lowers NIR and lifts SWIR; it must not be read as haze in the long bands."""
    odd = {"B02": 0.05, "B03": 0.03, "B04": 0.04, "B06": -0.03, "B07": -0.03, "B08": -0.03, "B8A": -0.03,
           "B11": 0.01, "B12": 0.01}
    state = BlockState.empty(SHAPE)
    scl = np.full(SHAPE, 4, np.uint8)
    burnscar.apply_pass(state, _pass(1, False), scl, ["pre"], np.zeros(SHAPE, np.uint8), DAYS[0],
                        np.ones(SHAPE, bool), [], res_m=RES)
    grid, stats = burnscar.normalise_haze(state.arrays, _pass(2, False, odd), np.ones(SHAPE, bool), DAYS[1], RES)
    off = [stats["offsets"][b] for b in BANDS]
    assert grid is not None and all(x >= 0 for x in off) and all(a >= b for a, b in zip(off, off[1:], strict=False))
    assert off[2] <= off[1] + 1e-6 and stats["offsets"]["B8A"] == 0.0


def test_patchy_haze_is_corrected_cell_by_cell() -> None:
    """Haze over half the block only: the clear half is not darkened by the hazy half's offset."""
    post = _pass(2, False)
    for b in BANDS:
        post[b][:, 128:] += HAZE[b]
    state = BlockState.empty(SHAPE)
    burnscar.apply_pass(state, _pass(1, False), np.full(SHAPE, 4, np.uint8), ["pre"], np.zeros(SHAPE, np.uint8),
                        DAYS[0], np.ones(SHAPE, bool), [], res_m=RES)
    grid, _ = burnscar.normalise_haze(state.arrays, post, np.ones(SHAPE, bool), DAYS[1], RES)
    fixed = burnscar.apply_haze_offsets(post, grid, RES)
    assert abs(float(np.median(fixed["B02"][:, :32])) - GROUND["B02"]) < 0.004  # outer clear columns
    assert abs(float(np.median(fixed["B02"][:, -32:])) - GROUND["B02"]) < 0.004  # outer hazy columns
