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

# Every optional key the parcel contract defines. CAPABILITIES below must be a
# subset of this — it exists so a typo in CAPABILITIES fails a test instead of
# silently dropping this carrier off a table on the docs site.
KNOWN_CAPABILITIES = frozenset(
    {"weight", "dimensions", "delivery_window", "pickup_point", "url", "history"}
)

# Which optional contract fields this carrier's API actually populates — feeds
# the comparison table on the docs site. Keep in lockstep with
# normalize_parcel() in parcels.py: everything not listed here comes back as a
# literal None there. Trunkrs is a home-delivery courier with no pickup point,
# weight or dimensions in the payload; ``timeSlot`` does give a real delivery
# window.
CAPABILITIES = frozenset({"delivery_window", "url", "history"})

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
# Standard service field name shared by every parcel-suite carrier.
CONF_TRACKING_CODE = "tracking_code"

# Delivered-parcels retention: keep delivered parcels visible for the last N
# days, or keep only the N most recent — mirrors the other suite carriers.
CONF_DELIVERED_FILTER_TYPE = "delivered_filter_type"
CONF_DELIVERED_FILTER_AMOUNT = "delivered_filter_amount"
DEFAULT_DELIVERED_FILTER_TYPE = "days"
DEFAULT_DELIVERED_FILTER_AMOUNT = 7

# Dynamic, status-driven polling — unconditional, no user-facing interval
# option. See carrier-research/dynamic-polling.md for the full algorithm and
# the reasoning behind it.
#
# Quiet window: no polling between these local hours except the two anchors
# below, for overnight / end-of-day catch-up.
QUIET_WINDOW_START_HOUR = 0
QUIET_WINDOW_END_HOUR = 6

# Cadence while polling is active (minutes). Hot = at least one tracked,
# not-yet-delivered parcel is out_for_delivery within HOT_LOOKAHEAD_HOURS of
# its planned_from (or has no planned_from at all); mid = anything else still
# in flight. This is a barcode-based coordinator (Section 2.1): when every
# tracked parcel is delivered, or nothing is tracked, polling stops entirely
# instead of falling to the mid tier — see coordinator.py's
# ``_hottest_tier_minutes``.
HOT_INTERVAL_MINUTES = 15
MID_INTERVAL_MINUTES = 45
HOT_LOOKAHEAD_HOURS = 1

# Small, stable per-install offset added to every computed interval so
# different installs don't all hit an anchor or tier boundary at the same
# second. Deterministic (hash of the config entry id), not random.
STAGGER_MINUTES = 7

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
