"""Every table lives in this package, one file per module (fires.py, air.py, sentinel.py, ...).

Importing the package imports every file in it, so Base.metadata knows all tables before
init_db() runs create_all. A new module only adds its own file here; nothing else changes.
"""

from blindspot import import_submodules

# Core tables first, so other model files can `from blindspot.models import SourceFetch`.
from blindspot.models.core import Boundary, JobRun, Place, SourceFetch

MODULES, FAILED = import_submodules(__name__, __path__)

__all__ = ["FAILED", "MODULES", "Boundary", "JobRun", "Place", "SourceFetch"]
