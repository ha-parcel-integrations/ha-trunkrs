"""pytest configuration for the Trunkrs test suite."""
import pytest

from pytest_homeassistant_custom_component.plugins import hass  # noqa: F401


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Make ``custom_components.trunkrs`` loadable from config-flow / setup tests."""
    yield
