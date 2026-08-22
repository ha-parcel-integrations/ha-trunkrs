# Working in this repository

Home Assistant custom integration for **Trunkrs** (NL same-day courier) parcel
tracking. Distributed via HACS; not part of HA core. Code-based carrier (no
inbox), built on the GLS/Dragonfly shape. No DTO layer.

## ⚠️ Read TODO.md first (pre-1.0 status vocabulary)

Field mapping is **done** (`carrier-research/trunkrs/api/tracing_details.md`). What remains is the
**status vocabulary**: `_STATUS_MAP` holds exactly one confirmed value,
`SHIPMENT_DELIVERED`; everything else reports `unknown` + a one-shot warning. **Do
not add speculative `SHIPMENT_*` entries** — a wrong guess silently reports the
wrong status, while `unknown` is honest and collects real names from users. Add a
value only when confirmed against a real parcel. See TODO.md.

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
- **`delivered` is hard-coded `False`** while the payload is unmapped, so no parcel
  is wrongly filed as completed and vanishes from view.
- **No pickup sensors** (unlike GLS's `en_route_to_parcel_shop` / `awaiting_pickup`)
  — we don't yet know whether Trunkrs exposes pickup points; two permanently-zero
  sensors are worse than adding them later.
- **Diagnostics is the collection mechanism**, not just debugging: it carries the
  raw payload + a "read this before sharing" note; `TO_REDACT` is deliberately
  broad because the real field names are unknown. The coordinator also logs the
  payload's top-level keys once (`_log_payload_shape`).
- Four bus events + device triggers carrying `device_id`, suppressed on first
  refresh, a change **to** DELIVERED fires only `_delivered`. Entities:
  `has_entity_name` + `translation_key`, `icons.json`, translated units.

## Running tests

```
python -m pytest tests/ --cov=custom_components.trunkrs
```

Coverage must stay **above 95%** (silver `test-coverage` rule). Payload-dependent
paths are tested by injecting *normalised* parcels directly (`test_calendar.py`,
`test_sensor.py`, `test_events.py`) — that verifies finished behaviour despite the
missing mapping.
