"""Burn scars from consecutive clear Sentinel-2 passes: indices, the rule, per-pixel memory, clusters.

Why compare two passes instead of thresholding one image: in Punjab, harvesting a field changes NBR
more than burning it (median dNBR 0.40 for harvested fields far from any fire vs 0.10 around VIIRS
fires, docs/research/sentinel2-burnscars.md section 3.3), and a single-date char rule flags 21% of
all cropland. What separates a burn is the direction of change: cutting rice exposes dry straw that
is *brighter* in short-wave infrared, while burning leaves char that is *darker* in near- and
short-wave infrared at once. So a burn here is a cropland pixel that

  1. darkened since its previous clear pass:  dB8A > 0.07, dB11 > 0.07, dB12 > 0.03
     (d = previous minus now, surface reflectance; Youden cut-offs from FIRMS-labelled fields,
     doc section 3.4), with the two passes at most 6 days apart (the char signal fades within days
     and longer gaps let tillage and harvest contaminate the change, doc sections 3.4 and 4);
  2. now looks charred: BAIS2 >= 0.78 and tasselled-cap brightness <= 0.34 (CEEW 2025,
     "Monitoring Crop Residue Burning Better", Tables 2 and 7);
  3. is cropland (ESA WorldCover 2021 class 40) and clear on both passes (SCL 2, 4, 5, 6 or 7);

then a 2x2 opening removes speckle and 4-connected clusters of at least 13 pixels at 20 m (0.52 ha)
are kept. On FIRMS-labelled fields this rule had the best trade-off of the rules tested (TPR 0.66 at
an upper-bound FPR of 0.35, doc section 3.5); tillage can still mimic it, so results are candidates.

Each pixel remembers its last clear observation (BlockState), so a pixel hidden by cloud on one
pass is compared with the pass before that, and each job run only reads the new passes.
"""

from __future__ import annotations

import io
import json
import math
import os
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
from shapely import STRtree
from shapely.geometry import Polygon, shape
from shapely.ops import unary_union

from blindspot.analytics.harvest import HARVEST_RULE, HarvestRule, harvested_between, unburnt_residue

if TYPE_CHECKING:
    from rasterio.transform import Affine


def _ndimage() -> Any:
    """scipy.ndimage, imported on first use. Only the jobs run this module's image steps; the web
    API imports the module for its constants and summaries and must start without scipy
    (serverless deployments do not ship it)."""
    from scipy import ndimage

    return ndimage


VALID_SCL = (2, 4, 5, 6, 7)  # dark area, vegetation, not vegetated, water, unclassified (doc 3.1)


# 4-connectivity cross, the same array as scipy.ndimage.generate_binary_structure(2, 1).
FOUR_CONNECTED = np.array([[False, True, False], [True, True, True], [False, True, False]])


@dataclass(frozen=True)
class BurnRule:
    """Thresholds of the burn rule. Changing any value means a new model_version."""

    d_b8a: float = 0.07  # NIR (narrow) darkening, reflectance units
    d_b11: float = 0.07  # SWIR-1 darkening
    d_b12: float = 0.03  # SWIR-2 darkening
    bais2_min: float = 0.78  # char state, CEEW 2025
    tbi_max: float = 0.34  # char state, CEEW 2025
    max_gap_days: int = 6  # previous clear pass at most this many days earlier
    rearm_days: int = 20  # a pixel that burnt is not counted again within this many days
    min_area_ha: float = 0.52  # 13 px at 20 m (doc section 4, step 5)
    b12_gate: tuple[float, float] = (0.02, 0.6)  # plausible median B12 of a real pass (SYNTHESIS 5.6)
    haze_b02_rise: float = 0.03  # blue rise on unchanged ground that marks a smoke-hazy pass (doc 3.8)
    haze_stable_b12: float = 0.02  # "unchanged ground": SWIR-2 within this of the last clear pass
    haze_min_px: int = 500  # need this many unchanged pixels in a block to judge haze
    # Cluster-level look-alike filter, added after the visual audit of 2026-10-31: char keeps SWIR-2
    # at or above NIR, so a burnt field's mean post-burn NBR is negative. Every audited cluster with
    # mean post NBR >= 0 was a flood-irrigated field (black in SWIR) or a cloud shadow SCL missed.
    nbr_post_max: float = 0.0
    # The per-pixel rule (above) is unchanged since s2burn-rules-1, so the block memory on disk is
    # keyed on pixel_version and a cluster-level change does not force reprocessing the season.
    pixel_version: str = "s2burn-rules-1"
    version: str = "s2burn-rules-2"

    def keeps(self, nbr_post_mean: float | None) -> bool:
        """Cluster-level filter: drop water / cloud-shadow look-alikes (mean post NBR >= 0)."""
        return nbr_post_mean is None or nbr_post_mean < self.nbr_post_max

    def min_px(self, res_m: int) -> int:
        """Minimum cluster size in pixels at a resolution (13 at 20 m, 4 at 40 m)."""
        return max(4, math.ceil(self.min_area_ha * 10_000 / (res_m * res_m)))

    def as_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


