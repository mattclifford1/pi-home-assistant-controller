#!/usr/bin/env python3
"""Bedroom controls -> Home Assistant bridge.

Two input sources, either or both may be present:

  USB volume knob        Sense HAT joystick
  ---------------        ------------------
  volume up   -> blinds close     up    -> blinds open
  volume down -> blinds open      down  -> blinds close
  mute press  -> lights toggle    click -> lights toggle

Blinds moves are interlocked: while the blinds are travelling, turning the
control the *opposite* way stops them where they are, and a command the same
way is ignored. See BLINDS below.

Also publishes the Sense HAT's temperature/humidity/pressure to HA via MQTT
discovery, when the HAT is attached.

Both input devices are optional and hot-pluggable: whichever is present is
used, and the service keeps running (and retries) if one is missing. So the
Sense HAT can be swapped back on at any time without touching this code.

Configuration comes from environment variables, which the systemd unit loads
from config.env. When run by hand, this script also reads ./config.env itself.
"""

import json
import os
import re
import select
import signal
import struct
import sys
import time
from pathlib import Path

# --- load config.env when present (so the script works without systemd too) ---
_CFG = Path(__file__).resolve().parent / "config.env"
if _CFG.exists():
    for _line in _CFG.read_text().splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))


def _env(name, default):
    return os.environ.get(name, default)


def _flag(name, default):
    return _env(name, default).lower() in ("1", "true", "yes")


# --- configuration knobs -----------------------------------------------------
MQTT_HOST = _env("MQTT_HOST", "192.168.1.221")
MQTT_PORT = int(_env("MQTT_PORT", "1883"))
MQTT_USER = _env("MQTT_USER", "")
MQTT_PASS = _env("MQTT_PASS", "")

NODE_ID = _env("NODE_ID", "sensehat")           # unique id for this Pi
DISCOVERY_PREFIX = _env("DISCOVERY_PREFIX", "homeassistant")

# Command topics the HA automation listens on.
LIGHTS_TOPIC = _env("LIGHTS_TOPIC", "sensehat/bedroom/lights/set")
BLINDS_TOPIC = _env("BLINDS_TOPIC", "sensehat/bedroom/blinds/set")

PUBLISH_INTERVAL = float(_env("PUBLISH_INTERVAL", "30"))   # seconds
TEMP_OFFSET = float(_env("TEMP_OFFSET", "0"))   # calibrate temp (CPU heats Pi)

# Minimum gap between repeated commands. Spinning the knob emits one event per
# detent; the blinds only need telling once, so extra events inside this window
# are dropped instead of spamming Home Assistant.
ACTION_DEBOUNCE = float(_env("ACTION_DEBOUNCE", "0.4"))

# --- blinds interlock ---
# How long a full open/close takes, seconds. Used as the fallback "still
# moving" window when Home Assistant isn't reporting the cover's state (or is
# reporting it late), so the interlock can never latch on forever.
BLINDS_TRAVEL_TIME = float(_env("BLINDS_TRAVEL_TIME", "30"))
# After a stop, ignore open/close until the control has been quiet for this
# long, so the rest of the knob spin that stopped the blinds can't restart them.
BLINDS_STOP_LOCKOUT = float(_env("BLINDS_STOP_LOCKOUT", "1.0"))
# Once HA has shown it reports travel, a move it hasn't confirmed with
# "opening"/"closing" within this many seconds is assumed not to have happened
# (e.g. "open" on blinds already fully open), and the interlock is released.
BLINDS_CONFIRM_TIME = float(_env("BLINDS_CONFIRM_TIME", "6"))
# HA takes a second or two to finish a stop, and rejects any open/close that
# arrives before then. A turn made in that window is held and sent the moment
# HA reports the blinds have stopped — or after this many seconds if it never
# does — instead of being lost.
BLINDS_STOP_SETTLE = float(_env("BLINDS_STOP_SETTLE", "5"))
# Optional feedback topic: HA publishes the cover's state here (opening /
# closing / open / closed / stopped) so the Pi knows exactly when a move ends
# rather than guessing from BLINDS_TRAVEL_TIME. Blank disables the feedback.
BLINDS_STATE_TOPIC = _env("BLINDS_STATE_TOPIC", "sensehat/bedroom/blinds/state")

