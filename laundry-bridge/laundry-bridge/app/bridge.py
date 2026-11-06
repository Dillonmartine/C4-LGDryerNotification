"""
laundry-bridge: LG ThinQ Connect -> Control4 Laundry Monitor driver.

Subscribes to ThinQ Connect MQTT push events for the LG washer/dryer and
POSTs to the Control4 driver when a cycle starts or finishes.

Config (environment; any VAR can instead be given as VAR_FILE=/path):
  THINQ_PAT        LG ThinQ personal access token (required)
  THINQ_COUNTRY    Two-letter country code (default US)
  C4_URL           Driver base URL, e.g. http://192.168.1.50:8765 (required)
  C4_SECRET        Shared Secret configured on the driver (required)
  DEVICE_TYPES     Comma list of ThinQ device types to watch
                   (default DEVICE_DRYER,DEVICE_WASHER)
  RESYNC_SECONDS   Safety status poll interval (default 900; 0 disables)
  STATE_DIR        Persistent dir for client id (default /data)
  LOG_LEVEL        INFO / DEBUG (default INFO)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from aiohttp import ClientSession, ClientTimeout

from thinqconnect.mqtt_client import ThinQMQTTClient
from thinqconnect.thinq_api import ThinQApi

LOG = logging.getLogger("laundry-bridge")

# Run states that mean "not running a cycle". Anything else counts as active.
IDLE_STATES = {"POWER_OFF", "INITIAL", "END", "SLEEP", "ERROR", "DETECTING", ""}
DONE_STATES = {"END"}

# device type -> (started path or None, done path)
ROUTES = {
    "DEVICE_DRYER": ("/dryer/started", "/dryer/done"),
    "DEVICE_WASHER": (None, "/washer/done"),
    "DEVICE_WASHTOWER_DRYER": ("/dryer/started", "/dryer/done"),
    "DEVICE_WASHTOWER_WASHER": (None, "/washer/done"),
}


def env(name: str, default: str | None = None, required: bool = False) -> str | None:
    file_var = os.environ.get(f"{name}_FILE")
    if file_var:
        return Path(file_var).read_text().strip()
    val = os.environ.get(name, default)
    if required and not val:
        LOG.critical("Missing required setting %s", name)
        sys.exit(2)
    return val


def find_run_state(obj: Any) -> str | None:
    """Pull runState.currentState out of a status/report payload of any shape."""
    if isinstance(obj, dict):
        rs = obj.get("runState")
        if isinstance(rs, dict) and "currentState" in rs:
            return str(rs["currentState"])
        for v in obj.values():
            found = find_run_state(v)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = find_run_state(v)
            if found is not None:
                return found
    return None


class C4Client:
    def __init__(self, session: ClientSession, base_url: str, secret: str):
        self._session = session
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {secret}"}

    async def post(self, path: str) -> bool:
        url = self._base + path
        for attempt in range(1, 4):
            try:
                async with self._session.post(
                    url, headers=self._headers, data=b"", timeout=ClientTimeout(total=5)
                ) as r:
                    if r.status == 200:
                        LOG.info("Control4 <- %s (%s)", path, (await r.text()).strip())
                        return True
                    LOG.error("Control4 rejected %s: HTTP %s", path, r.status)
                    if r.status in (401, 404):
                        return False  # config problem; retrying won't help
            except Exception as err:  # noqa: BLE001
                LOG.warning("Control4 post %s failed (attempt %d): %s", path, attempt, err)
            await asyncio.sleep(2 ** attempt)
        return False


class Tracker:
    """Per-device state machine: fires only on real transitions."""

    def __init__(self, c4: C4Client, devices: dict[str, dict]):
        self._c4 = c4
        self._devices = devices          # device_id -> {"type":..., "alias":...}
        self._state: dict[str, str | None] = {}
        self._lock = asyncio.Lock()

    async def update(self, device_id: str, new_state: str | None, source: str) -> None:
        dev = self._devices.get(device_id)
        if dev is None or new_state is None:
            return
        async with self._lock:
            old = self._state.get(device_id)
            self._state[device_id] = new_state
        if old == new_state:
            return
        LOG.info("%s: %s -> %s (%s)", dev["alias"], old, new_state, source)
        if old is None:
            return  # first observation after startup: record, don't announce
        started_path, done_path = ROUTES[dev["type"]]
        was_active = old not in IDLE_STATES
        is_active = new_state not in IDLE_STATES
        if was_active and new_state in DONE_STATES:
            await self._c4.post(done_path)
        elif started_path and not was_active and is_active:
            await self._c4.post(started_path)

    async def push_done(self, device_id: str, push_code: str) -> None:
        """Fallback: LG's own 'complete' notification. Driver debounces dupes."""
        dev = self._devices.get(device_id)
        if dev is None:
            return
        LOG.info("%s: push notification %s", dev["alias"], push_code)
        if "COMPLETE" in push_code.upper():
            async with self._lock:
                self._state[device_id] = "END"
            await self._c4.post(ROUTES[dev["type"]][1])


