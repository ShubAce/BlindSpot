"""GK-2A AMI Level-1B full-disk scans from AWS Open Data: list them, Range-read our window.

Why GK-2A: polar fire satellites (VIIRS, MODIS) look at Punjab a few times a day, and none of them
between Aqua's afternoon pass (~15:10-16:40 IST) and Terra's evening one (~20:00 IST), which is
when most residue fires now burn. KMA's geostationary GK-2A (sub-satellite point 128.2 E) scans
the full disk every 10 minutes, and NOAA's Open Data Dissemination programme mirrors its Level-1B
files on AWS without a key. Nobody publishes a GK-2A fire product there, so we detect fires
ourselves (analytics/geofire.py) from two infrared channels.

Why Range reads: one full-disk file is 10-35 MB, but it is a NetCDF-4 (HDF5) file whose pixel
array is stored in gzip chunks of 1375 x 1375 pixels. Punjab and Haryana lie in chunk (0, 0) plus
the first ~45 rows of chunk (1375, 0). So per channel we fetch the first MiB (the HDF5 metadata,
which also holds chunk (0, 0)), then only the start of chunk (1375, 0): the chunks are deflated
without the shuffle filter, so the first rows of a chunk come out of the first bytes of its
compressed stream. About 1.1 MB per channel instead of 30 MB.

Every byte comes through blindspot.http.fetch (one source_fetch row per request, with retries).
Attribution: "KMA GK-2A satellite data, via NOAA Open Data Dissemination on AWS".
"""

from __future__ import annotations

import io
import logging
import re
import xml.etree.ElementTree as ET
import zlib
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import orjson

from blindspot.config import BBox, get_settings
from blindspot.http import fetch, set_fetch_records
from blindspot.provenance import SourceInfo, register_source

if TYPE_CHECKING:  # pyproj and pandas are imported where they are used (job-time only), so the
    # web API, which imports this module just for its source label, starts without them.
    import pyproj

log = logging.getLogger(__name__)

SOURCE_KEY = "gk2a"
register_source(
    SourceInfo(
        SOURCE_KEY,
        "KMA GK-2A AMI L1B full disk, via NOAA Open Data on AWS",
        "fires_geo",
        30,
        "https://registry.opendata.aws/noaa-gk2a-pds/",
    )
)

BUCKET_URL = "https://noaa-gk2a-pds.s3.amazonaws.com"
_S3_NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
# AMI/L1B/FD/202610/29/11/gk2a_ami_le1b_sw038_fd020ge_202610291130.nc
_KEY_RE = re.compile(r"gk2a_ami_le1b_(?P<ch>[a-z]{2}\d{3})_fd(?P<res>\d{3})ge_(?P<t>\d{12})\.nc$")

# Channels we read, all on the 2 km grid (file-name resolution code "020"):
#   sw038  3.8 um shortwave infrared: a sub-pixel fire raises it far more than the 11 um channel.
#   ir112  11.2 um window: the background temperature, and cold (cloudy) pixels.
#   nr016  1.6 um near infrared: daylight reflectance, to screen bright clouds.
GRID_RES_CODE = "020"
# Central wavelengths (um) used to invert the Planck function. These are the values in satpy's
# ami_l1b reader (satpy/etc/readers/ami_l1b.yaml); with them our brightness temperatures equal
# satpy's calib_mode="FILE" output exactly (docs/research/geostationary-fires.md section 4.2).
CENTRAL_WAVELENGTH_UM = {"sw038": 3.83, "ir112": 11.23}

FIRST_READ_BYTES = 1 << 20  # first request: HDF5 metadata and, usually, all of chunk (0, 0)
MIN_READ_BYTES = 1 << 16  # smallest extra request when h5py needs bytes we have not fetched
PREFIX_STEP_BYTES = 1 << 17  # how much more of a chunk's stream to fetch when a prefix is short

# Seconds in KMA's time attributes count from 2000-01-01 12:00:00 UTC (checked against the
# `scene_acquisition_time` string in the same file, e.g. 846631832.08 s -> 2026-10-30 11:30:32).
KMA_EPOCH = datetime(2000, 1, 1, 12, 0, 0, tzinfo=UTC)


# --- listing --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ScanFile:
    """One channel file of one full-disk scan."""

    slot: datetime  # nominal scan start in UTC, from the file name (every 10 minutes)
    channel: str
    key: str
    size: int
    last_modified: datetime | None

    @property
    def url(self) -> str:
        return f"{BUCKET_URL}/{self.key}"


def hour_prefix(hour: datetime) -> str:
    """S3 prefix holding every channel of every scan that starts in this UTC hour."""
    return f"AMI/L1B/FD/{hour:%Y%m}/{hour:%d}/{hour:%H}/"