# --- USB volume knob ---
ENABLE_KNOB = _flag("ENABLE_KNOB", "true")
# Explicit input device path; leave blank to auto-detect by name.
KNOB_DEVICE = _env("KNOB_DEVICE", "")
# Substring matched against device names in /proc/bus/input/devices.
KNOB_NAME_MATCH = _env("KNOB_NAME_MATCH", "USB-AUDIO")

# --- Sense HAT (optional; absent while the knob is fitted instead) ---
ENABLE_SENSEHAT = _flag("ENABLE_SENSEHAT", "true")
# Auto-orientation from the accelerometer, so joystick directions stay
# physically correct however the Pi is placed (0/90/180/270).
AUTO_ORIENT = _flag("AUTO_ORIENT", "true")
# Used when the Pi is lying flat (gravity straight down -> orientation unknown).
DEFAULT_ROTATION = int(_env("DEFAULT_ROTATION", "180")) % 360
# Added to the detected rotation, for calibrating if directions come out
# turned 90/180 from what you expect. Must be a multiple of 90.
ROTATION_OFFSET = int(_env("ROTATION_OFFSET", "0")) % 360

import paho.mqtt.client as mqtt  # noqa: E402


# --- actions -----------------------------------------------------------------
# Commands are published non-retained: they are one-shot actions, so HA must
# not replay them on restart.
_last_action = {}


# What the blinds are believed to be doing, so a second command can be turned
# into a stop instead of a queued reversal. "until" is a safety deadline: if HA
# never tells us the move finished, we assume it has after BLINDS_TRAVEL_TIME.
_blinds = {
    "moving": None,          # "open" | "close" | None (idle)
    "until": 0.0,            # monotonic time the assumed travel ends
    "lockout": 0.0,          # no open/close accepted before this time
    "reports_travel": False,  # has HA ever sent us "opening"/"closing"?
    "confirmed": False,      # has HA confirmed the current move is travelling?
    "confirm_by": 0.0,       # monotonic time HA must have confirmed it by
    "stop_settle": 0.0,      # waiting for HA to finish a stop until this time
    "queued": None,          # "open" | "close" to send once the stop is done
}


# Direction -> present participle, for readable log lines.
_TRAVELLING = {"open": "opening", "close": "closing"}


def blinds_moving():
    """Direction currently being travelled, or None. Expires on its deadline."""
    now = time.monotonic()
    if _blinds["moving"] is not None and now >= _blinds["until"]:
        _blinds["moving"] = None
    if (_blinds["moving"] is not None and _blinds["reports_travel"]
            and not _blinds["confirmed"] and now >= _blinds["confirm_by"]):
        # Without this, a command that moved nothing (blinds already there)
        # armed the interlock for the full travel time, so the next turn the
        # other way sent "stop" instead of moving them.
        print("blinds: HA never reported travel -> interlock released",
              flush=True)
        _blinds["moving"] = None
    return _blinds["moving"]


def request_blinds(direction, client):
    """Apply the interlock, then publish open/close/stop as appropriate.

    While the blinds are moving:
      - the opposite direction stops them where they are;
      - the same direction is ignored (they are already going that way).
    Otherwise the move is only started once any post-stop lockout has passed.
    """
    now = time.monotonic()
    moving = blinds_moving()

    if moving:
        if direction == moving:
            print(f"blinds: already {_TRAVELLING[moving]} — ignored", flush=True)
            return
        client.publish(BLINDS_TOPIC, "stop", retain=False)
        _blinds["moving"] = None
        _blinds["lockout"] = now + BLINDS_STOP_LOCKOUT
        if _blinds["reports_travel"]:
            _blinds["stop_settle"] = now + BLINDS_STOP_SETTLE
        _blinds["queued"] = None
        print("action: blinds_stop", flush=True)
        return

    if now < _blinds["lockout"]:
        # Extend while the knob keeps turning: a long spin outlasts a fixed
        # lockout, and its tail would otherwise start the blinds again.
        _blinds["lockout"] = now + BLINDS_STOP_LOCKOUT
        print("blinds: settling after stop — ignored", flush=True)
        return

    if now < _blinds["stop_settle"]:
        _blinds["queued"] = direction
        print(f"blinds: HA still stopping — will {direction} when it's done",
              flush=True)
        return

    start_blinds(direction, client, now)


