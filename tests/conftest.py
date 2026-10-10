"""Shared fixtures."""

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    """Let HA load custom_components/plug_and_claim in every test.

    The integration depends on recorder, so every test gets HA's in-memory
    test recorder (recorder_mock must be set up before hass).
    """
    yield
