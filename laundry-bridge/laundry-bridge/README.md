# laundry-bridge

LG ThinQ Connect (official API, MQTT push) → Control4 **Laundry Monitor** driver.

Fires `POST /dryer/started`, `/dryer/done`, `/washer/done` to the driver with
`Authorization: Bearer <secret>` on real state transitions only.

## Setup

1. **LG token**: create a Personal Access Token on the LG ThinQ developer site
   (https://smartsolution.developer.lge.com → ThinQ Connect → PAT) with
   *view all devices* and *view all device statuses*. Control scope is not needed.
2. **Secrets**
   ```sh
   mkdir -p secrets && chmod 700 secrets
   printf '%s' 'YOUR_LG_PAT'        > secrets/thinq_pat
   printf '%s' 'SAME_AS_DRIVER_PROP' > secrets/c4_secret   # e.g. openssl rand -hex 32
   chmod 600 secrets/*
   ```
3. **Config**: `cp .env.example .env` and set `C4_URL` to the controller IP and
   the driver's Listen Port.
4. **Run**
   ```sh
   docker compose up -d --build
   docker compose logs -f
   ```
   You should see `Watching <your dryer> (DEVICE_DRYER)` and `MQTT connected`.
5. In the driver properties, set **Allowed Source IP** to this Docker host's LAN IP.

## Behaviour

- Push events over MQTT; a status poll on every (re)connect and every
  `RESYNC_SECONDS` (default 15 min) as a safety net.
- First state seen after startup is recorded, not announced (no stale
  "dryer done" on container restart).
- `active -> END` fires done; `idle -> active` fires started (dryer).
  LG's own "...COMPLETE" push notification also fires done; the driver
  debounces duplicates.
- A stable client id is kept in the `/data` volume. LG warns that creating
  many clients can get API calls blocked, so don't delete the volume casually.

## Testing the Control4 side without the dryer

```sh
curl -X POST -H "Authorization: Bearer $(cat secrets/c4_secret)" http://<controller-ip>:8765/dryer/done
```

## Troubleshooting

- `No matching devices` — the log lists the device types on your account;
  add the right one to `DEVICE_TYPES` (washtower units report differently).
- `LOG_LEVEL=DEBUG` dumps raw MQTT messages so you can see the exact
  `currentState` values your model sends.