def start_blinds(direction, client, now):
    client.publish(BLINDS_TOPIC, direction, retain=False)
    _blinds["moving"] = direction
    _blinds["until"] = now + BLINDS_TRAVEL_TIME
    _blinds["confirmed"] = False
    _blinds["confirm_by"] = now + BLINDS_CONFIRM_TIME
    print(f"action: blinds_{direction}", flush=True)


def tick_blinds(client):
    """Send a move held back while HA finished a stop. Call regularly."""
    if _blinds["queued"] is None:
        return
    now = time.monotonic()
    if now < _blinds["stop_settle"]:
        return
    direction, _blinds["queued"] = _blinds["queued"], None
    if blinds_moving():
        # Something else (the app, a schedule) started the blinds meanwhile.
        print(f"blinds: queued {direction} dropped — blinds moving", flush=True)
        return
    start_blinds(direction, client, now)


def on_blinds_state(client, userdata, msg):
    """Track the real cover state, when HA is publishing it."""
    try:
        payload = msg.payload.decode("utf-8", "replace").strip().lower()
    except Exception:
        return
    before = _blinds["moving"]
    if payload in ("opening", "closing"):
        # The cover reports travel, so its terminal states are trustworthy too.
        _blinds["reports_travel"] = True
        _blinds["confirmed"] = True
        _blinds["moving"] = "open" if payload == "opening" else "close"
        _blinds["until"] = time.monotonic() + BLINDS_TRAVEL_TIME
    elif payload in ("open", "closed", "stopped"):
        # Only believe "it has arrived" from a cover that reports travel at
        # all; one that only ever says open/closed would clear the interlock
        # the instant a move started.
        if _blinds["reports_travel"]:
            _blinds["moving"] = None
            # The stop has landed; HA will now accept a new move.
            _blinds["stop_settle"] = 0.0
    else:
        return
    # Log every report, not just interlock transitions: whether HA confirmed a
    # move the Pi asked for is the first thing to check when the blinds don't
    # respond. HA only publishes on state changes, so this stays quiet.
    now_moving = _blinds["moving"]
    change = (f"interlock {'armed (' + now_moving + ')' if now_moving else 'released'}"
              if now_moving != before else "interlock unchanged")
    print(f"blinds: HA says {payload!r} -> {change}", flush=True)


def do_action(name, client):
    """Publish one command, debounced per action."""
    now = time.monotonic()
    if now - _last_action.get(name, 0.0) < ACTION_DEBOUNCE:
        return
    _last_action[name] = now
    if name == "lights_toggle":
        client.publish(LIGHTS_TOPIC, "toggle", retain=False)
        print(f"action: {name}", flush=True)
    elif name == "blinds_open":
        request_blinds("open", client)     # interlocked; logs its own outcome
    elif name == "blinds_close":
        request_blinds("close", client)


# --- USB volume knob ---------------------------------------------------------
# struct input_event { struct timeval time; __u16 type; __u16 code; __s32 value; }
_EV_FORMAT = "@llHHi"
_EV_SIZE = struct.calcsize(_EV_FORMAT)
_EV_KEY = 0x01
KEY_MUTE = 113
KEY_VOLUMEDOWN = 114
KEY_VOLUMEUP = 115

# Knob key -> action. Edit here to remap the knob.
KNOB_MAP = {
    KEY_VOLUMEUP: "blinds_close",
    KEY_VOLUMEDOWN: "blinds_open",
    KEY_MUTE: "lights_toggle",
}


