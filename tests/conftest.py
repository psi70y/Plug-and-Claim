"""Shared fixtures."""

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    """Let HA load custom_components/plug_and_claim in every test.

    The integration depends on recorder, so every test gets HA's in-memory
    test recorder (recorder_mock must be set up before hass).
    """
    yield


@pytest.fixture(autouse=True)
def mock_github_releases(aioclient_mock):
    """Never reach GitHub from tests: the new-version check gets a 404
    unless a test registers its own reply (see test_update_check.py)."""
    from custom_components.plug_and_claim.update_check import LATEST_RELEASE_URL

    aioclient_mock.get(LATEST_RELEASE_URL, status=404)
    return aioclient_mock
