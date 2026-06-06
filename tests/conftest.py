"""Test bootstrap.

These are lightweight pure-logic tests for the DSP/colour/encoder code, which
have no Home Assistant dependency. To import the integration's submodules without
triggering the HA-dependent package ``__init__.py``, we register a stub package
pointing at the integration directory, and shim ``enum.StrEnum`` for Python < 3.11.
"""

from __future__ import annotations

import enum
import os
import sys
import types

if not hasattr(enum, "StrEnum"):
    class _StrEnum(str, enum.Enum):
        def __str__(self) -> str:
            return self.value

    enum.StrEnum = _StrEnum  # type: ignore[attr-defined]

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PKG = os.path.join(_ROOT, "custom_components", "hue_music_sync")

if "hue_music_sync" not in sys.modules:
    _pkg = types.ModuleType("hue_music_sync")
    _pkg.__path__ = [_PKG]
    sys.modules["hue_music_sync"] = _pkg