def find_knob():
    """Locate the knob's /dev/input/eventN, or None if it isn't plugged in."""
    if KNOB_DEVICE:
        return KNOB_DEVICE if os.path.exists(KNOB_DEVICE) else None
    try:
        blocks = Path("/proc/bus/input/devices").read_text().split("\n\n")
    except OSError:
        return None
    for block in blocks:
        name = re.search(r'N: Name="([^"]*)"', block)
        if not name or KNOB_NAME_MATCH.lower() not in name.group(1).lower():
            continue
        handlers = re.search(r"H: Handlers=(.*)", block)
        if not handlers:
            continue
        for token in handlers.group(1).split():
            if token.startswith("event"):
                path = f"/dev/input/{token}"
                if os.path.exists(path):
                    return path
    return None


class Knob:
    """Reads key events straight from the input device (no evdev dependency)."""

    def __init__(self):
        self.fd = None
        self.path = None
        self.next_retry = 0.0

    def ensure_open(self):
        if self.fd is not None or not ENABLE_KNOB:
            return
        now = time.monotonic()
        if now < self.next_retry:
            return
        self.next_retry = now + 5.0
        path = find_knob()
        if not path:
            return
        try:
            self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            self.path = path
            print(f"knob: using {path}", flush=True)
        except OSError as exc:
            print(f"knob: cannot open {path}: {exc}", flush=True)
            self.fd = None

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
        self.fd = None
        self.path = None

    def poll(self, client):
        """Dispatch any pending key presses."""
        self.ensure_open()
        if self.fd is None:
            return
        try:
            readable, _, _ = select.select([self.fd], [], [], 0)
            if not readable:
                return
            data = os.read(self.fd, _EV_SIZE * 64)
        except OSError:
            print("knob: disconnected", flush=True)
            self.close()
            self.next_retry = time.monotonic() + 2.0
            return
        for offset in range(0, len(data) - _EV_SIZE + 1, _EV_SIZE):
            _, _, etype, code, value = struct.unpack_from(
                _EV_FORMAT, data, offset)
            # value 1 = press, 2 = autorepeat, 0 = release. A detent of the
            # knob is a press+release pair; act on the press only.
            if etype == _EV_KEY and value == 1 and code in KNOB_MAP:
                do_action(KNOB_MAP[code], client)


# --- Sense HAT ---------------------------------------------------------------
readings = {"temperature": None, "humidity": None, "pressure": None}
ui = {"rotation": DEFAULT_ROTATION}   # current physical rotation (0/90/180/270)

# Joystick directions in clockwise order, used to remap raw events by rotation.
_CW = ("up", "right", "down", "left")

# Joystick direction -> action.
STICK_MAP = {
    "up": "blinds_open",
    "down": "blinds_close",
    "middle": "lights_toggle",
}


def open_sensehat():
    """Return a SenseHat, or None when the HAT isn't fitted."""
    if not ENABLE_SENSEHAT:
        return None
    try:
        from sense_hat import SenseHat
        sense = SenseHat()
        sense.clear()          # LED matrix stays off
        print("sense hat: detected", flush=True)
        return sense
    except Exception as exc:
        print(f"sense hat: not available ({exc.__class__.__name__}) — "
              "running without it", flush=True)
        return None


def read_sensors(sense):
    readings["temperature"] = round(sense.get_temperature() - TEMP_OFFSET, 1)
    readings["humidity"] = round(sense.get_humidity(), 1)
    readings["pressure"] = round(sense.get_pressure(), 1)
    return readings


