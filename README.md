# Laundry Monitor

Whole-house Control4 announcements when the LG dryer (or washer) finishes.

```
LG dryer ──► LG ThinQ cloud ──MQTT push──► laundry-bridge (Docker)
                                                │  POST /dryer/done
                                                │  Authorization: Bearer <secret>
                                                ▼
                                   Laundry Monitor driver (Control4 controller)
                                                │  fires Composer events
                                                ▼
                         Dryer Done - Announce ──► Announcement in selected rooms
```

Two components:

| Component | Runs on | Files |
|---|---|---|
| **laundry-bridge** | Docker host | `laundry-bridge.zip` (Dockerfile, compose, Python service) |
| **Laundry Monitor driver** | Control4 controller | `laundry_monitor.c4z` |

The bridge holds the LG token and talks to LG. The controller never sees LG
credentials; it only accepts authenticated calls from the bridge on the LAN.

---

## 1. Prerequisites

- Composer Pro access
- A Docker host on the same LAN as the controller
- LG ThinQ account with the dryer registered in the ThinQ app
- DHCP reservations or static IPs for the controller and the Docker host (both IPs are configured below) highly recommended

## 2. Generate the shared secret

One random value is shared by the driver and the bridge:

```sh
openssl rand -hex 32
```

Keep it handy for steps 3 and 5.

## 3. Install the Control4 driver

1. Copy `laundry_monitor.c4z` to `Documents\Control4\Drivers` on the Composer PC,
   or use **Driver → Add or Update Driver**.
2. **System Design** → search **Laundry Monitor** under *Local* → add it to a room
   (e.g. Laundry).
3. Set the properties:

| Property | Value | Notes |
|---|---|---|
| Listen Port | `8765` | Change only if the port is in use |
| Shared Secret | value from step 2 | All requests are rejected until this is set |
| Allowed Source IP | Docker host LAN IP | Blank allows any source (not recommended) |
| Debounce Seconds | `120` | Ignores repeat events inside this window |
| Announce Start | `09:00` | 24-hour, controller local time |
| Announce End | `19:00` | Windows crossing midnight (e.g. `22:00`–`06:00`) are supported |
| Driver Status | *read-only* | Should show `Listening on port 8765` |
| Last Event | *read-only* | Last event fired and when |

Times follow the controller's local time zone (set by the project location), so
DST is handled automatically. A malformed time fails open (announces anyway).

### Events

| Event | Fires when |
|---|---|
| Dryer Started | Dryer goes from idle to running |
| Dryer Done | Dryer finishes (always) |
| Dryer Done - Announce | Dryer finishes inside the announce window |
| Dryer Done - Quiet Hours | Dryer finishes outside the announce window |
| Washer Done | Washer finishes (always) |
| Washer Done - Announce | Washer finishes inside the announce window |
| Washer Done - Quiet Hours | Washer finishes outside the announce window |
| Auth Failure | Request rejected: wrong secret or source IP |

### Variables

| Variable | Values |
|---|---|
| `DRYER_STATE` | `idle`, `running`, `done` |
| `WASHER_STATE` | `idle`, `done` |
| `IN_ANNOUNCE_WINDOW` | `1` / `0`, set on each done event |

### Endpoints

All `POST`, with header `Authorization: Bearer <Shared Secret>`:

| Path | Effect |
|---|---|
| `/dryer/started` | Dryer Started; `DRYER_STATE=running` |
| `/dryer/done` | Dryer Done + Announce/Quiet Hours; `DRYER_STATE=done` |
| `/washer/done` | Washer Done + Announce/Quiet Hours; `WASHER_STATE=done` |

Responses: `200 ok`, `200 debounced`, `401` (auth/IP), `404` (unknown path),
`400` (malformed or non-POST), `413` (request over 8 KB).

## 4. Program the announcement

1. **Agents → Announcements** → add an announcement (recorded clip or TTS)
   such as "The dryer is done." Choose rooms and volume.
2. **Programming** → select **Laundry Monitor** → **Dryer Done - Announce**
   → drag in the announcement.
3. Optional: on **Dryer Done - Quiet Hours**, add a silent cue (keypad LED,
   touchscreen notification).
4. Optional: on **Auth Failure**, send yourself a push. Unexpected failures mean
   something else on the LAN is calling the driver.
5. Save / refresh navigators.

### Test the driver before the bridge exists

From any machine whose IP matches **Allowed Source IP**:

```sh
curl -X POST -H "Authorization: Bearer <secret>" http://<controller-ip>:8765/dryer/done
```

Expect `ok`, an updated **Last Event**, and the announcement (if inside the window).
A second call within 120 s returns `debounced`.

