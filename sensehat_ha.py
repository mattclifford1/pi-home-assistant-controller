#!/usr/bin/env python3
"""Sense HAT <-> Home Assistant bridge.

- Publishes temperature/humidity/pressure to HA via MQTT discovery.
- Joystick (bedroom controls):
    click     -> toggle the bedroom lights
    up        -> open the bedroom blinds
    down      -> close the bedroom blinds
    left/right-> unused
- LED matrix is kept OFF.

Joystick directions are auto-corrected for however the Pi is physically
oriented, using the accelerometer (see rotate_dir / detect_rotation).

Configuration comes from environment variables, which the systemd unit loads
from config.env. When run by hand, this script also reads ./config.env itself.
"""

import json
import os
import signal
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

# Auto-orientation from the accelerometer: works out how the Pi is placed
# (0/90/180/270) so the joystick directions stay physically correct.
AUTO_ORIENT = _env("AUTO_ORIENT", "true").lower() in ("1", "true", "yes")
# Used when the Pi is lying flat (gravity straight down -> orientation unknown).
DEFAULT_ROTATION = int(_env("DEFAULT_ROTATION", "180")) % 360
# Added to the detected rotation, for calibrating if directions come out
# turned 90/180 from what you expect. Must be a multiple of 90.
ROTATION_OFFSET = int(_env("ROTATION_OFFSET", "0")) % 360

from sense_hat import SenseHat  # noqa: E402
import paho.mqtt.client as mqtt  # noqa: E402


# --- runtime state -----------------------------------------------------------
# Latest sensor readings.
readings = {"temperature": None, "humidity": None, "pressure": None}

ui = {"rotation": DEFAULT_ROTATION}   # current physical rotation (0/90/180/270)


def read_sensors(sense):
    readings["temperature"] = round(sense.get_temperature() - TEMP_OFFSET, 1)
    readings["humidity"] = round(sense.get_humidity(), 1)
    readings["pressure"] = round(sense.get_pressure(), 1)
    return readings


# --- orientation ------------------------------------------------------------
# Joystick directions in clockwise order, used to remap raw events by rotation.
_CW = ("up", "right", "down", "left")


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


# --- joystick actions --------------------------------------------------------
def apply_direction(logical, client):
    """Act on a rotation-corrected direction.

    Commands are published non-retained: they are one-shot actions, so HA must
    not replay them on restart.
    """
    if logical == "middle":
        client.publish(LIGHTS_TOPIC, "toggle", retain=False)
        print("bedroom lights: toggle", flush=True)
    elif logical == "up":
        client.publish(BLINDS_TOPIC, "open", retain=False)
        print("bedroom blinds: open", flush=True)
    elif logical == "down":
        client.publish(BLINDS_TOPIC, "close", retain=False)
        print("bedroom blinds: close", flush=True)
    # left/right: unused for now


def handle_event(event, client):
    """Act once per press (not while held, so blinds aren't spammed)."""
    if event.action != "pressed":
        return
    if event.direction == "middle":
        apply_direction("middle", client)
        return
    apply_direction(rotate_dir(event.direction, ui["rotation"]), client)


# --- lifecycle ---------------------------------------------------------------
_running = True


def _stop(*_):
    global _running
    _running = False


def on_connect(client, userdata, flags, reason_code, properties=None):
    print(f"MQTT connected: {reason_code}", flush=True)
    publish_discovery(client)


def main():
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    sense = SenseHat()
    sense.clear()              # LED matrix stays off
    read_sensors(sense)
    update_orientation(sense)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=NODE_ID)
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.on_connect = on_connect
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()

    last_publish = 0.0
    last_orient = 0.0
    try:
        while _running:
            for event in sense.stick.get_events():
                handle_event(event, client)
            now = time.monotonic()
            # Re-check physical orientation a couple of times a second.
            if now - last_orient >= 0.4:
                update_orientation(sense)
                last_orient = now
            if now - last_publish >= PUBLISH_INTERVAL:
                publish_sensors(client, sense)
                last_publish = now
            time.sleep(0.05)
    finally:
        sense.clear()
        client.loop_stop()
        client.disconnect()
        print("stopped", flush=True)


if __name__ == "__main__":
    sys.exit(main())
