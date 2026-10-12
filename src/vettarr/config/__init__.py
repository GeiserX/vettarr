"""Configuration.

- ``schema``, ``loader``, ``writer``, ``components``, ``import_env`` and ``cli``:
  the ``config.yaml`` contract described in docs/CONFIG.md.
- ``runtime.get_config()``: the one ``Config`` the running app reads, from
  ``CONFIG_PATH`` when that file exists, otherwise built in memory from the
  environment variables through the ``import-env`` mapping.
- ``vettarr.config.env``: ``settings``, the flat view over that Config
  the app's modules read (``settings.prowlarr_url``, ``settings.trackers``,
  ...), and ``TrackerConfig``.

``settings`` and ``TrackerConfig`` are reachable as ``vettarr.config.X``,
imported on first use, so ``config check`` and ``config example`` never load
more than they need. Nothing is resolved at import: the Config is read on the
first ``settings`` attribute that needs it.
"""

from __future__ import annotations

import importlib.util
from typing import Any


def __getattr__(name: str) -> Any:
    # Dunders and submodules are never the old module's names; leaving them
    # to the import system keeps `from vettarr.config import cli` from
    # importing the environment loader.
    if name.startswith("__") or importlib.util.find_spec(f"{__name__}.{name}") is not None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from vettarr.config import env

    try:
        return getattr(env, name)
    except AttributeError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