def parse_listing(xml_text: str) -> list[ScanFile]:
    """ScanFiles from one S3 ListObjectsV2 response (only full-disk 2 km L1B files)."""
    root = ET.fromstring(xml_text)
    files = []
    for item in root.findall("s3:Contents", _S3_NS):
        key = item.findtext("s3:Key", default="", namespaces=_S3_NS)
        match = _KEY_RE.search(key)
        if not match or match["res"] != GRID_RES_CODE:
            continue
        modified = item.findtext("s3:LastModified", default="", namespaces=_S3_NS)
        files.append(
            ScanFile(
                slot=datetime.strptime(match["t"], "%Y%m%d%H%M").replace(tzinfo=UTC),
                channel=match["ch"],
                key=key,
                size=int(item.findtext("s3:Size", default="0", namespaces=_S3_NS)),
                last_modified=(
                    datetime.fromisoformat(modified) if modified else None
                ),
            )
        )
    return files


def list_hour(hour: datetime) -> list[ScanFile]:
    """Every 2 km channel file of the scans starting in one UTC hour (one keyless S3 call).

    A missing scan simply is not listed (KMA skips some slots, e.g. 06:00Z on 29-30 Oct 2026),
    so we always list before reading instead of guessing file names.
    """
    hour = hour.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    result = fetch(
        SOURCE_KEY,
        f"{BUCKET_URL}/",
        params={"list-type": "2", "prefix": hour_prefix(hour)},
        expect="text",
        meta={"kind": "listing", "hour": hour.isoformat()},
    )
    files = parse_listing(result.data)
    set_fetch_records(result.fetch_id, len(files))
    return files


def complete_scans(
    files: Iterable[ScanFile], channels: Iterable[str]
) -> dict[datetime, dict[str, ScanFile]]:
    """Group files by scan slot, keeping only scans that have every channel asked for."""
    wanted = set(channels)
    by_slot: dict[datetime, dict[str, ScanFile]] = {}
    for f in files:
        if f.channel in wanted:
            by_slot.setdefault(f.slot, {})[f.channel] = f
    return {slot: chans for slot, chans in sorted(by_slot.items()) if set(chans) == wanted}


# --- navigation (where each pixel is) --------------------------------------------------------------


