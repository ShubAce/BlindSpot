"""BlindSpot backend: ingest jobs, analytics and the FastAPI service, over one PostGIS database."""

from __future__ import annotations

import importlib
import logging
import pkgutil
from collections.abc import Iterable
from types import ModuleType

__version__ = "0.1.0"

log = logging.getLogger(__name__)


def import_submodules(package: str, path: Iterable[str]) -> tuple[list[ModuleType], dict[str, str]]:
    """Import every public module in a package folder, in name order.

    models/, api/routers/ and jobs/ call this so a new module is picked up just by adding its
    file; nobody edits a shared list. A module that fails to import is logged and returned in
    the error dict instead of taking the whole process down with it.
    """
    modules: list[ModuleType] = []
    failed: dict[str, str] = {}
    for info in sorted(pkgutil.iter_modules(path), key=lambda m: m.name):
        if info.name.startswith("_"):  # skips __main__ and private helpers
            continue
        name = f"{package}.{info.name}"
        try:
            modules.append(importlib.import_module(name))
        except Exception as exc:  # one broken module must not break the others
            log.exception("could not import %s", name)
            failed[name] = f"{type(exc).__name__}: {exc}"
    return modules, failed
