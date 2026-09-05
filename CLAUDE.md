# Working in this repository

Home Assistant custom integration for **Trunkrs** (NL same-day courier) parcel
tracking. Distributed via HACS; not part of HA core. Code-based carrier (no
inbox), built on the GLS/Dragonfly shape. No DTO layer.

## ⚠️ Status vocabulary is still incomplete

Field mapping is **done** (`carrier-research/trunkrs/api/tracing_details.md`). What remains is the
**status vocabulary**: `_STATUS_MAP` (`parcels.py`) holds four confirmed values —
`SHIPMENT_DELIVERED`, `SHIPMENT_SORTED` (#5), `SHIPMENT_ACCEPTED_BY_DRIVER`
and `SHIPMENT_SORTED_AT_SUB_DEPOT` (both #6) — mapping to `DELIVERED`,
`IN_TRANSIT`, `OUT_FOR_DELIVERY` and `IN_TRANSIT` respectively; everything else
reports `unknown` + a one-shot warning. **Do not add speculative `SHIPMENT_*`
entries** — a wrong guess silently reports the wrong status, while `unknown` is
honest and collects real names from users. Add a value only when confirmed
against a real parcel.

## ⚠️ Version 1.0.0 decision — status vocabulary is still incomplete

History is 0.11.0 → 0.12.0 → 1.0.0, so this was **not** a first release and
the "first release may jump straight to 1.0.0" exception in
`CONVENTIONS.md` does not apply. The normal bar — status vocabulary
complete, mapping cleanly onto every canonical `ParcelStatus` — is not met:
`_STATUS_MAP` (`parcels.py`) covers three of the eight canonical statuses
(DELIVERED, IN_TRANSIT, OUT_FOR_DELIVERY, from four confirmed raw values).
RETURNING and PROBLEM have zero confirmed raw triggers, and the whole
failed/not-delivered vocabulary (`reasonCode`, see `_note_reason_code`) is
unconfirmed.

**Decision: the version stays at 1.0.0 anyway.** The confirmed values cover
the normal happy-path delivery flow (sorted → out for delivery →
delivered), and every unmapped raw status already degrades safely to
`unknown` plus a one-shot warning rather than a silently wrong guess — the
same pre-1.0 discipline carried forward unchanged, version bump aside. This
is the maintainer knowingly accepting that `status_vocab` is not provably
complete, not a claim that the vocabulary is finished — treat RETURNING and
PROBLEM as still-open gaps, not settled non-issues, and keep applying the
"don't guess, wait for a confirmed value" rule above to them.

## Shared conventions — fetch when relevant

Suite-wide rules live in
[`.github/CONVENTIONS.md`](https://github.com/ha-parcel-integrations/.github/blob/main/CONVENTIONS.md)
and are **not** repeated here. Don't fetch it every session — fetch it **before**
you act in one of these areas:

| Before you … | Fetch `CONVENTIONS.md` § |
|---|---|
| touch entities, sensors, config/options flow, coordinator, diagnostics, translations | *Home Assistant developer docs* (its table points on to the canonical HA page — don't rely on memory) |
| add/rename a parcel field, a `ParcelStatus`, or a bus event; change first-refresh or unmapped-status logging | *Parcel contract* (this repo implements it; below is only where Trunkrs deviates) |
| ship anything while below 1.0.0 (status vocabulary unconfirmed) | *Pre-1.0 releases* — one-shot WARNINGs for every guessed status |
| consider "fixing" a lint/pattern the skill flags (poll interval, inline client) | *Deliberate skill divergences* |
| commit, bump, tag, release, or write release notes; add a feature without a test | *Workflow / Commits / Versioning / Testing* |

**API mechanics live in `carrier-research/trunkrs/api/` (private research repo)** — the `.app` base,
the Basic-auth scheme, the `/tracing/verify` and `/tracing/details` endpoints, and
the payload→canonical mapping. Do not duplicate them here.

**Suite-wide tripwires, kept inline on purpose:**
- **First refresh in `__init__.py`, before `async_forward_entry_setups`** — from
  a forwarded platform HA can't catch `ConfigEntryNotReady` cleanly.
- **Setup cleanup filters `domain == "sensor"` and excludes
  `non_parcel_unique_ids`** — else it deletes the refresh button / diagnostic
  sensors. Per-parcel sensors are removed via the entity registry (self-removal
  races and leaves ghosts).

## Carrier-specific decisions (integration only)

- **One credential pair identifies exactly one parcel** (Trunkrs number + receiver
  postcode, HTTP Basic — no account/token/key), so tracked parcels are stored as
  `{trunkrs_nr, postal_code}` pairs and postcode-keyed hubs (`unique_id` = postcode)
  let several hubs coexist (`single_config_entry` deliberately absent). Setup asks
  only the postcode (the default for every added parcel). Parcels live in
  `entry.options[CONF_PARCELS]`, managed three ways (options flow,
  `track_parcel`/`untrack_parcel` services, a dashboard button). Option changes
  apply **live** via an options update listener that refreshes the coordinator
  (**not** a reload — avoids the config-entry-listener deprecation).
- **Service field is `tracking_code`** (suite standard). The old `trunkrs_nr`
  service-field alias was removed (#2). The *stored* dict key stays `trunkrs_nr` —
  that's an internal storage key, not the service field, and was never part of
  the deprecation; don't conflate them.
- **`TrunkrsAuthError` is split from `TrunkrsApiError`**: a 401/403 means the
  number/postcode pair is wrong (user-fixable); anything else is an outage
  (retryable). Do not collapse them.
- **`async_verify_parcel` returns `True`/`False`/`None`.** `None` = "couldn't reach
  Trunkrs" → callers **accept the parcel anyway** (an outage must never stop a user
  adding a valid parcel). Only a definite `False` blocks. (Verify-before-store is a
  capability GLS lacks, so unlike GLS this rejects typos up front.)
- **`delivered` is `raw_status == STATE_DELIVERED`** (`parcels.py`) — a real
  check against the one confirmed terminal state, not a guess. An unmapped
  status still reports `delivered: False`, so no parcel is wrongly filed as
  completed and vanishes from view.
- **No pickup sensors** (unlike GLS's `en_route_to_parcel_shop` / `awaiting_pickup`)
  — we don't yet know whether Trunkrs exposes pickup points; two permanently-zero
  sensors are worse than adding them later.
- **Diagnostics is the collection mechanism**, not just debugging: it carries the
  raw payload + a "read this before sharing" note; `TO_REDACT` is deliberately
  broad because the real field names are unknown.
- Four bus events + device triggers carrying `device_id`, suppressed on first
  refresh, a change **to** DELIVERED fires only `_delivered`. Entities:
  `has_entity_name` + `translation_key`, `icons.json`, translated units.

## Polling

Polling is dynamic and status-driven, unconditionally — there is no
user-facing interval option. The coordinator recomputes its own cadence at
the end of every refresh: a quiet window (00:00–06:00 local, with catch-up
anchors at each end), a 15-minute hot tier when a tracked parcel is
`out_for_delivery` (immediately, or from an hour before `planned_from`), a
45-minute mid tier otherwise, and a full stop (`update_interval = None`)
when nothing is tracked or everything tracked is delivered. `SHIPMENT_ACCEPTED_BY_DRIVER`
maps to `out_for_delivery` (confirmed in issue #6, see the status vocabulary
note above), so the hot tier is live: a tracked parcel in that raw state
gets 15-minute polling immediately if no `planned_from` is known, or once
`now` is within `HOT_LOOKAHEAD_HOURS` (1h) of `planned_from`/`planned_to`
(`timeSlot.from`/`to`, falling back to `low`/`high`). Every other active
status — including `unknown` — lands on the mid tier. See `coordinator.py`'s
`_hottest_tier_minutes` / `_next_update_interval` and `ha-carrier-template`'s
`example_carrier/coordinator.py` for the canonical shape this mirrors.

## Running tests

```
python -m pytest tests/ --cov=custom_components.trunkrs
```

Coverage must stay **above 95%** (silver `test-coverage` rule). Payload-dependent
paths are tested by injecting *normalised* parcels directly (`test_calendar.py`,
`test_sensor.py`, `test_events.py`) — that verifies finished behaviour despite the
missing mapping.
