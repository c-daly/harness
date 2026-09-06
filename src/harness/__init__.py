"""Harness -- a personal event-sourced multi-model agent harness.

Importing this package must stay side-effect free: it reads installed
distribution metadata and nothing else. Every subsystem lives in a submodule.
"""

from importlib.metadata import PackageNotFoundError, version as _metadata_version

try:
    __version__ = _metadata_version("harness")
except PackageNotFoundError:  # imported from a source tree that was never installed
    __version__ = "0+unknown"

__all__ = ["__version__"]