## 5. Deploy the bridge

### LG token

1. Open **https://connect-pat.lgthinq.com** and sign in with the LG account the dryer is on.
2. **ADD NEW TOKEN** → name it `laundry-bridge`.
3. Select only:
   - Permission to view all devices
   - Permission to view all device statuses
4. Create and copy the token. Revoke it on the same page if it ever leaks.

### Files

```
laundry-bridge/
├── .env                  ← non-secret config (from .env.example)
├── secrets/
│   ├── thinq_pat         ← LG token
│   └── c4_secret         ← same value as the driver's Shared Secret
├── docker-compose.yml
├── Dockerfile
└── app/
    ├── bridge.py
    └── requirements.txt
```

```sh
unzip laundry-bridge.zip && cd laundry-bridge
mkdir -p secrets && chmod 700 secrets
printf '%s' 'YOUR_LG_TOKEN'   > secrets/thinq_pat
printf '%s' 'YOUR_C4_SECRET'  > secrets/c4_secret
chmod 600 secrets/*
cp .env.example .env
```

### `.env`

```env
THINQ_COUNTRY=US
C4_URL=http://192.168.1.50:8765
DEVICE_TYPES=DEVICE_DRYER,DEVICE_WASHER
RESYNC_SECONDS=900
LOG_LEVEL=INFO
```

| Setting | Meaning |
|---|---|
| `THINQ_COUNTRY` | Country of the LG account; must match the token |
| `C4_URL` | Controller IP and driver Listen Port, no trailing slash |
| `DEVICE_TYPES` | ThinQ device types to watch; `DEVICE_DRYER` alone for dryer only |
| `RESYNC_SECONDS` | Backup status poll interval; `0` disables |
| `LOG_LEVEL` | `DEBUG` logs raw MQTT messages |

Any setting can be supplied as `NAME_FILE=/path` instead; compose does this for
the two secrets.

### Run

```sh
docker compose up -d --build
docker compose logs -f
```

Healthy startup:

```
Watching Dryer (DEVICE_DRYER)
Dryer: None -> POWER_OFF (startup)
MQTT connected
```

## 6. How the bridge decides

- **Push first.** The LG SDK registers a client, obtains a certificate, and
  subscribes to the account's MQTT topic. Status changes arrive in seconds.
- **Resync.** A status poll runs on every (re)connect and every `RESYNC_SECONDS`.
- **Transitions only.**
  - running → `END` sends `/dryer/done`
  - idle → running sends `/dryer/started`
  - LG's own `…COMPLETE` push notification also sends done (the driver debounces duplicates)
  - Idle states: `POWER_OFF`, `INITIAL`, `END`, `SLEEP`, `ERROR`, `DETECTING`. Anything else counts as running.
- **No stale alerts.** The first state seen after a restart is recorded, not announced.
- **Retries.** Posts to Control4 retry 3× with backoff; `401`/`404` are not retried.
- **Stable client ID.** Stored in the `laundry-data` volume. LG may block accounts
  that create many clients, so don't delete the volume routinely.

## 7. Security notes

- **LG token:** read-only scopes, stored as a Docker secret, never in `.env` or the image.
- **Container:** non-root, read-only root filesystem, all capabilities dropped,
  `no-new-privileges`, 256 MB limit, heartbeat healthcheck.
- **Driver:** rejects everything until a secret is set; source-IP allowlist;
  8 KB request cap; length-independent secret comparison.
- **Transport:** bridge → controller is plain HTTP on the LAN, so the bearer
  secret is visible to anyone sniffing that segment. Keep the controller and
  Docker host on a trusted VLAN and never port-forward the driver port.


## 8. Troubleshooting

| Symptom | Check |
|---|---|
| Driver Status stuck at `Starting` | Composer Lua tab → Print output; confirm the port is free |
| `401` from curl | Secret mismatch, or caller IP ≠ Allowed Source IP |
| Bridge: `Cannot list ThinQ devices` | Token wrong/revoked, or country mismatch |
| Bridge: `No matching devices` | Log lists the account's device types; adjust `DEVICE_TYPES` (WashTower units differ) |
| Bridge: `API calls exceeded` | LG rate limit; clears on its own |
| Cycle ends, nothing announced | `LOG_LEVEL=DEBUG` and check the `currentState` your model sends; if it isn't `END`, add it to `DONE_STATES` in `bridge.py` |
| Announced at night | Programming still attached to **Dryer Done** instead of **Dryer Done - Announce** |
| Event fires but no audio | Announcement agent rooms/volume; test the announcement directly in Composer |
