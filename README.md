# Burd Parcel Tracker

[![Release](https://img.shields.io/github/v/release/mikkelke/ha-burd.svg)](https://github.com/mikkelke/ha-burd/releases)
[![Downloads](https://img.shields.io/github/downloads/mikkelke/ha-burd/total.svg)](https://github.com/mikkelke/ha-burd/releases)
[![Marketplace](https://img.shields.io/badge/Marketplace-Custom-41BDF5.svg)](https://hacs.xyz)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> 📦 Part of [**ha-parcel-integrations**](https://ha-parcel-integrations.github.io/), a family of package-tracking integrations for Home Assistant that all publish the same parcel format, statuses and events, so they plug into cross-carrier automations and the optional [Parcel Aggregator](https://github.com/ha-parcel-integrations/ha-parcel-aggregator). Browse the other carriers on the website.
>
> 💬 Questions or feedback? Join the discussion on the [Home Assistant community](https://community.home-assistant.io/t/packages-postnl-dhl-nl-dpd-and-gls-parcel-integration/112433/).

A custom Home Assistant integration that tracks your [Burd](https://burd.dk) evening deliveries in Denmark. No account is needed: paste the Min levering link from the text message or e-mail Burd sends you, or enter the 18-digit shipment number.

> ⚠️ **Pre-1.0 release.** Built from what Burd's own tracking pages read; no real shipment has been followed from registration to delivery yet. Anything the integration does not recognise — a status, a new field, a timestamp without a time zone, or a Min levering link for another shipment — logs one `WARNING` with a ready-made issue link instead of failing silently. Please [report it](https://github.com/mikkelke/ha-burd/issues/new?template=unrecognised_status.yml) when you see one.

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Options](#options)
- [Removal](#removal)
- [Sensors](#sensors)
- [Parcel status reference](#parcel-status-reference)
- [Events](#events)
- [Services](#services)
- [Examples](#examples)
- [Debugging](#debugging)
- [Troubleshooting](#troubleshooting)
- [Related integrations](#related-integrations)
- [Disclaimer](#disclaimer)
- [Contributing](#contributing)
- [License](#license)

## Features

- Track any number of Burd shipments by Min levering link or shipment number — no account needed
- Per-parcel sensor with the canonical status (`registered` / `in_transit` / `out_for_delivery` / `delivered` / …), Burd's own status text (Danish when Home Assistant runs in Danish, English otherwise), the webshop that sent it and a link to Burd's tracking page
- With the Min levering link: the evening Burd plans to deliver, as the parcel's delivery window
- Summary sensors: incoming parcels, next delivery, recently delivered parcels
- Read-only **Deliveries** calendar with the expected delivery windows
- `burd.track_parcel` / `burd.untrack_parcel` services, so a dashboard button can add a parcel
- Events + device triggers for no-code automations (parcel registered, status changed, delivered, delivery time changed)
- Opt-in per-parcel status history
- Manual refresh button and a diagnostic last-update sensor

## Requirements

- Home Assistant 2025.12 or newer
- A Burd shipment and its Min levering link (`https://k.burd.dk/f/…`) from Burd's text message or e-mail, or its 18-digit shipment number — no account needed

## Installation

### Marketplace (recommended)

Click the button below to open this repository in the Marketplace, then install **Burd**:

[![Open your Home Assistant instance and open a repository inside the Marketplace.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=mikkelke&repository=ha-burd&category=integration)

Or add it by hand: in the Marketplace, choose the three-dot menu → **Custom repositories**, add `https://github.com/mikkelke/ha-burd` as an **Integration** and install **Burd**.

Restart Home Assistant if it asks you to.

### Manual

Copy `custom_components/burd` into your `config/custom_components/` folder and restart Home Assistant.

## Configuration

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=burd)

Add the integration via **Settings → Devices & Services → Add Integration → Burd**. There is nothing to fill in: the hub is created immediately (Burd tracking needs no account).

Then add parcels via the integration's **Configure** dialog, the [`burd.track_parcel`](#services) service, or a [dashboard button](examples/dashboards/add_parcel_card.yaml). Each line takes:

| You paste | What you get |
|---|---|
| The Min levering link, `https://k.burd.dk/f/<token>` | Status, history and the planned delivery evening |
| The burd.dk tracking link, `https://burd.dk/track?code=<number>` | Status and history |
| The 18-digit shipment number | Status and history |

The Min levering link is looked up once when you add it, to learn its shipment number; a link Burd does not recognise (for example one that has expired) is refused, and you can enter the shipment number instead. A shipment number is never looked up first: Burd only knows it once the webshop has registered the shipment, until then it shows as `unknown`.

The link carries a token that opens your Min levering page, with your name, address and phone number. The integration stores it in its own options and uses it only to read the delivery window; it never appears in sensor attributes, events, logs or diagnostics. Home Assistant itself records the data of every service call, so a link passed to `burd.track_parcel` ends up in its database and automation traces too; add the parcel in the **Configure** dialog if you do not want that.

## Options

Open **Configure** on the integration entry:

| Section | Option | Default | Description |
|---|---|---|---|
| Parcels | Add / remove | — | Manage the tracked shipments, one per line. A shipment added by link shows as `NUMBER/TOKEN`. Changes apply immediately, no restart. |
| Delivered parcels | Filter by / amount | last 7 days | How long delivered parcels stay visible on the delivered sensor. |
| Parcel history | Include status history | off | Adds a `history` attribute per parcel with each status update. |

Polling isn't one of these settings: the integration polls on a dynamic,
status-driven schedule with nothing to configure.

## Dynamic polling

Polling isn't a setting here — the integration adjusts its own cadence to
what your tracked parcels are actually doing:

- **Quiet hours** — no polling between 00:00–06:00 local time, aside from one
  catch-up check at each end of that window (around midnight and around 6
  AM), so an overnight update is never missed.
- **Hot (every 15 minutes)** — while any tracked parcel is out for delivery
  today, starting an hour before its delivery window opens (or immediately if
  no window is known yet, which is the case for a parcel added by number).
- **Normal (every 45 minutes)** — for anything else still on its way.
- **Fully paused** — once every tracked parcel has been delivered, or nothing
  is tracked at all, polling stops until you add a parcel back (adding one
  always triggers an immediate check, regardless of the pause). A cancelled or
  returned shipment counts as finished 24 hours after Burd set that status.
- A small, fixed per-hub offset is added on top, so not every Burd
  hub out there polls at exactly the same second.

## Removal

Standard HA removal applies: **Settings → Devices & Services → Burd → ⋮ → Delete**. Nothing is stored on Burd's side.

## Sensors

| Entity | Description |
|---|---|
| `sensor.burd_incoming_parcels` | Number of active tracked parcels, full list under the `parcels` attribute |
| `sensor.burd_parcel_<code>` | One per tracked parcel; state is the canonical status, attributes carry the full normalised parcel |
| `sensor.burd_next_delivery` | Earliest expected delivery moment across all active parcels |
| `sensor.burd_delivered_parcels` | Recently delivered parcels (see the retention option) |
| `calendar.burd_deliveries` | Read-only calendar of expected deliveries, no extra API calls |
| `sensor.burd_last_successful_update` | Diagnostic: when Burd was last polled successfully |

A delivered parcel moves from its per-parcel sensor to the delivered sensor automatically. Burd delivers to your door, so there are no pickup-point sensors.

## Parcel status reference

The `status` field is the carrier-agnostic enum shared by the whole integration family:

| Status | Meaning |
|---|---|
| `registered` | The webshop has announced it and Burd does not have it yet, also when Burd expected it today but did not get it |
| `in_transit` | At Burd's terminal, also when the delivery has been moved to another evening |
| `out_for_delivery` | With the courier this evening |
| `delivered` | Delivered |
| `problem` | Not delivered (Burd tries again within two working days before returning it), or the delivery was cancelled |
| `returning` | Going back to the webshop |
| `unknown` | Burd does not know the number yet, or a status we have not mapped yet |

`at_pickup_point` is never reported: Burd delivers to your door.

Burd's own text for the status is always available as `raw_status`, as Burd writes it.

## Events

The integration fires these on the event bus (also available as device triggers on the Burd device):

| Event | When |
|---|---|
| `burd_parcel_registered` | A new parcel appears in the active list |
| `burd_parcel_status_changed` | A parcel's canonical status changes (`old_status` / `new_status` in the payload), except the final hop to delivered |
| `burd_parcel_delivered` | A parcel is delivered |
| `burd_parcel_delivery_time_changed` | The expected delivery window changes |

Every payload is the full normalised parcel plus the hub's `device_id`. Events are suppressed on the first refresh after start-up.

## Services

| Service | Fields | Description |
|---|---|---|
| `burd.track_parcel` | `tracking_code` | Start tracking a shipment; takes the Min levering link, the burd.dk tracking link or the shipment number |
| `burd.untrack_parcel` | `tracking_code` | Stop tracking a shipment; takes the same forms |

## Examples

Ready-to-paste automations and dashboard snippets live in [`examples/`](examples/), including tracking a new parcel straight from a dashboard.

### Community Lovelace cards

Third-party cards that work with this integration's sensors:

- [jonisnet/ha-parcel-card](https://github.com/jonisnet/ha-parcel-card)
- [klaptafel/ha-package-tracker-card](https://github.com/klaptafel/ha-package-tracker-card)

## Debugging

```yaml
logger:
  logs:
    custom_components.burd: debug
```

## Troubleshooting

- **A parcel shows `unknown`** — Burd does not know the number yet (the webshop has not registered the shipment), or the number is wrong. It picks up automatically once Burd has it. A parcel coming from abroad can only be tracked once Burd has received it.
- **No delivery window** — only a parcel added by its Min levering link has one. Re-add it with the link to get it.
- **The log says "Unrecognised Burd data"** — please [open an issue](https://github.com/mikkelke/ha-burd/issues/new?template=unrecognised_status.yml) with the logged line so the integration can handle it.

## Related integrations

This integration is part of [**ha-parcel-integrations**](https://ha-parcel-integrations.github.io/) — a family of
parcel-carrier integrations that all publish the same canonical parcel format,
statuses and events.

- [**Parcel Aggregator**](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) rolls every installed carrier
  up into one set of sensors.
- Browse [the organisation](https://ha-parcel-integrations.github.io/) for the current list of supported carriers.

## Disclaimer

This is an independent, community-built project. It is not affiliated with, endorsed by, sponsored by, or supported by Burd, Home Assistant, or any other third party referenced in this project. Please don't contact Burd for support with this integration.

All third-party trademarks, trade names, product names, logos, and other brand assets are the property of their respective owners. References to them are solely to identify the relevant carrier or service and do not imply affiliation, sponsorship, or endorsement. Nothing in this project grants or implies any licence or right to use third-party brand assets.

This integration may rely on public, unofficial, or undocumented carrier interfaces, accessed with your own account or API key where required. These may change or be withdrawn without notice and may be subject to Burd's terms. Data is sent only to Burd's own services or those of its group; this project operates no servers of its own. You are responsible for ensuring that your use complies with applicable law and those terms. Use is at your own risk; see the [licence](LICENSE) for warranty limitations.

This integration uses the same public tracking endpoints as Burd's own tracking page and Min levering page.

## Contributing

Pull requests and issues are welcome. Please open an issue before
submitting a large change.

## License

[MIT](LICENSE)
