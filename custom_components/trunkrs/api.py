"""Trunkrs consumer tracking API client.

No account and no API key: a parcel is addressed by HTTP Basic auth, where the
username is the Trunkrs number and the password is the receiver's postcode
(see ``const.py``). One credential pair identifies exactly one parcel.
"""
from __future__ import annotations

import logging
from typing import Any

import aiohttp

from .const import DETAILS_URL, VERIFY_URL

_LOGGER = logging.getLogger(__name__)


class TrunkrsApiError(Exception):
    """Raised when a Trunkrs API call returns an unexpected status."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"Trunkrs API request failed with status {status_code}")
        self.status_code = status_code


class TrunkrsAuthError(TrunkrsApiError):
    """Raised when Trunkrs rejects the number/postcode pair (401/403).

    Deliberately split from :class:`TrunkrsApiError`: an invalid pair is a
    permanent, user-fixable problem (wrong number or postcode), while any other
    non-200 is a transient service problem that should be retried. Do not
    collapse the two — a Trunkrs outage must not look like a bad parcel number.
    """


class TrunkrsApiClient:
    """Client for the Trunkrs consumer tracking endpoints."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Initialise the client with an aiohttp session."""
        self._session = session

    @staticmethod
    def _auth(trunkrs_nr: str, postal_code: str) -> aiohttp.BasicAuth:
        """Build the Basic auth credentials that identify one parcel."""
        return aiohttp.BasicAuth(trunkrs_nr, postal_code.replace(" ", ""))

    async def async_verify(self, trunkrs_nr: str, postal_code: str) -> bool:
        """Return whether the number/postcode pair is known to Trunkrs.

        ``True`` on HTTP 200, ``False`` on 401/403. Any other non-200 raises
        :class:`TrunkrsApiError` so a service outage is not reported to the
        user as "invalid parcel number".
        """
        async with self._session.get(
            VERIFY_URL, auth=self._auth(trunkrs_nr, postal_code)
        ) as response:
            if response.status == 200:
                return True
            if response.status in (401, 403):
                return False
            raise TrunkrsApiError(response.status)

    async def async_get_parcel(
        self, trunkrs_nr: str, postal_code: str
    ) -> dict[str, Any] | None:
        """Fetch one parcel's tracking details.

        Returns the parsed JSON payload, or ``None`` when the body is empty.
        A 401/403 raises :class:`TrunkrsAuthError` (bad pair); any other
        non-200 raises :class:`TrunkrsApiError`. Network errors propagate as
        ``aiohttp.ClientError`` — the coordinator handles both.
        """
        async with self._session.get(
            DETAILS_URL, auth=self._auth(trunkrs_nr, postal_code)
        ) as response:
            if response.status in (401, 403):
                raise TrunkrsAuthError(response.status)
            if response.status != 200:
                raise TrunkrsApiError(response.status)
            # Content-Type has not been verified against a live response, so
            # parse leniently rather than insisting on application/json.
            try:
                return await response.json(content_type=None)
            except ValueError as err:
                _LOGGER.warning(
                    "Trunkrs returned an unparseable body for %s: %s", trunkrs_nr, err
                )
                return None