@dataclass(frozen=True)
class Navigation:
    """The fixed-grid geostationary projection of a file, from its own global attributes.

    Pixel (row, col), 0-based, has its centre at scan angles
        x = (col + 1 - coff) * 2**16 / cfac   and   y = (row + 1 - loff) * 2**16 / lfac  (degrees),
    the CGMS normalised geostationary projection (CGMS LRIT/HRIT Global Specification, section
    4.4). lfac is negative, so row 0 is the north edge. PROJ's `geos` projection measures the same
    angles in metres, as angle (radians) x satellite height above the ellipsoid.
    """

    sub_lon_deg: float
    cfac: float
    lfac: float
    coff: float
    loff: float
    sat_distance_m: float  # from the Earth's centre (nominal_satellite_height)
    a_m: float  # earth_equatorial_radius
    b_m: float  # earth_polar_radius
    lines: int
    columns: int

    @classmethod
    def from_attrs(cls, attrs: dict[str, Any]) -> Navigation:
        if str(attrs.get("projection_type", "")).upper() != "GEOS":
            raise ValueError(f"unexpected projection_type {attrs.get('projection_type')!r}")
        return cls(
            sub_lon_deg=float(np.degrees(float(attrs["sub_longitude"]))),
            cfac=float(attrs["cfac"]),
            lfac=float(attrs["lfac"]),
            coff=float(attrs["coff"]),
            loff=float(attrs["loff"]),
            sat_distance_m=float(attrs["nominal_satellite_height"]),
            a_m=float(attrs["earth_equatorial_radius"]),
            b_m=float(attrs["earth_polar_radius"]),
            lines=int(attrs["number_of_lines"]),
            columns=int(attrs["number_of_columns"]),
        )

    @property
    def height_m(self) -> float:
        """Satellite height above the equator, which PROJ's geos `h` expects."""
        return self.sat_distance_m - self.a_m

    @property
    def step_x_m(self) -> float:
        """Column spacing in projected metres (~2004 m: 56 microradians at 35,786 km)."""
        return float(np.radians(2**16 / self.cfac)) * self.height_m

    @property
    def step_y_m(self) -> float:
        return float(np.radians(2**16 / abs(self.lfac))) * self.height_m

    def crs(self) -> pyproj.CRS:
        # sweep=y is the default and matches satpy's AMI reader (research doc section 4.7).
        return _geos_crs(self.sub_lon_deg, self.height_m, self.a_m, self.b_m)

    def to_lonlat(self, rows: np.ndarray, cols: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Longitude/latitude of fractional pixel positions (row/col 0.0 = centre of pixel 0)."""
        x = (np.asarray(cols, dtype=float) + 1.0 - self.coff) * self.step_x_m
        y = (np.asarray(rows, dtype=float) + 1.0 - self.loff) * self.step_y_m * np.sign(self.lfac)
        lon, lat = _to_lonlat(self.crs())(x, y)
        bad = ~(np.isfinite(lon) & np.isfinite(lat)) | (np.abs(lon) > 360)
        return np.where(bad, np.nan, lon), np.where(bad, np.nan, lat)

    def to_rowcol(self, lon: np.ndarray, lat: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Integer pixel (row, col) containing each lon/lat; -1 where the point is off the disk."""
        x, y = _from_lonlat(self.crs())(np.asarray(lon, float), np.asarray(lat, float))
        ok = np.isfinite(x) & np.isfinite(y) & (np.abs(x) < 1e30)
        col = np.floor(np.where(ok, x, 0.0) / self.step_x_m + self.coff - 0.5).astype(int)
        row = np.floor(
            np.where(ok, y, 0.0) / (self.step_y_m * np.sign(self.lfac)) + self.loff - 0.5
        ).astype(int)
        return np.where(ok, row, -1), np.where(ok, col, -1)

    def view_zenith_deg(self, lon: np.ndarray, lat: np.ndarray) -> np.ndarray:
        """Satellite view zenith angle at each ground point (spherical Earth is plenty here)."""
        lat_r, dlon = np.radians(lat), np.radians(np.asarray(lon) - self.sub_lon_deg)
        cos_gamma = np.cos(lat_r) * np.cos(dlon)  # central angle between point and sub-point
        gamma = np.arccos(np.clip(cos_gamma, -1, 1))
        r, d = self.a_m, self.sat_distance_m
        slant = np.sqrt(r * r + d * d - 2 * r * d * cos_gamma)
        return np.degrees(np.arcsin(np.clip(d * np.sin(gamma) / slant, -1, 1)))


@lru_cache(maxsize=8)
def _geos_crs(lon0: float, h: float, a: float, b: float) -> pyproj.CRS:
    import pyproj

    return pyproj.CRS.from_proj4(
        f"+proj=geos +lon_0={lon0} +h={h} +a={a} +b={b} +units=m +no_defs"
    )


@lru_cache(maxsize=8)
def _transformers(crs: pyproj.CRS) -> tuple[pyproj.Transformer, pyproj.Transformer]:
    import pyproj

    fwd = pyproj.Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    inv = pyproj.Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    return fwd, inv


def _to_lonlat(crs: pyproj.CRS):  # type: ignore[no-untyped-def]
    return _transformers(crs)[1].transform


def _from_lonlat(crs: pyproj.CRS):  # type: ignore[no-untyped-def]
    return _transformers(crs)[0].transform


@dataclass(frozen=True)
class GridWindow:
    """A block of the 2 km full-disk grid: rows [row0, row1) and columns [col0, col1)."""

    row0: int
    row1: int
    col0: int
    col1: int

    @property
    def shape(self) -> tuple[int, int]:
        return (self.row1 - self.row0, self.col1 - self.col0)

    def centres(self, nav: Navigation) -> tuple[np.ndarray, np.ndarray]:
        """lon, lat of every pixel centre in the window (2-D arrays)."""
        rr, cc = np.mgrid[self.row0 : self.row1, self.col0 : self.col1]
        return nav.to_lonlat(rr, cc)

    def corners(self, nav: Navigation) -> tuple[np.ndarray, np.ndarray]:
        """lon, lat of pixel corners: arrays of shape (rows + 1, cols + 1). The footprint of
        pixel (i, j) is corners [i, j], [i, j+1], [i+1, j+1], [i+1, j]."""
        rr, cc = np.mgrid[self.row0 - 0.5 : self.row1 + 0.5, self.col0 - 0.5 : self.col1 + 0.5]
        return nav.to_lonlat(rr, cc)


def window_for_bbox(nav: Navigation, bbox: BBox, margin_px: int) -> GridWindow:
    """The smallest grid window holding every point of `bbox`, plus `margin_px` pixels each side
    (the fire test compares a pixel with its neighbours, so pixels at the AOI edge need some)."""
    min_lon, min_lat, max_lon, max_lat = bbox
    t = np.linspace(0.0, 1.0, 41)
    along_lon, along_lat = min_lon + t * (max_lon - min_lon), min_lat + t * (max_lat - min_lat)
    lo, hi = np.full_like(t, 1.0), np.full_like(t, 1.0)
    edge_lon = np.concatenate([along_lon, along_lon, lo * min_lon, hi * max_lon])
    edge_lat = np.concatenate([lo * min_lat, hi * max_lat, along_lat, along_lat])
    rows, cols = nav.to_rowcol(edge_lon, edge_lat)
    if (rows < 0).any() or (cols < 0).any():
        raise ValueError(f"bbox {bbox} is not entirely on the GK-2A disk")
    return GridWindow(
        row0=max(0, int(rows.min()) - margin_px),
        row1=min(nav.lines, int(rows.max()) + 1 + margin_px),
        col0=max(0, int(cols.min()) - margin_px),
        col1=min(nav.columns, int(cols.max()) + 1 + margin_px),
    )


# --- reading -----------------------------------------------------------------------------------------


class _RangeFile(io.RawIOBase):
    """A read-only, seekable file over HTTP Range requests, for h5py.

    Each request goes through blindspot.http.fetch, so it is logged in source_fetch and retried
    like any other upstream call. Fetched spans are kept, so h5py's many small metadata reads
    are served from the first request.
    """

    def __init__(self, url: str, meta: dict[str, Any]) -> None:
        super().__init__()
        self.url = url
        self.meta = meta
        self.size: int | None = None
        self.pos = 0
        self.spans: list[tuple[int, bytes]] = []  # (start offset, bytes), non-overlapping
        self.fetch_ids: list[int] = []
        self.bytes_fetched = 0
        self.requests = 0
        self.fetch_range(0, FIRST_READ_BYTES)

    def fetch_range(self, start: int, length: int) -> None:
        """Download bytes [start, start + length) (clipped to the file) and keep them."""
        if self.size is not None:
            length = min(length, self.size - start)
        if length <= 0:
            return
        end = start + length - 1
        result = fetch(
            SOURCE_KEY,
            self.url,
            headers={"Range": f"bytes={start}-{end}"},
            expect="bytes",
            meta={**self.meta, "range": [start, end]},
        )
        data: bytes = result.data
        content_range = result.headers.get("content-range", "")
        if result.status_code == 206 and "/" in content_range:
            self.size = int(content_range.rsplit("/", 1)[1])
        elif result.status_code == 200:  # the server ignored Range and sent the whole file
            self.size, start = len(data), 0
        else:
            raise OSError(f"unexpected HTTP {result.status_code} for a Range request")
        if result.fetch_id is not None:
            self.fetch_ids.append(result.fetch_id)
        self.bytes_fetched += len(data)
        self.requests += 1
        self.spans.append((start, data))
        self.spans.sort(key=lambda s: s[0])

    def _covered(self, start: int, stop: int) -> bytes | None:
        for s, data in self.spans:
            if s <= start and stop <= s + len(data):
                return data[start - s : stop - s]
        return None

    def read_bytes(self, start: int, stop: int) -> bytes:
        """Bytes [start, stop), fetching whatever part is missing in one request."""
        assert self.size is not None
        stop = min(stop, self.size)
        got = self._covered(start, stop)
        if got is None:
            # Fetch from the end of the span that covers `start` (if any) to `stop`.
            fetch_from = start
            for s, data in self.spans:
                if s <= start < s + len(data):
                    fetch_from = s + len(data)
            self.fetch_range(fetch_from, max(stop - fetch_from, MIN_READ_BYTES))
            if fetch_from > start:  # stitch the two spans
                head = self._covered(start, fetch_from) or b""
                tail = self._covered(fetch_from, stop) or b""
                return head + tail
            got = self._covered(start, stop)
            if got is None:
                raise OSError(f"could not read bytes {start}-{stop} of {self.url}")
        return got

    # io.RawIOBase interface used by h5py's file-object driver
    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        assert self.size is not None
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self.pos, io.SEEK_END: self.size}[whence]
        self.pos = base + offset
        return self.pos

    def readinto(self, buffer: Any) -> int:
        assert self.size is not None
        n = max(0, min(len(buffer), self.size - self.pos))
        if n:
            buffer[:n] = self.read_bytes(self.pos, self.pos + n)
            self.pos += n
        return n


