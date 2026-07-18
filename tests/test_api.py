"""Tests for the Trunkrs API client."""
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.trunkrs.api import (
    TrunkrsApiClient,
    TrunkrsApiError,
    TrunkrsAuthError,
)


def _session(status: int, json_value=None, json_error: bool = False) -> MagicMock:
    response = AsyncMock()
    response.status = status
    if json_error:
        response.json = AsyncMock(side_effect=ValueError("bad json"))
    else:
        response.json = AsyncMock(return_value=json_value)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get = MagicMock(return_value=ctx)
    return session


# --- auth ------------------------------------------------------------------


async def test_basic_auth_is_number_and_postcode():
    """The credential pair IS the parcel identity: user=number, pass=postcode."""
    session = _session(200, {"ok": True})
    client = TrunkrsApiClient(session)
    await client.async_get_parcel("TR123", "1234 AB")

    auth = session.get.call_args.kwargs["auth"]
    assert isinstance(auth, aiohttp.BasicAuth)
    assert auth.login == "TR123"
    # spaces are stripped so "1234 AB" and "1234AB" address the same parcel
    assert auth.password == "1234AB"


# --- verify ----------------------------------------------------------------


async def test_verify_true_on_200():
    client = TrunkrsApiClient(_session(200))
    assert await client.async_verify("TR123", "1234AB") is True


@pytest.mark.parametrize("status", [401, 403])
async def test_verify_false_on_auth_status(status):
    client = TrunkrsApiClient(_session(status))
    assert await client.async_verify("TR123", "1234AB") is False


async def test_verify_raises_on_server_error():
    """A 5xx is an outage, not a bad parcel number — must not report False."""
    client = TrunkrsApiClient(_session(500))
    with pytest.raises(TrunkrsApiError):
        await client.async_verify("TR123", "1234AB")


# --- details ---------------------------------------------------------------


async def test_get_parcel_returns_payload():
    client = TrunkrsApiClient(_session(200, {"some": "payload"}))
    assert await client.async_get_parcel("TR123", "1234AB") == {"some": "payload"}


@pytest.mark.parametrize("status", [401, 403])
async def test_get_parcel_raises_auth_error(status):
    """A bad pair raises the auth subclass so the coordinator can say so."""
    client = TrunkrsApiClient(_session(status))
    with pytest.raises(TrunkrsAuthError):
        await client.async_get_parcel("TR123", "1234AB")


async def test_get_parcel_raises_api_error_on_server_error():
    client = TrunkrsApiClient(_session(503))
    with pytest.raises(TrunkrsApiError) as err:
        await client.async_get_parcel("TR123", "1234AB")
    assert not isinstance(err.value, TrunkrsAuthError)


async def test_get_parcel_returns_none_on_unparseable_body():
    client = TrunkrsApiClient(_session(200, json_error=True))
    assert await client.async_get_parcel("TR123", "1234AB") is None


async def test_get_parcel_propagates_network_error():
    session = MagicMock()
    session.get = MagicMock(side_effect=aiohttp.ClientError("boom"))
    client = TrunkrsApiClient(session)
    with pytest.raises(aiohttp.ClientError):
        await client.async_get_parcel("TR123", "1234AB")
