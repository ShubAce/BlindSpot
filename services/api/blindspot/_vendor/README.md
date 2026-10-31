# Vendored native libraries

`libexpat.so.1`: Expat 2.8.1 (MIT, `COPYING.expat`), x86-64, extracted from the conda-forge package
`linux-64/libexpat-2.8.1-hecca717_1.conda` (package sha256
`16feffd9ddbbe5b718515d38ee376c685ba95491cd901244e24671d20b952a77`, as listed by
api.anaconda.org; file sha256 `c995a80395904d85c4d35f4a185c3bae2fcdc562141dd6106c8ebe29571d33b3`).
Its soname is `libexpat.so.1`; it needs only `libc.so.6` and `libm.so.6`, at glibc symbol versions up
to 2.14.

Why: Vercel's Python runtime (Python 3.12, glibc 2.34) has no `libexpat.so.1`, and the GDAL in
rasterio's manylinux wheel needs it, so rasterio could not be imported and the API answered 502 for
every Sentinel-2 picture it had to draw from Earth Search (checked on 2026-10-31 with a probe function
on the same runtime: every other library rasterio's wheel needs was present). `preload_expat()` in
`__init__.py` loads it before rasterio; `infra/deploy_vercel_api.py` copies this folder into the bundle.
The jobs (GitHub's runners) and local development use the system's own library.
