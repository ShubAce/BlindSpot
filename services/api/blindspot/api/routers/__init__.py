"""One router module per area (fires.py, orbits.py, scars.py, ...), each owned by its module.

Any module in this folder that defines `router = APIRouter(prefix="/api/<area>", ...)` is mounted
by blindspot.api.main automatically; nobody edits a shared list. A module that fails to import
is logged and skipped, so one broken area cannot take the rest of the API down.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

from blindspot import import_submodules

log = logging.getLogger(__name__)


def discover_routers() -> tuple[list[APIRouter], dict[str, str]]:
    """The `router` of every module in this package, and {module: error} for those that failed."""
    modules, failed = import_submodules(__name__, __path__)
    routers = []
    for module in modules:
        router = getattr(module, "router", None)
        if isinstance(router, APIRouter):
            routers.append(router)
        else:
            log.warning("%s has no `router = APIRouter(...)`; not mounted", module.__name__)
    return routers, failed
