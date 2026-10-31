"""Native libraries the serving runtime lacks (README.md says where each came from).

Vercel's Python runtime has no libexpat.so.1, and the GDAL inside rasterio's wheel needs it, so
`import rasterio` failed there and every Earth Search picture the API had to draw itself answered 502
(Planetary Computer's copies of the 28 Oct 2026 scenes are faulty, so those had no other source).
Loading a library with the soname libexpat.so.1 before rasterio is enough: the dynamic loader reuses
an already loaded library of that name instead of searching the disk.
"""

from __future__ import annotations

import ctypes
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_loaded = False


def preload_expat() -> None:
    """Load libexpat.so.1 before rasterio: the system's where there is one, else the vendored copy.
    A no-op off Linux and after the first call; if both fail, rasterio's import fails as before and
    its caller reports that."""
    global _loaded
    if _loaded or os.name != "posix":
        return
    _loaded = True
    try:
        ctypes.CDLL("libexpat.so.1", mode=ctypes.RTLD_GLOBAL)
    except OSError:
        try:
            ctypes.CDLL(os.path.join(_HERE, "libexpat.so.1"), mode=ctypes.RTLD_GLOBAL)
        except OSError:
            pass
