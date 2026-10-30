"""Crop state between Sentinel-2 passes: when a paddy field was harvested, and whether its straw is
still lying there unburnt (the fields that can burn next).

Why these tests (docs/research/sentinel2-burnscars.md section 4, step 4):
- Harvest: a combine removes the green canopy, so NDVI falls sharply (by more than 0.25, to below
  0.35), and the dry straw left behind is bright in short-wave infrared, so SWIR-1 (B11) rises. The
  SWIR rise is what tells a harvest apart from a cloud shadow or a flooded field, where NDVI also
  falls but SWIR does not rise.
- Still a burn risk: harvested, not burnt since (no burn detected on a later pass), not tilled
  since (darkened without a char signature: the straw was worked into the soil), not green again
  (NDVI still below 0.35, so no wheat has emerged), and seen clear recently enough to trust.

Fields harvested before the first clear pass of the season have no observed transition, so they are
not listed; we only report harvests we actually saw happen.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class HarvestRule:
    ndvi_drop: float = 0.25  # NDVI must fall by more than this between two clear passes
    ndvi_after_max: float = 0.35  # ...to below this (bare stubble / straw)
    max_gap_days: int = 12  # harvest is not fleeting, so a longer gap between passes is fine
    recent_days: int = 12  # a candidate must have a clear view this close to the newest pass


HARVEST_RULE = HarvestRule()


def harvested_between(
    pre_ndvi: np.ndarray,
    post_ndvi: np.ndarray,
    pre_b11: np.ndarray,
    post_b11: np.ndarray,
    comparable: np.ndarray,
    rule: HarvestRule = HARVEST_RULE,
) -> np.ndarray:
    """True where a standing crop on the earlier pass is harvested on the later one.

    `comparable` marks pixels clear on both passes and close enough in time to compare."""
    with np.errstate(invalid="ignore"):
        return (
            comparable
            & (pre_ndvi - post_ndvi > rule.ndvi_drop)
            & (post_ndvi < rule.ndvi_after_max)
            & (post_b11 > pre_b11)
        )


def unburnt_residue(
    harv_before: np.ndarray,
    burn_day: np.ndarray,
    tilled_day: np.ndarray,
    last_day: np.ndarray,
    last_ndvi: np.ndarray,
    scope: np.ndarray,
    newest_day: int,
    rule: HarvestRule = HARVEST_RULE,
) -> np.ndarray:
    """Pixels harvested this season whose straw is still unburnt on their latest clear pass.

    All *_day arrays are day numbers (0 = never); a burn or tillage dated before the harvest belongs
    to an earlier cycle and does not count against it."""
    harv = harv_before.astype(np.int32)
    last = last_day.astype(np.int32)
    with np.errstate(invalid="ignore"):
        bare = last_ndvi.astype(np.float32) < rule.ndvi_after_max
    return (
        scope
        & (harv > 0)
        & (burn_day.astype(np.int32) < harv)
        & (tilled_day.astype(np.int32) < harv)
        & bare
        & (last > 0)
        & (newest_day - last <= rule.recent_days)
    )


__all__ = ["HARVEST_RULE", "HarvestRule", "harvested_between", "unburnt_residue"]
