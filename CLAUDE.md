# Working in this repository

This is a Home Assistant custom integration for Trunkrs (NL same-day courier)
parcel tracking. Distributed via HACS; not part of HA core.

## ⚠️ Read TODO.md first

This is a **preview release**. The transport is proven but the response
**payload is not mapped yet**, so every parcel reports `unknown`. See
[TODO.md](TODO.md) for exactly what is missing and how to unblock it. Do not
"fix" the unmapped fields by guessing field names — that is the one thing this
repo deliberately refuses to do.

## Always consult HA developer documentation

Home Assistant's integration patterns evolve continuously. **Do not rely on
memory of past patterns** — fetch the canonical page before changing a topic
area, and check the developer blog before introducing anything you only "know"
from training data.

| When you change | Fetch first |
|---|---|
| Entity properties, naming, lifecycle, attributes | https://developers.home-assistant.io/docs/core/entity/ |
| Sensor specifics (state/device classes, units) | https://developers.home-assistant.io/docs/core/entity/sensor |
| Config flow, options flow, reauth, reconfigure | https://developers.home-assistant.io/docs/config_entries_config_flow_handler |
| DataUpdateCoordinator pattern | https://developers.home-assistant.io/docs/integration_fetching_data |
| Quality scale rules | https://developers.home-assistant.io/docs/core/integration-quality-scale |
| Diagnostics | https://developers.home-assistant.io/docs/core/integration/diagnostics |
| Translations | https://developers.home-assistant.io/docs/internationalization/core |

Branding is handled by the local `brand/` folder (HACS reads `icon.png` from
it). The official `home-assistant/brands` repo is for HA Core integrations and
does not apply here.

## The Trunkrs API

Reverse-engineered from the `parcel.trunkrs.nl` consumer tracking SPA.

- **Base** `https://api.trunkrs.app/v2` (note `.app` — the documented V1/V2
  client API on docs.trunkrs.nl is a separate, API-key business product and is
  **not** what this integration uses).
- **Auth** is HTTP **Basic**, username = Trunkrs number, password = receiver
  postcode. There is no account, no token, no API key: **one credential pair
  identifies exactly one parcel**. That is why tracked parcels are stored as
  `{trunkrs_nr, postal_code}` pairs and the auth header is built per request.
- **`GET /tracing/verify`** → 200 valid / 401 invalid. Used to validate before
  storing a parcel — a capability GLS does not have, so unlike GLS this
  integration rejects typos up front.
- **`GET /tracing/details`** → the tracking payload. No query parameters.

## Architecture: built on the GLS/Dragonfly template

Trunkrs is a **code-based** carrier (no inbox), so it follows the GLS shape:

- Single-instance-per-postcode hub; `unique_id` is the postcode, so several
  hubs (home, work) can coexist.
- Setup asks only the postcode; it becomes the default for every parcel added.
- Parcels live in `entry.options[CONF_PARCELS]` and are managed three ways: the
  sectioned options flow, the `track_parcel`/`untrack_parcel` services, or a
  dashboard button calling the service.
- Option changes apply **live** via an options update listener that refreshes
  the coordinator — **not** a reload. That avoids the config-entry-listener
  deprecation and lets per-parcel sensors appear/disappear immediately.
- **First refresh runs in `__init__.py`** before `async_forward_entry_setups`.
  Raising `ConfigEntryNotReady` from a forwarded platform is too late for HA to
  catch cleanly. Do not move it into a platform.
- Per-parcel sensors are removed via the **entity registry**, never by
  self-removal (self-removal races with coordinator-listener cleanup and leaves
  ghost entities). The setup-time cleanup loop filters on
  `entity_entry.domain == "sensor"` so it never deletes the refresh button, and
  excludes the summary/diagnostic unique_ids via `non_parcel_unique_ids`.

## Deliberate decisions

- **`TrunkrsAuthError` is split from `TrunkrsApiError`.** A 401/403 means the
  number/postcode pair is wrong (user-fixable); anything else is an outage
  (retryable). The coordinator logs them differently. Do not collapse them.
- **`async_verify_parcel` returns `True` / `False` / `None`.** `None` means "we
  could not reach Trunkrs" and callers then **accept the parcel anyway** — an
  outage must never stop a user adding a parcel they know is valid. Only a
  definite `False` blocks.
- **`delivered` is hard-coded `False`** while the payload is unmapped, so no
  parcel is ever wrongly filed away as completed and disappears from view.
- **No pickup sensors.** GLS ships `en_route_to_parcel_shop` / `awaiting_pickup`;
  those were left out here because we do not know whether Trunkrs exposes
  pickup points at all. Two permanently-zero sensors are worse than adding them
  later. Revisit when the payload is known.
- **Diagnostics is the collection mechanism**, not just a debugging aid — it
  carries the raw payload plus a note asking users to share it. Its `TO_REDACT`
  is deliberately broad because the real field names are unknown, and the
  output carries an explicit "read this before sharing" warning. Tighten it
  once the payload is documented.
- **The coordinator logs the payload's top-level keys once** (`_log_payload_shape`)
  — a second, lighter-weight collection channel than a full diagnostics upload.

## Suite conventions this repo follows

- Parcels are published in the carrier-agnostic canonical shape shared by every
  integration in the `ha-parcel-integrations` org, so the aggregator can read
  them uniformly. Do not add or rename canonical keys here alone.
- Four bus events (`registered` / `status_changed` / `delivered` /
  `delivery_time_changed`), each also exposed as a device trigger, each
  carrying `device_id`. Events are suppressed on the first refresh. A change
  **to** `DELIVERED` fires only `_delivered`, never also `_status_changed`.
- `has_entity_name = True` + `translation_key` on every entity; names and units
  live in `strings.json` and the translations, icons in `icons.json`. No
  `_attr_name`, no `_attr_icon`, no `_attr_native_unit_of_measurement`.
- One-line commit messages.

## Running tests

```
python -m pytest tests/ --cov=custom_components.trunkrs
```

Coverage must stay **above 95%** (the silver `test-coverage` rule). Currently
101 tests / 98%. Note that the payload-dependent code paths are tested by
injecting *normalised* parcels directly (see `test_calendar.py`,
`test_sensor.py`, `test_events.py`) — that is how the finished behaviour is
verified despite the missing mapping.