RULE = BurnRule()

# Visual audit of the stored clusters (validation record, not input data). On 2026-10-31 the sentinel
# module rendered pre/post true-colour and SWIR (B12-B8A-B4) chips straight from the Sentinel-2 COGs
# for clusters drawn at random (seeded md5 order) from burn_cluster and judged each by eye:
# burnt = a harvested field that turned dark grey/black in true colour and dark maroon in SWIR.
# "Uncertain" clusters (e.g. a pre pass too hazy to judge) are counted as NOT burnt. Look-alike
# strata were inspected separately and are not in the precision. Chips and method:
# docs/build-logs/sentinel-scars.md. Re-audit whenever RULE changes.
AUDIT: dict[str, Any] = {
    "date": "2026-10-31",
    "model_version": "s2burn-rules-2",
    "method": "random clusters, pre/post true colour + SWIR chips judged by eye; no field visits",
    "candidate_unseen": {
        "audited": 23, "burnt": 20,
        "by_tile": {
            "43REP": {"audited": 11, "burnt": 11},
            "43RFP": {"audited": 4, "burnt": 2, "false": ["16446 cloud/smoke shadow", "7673 flooded field"]},
            "other hotspot tiles": {"audited": 8, "burnt": 7, "uncertain": ["48980 pre pass too hazy"]},
        },
    },
    "firms_confirmed": {"audited": 12, "burnt": 10,
                        "uncertain": ["24423 pre pass is thick haze, not judgeable",
                                      "17724 subtle darkening, tillage or burn"]},
    "lookalikes_removed": ("before the filter 1 of 10 random clusters was a flood-irrigated field; all 3 "
                           "audited clusters with mean post NBR >= 0 were water or cloud shadow; such "
                           "clusters are dropped by BurnRule.keeps"),
    "known_lookalikes": ("cloud and smoke-plume shadows in partly cloudy passes and flooded fields with "
                         "mean post NBR just below 0 still pass, often with high confidence; tillage of "
                         "wet soil can mimic char; a hazy pre pass can exaggerate the darkening"),
    "recall_vs_firms": {"viirs_detections": 17, "scar_same_window": 6, "scar_any_window": 15,
                        "note": "VIIRS nominal/high detections in the processed 43REP square, between "
                                "two clear (<=30% cloud) passes <=6 days apart; small sample"},
}