@dataclass
class ChannelWindow:
    """One channel of one scan, cut to a grid window: raw counts plus what is needed to use them."""

    file: ScanFile
    window: GridWindow
    counts: np.ndarray  # uint16 counts with the quality bits removed
    quality: np.ndarray  # uint8: 0 good, 1 conditionally usable, 2 off the disk, 3 error
    attrs: dict[str, Any]  # the file's global attributes (calibration, navigation, times)
    fetch_ids: list[int] = field(default_factory=list)
    bytes_fetched: int = 0
    requests: int = 0

    @property
    def navigation(self) -> Navigation:
        return Navigation.from_attrs(self.attrs)


def _plain(value: Any) -> Any:
    """An HDF5 attribute as a plain Python value (1-element arrays unwrapped, bytes decoded)."""
    if isinstance(value, bytes | np.bytes_):
        return value.decode("utf-8", "replace")
    if isinstance(value, np.ndarray):
        if value.size == 1:
            return _plain(value.reshape(-1)[0])
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    return value


def read_window(
    file: ScanFile,
    bbox: BBox | None = None,
    *,
    window: GridWindow | None = None,
    margin_px: int = 8,
) -> ChannelWindow:
    """Fetch one channel file's pixels for `window` (or for `bbox`, located with the file's own
    navigation), reading only the compressed chunk bytes that hold them.

    Raises FetchError when a request fails (already recorded) and ValueError when the file is not
    laid out as expected (the job then records the scan as failed instead of guessing).
    """
    # h5py is imported here, not at module level: only the geostationary jobs read scans, and the
    # web API imports this module just for its source label (serverless ships no h5py).
    import h5py

    meta = {"kind": "range", "channel": file.channel, "slot": file.slot.isoformat()}
    rf = _RangeFile(file.url, meta=meta)
    with h5py.File(rf, "r") as h5:
        attrs = {k: _plain(v) for k, v in h5.attrs.items()}
        nav = Navigation.from_attrs(attrs)
        if window is None:
            if bbox is None:
                raise ValueError("give a bbox or a window")
            window = window_for_bbox(nav, bbox, margin_px)
        ds = h5["image_pixel_values"]
        if ds.dtype != np.dtype("<u2") or ds.chunks is None or ds.shuffle:
            raise ValueError(
                f"unexpected layout: dtype {ds.dtype}, chunks {ds.chunks}, shuffle {ds.shuffle}"
            )
        if ds.compression not in ("gzip", None):
            raise ValueError(f"unexpected compression {ds.compression!r}")
        valid_bits = int(_plain(ds.attrs["number_of_valid_bits_per_pixel"]))
        chunk_rows, chunk_cols = ds.chunks
        pieces = []  # (chunk row0, chunk col0, byte offset, stored size, deflated?)
        for r0 in range(window.row0 // chunk_rows * chunk_rows, window.row1, chunk_rows):
            for c0 in range(window.col0 // chunk_cols * chunk_cols, window.col1, chunk_cols):
                info = ds.id.get_chunk_info_by_coord((r0, c0))
                deflated = ds.compression == "gzip" and not (info.filter_mask & 1)
                pieces.append((r0, c0, int(info.byte_offset), int(info.size), deflated))

    raw = np.full(window.shape, 0xFFFF, dtype=np.uint16)  # fill value = error pixel
    for r0, c0, offset, size, deflated in pieces:
        rows_needed = min(window.row1, r0 + chunk_rows) - r0  # this chunk's rows, from its top
        block = _decode_chunk_prefix(rf, offset, size, deflated, rows_needed * chunk_cols * 2)
        block = np.frombuffer(block, dtype="<u2").reshape(rows_needed, chunk_cols)
        rs, re_ = max(window.row0, r0), min(window.row1, r0 + chunk_rows)
        cs, ce = max(window.col0, c0), min(window.col1, c0 + chunk_cols)
        raw[rs - window.row0 : re_ - window.row0, cs - window.col0 : ce - window.col0] = block[
            rs - r0 : re_ - r0, cs - c0 : ce - c0
        ]
    # The top 2 of 16 bits are the quality flag; the low `valid_bits` bits are the count
    # (file attributes `data_quality_flag_meaning`, `number_of_valid_bits_per_pixel`).
    quality = (raw >> 14).astype(np.uint8)
    counts = raw & np.uint16((1 << valid_bits) - 1)
    return ChannelWindow(
        file=file,
        window=window,
        counts=counts,
        quality=quality,
        attrs=attrs,
        fetch_ids=rf.fetch_ids,
        bytes_fetched=rf.bytes_fetched,
        requests=rf.requests,
    )


def _decode_chunk_prefix(
    rf: _RangeFile, offset: int, size: int, deflated: bool, nbytes: int
) -> bytes:
    """The first `nbytes` of a chunk's decompressed data, fetching only as much of its compressed
    stream as that needs (deflate decodes front to back, and there is no shuffle filter)."""
    if not deflated:
        return rf.read_bytes(offset, offset + nbytes)
    decoder = zlib.decompressobj()
    out = bytearray()
    pos = offset
    step = min(size, max(PREFIX_STEP_BYTES, nbytes // 2))
    while len(out) < nbytes and pos < offset + size:
        stop = min(offset + size, pos + step)
        out += decoder.decompress(rf.read_bytes(pos, stop), nbytes - len(out))
        # Bytes held back by max_length stay in unconsumed_tail; feed them before fetching more.
        while decoder.unconsumed_tail and len(out) < nbytes:
            out += decoder.decompress(decoder.unconsumed_tail, nbytes - len(out))
        pos = stop
        step = PREFIX_STEP_BYTES
    if len(out) < nbytes:
        raise ValueError(f"chunk at byte {offset} decoded to {len(out)} bytes, needed {nbytes}")
    return bytes(out[:nbytes])


# --- calibration ---------------------------------------------------------------------------------


def _valid(cw: ChannelWindow, radiance: np.ndarray) -> np.ndarray:
    return (cw.quality == 0) & np.isfinite(radiance) & (radiance > 0)


def radiance(cw: ChannelWindow) -> np.ndarray:
    """Spectral radiance from counts with the file's linear calibration (KMA: DN_to_Radiance_Gain
    and _Offset; mW m-2 sr-1 (cm-1)-1 for infrared, W m-2 sr-1 um-1 for visible/near-infrared)."""
    gain, offset = float(cw.attrs["DN_to_Radiance_Gain"]), float(cw.attrs["DN_to_Radiance_Offset"])
    return gain * cw.counts.astype(np.float64) + offset


def brightness_temperature(cw: ChannelWindow) -> np.ndarray:
    """Brightness temperature (K) of an infrared channel; NaN where the pixel is not good.

    Radiance -> effective temperature by inverting Planck's law at the channel's central
    wavenumber, then KMA's quadratic correction to the band-averaged brightness temperature
    (file attributes Teff_to_Tbb_c0/c1/c2). Planck, light-speed and Boltzmann constants are read
    from the file itself (Plank_constant_h, light_speed, Boltzmann_constant_k).
    """
    wl = CENTRAL_WAVELENGTH_UM.get(cw.file.channel)
    if wl is None:
        raise ValueError(f"no central wavelength for {cw.file.channel}")
    rad = radiance(cw)
    h = float(cw.attrs["Plank_constant_h"])
    c = float(cw.attrs["light_speed"])
    k = float(cw.attrs["Boltzmann_constant_k"])
    wn = 1e6 / wl  # wavenumber in m-1
    good = _valid(cw, rad)
    # mW m-2 sr-1 (cm-1)-1 -> W m-2 sr-1 (m-1)-1 is a factor 1e-5.
    with np.errstate(invalid="ignore", divide="ignore"):
        rad_si = np.where(good, rad, 1.0) * 1e-5
        te = (h * c / k) * wn / np.log(2.0 * h * c * c * wn**3 / rad_si + 1.0)
    c0, c1, c2 = (float(cw.attrs[f"Teff_to_Tbb_c{i}"]) for i in range(3))
    bt = c0 + c1 * te + c2 * te * te
    return np.where(good, bt, np.nan)


def albedo(cw: ChannelWindow) -> np.ndarray:
    """Top-of-atmosphere albedo of a visible/near-infrared channel (KMA's Radiance_to_Albedo_c),
    not yet divided by cos(solar zenith). NaN where the pixel is not good."""
    rad = radiance(cw)
    good = (cw.quality == 0) & np.isfinite(rad)
    return np.where(good, rad * float(cw.attrs["Radiance_to_Albedo_c"]), np.nan)


def kma_time(seconds: float) -> datetime:
    """A KMA time attribute (seconds since 2000-01-01 12:00 UTC) as an aware UTC datetime."""
    return KMA_EPOCH + timedelta(seconds=float(seconds))


def row_time(attrs: dict[str, Any], row: float) -> datetime:
    """When the scan reached a given row: the full disk is swept north to south between
    observation_start_time and observation_end_time (~9 minutes, in 23 swaths), so we interpolate
    linearly by row. Good to about the length of one swath (~25 s)."""
    start = float(attrs["observation_start_time"])
    end = float(attrs["observation_end_time"])
    lines = int(attrs["number_of_lines"])
    return kma_time(start + (end - start) * (row + 0.5) / lines)


# --- one scan: the channels we need, cached on disk ----------------------------------------------

# Cached scans are raw counts + calibration attributes, so re-running the detector with new
# thresholds never downloads anything again. ~150-200 KB per scan; the job prunes old ones.
CACHE_SUBDIR = "gk2a"
_CACHED_ATTRS = (
    "DN_to_Radiance_Gain", "DN_to_Radiance_Offset", "Teff_to_Tbb_c0", "Teff_to_Tbb_c1",
    "Teff_to_Tbb_c2", "Plank_constant_h", "light_speed", "Boltzmann_constant_k",
    "Radiance_to_Albedo_c", "projection_type", "sub_longitude", "cfac", "lfac", "coff", "loff",
    "nominal_satellite_height", "earth_equatorial_radius", "earth_polar_radius",
    "number_of_lines", "number_of_columns", "observation_start_time", "observation_end_time",
    "scene_acquisition_time", "file_name",
)


@dataclass
class Scan:
    """The channels of one full-disk scan that the fire test needs, cut to our window."""

    slot: datetime
    window: GridWindow
    channels: dict[str, ChannelWindow]
    from_cache: bool = False

    @property
    def attrs(self) -> dict[str, Any]:
        return self.channels["sw038"].attrs

    @property
    def navigation(self) -> Navigation:
        return self.channels["sw038"].navigation

    @property
    def fetch_ids(self) -> list[int]:
        return [i for cw in self.channels.values() for i in cw.fetch_ids]

    @property
    def bytes_fetched(self) -> int:
        return sum(cw.bytes_fetched for cw in self.channels.values())

    def bt(self, channel: str) -> np.ndarray:
        return brightness_temperature(self.channels[channel])

    def albedo(self, channel: str) -> np.ndarray | None:
        cw = self.channels.get(channel)
        return albedo(cw) if cw is not None else None


def _cache_file(slot: datetime) -> Path:
    # Read the settings at call time: tests point CACHE_DIR at a temporary folder.
    return get_settings().cache_path(CACHE_SUBDIR, f"{slot:%Y%m%d}", f"{slot:%H%M}.npz")


def _save_cache(scan: Scan) -> None:
    arrays: dict[str, np.ndarray] = {}
    meta: dict[str, Any] = {
        "slot": scan.slot.isoformat(),
        "window": [scan.window.row0, scan.window.row1, scan.window.col0, scan.window.col1],
        "channels": {},
    }
    for ch, cw in scan.channels.items():
        arrays[f"{ch}_counts"] = cw.counts
        arrays[f"{ch}_quality"] = cw.quality
        meta["channels"][ch] = {
            "key": cw.file.key,
            "size": cw.file.size,
            "last_modified": cw.file.last_modified.isoformat() if cw.file.last_modified else None,
            "attrs": {k: cw.attrs[k] for k in _CACHED_ATTRS if k in cw.attrs},
            "fetch_ids": cw.fetch_ids,
            "bytes_fetched": cw.bytes_fetched,
            "requests": cw.requests,
        }
    arrays["meta"] = np.frombuffer(orjson.dumps(meta), dtype=np.uint8)
    path = _cache_file(scan.slot)
    tmp = path.with_name(path.stem + ".partial.npz")
    np.savez_compressed(tmp, **arrays)
    tmp.replace(path)


def _load_cache(slot: datetime, channels: Iterable[str]) -> Scan | None:
    path = _cache_file(slot)
    if not path.exists():
        return None
    try:
        with np.load(path) as npz:
            meta = orjson.loads(npz["meta"].tobytes())
            window = GridWindow(*meta["window"])
            out: dict[str, ChannelWindow] = {}
            for ch in channels:
                info = meta["channels"].get(ch)
                if info is None:
                    return None  # this channel was not read last time (say nr016 at night)
                lm = info["last_modified"]
                out[ch] = ChannelWindow(
                    file=ScanFile(
                        slot, ch, info["key"], info["size"], datetime.fromisoformat(lm) if lm else None
                    ),
                    window=window,
                    counts=npz[f"{ch}_counts"],
                    quality=npz[f"{ch}_quality"],
                    attrs=info["attrs"],
                    fetch_ids=info["fetch_ids"],
                    bytes_fetched=info["bytes_fetched"],
                    requests=info["requests"],
                )
    except Exception:  # a truncated or old-format cache file is simply downloaded again
        log.warning("ignoring unreadable GK-2A cache file %s", path, exc_info=True)
        return None
    return Scan(slot=slot, window=window, channels=out, from_cache=True)


def load_cached_scan(slot: datetime, channels: Iterable[str]) -> Scan | None:
    """A scan from the disk cache only (None when it is not cached): no network."""
    return _load_cache(slot, list(dict.fromkeys(["sw038", *channels])))


def load_scan(
    files: dict[str, ScanFile], bbox: BBox, channels: Iterable[str], *, use_cache: bool = True
) -> Scan:
    """Read `channels` of one scan for the window around `bbox` (from the disk cache if we
    already have it). sw038 is read first, to fix the window from the file's own navigation;
    the other channels are then read in parallel with the same window."""
    wanted = list(dict.fromkeys(["sw038", *channels]))
    slot = next(iter(files.values())).slot
    if use_cache:
        cached = _load_cache(slot, wanted)
        if cached is not None:
            return cached
    missing = [ch for ch in wanted if ch not in files]
    if missing:
        raise ValueError(f"scan {slot:%Y-%m-%d %H:%M}Z has no {', '.join(missing)} file")
    first = read_window(files["sw038"], bbox)
    out = {"sw038": first}
    rest = [ch for ch in wanted if ch != "sw038"]
    if rest:
        with ThreadPoolExecutor(max_workers=len(rest)) as pool:
            reads = {ch: pool.submit(read_window, files[ch], window=first.window) for ch in rest}
            out.update({ch: future.result() for ch, future in reads.items()})
    scan = Scan(slot=slot, window=first.window, channels=out)
    if use_cache:
        try:
            _save_cache(scan)
        except OSError:
            log.warning("could not cache GK-2A scan %s", slot, exc_info=True)
    return scan


def prune_cache(keep_days: float) -> int:
    """Delete cached scans older than `keep_days`; returns how many files were removed."""
    root = get_settings().cache_dir / CACHE_SUBDIR
    if not root.exists():
        return 0
    cutoff = datetime.now(UTC) - timedelta(days=keep_days)
    removed = 0
    for day_dir in root.iterdir():
        try:
            day = datetime.strptime(day_dir.name, "%Y%m%d").replace(tzinfo=UTC)
        except ValueError:
            continue
        if day + timedelta(days=1) < cutoff:
            for f in day_dir.glob("*.npz"):
                f.unlink(missing_ok=True)
                removed += 1
            with suppress(OSError):
                day_dir.rmdir()
    return removed


# --- polar reference for validation: NASA FIRMS keyless regional CSVs ------------------------------
# The fires module owns the main FIRMS ingest (table fire_detection). The GK-2A job reads that
# table when it covers the period; otherwise it reads the same keyless files itself, with these.

FIRMS_CSV = (
    "https://firms.modaps.eosdis.nasa.gov/data/active_fire/{dir}/csv/{prefix}_South_Asia_{span}.csv"
)
FIRMS_FILES = {  # FIRMS directory and file prefix per sensor (docs/research/firms-and-orbits.md 1.1)
    "VIIRS_SNPP": ("suomi-npp-viirs-c2", "SUOMI_VIIRS_C2"),
    "VIIRS_NOAA20": ("noaa-20-viirs-c2", "J1_VIIRS_C2"),
    "VIIRS_NOAA21": ("noaa-21-viirs-c2", "J2_VIIRS_C2"),
    "MODIS": ("modis-c6.1", "MODIS_C6_1"),
}
# FIRMS `satellite` codes (FIRMS attribute docs): N = Suomi NPP, N20 = NOAA-20, N21 = NOAA-21,
# A = Aqua, T = Terra.
FIRMS_SATELLITE = {
    "N": ("Suomi NPP", "VIIRS"),
    "N20": ("NOAA-20", "VIIRS"),
    "N21": ("NOAA-21", "VIIRS"),
    "A": ("Aqua", "MODIS"),
    "T": ("Terra", "MODIS"),
}


@dataclass(frozen=True)
class PolarDetections:
    """Polar-orbiter fire detections as columns (all arrays have the same length)."""

    lon: np.ndarray
    lat: np.ndarray
    acq_utc: np.ndarray  # datetime64[s], UTC
    satellite: np.ndarray  # "Suomi NPP", "NOAA-20", "NOAA-21", "Aqua", "Terra" (as fire_detection)
    instrument: np.ndarray  # "VIIRS" or "MODIS"
    frp_mw: np.ndarray
    scan_km: np.ndarray
    track_km: np.ndarray
    fetch_ids: tuple[int, ...] = ()
    source: str = "firms_csv"

    def __len__(self) -> int:
        return int(self.lon.size)

    def subset(self, keep: np.ndarray) -> PolarDetections:
        cols = {k: getattr(self, k)[keep] for k in _POLAR_COLUMNS}
        return PolarDetections(**cols, fetch_ids=self.fetch_ids, source=self.source)


_POLAR_COLUMNS = ("lon", "lat", "acq_utc", "satellite", "instrument", "frp_mw", "scan_km", "track_km")


def empty_polar(fetch_ids: Iterable[int] = (), source: str = "firms_csv") -> PolarDetections:
    empty = np.array([], dtype=float)
    return PolarDetections(
        lon=empty, lat=empty, acq_utc=np.array([], dtype="datetime64[s]"),
        satellite=np.array([], dtype=object), instrument=np.array([], dtype=object),
        frp_mw=empty, scan_km=empty, track_km=empty, fetch_ids=tuple(fetch_ids), source=source,
    )


def parse_firms_csv(text: str, bbox: BBox) -> dict[str, np.ndarray]:
    """Columns of a FIRMS CSV, kept only inside `bbox`. acq_time is HHMM in UTC and must be read as
    a string (zero-padded: '0809'); docs/research/firms-and-orbits.md section 1.2."""
    import pandas as pd

    df = pd.read_csv(io.StringIO(text), dtype={"acq_time": str, "satellite": str})
    if df.empty:
        return {}
    min_lon, min_lat, max_lon, max_lat = bbox
    df = df[df.longitude.between(min_lon, max_lon) & df.latitude.between(min_lat, max_lat)]
    df = df[df.satellite.isin(list(FIRMS_SATELLITE))]
    if df.empty:
        return {}
    hhmm = df.acq_time.str.zfill(4)
    stamp = df.acq_date + " " + hhmm.str[:2] + ":" + hhmm.str[2:]
    acq = pd.to_datetime(stamp, format="%Y-%m-%d %H:%M", utc=True).dt.tz_localize(None)
    return {
        "lon": df.longitude.to_numpy(float),
        "lat": df.latitude.to_numpy(float),
        "acq_utc": acq.to_numpy(dtype="datetime64[s]"),
        "satellite": df.satellite.map(lambda s: FIRMS_SATELLITE[s][0]).to_numpy(dtype=object),
        "instrument": df.satellite.map(lambda s: FIRMS_SATELLITE[s][1]).to_numpy(dtype=object),
        "frp_mw": df.frp.to_numpy(float),
        "scan_km": df.scan.to_numpy(float),
        "track_km": df.track.to_numpy(float),
    }


def fetch_firms_reference(bbox: BBox, span: str = "7d") -> PolarDetections:
    """Every FIRMS VIIRS (S-NPP, NOAA-20, NOAA-21) and MODIS (Aqua, Terra) detection in `bbox`,
    from the keyless South Asia files (`span` 24h, 48h or 7d), with duplicates removed."""
    if span not in ("24h", "48h", "7d"):
        raise ValueError("span must be 24h, 48h or 7d")
    parts: list[dict[str, np.ndarray]] = []
    fetch_ids: list[int] = []
    for family, (folder, prefix) in FIRMS_FILES.items():
        result = fetch(
            "firms",
            FIRMS_CSV.format(dir=folder, prefix=prefix, span=span),
            expect="text",
            timeout=60.0,
            meta={"product": family, "span": span, "purpose": "GK-2A validation reference"},
        )
        cols = parse_firms_csv(result.data, bbox)
        set_fetch_records(result.fetch_id, int(cols["lon"].size) if cols else 0)
        if result.fetch_id is not None:
            fetch_ids.append(result.fetch_id)
        if cols:
            parts.append(cols)
    if not parts:
        return empty_polar(fetch_ids)
    merged = {k: np.concatenate([p[k] for p in parts]) for k in _POLAR_COLUMNS}
    columns = (merged["lon"], merged["lat"], merged["acq_utc"], merged["satellite"])
    keys = [f"{lo:.5f}|{la:.5f}|{t}|{sat}" for lo, la, t, sat in zip(*columns, strict=True)]
    _, first = np.unique(np.array(keys), return_index=True)
    keep = np.sort(first)
    return PolarDetections(**{k: v[keep] for k, v in merged.items()}, fetch_ids=tuple(fetch_ids))
