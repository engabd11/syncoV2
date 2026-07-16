"""Home-Assistant-harness tests (config flow, setup).

These need ``pytest-homeassistant-custom-component`` (which pins a matching
``homeassistant`` build) and therefore run in CI, not in the lightweight local
environment the pure-DSP tests in ``tests/`` use. They skip cleanly when the
harness isn't installed.
"""

from __future__ import annotations

import os
import sys

import pytest

# Make ``custom_components`` importable when pytest's rootdir is this
# directory (CI invokes ``pytest tests_ha/``).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

pytest.importorskip("pytest_homeassistant_custom_component")


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow loading custom_components/ from this repo checkout."""
    return