class Bridge:
    def __init__(self) -> None:
        self.pat = env("THINQ_PAT", required=True)
        self.country = env("THINQ_COUNTRY", "US")
        self.c4_url = env("C4_URL", required=True)
        self.c4_secret = env("C4_SECRET", required=True)
        self.types = {t.strip() for t in env("DEVICE_TYPES", "DEVICE_DRYER,DEVICE_WASHER").split(",") if t.strip()}
        self.resync = int(env("RESYNC_SECONDS", "900"))
        self.state_dir = Path(env("STATE_DIR", "/data"))
        self.heartbeat = self.state_dir / "heartbeat"
        self.loop: asyncio.AbstractEventLoop | None = None
        self.stop = asyncio.Event()

    def client_id(self) -> str:
        # LG warns against creating many clients; keep one stable id per install.
        f = self.state_dir / "client_id"
        if f.exists():
            return f.read_text().strip()
        cid = str(uuid.uuid4())
        self.state_dir.mkdir(parents=True, exist_ok=True)
        f.write_text(cid)
        return cid

    # ---- MQTT callbacks (run on awscrt threads) -------------------------
    def _on_message(self, topic, payload, dup, qos, retain, **kwargs):
        try:
            msg = json.loads(payload.decode())
        except Exception:  # noqa: BLE001
            LOG.warning("Undecodable MQTT payload on %s", topic)
            return
        asyncio.run_coroutine_threadsafe(self._handle(msg), self.loop)

    def _on_success(self, connection, callback_data):
        LOG.info("MQTT connected")
        asyncio.run_coroutine_threadsafe(self._resync("reconnect"), self.loop)

    def _on_interrupted(self, connection, error, **kwargs):
        LOG.warning("MQTT interrupted: %s", error)

    def _on_failure(self, connection, callback_data):
        LOG.error("MQTT connection failure: %s", getattr(callback_data, "error", callback_data))

    def _on_closed(self, connection, callback_data):
        LOG.warning("MQTT connection closed")

    # ---- handlers --------------------------------------------------------
    async def _handle(self, msg: dict) -> None:
        LOG.debug("MQTT message: %s", msg)
        device_id = msg.get("deviceId")
        push_type = msg.get("pushType")
        if push_type == "DEVICE_STATUS":
            await self.tracker.update(device_id, find_run_state(msg.get("report", {})), "push")
        elif push_type == "DEVICE_PUSH" and msg.get("pushCode"):
            await self.tracker.push_done(device_id, str(msg["pushCode"]))

    async def _resync(self, why: str) -> None:
        for device_id in self.devices:
            try:
                status = await self.api.async_get_device_status(device_id)
                await self.tracker.update(device_id, find_run_state(status), why)
            except Exception as err:  # noqa: BLE001
                LOG.warning("Status poll failed for %s: %s", device_id, err)

    async def _resync_loop(self) -> None:
        while not self.stop.is_set():
            self.heartbeat.touch()
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=self.resync or 60)
            except asyncio.TimeoutError:
                if self.resync:
                    await self._resync("poll")

    async def _subscribe_devices(self) -> None:
        for device_id in self.devices:
            for fn in (self.api.async_post_event_subscribe, self.api.async_post_push_subscribe):
                try:
                    await fn(device_id)
                except Exception as err:  # noqa: BLE001
                    # "already subscribed" comes back as an error; harmless
                    LOG.debug("%s(%s): %s", fn.__name__, device_id, err)

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            self.loop.add_signal_handler(sig, self.stop.set)

        async with ClientSession() as session:
            cid = self.client_id()
            self.api = ThinQApi(session=session, access_token=self.pat,
                                country_code=self.country, client_id=cid)
            try:
                device_list = await self.api.async_get_device_list() or []
            except Exception as err:  # noqa: BLE001
                LOG.critical("Cannot list ThinQ devices (check PAT/country/network): %s", err)
                await asyncio.sleep(30)  # avoid hammering LG under restart: unless-stopped
                sys.exit(1)
            self.devices = {
                d["deviceId"]: {"type": d["deviceInfo"]["deviceType"],
                                "alias": d["deviceInfo"].get("alias", d["deviceId"])}
                for d in device_list
                if d.get("deviceInfo", {}).get("deviceType") in self.types
                and d["deviceInfo"]["deviceType"] in ROUTES
            }
            if not self.devices:
                LOG.critical("No matching devices. Account has: %s",
                             [d.get("deviceInfo", {}).get("deviceType") for d in device_list])
                sys.exit(3)
            for d in self.devices.values():
                LOG.info("Watching %s (%s)", d["alias"], d["type"])

            self.tracker = Tracker(C4Client(session, self.c4_url, self.c4_secret), self.devices)
            await self._subscribe_devices()
            await self._resync("startup")

            mqtt = await ThinQMQTTClient(
                self.api, cid, self._on_message,
                on_connection_interrupted=self._on_interrupted,
                on_connection_success=self._on_success,
                on_connection_failure=self._on_failure,
                on_connection_closed=self._on_closed,
            )
            if not await mqtt.async_prepare_mqtt():
                LOG.critical("MQTT preparation (client register / certificate) failed")
                sys.exit(4)
            await mqtt.async_connect_mqtt()
            if not mqtt.is_connected:
                LOG.critical("Could not connect to ThinQ MQTT")
                sys.exit(5)

            await self._resync_loop()
            LOG.info("Shutting down")
            try:
                await mqtt.async_disconnect()
            except Exception as err:  # noqa: BLE001
                LOG.debug("Disconnect: %s", err)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("thinqconnect").setLevel(logging.WARNING)
    asyncio.run(Bridge().run())


if __name__ == "__main__":
    main()
