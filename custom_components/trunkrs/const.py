"""Constants for the Trunkrs parcel tracker integration."""
from enum import StrEnum

from homeassistant.const import Platform

DOMAIN = "trunkrs"


class ParcelStatus(StrEnum):
    """Carrier-agnostic parcel status.

    Mirrors the enum the other suite integrations (DHL, DPD, PostNL, GLS,
    Dragonfly) publish on the ``status`` field of each normalised parcel, so
    cross-carrier automations and the aggregator can target
    ``status: out_for_delivery`` regardless of carrier. Listed in roughly the
    order a parcel moves through.
    """

    REGISTERED = "registered"               # Sender announced the parcel; not handed over yet
    IN_TRANSIT = "in_transit"               # In the carrier's network
    OUT_FOR_DELIVERY = "out_for_delivery"   # On a delivery vehicle today
    AT_PICKUP_POINT = "at_pickup_point"     # Waiting at a pickup location
    DELIVERED = "delivered"                 # Handed over
    RETURNING = "returning"                 # Failed delivery, going back to sender
    PROBLEM = "problem"                     # Carrier reports an exception/issue
    UNKNOWN = "unknown"                     # Raw status we have not mapped yet


PLATFORMS = [Platform.BUTTON, Platform.CALENDAR, Platform.SENSOR]

# --- Trunkrs consumer tracking API -----------------------------------------
#
# Reverse-engineered from the parcel.trunkrs.nl consumer tracking SPA. There is
# no account/inbox: a parcel is identified by HTTP **Basic auth**, where the
# username is the Trunkrs number and the password is the receiver's postcode:
#
#     Authorization: Basic base64("<trunkrs_nr>:<postal_code>")
#
# so one credential pair == one parcel. That makes Trunkrs a *code-based*
# carrier, the same model as GLS and Dragonfly.
API_BASE_URL = "https://api.trunkrs.app/v2"

# Returns 200 when the number+postcode pair is valid, 401 when it is not —
# used by the config flow / service to validate before storing a parcel.
VERIFY_URL = f"{API_BASE_URL}/tracing/verify"

# The main tracking payload. Takes no query parameters; the parcel identity
# comes entirely from the Basic auth header.
DETAILS_URL = f"{API_BASE_URL}/tracing/details"

# Consumer tracking page. Trunkrs does not document a deep-link format that we
# have been able to confirm, so this is the plain entry page — the user still
# has to type the number there. Revisit if a deep link is discovered.
TRACKING_URL = "https://parcel.trunkrs.nl/"

# Tracked parcels live in the config entry options as a list of
# ``{trunkrs_nr, postal_code}`` dicts — Trunkrs has no account/feed, the user
# enters the codes themselves.
CONF_PARCELS = "parcels"
CONF_TRUNKRS_NR = "trunkrs_nr"
CONF_POSTAL_CODE = "postal_code"

# Delivered-parcels retention: keep delivered parcels visible for the last N
# days, or keep only the N most recent — mirrors the other suite carriers.
CONF_DELIVERED_FILTER_TYPE = "delivered_filter_type"
CONF_DELIVERED_FILTER_AMOUNT = "delivered_filter_amount"
DEFAULT_DELIVERED_FILTER_TYPE = "days"
DEFAULT_DELIVERED_FILTER_AMOUNT = 7

# Refresh interval (minutes) controls how often the coordinator polls Trunkrs.
# Default 30 min keeps the load on the consumer endpoint gentle; the minimum
# is 15 min for the same reason. Kept identical to the other suite carriers.
CONF_REFRESH_INTERVAL = "refresh_interval"
REFRESH_INTERVAL_OPTIONS = (15, 30, 60, 120, 240)
DEFAULT_REFRESH_INTERVAL = 30

# Per-parcel status history is opt-in and off by default, kept identical to
# the other suite carriers. Trunkrs returns the timeline in the same call, so
# no extra request is involved either way.
CONF_INCLUDE_HISTORY = "include_history"
DEFAULT_INCLUDE_HISTORY = False

# Cap each parcel's history to the most recent N events so the attribute
# stays well under HA's ~16 KB state-attribute limit.
HISTORY_MAX_EVENTS = 20

# Surfaced in the "unrecognised status" warning. Points at the pre-filled issue
# template rather than a blank form, so a user who follows the link from their
# log lands somewhere that already asks the right questions.
NEW_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-trunkrs/issues/new"
    "?template=unrecognised_status.yml"
)
