"""Home-Assistant-harness tests (config flow, setup).

These need ``pytest-homeassistant-custom-component`` (which pins a matching
``homeassistant`` build) and therefore run in CI, not in the lightweight local
environment the pure-DSP tests in ``tests/`` use. They skip cleanly when the
harness isn't installed.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow loading custom_components/ from this repo checkout."""
    return