def wilson_interval(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """95 % Wilson score interval for a proportion k/n (well behaved at k = n, unlike k/n +- 2se)."""
    if n <= 0:
        return None
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return round(max(0.0, centre - half), 3), round(min(1.0, centre + half), 3)


def audit_summary() -> dict[str, Any]:
    """The audit with precision and recall as proportions and 95 % Wilson intervals."""
    out = {k: v for k, v in AUDIT.items() if k not in ("candidate_unseen", "firms_confirmed", "recall_vs_firms")}
    for key in ("candidate_unseen", "firms_confirmed"):
        s = AUDIT[key]
        out[key] = {**s, "precision": round(s["burnt"] / s["audited"], 3),
                    "precision_ci95": wilson_interval(s["burnt"], s["audited"])}
    r = AUDIT["recall_vs_firms"]
    out["recall_vs_firms"] = {**r, "recall_same_window": round(r["scar_same_window"] / r["viirs_detections"], 3),
                              "recall_same_window_ci95": wilson_interval(r["scar_same_window"], r["viirs_detections"])}
    out["current"] = AUDIT["model_version"] == RULE.version
    return out


# --- indices ------------------------------------------------------------------------------------


def _ratio(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(b != 0, a / b, np.nan).astype(np.float32)


def ndvi(r: dict[str, np.ndarray]) -> np.ndarray:
    return _ratio(r["B08"] - r["B04"], r["B08"] + r["B04"])


def nbr(r: dict[str, np.ndarray]) -> np.ndarray:
    """Normalised burn ratio with the narrow NIR band (B8A), as in the research doc."""
    return _ratio(r["B8A"] - r["B12"], r["B8A"] + r["B12"])


def bais2(r: dict[str, np.ndarray]) -> np.ndarray:
    """Burned Area Index for Sentinel-2 (Filipponi 2018, doi:10.3390/ECRS-2-05177)."""
    with np.errstate(divide="ignore", invalid="ignore"):
        red_edge = np.sqrt(np.clip(r["B06"] * r["B07"] * r["B8A"] / r["B04"], 0, None))
        swir = (r["B12"] - r["B8A"]) / np.sqrt(np.clip(r["B12"] + r["B8A"], 1e-6, None)) + 1
    return ((1 - red_edge) * swir).astype(np.float32)


def tbi(r: dict[str, np.ndarray]) -> np.ndarray:
    """Tasselled-cap brightness as used by Deshpande et al. 2022 and CEEW 2025."""
    return (
        0.3510 * r["B02"] + 0.3813 * r["B03"] + 0.3437 * r["B04"]
        + 0.7196 * r["B08"] + 0.2396 * r["B11"] + 0.1949 * r["B12"]
    ).astype(np.float32)


def valid_scl(scl: np.ndarray) -> np.ndarray:
    return np.isin(scl, VALID_SCL)


def confidence(
    tbi_post: float, d_b12: float, n_px: int, min_px: int, *, hazy: bool, gap_days: float,
    rule: BurnRule = RULE,
) -> float:
    """A transparent 0-1 score of how burn-like a cluster is. Three equal parts, each 0 at the
    rule's own cut-off and 1 well beyond it:
      char  - how far the cluster's brightness is below the char cut-off (0.08 below = 1); char is
              darker than tilled soil, which is the main look-alike;
      dark  - how much more SWIR-2 darkened than required (0.06 more = 1);
      size  - 1 at four times the minimum size (single-pixel noise cannot reach it).
    A smoke-hazy pass multiplies it by 0.7 and a gap over 5 days by 0.85 (doc 3.4 and 3.8)."""
    char = min(max((rule.tbi_max - tbi_post) / 0.08, 0.0), 1.0)
    dark = min(max((d_b12 - rule.d_b12) / 0.06, 0.0), 1.0)
    size = min(max(math.log2(max(n_px, 1) / min_px) / 2, 0.0), 1.0)
    score = (char + dark + size) / 3
    if hazy:
        score *= 0.7
    if gap_days > 5:
        score *= 0.85
    return round(score, 3)


# --- per-pixel memory between passes --------------------------------------------------------------

STATE_FIELDS: dict[str, Any] = {
    "last_day": np.uint16,  # day number of the last clear observation (0 = never)
    "last_item": np.uint16,  # which item it came from (index into the tile item registry)
    "last_b8a": np.float16,
    "last_b11": np.float16,
    "last_b12": np.float16,
    "last_ndvi": np.float16,  # from the last clear pass that was not smoke-hazy
    "ndvi_day": np.uint16,  # ...and its day: haze lowers NDVI, so hazy passes never set it
    "last_nbr": np.float16,
    "clear_b02": np.float16,  # blue and SWIR-2 of the last pass that was not hazy: the haze
    "clear_b12": np.float16,  # reference (blue rises under smoke, SWIR-2 barely moves)
    "harv_after": np.uint16,  # last clear day with standing crop before the harvest
    "harv_before": np.uint16,  # first clear day showing it harvested (0 = not seen harvested)
    "burn_day": np.uint16,  # pass on which a burn was detected (0 = never)
    "tilled_day": np.uint16,  # darkened without char after harvest: residue worked in
}


@dataclass
class BlockState:
    """What one block of one tile remembers between job runs (a few MB, stored as .npz).

    `last_item` values index the tile's item registry (a list of item ids shared by all blocks of
    the tile), so clusters stitched across blocks agree on which scene each pixel came from."""

    shape: tuple[int, int]
    arrays: dict[str, np.ndarray]
    applied_days: list[int] = field(default_factory=list)  # passes already folded in

    @classmethod
    def empty(cls, shape: tuple[int, int]) -> BlockState:
        arrays = {}
        for name, dtype in STATE_FIELDS.items():
            fill = np.nan if np.dtype(dtype).kind == "f" else 0
            arrays[name] = np.full(shape, fill, dtype=dtype)
        return cls(shape=shape, arrays=arrays)

    def save(self, path: Path) -> None:
        """Write atomically (temp file, then replace) so a crash never leaves half a state."""
        meta = {"applied_days": self.applied_days}
        buffer = io.BytesIO()
        np.savez_compressed(buffer, meta=np.frombuffer(json.dumps(meta).encode(), dtype=np.uint8),
                            **self.arrays)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(buffer.getvalue())
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: Path, shape: tuple[int, int]) -> BlockState:
        if not path.exists():
            return cls.empty(shape)
        with np.load(path) as data:
            if any(name not in data for name in STATE_FIELDS):
                raise ValueError(f"{path}: state written by an older layout; rebuild this tile")
            meta = json.loads(bytes(data["meta"]).decode())
            arrays = {name: data[name] for name in STATE_FIELDS}
        if next(iter(arrays.values())).shape != shape:
            raise ValueError(f"{path}: state shape differs from the block; rebuild this tile")
        return cls(shape, arrays, meta["applied_days"])


@dataclass
class PassResult:
    """What one pass did to one block: the gate verdict, haze flag and the new burn pixels."""

    used: bool  # False when the pass had no usable pixels here or failed the gate
    reason: str | None
    stats: dict[str, Any]
    burn: np.ndarray | None = None  # bool, after the 2x2 opening
    pre_day: np.ndarray | None = None  # uint16 day of each burn pixel's previous clear pass
    pre_item: np.ndarray | None = None  # uint16 index into the tile's item registry
    post_item: np.ndarray | None = None
    evidence: dict[str, np.ndarray] = field(default_factory=dict)  # float32 per-pixel features
    hazy: bool = False


def _median(values: np.ndarray) -> float | None:
    values = values[np.isfinite(values)]
    return float(np.median(values)) if values.size else None


def _open_2x2(mask: np.ndarray) -> np.ndarray:
    """2x2 opening with edge padding, so pixels on a block edge are not eroded by the edge itself."""
    padded = np.pad(mask, 1, mode="edge")
    opened = _ndimage().binary_opening(padded, structure=np.ones((2, 2), bool))
    return opened[1:-1, 1:-1]


def haze_check(
    a: dict[str, np.ndarray], refl: dict[str, np.ndarray], valid: np.ndarray, rule: BurnRule = RULE
) -> tuple[bool, float | None]:
    """Is this pass smoke-hazy over this block? Returns (hazy, median blue rise).

    Smoke brightens blue strongly and SWIR-2 hardly at all (doc 3.8: B02 0.063 -> 0.117 while B12
    0.189 -> 0.197). Harvest brightens both, so the blue rise is measured only on ground whose SWIR-2
    is unchanged since the last clear pass; a whole-block median would mistake the season's harvest
    for haze. Too few such pixels (e.g. first pass) means "not judged", treated as clear."""
    ref_b02 = a["clear_b02"].astype(np.float32)
    ref_b12 = a["clear_b12"].astype(np.float32)
    with np.errstate(invalid="ignore"):
        stable = valid & np.isfinite(ref_b02) & (np.abs(refl["B12"] - ref_b12) < rule.haze_stable_b12)
    if int(stable.sum()) < rule.haze_min_px:
        return False, None
    rise = _median((refl["B02"] - ref_b02)[stable])
    return bool(rise is not None and rise > rule.haze_b02_rise), (round(rise, 4) if rise is not None else None)


def registry_index(registry: list[str], item_id: str) -> int:
    """Position of an item id in a tile's registry, appending it the first time it is seen."""
    if item_id not in registry:
        registry.append(item_id)
    return registry.index(item_id)


def apply_pass(
    state: BlockState,
    refl: dict[str, np.ndarray],
    scl: np.ndarray,
    item_ids: list[str],
    item_index: np.ndarray,
    day: int,
    scope: np.ndarray,
    registry: list[str],
    rule: BurnRule = RULE,
    harvest: HarvestRule = HARVEST_RULE,
    res_m: int = 20,
) -> PassResult:
    """Fold one clear-sky pass into a block's memory and return the burns it reveals.

    `refl` holds reflectance (NaN = no data) for the 9 bands, `item_index` says which of `item_ids`
    supplied each pixel, `scope` marks the pixels this block may judge (owned by this tile, inside
    the area being processed, and cropland), and `registry` is the tile's item-id list. The state
    is updated in place; a pass that fails the plausibility gate leaves it untouched.

    The 2x2 opening that removes speckle is applied at 20 m only: at 40 m it would demand 80 m of
    solid burn and erase most ~1 ha fields, and the 4-pixel minimum cluster already drops speckle."""
    valid = scope & valid_scl(scl) & np.isfinite(refl["B12"]) & np.isfinite(refl["B8A"])
    valid &= np.isfinite(refl["B11"]) & np.isfinite(refl["B04"]) & np.isfinite(refl["B08"])
    n_valid = int(valid.sum())
    stats: dict[str, Any] = {"valid_px": n_valid, "scope_px": int(scope.sum())}
    if n_valid == 0:
        return PassResult(False, "no clear cropland pixels", stats)
    b12_med = _median(refl["B12"][valid])
    b02_med = _median(refl["B02"][valid])
    stats.update(b12_median=b12_med, b02_median=b02_med)
    lo, hi = rule.b12_gate
    if b12_med is None or not lo < b12_med < hi:
        # A corrupt or mis-scaled pass (Planetary Computer's 2026-10-28 copy gave a median of
        # -0.087). Do not let it touch the memory.
        return PassResult(False, f"implausible cropland B12 median {b12_med}", stats)
    a = state.arrays
    hazy, haze_rise = haze_check(a, refl, valid, rule)
    stats.update(hazy=hazy, haze_b02_rise=haze_rise)

    post_ndvi = ndvi(refl)
    post_nbr = nbr(refl)
    post_bais2 = bais2(refl)
    post_tbi = tbi(refl)
    last_day = a["last_day"].astype(np.int32)
    gap = np.where(last_day > 0, day - last_day, 10_000)
    has_prev = valid & (last_day > 0)
    pre_b8a = a["last_b8a"].astype(np.float32)
    pre_b11 = a["last_b11"].astype(np.float32)
    pre_b12 = a["last_b12"].astype(np.float32)
    pre_ndvi = a["last_ndvi"].astype(np.float32)
    with np.errstate(invalid="ignore"):
        d_b8a = pre_b8a - refl["B8A"]
        d_b11 = pre_b11 - refl["B11"]
        d_b12 = pre_b12 - refl["B12"]
        darkened = (d_b8a > rule.d_b8a) & (d_b11 > rule.d_b11) & (d_b12 > rule.d_b12)
        charred = (post_bais2 >= rule.bais2_min) & (post_tbi <= rule.tbi_max)
    burn_day = a["burn_day"].astype(np.int32)
    rearmed = (burn_day == 0) | (day - burn_day > rule.rearm_days)
    candidate = has_prev & (gap <= rule.max_gap_days) & darkened & charred & rearmed
    # The opening may not add pixels that failed the rule.
    burn = (_open_2x2(candidate) & candidate) if res_m <= 20 else candidate

    # Harvest bookkeeping: standing -> harvested, and harvested -> tilled (dark without char).
    # Smoke haze lowers NDVI and brightens the visible bands (so TBI rises), which would fake
    # harvests and tillage; a hazy pass therefore makes neither call and leaves NDVI memory alone.
    if hazy:
        harvested_now = np.zeros_like(valid)
        tilled_now = np.zeros_like(valid)
    else:
        ndvi_day = a["ndvi_day"].astype(np.int32)
        ndvi_gap = np.where(ndvi_day > 0, day - ndvi_day, 10_000)
        comparable = valid & (ndvi_day > 0) & (ndvi_gap <= harvest.max_gap_days)
        harvested_now = harvested_between(pre_ndvi, post_ndvi, pre_b11, refl["B11"], comparable, harvest)
        a["harv_after"][harvested_now] = a["ndvi_day"][harvested_now]
        a["harv_before"][harvested_now] = day
        tilled_now = has_prev & (gap <= rule.max_gap_days) & darkened & ~charred & (a["harv_before"] > 0)
        a["tilled_day"][tilled_now & ~burn] = day
    a["burn_day"][burn] = day

    pre_day = a["last_day"].copy()
    pre_item = a["last_item"].copy()
    evidence = {
        "d_b8a": d_b8a, "d_b11": d_b11, "d_b12": d_b12,
        "dnbr": a["last_nbr"].astype(np.float32) - post_nbr, "nbr_post": post_nbr,
        "bais2_post": post_bais2, "tbi_post": post_tbi, "ndvi_pre": pre_ndvi, "ndvi_post": post_ndvi,
    }
    # Remember this pass for every clear pixel.
    global_items = np.array([registry_index(registry, i) for i in item_ids] + [0], dtype=np.uint16)
    post_item = global_items[np.minimum(item_index, len(item_ids))]
    a["last_day"][valid] = day
    a["last_item"][valid] = post_item[valid]
    a["last_b8a"][valid] = refl["B8A"][valid]
    a["last_b11"][valid] = refl["B11"][valid]
    a["last_b12"][valid] = refl["B12"][valid]
    a["last_nbr"][valid] = post_nbr[valid]
    if not hazy:
        a["last_ndvi"][valid] = post_ndvi[valid]
        a["ndvi_day"][valid] = day
    if not hazy:
        a["clear_b02"][valid] = refl["B02"][valid]
        a["clear_b12"][valid] = refl["B12"][valid]
    stats.update(burn_px=int(burn.sum()), harvested_px=int(harvested_now.sum()),
                 tilled_px=int(tilled_now.sum()))
    return PassResult(True, None, stats, burn, pre_day, pre_item, post_item, evidence, hazy)


def harvest_candidates(state: BlockState, scope: np.ndarray, newest_day: int,
                       harvest: HarvestRule = HARVEST_RULE) -> np.ndarray:
    """This block's harvested fields with straw still unburnt (see analytics.harvest)."""
    a = state.arrays
    return unburnt_residue(a["harv_before"], a["burn_day"], a["tilled_day"], a["last_day"],
                           a["last_ndvi"], scope, newest_day, harvest)


# --- pixels -> polygons ---------------------------------------------------------------------------


Counter_t = dict[int, int]  # value -> pixel count


@dataclass
class Part:
    """A cluster (or a piece of one cut by a block edge) with running sums of its evidence."""

    key: int  # burns: previous-pass day; harvests: harvested_before day
    geom: Polygon  # tile CRS (UTM metres)
    n_px: int
    sums: dict[str, float]
    tags: dict[str, Counter_t]  # categorical values -> pixel counts (items, days)
    touches_edge: bool

    def mean(self, name: str) -> float | None:
        value = self.sums.get(name)
        return None if value is None or self.n_px == 0 else value / self.n_px

    def mode(self, name: str) -> int | None:
        counts = self.tags.get(name)
        return max(counts, key=counts.__getitem__) if counts else None

    def max_tag(self, name: str) -> int | None:
        counts = self.tags.get(name)
        return max(counts) if counts else None

    def min_tag(self, name: str) -> int | None:
        counts = self.tags.get(name)
        return min(counts) if counts else None


def extract_parts(
    mask: np.ndarray,
    keys: np.ndarray,
    transform: Affine,
    values: dict[str, np.ndarray],
    tags: dict[str, np.ndarray],
) -> list[Part]:
    """4-connected clusters of `mask`, split by `keys` (pixels of different keys never join).

    `values` are summed per cluster (NaN counted as 0, so pass finite arrays), `tags` counted per
    value. Polygons are exact pixel outlines in the block's CRS."""
    parts: list[Part] = []
    if not mask.any():
        return parts
    for key in np.unique(keys[mask]):
        sub = mask & (keys == key)
        labels, n = _ndimage().label(sub, structure=FOUR_CONNECTED)
        if n == 0:
            continue
        index = np.arange(1, n + 1)
        counts = _ndimage().sum_labels(sub, labels, index).astype(int)
        sums = {name: _ndimage().sum_labels(np.nan_to_num(arr), labels, index) for name, arr in values.items()}
        tag_counts: list[dict[str, Counter_t]] = [{} for _ in range(n)]
        for name, arr in tags.items():
            # Count (label, value) pairs in one vectorised pass; values are uint16.
            pairs = labels[sub].astype(np.int64) * 65_536 + arr[sub].astype(np.int64)
            uniq, cnt = np.unique(pairs, return_counts=True)
            for pair, c in zip(uniq.tolist(), cnt.tolist(), strict=True):
                bucket = tag_counts[pair // 65_536 - 1].setdefault(name, {})
                bucket[pair % 65_536] = c
        edge = np.zeros(n + 1, bool)
        for line in (labels[0, :], labels[-1, :], labels[:, 0], labels[:, -1]):
            edge[np.unique(line)] = True
        from rasterio import features  # job-time only; see _ndimage()

        for geom, lab in features.shapes(labels.astype(np.int32), mask=labels > 0,
                                         transform=transform, connectivity=4):
            i = int(lab) - 1
            parts.append(Part(
                key=int(key), geom=shape(geom), n_px=int(counts[i]),
                sums={name: float(s[i]) for name, s in sums.items()},
                tags={k: dict(v) for k, v in tag_counts[i].items()},
                touches_edge=bool(edge[i + 1]),
            ))
    return parts


def _find(parent: list[int], i: int) -> int:
    """Root of i in a union-find forest (with path halving)."""
    while parent[i] != i:
        parent[i] = parent[parent[i]]
        i = parent[i]
    return i


def merge_parts(parts: Iterable[Part]) -> list[Part]:
    """Join pieces of one cluster cut by block edges: same key and a shared boundary segment.
    (Pieces meeting only at a corner stay apart, matching 4-connectivity.)"""
    by_key: dict[int, list[Part]] = defaultdict(list)
    out: list[Part] = []
    for part in parts:
        (by_key[part.key] if part.touches_edge else out).append(part)
    for group in by_key.values():
        parent = list(range(len(group)))  # union-find over the pieces
        tree = STRtree([p.geom for p in group])
        for i, part in enumerate(group):
            for j in tree.query(part.geom, predicate="intersects"):
                j = int(j)
                if j > i and part.geom.intersection(group[j].geom).length > 0:
                    parent[_find(parent, j)] = _find(parent, i)
        clusters: dict[int, list[Part]] = defaultdict(list)
        for i, part in enumerate(group):
            clusters[_find(parent, i)].append(part)
        for members in clusters.values():
            if len(members) == 1:
                out.append(members[0])
                continue
            sums: dict[str, float] = defaultdict(float)
            tags: dict[str, Counter_t] = defaultdict(dict)
            for m in members:
                for k, v in m.sums.items():
                    sums[k] += v
                for k, counts in m.tags.items():
                    for val, c in counts.items():
                        tags[k][val] = tags[k].get(val, 0) + c
            geom = unary_union([m.geom for m in members])
            if geom.geom_type != "Polygon":  # numerically split; keep the largest piece's outline
                geom = max(getattr(geom, "geoms", [geom]), key=lambda g: g.area)
            out.append(Part(members[0].key, geom, sum(m.n_px for m in members), dict(sums),
                            dict(tags), touches_edge=False))
    return out


def iter_large(parts: Iterable[Part], min_px: int) -> Iterator[Part]:
    for part in parts:
        if part.n_px >= min_px:
            yield part


__all__ = [
    "HARVEST_RULE", "RULE", "STATE_FIELDS", "VALID_SCL", "BlockState", "BurnRule", "HarvestRule",
    "Part", "PassResult", "apply_pass", "bais2", "confidence", "extract_parts", "harvest_candidates",
    "registry_index",
    "iter_large", "merge_parts", "nbr", "ndvi", "tbi", "valid_scl",
]