def rotate_dir(direction, deg):
    """Map a raw joystick direction to its logical (upright) direction.

    The joystick reports in the board frame; to express it in the upright
    physical frame we rotate by the inverse of the rotation (-deg). Verified
    on-device across all four orientations.
    """
    if direction not in _CW:
        return direction
    return _CW[(_CW.index(direction) - (deg // 90)) % 4]


def detect_rotation(sense):
    """Pick 0/90/180/270 from gravity, or None if flat/ambiguous (unknown)."""
    a = sense.get_accelerometer_raw()
    x, y = a["x"], a["y"]
    # Need one in-plane axis clearly dominant, else we can't tell (flat or 45°).
    # Signs below were calibrated to this board.
    if abs(y) >= abs(x) + 0.2 and abs(y) > 0.5:
        return 180 if y < 0 else 0
    if abs(x) >= abs(y) + 0.2 and abs(x) > 0.5:
        return 90 if x < 0 else 270
    return None


def update_orientation(sense):
    """Re-read the physical orientation and remember it."""
    base = detect_rotation(sense) if AUTO_ORIENT else DEFAULT_ROTATION
    if base is None:
        return  # unknown right now — keep whatever we last had
    ui["rotation"] = (base + ROTATION_OFFSET) % 360


def handle_stick(event, client):
    """Act once per press (not while held, so blinds aren't spammed)."""
    if event.action != "pressed":
        return
    direction = event.direction
    if direction != "middle":
        direction = rotate_dir(direction, ui["rotation"])
    action = STICK_MAP.get(direction)
    if action:
        do_action(action, client)


# --- MQTT --------------------------------------------------------------------
DEVICE = {
    "identifiers": [f"{NODE_ID}_pi"],
    "name": "Sense HAT (Raspberry Pi)",
    "manufacturer": "Raspberry Pi",
    "model": "Sense HAT",
}

SENSORS = {
    "temperature": {"name": "Sense HAT Temperature", "unit": "°C",
                    "dev_cls": "temperature", "icon": "mdi:thermometer"},
    "humidity": {"name": "Sense HAT Humidity", "unit": "%",
                 "dev_cls": "humidity", "icon": "mdi:water-percent"},
    "pressure": {"name": "Sense HAT Pressure", "unit": "hPa",
                 "dev_cls": "pressure", "icon": "mdi:gauge"},
}

STATE_TOPIC = f"{NODE_ID}/sensors/state"


def publish_discovery(client):
    for key, meta in SENSORS.items():
        topic = f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/{key}/config"
        payload = {
            "name": meta["name"],
            "unique_id": f"{NODE_ID}_{key}",
            "state_topic": STATE_TOPIC,
            "value_template": f"{{{{ value_json.{key} }}}}",
            "unit_of_measurement": meta["unit"],
            "device_class": meta["dev_cls"],
            "state_class": "measurement",
            "icon": meta["icon"],
            "device": DEVICE,
        }
        client.publish(topic, json.dumps(payload), retain=True)


def publish_sensors(client, sense):
    data = read_sensors(sense)
    client.publish(STATE_TOPIC, json.dumps(data), retain=True)


# --- lifecycle ---------------------------------------------------------------
_running = True


def _stop(*_):
    global _running
    _running = False


def on_connect(client, userdata, flags, reason_code, properties=None):
    print(f"MQTT connected: {reason_code}", flush=True)
    publish_discovery(client)
    if BLINDS_STATE_TOPIC:
        # Re-subscribed on every (re)connect, since a reconnect drops these.
        client.subscribe(BLINDS_STATE_TOPIC)


def main():
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    sense = open_sensehat()
    if sense:
        read_sensors(sense)
        update_orientation(sense)

    knob = Knob()
    knob.ensure_open()
    if knob.fd is None and ENABLE_KNOB:
        print("knob: not found yet — will keep looking", flush=True)
    if sense is None and knob.fd is None:
        print("warning: no input device present (no Sense HAT, no knob)",
              flush=True)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=NODE_ID)
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.on_connect = on_connect
    if BLINDS_STATE_TOPIC:
        client.message_callback_add(BLINDS_STATE_TOPIC, on_blinds_state)
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()

    last_publish = 0.0
    last_orient = 0.0
    try:
        while _running:
            knob.poll(client)
            tick_blinds(client)
            if sense:
                for event in sense.stick.get_events():
                    handle_stick(event, client)
            now = time.monotonic()
            if sense:
                # Re-check physical orientation a couple of times a second.
                if now - last_orient >= 0.4:
                    update_orientation(sense)
                    last_orient = now
                if now - last_publish >= PUBLISH_INTERVAL:
                    publish_sensors(client, sense)
                    last_publish = now
            time.sleep(0.02)
    finally:
        knob.close()
        if sense:
            sense.clear()
        client.loop_stop()
        client.disconnect()
        print("stopped", flush=True)


if __name__ == "__main__":
    sys.exit(main())
