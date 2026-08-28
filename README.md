# Bedroom controls → Home Assistant

Bridges physical controls on a Raspberry Pi 3 to Home Assistant over MQTT.

Two input devices are supported, **either or both**. Whichever is plugged in is
used; the service starts fine with one missing and picks it up automatically if
you swap hardware — no code or config change needed.

| Action                | USB volume knob | Sense HAT joystick |
|-----------------------|-----------------|--------------------|
| Open the blinds       | volume up       | up                 |
| Close the blinds      | volume down     | down               |
| Toggle bedroom lights | mute press      | click              |

- **Currently fitted:** the USB volume knob (the Sense HAT is off the board).
- **Sensors** (temperature, humidity, pressure) publish to HA via MQTT discovery
  — only while the Sense HAT is attached.
- **LED matrix** stays off. The old matrix dashboard is kept in git history and
  in `sensehat_ha.py.matrix-version.bak`.

## Hardware / host facts (this machine)

- Raspberry Pi 3 Model B Rev 1.2.
- **USB volume knob**: `ZhenHuiDesignTechnology USB-AUDIO SYSTEM`, currently
  `/dev/input/event2`. Emits `KEY_VOLUMEUP` (115), `KEY_VOLUMEDOWN` (114),
  `KEY_MUTE` (113). Auto-detected by name, so the event number can change.
- **Sense HAT** (**v1** — no light/colour sensor; other onboard sensors are the
  IMU: accel/gyro/magnetometer). Currently **detached**.
- Pi IP: `192.168.1.64`
- Home Assistant: `http://192.168.1.221:8123`
- MQTT broker: Mosquitto add-on on the HA box, `192.168.1.221:1883`,
  login `pi_sensehat` (defined in the add-on's own **Logins**, not an HA user).

---

## Setup checklist

### 1. Set up the MQTT broker (in Home Assistant)

The Mosquitto add-on runs inside Home Assistant.

1. In HA: **Settings → Add-ons → Add-on Store**, search **"Mosquitto broker"**,
   **Install**, then **Start** it. (Turn on "Start on boot".)
2. Give the Pi a login. Either create an HA user (**Settings → People → Users**),
   or — what's used here — define one directly in the add-on:
   **Settings → Add-ons → Mosquitto broker → Configuration → Logins**:
   ```yaml
   logins:
     - username: pi_sensehat
       password: pi3
   ```
   Save, then **restart the add-on**.
3. Add the MQTT integration so HA talks to the broker:
   **Settings → Devices & Services → Add Integration → MQTT**. It should
   auto-detect the broker (`core-mosquitto`). Accept defaults.

### 2. Fill in credentials on the Pi

Edit `config.env` with the host, port, username, and password.

### 3. Enable I2C, install packages, install the service (run on the Pi)

One script does all the `sudo` parts (enable I2C, install `sense-hat` +
`python3-paho-mqtt`, install + enable the systemd service):

```bash
sudo bash /home/matt/sensehat-ha/setup.sh
```

I2C is enabled by this, which needs a reboot:

```bash
sudo reboot
```

### 4. After reboot

The service autostarts. Verify and watch logs:

```bash
systemctl status sensehat-ha
journalctl -u sensehat-ha -f
```

You want `MQTT connected: Success`. Then add the HA automation from
`ha-automation.yaml` so the joystick actually drives the bedroom.

---

## How it works (for future tweaking)

- `sensehat_ha.py` — the service. Reads the knob and/or Sense HAT, publishes
  sensors, and talks MQTT.
- `config.env` — broker credentials, command topics, and tuning knobs. Edit,
  then `sudo systemctl restart sensehat-ha`.
- `sensehat-ha.service` — systemd unit (autostart + restart on failure).
- `ha-automation.yaml` — paste into Home Assistant so the joystick commands
  actually drive the lights/blinds.

### MQTT topics

| Topic                          | Payload            | Effect                    |
|--------------------------------|--------------------|---------------------------|
| `sensehat/sensors/state`       | JSON, retained     | Sensor readings → HA      |
| `sensehat/bedroom/lights/set`  | `toggle`           | Toggle bedroom lights     |
| `sensehat/bedroom/blinds/set`  | `open` / `close`   | Open / close blinds       |

Commands are published **non-retained** — they're one-shot actions, so HA won't
replay the last one on restart.

### USB volume knob

Read straight from `/dev/input/eventN` by parsing `input_event` structs — no
`evdev` package needed, so there is nothing extra to install. The user `matt` is
in the `input` group, which is what grants read access.

- **Auto-detected** by matching `KNOB_NAME_MATCH` (default `USB-AUDIO`) against
  `/proc/bus/input/devices`, so the `eventN` number changing across reboots or
  re-plugs doesn't matter. Pin a device with `KNOB_DEVICE` if you ever need to.
- **Hot-plug tolerant**: if the knob is missing at startup, or unplugged while
  running, the service keeps going and retries every ~5s.
- Only **key presses** act (releases and autorepeats are ignored), and repeats
  inside `ACTION_DEBOUNCE` are dropped — so spinning the knob several detents
  sends a single "open"/"close" rather than a burst.
- **Remap** the knob by editing `KNOB_MAP` in `sensehat_ha.py`. The device also
  reports play/pause and next/prev track keys, which are unused.

### Joystick orientation (Sense HAT, auto)

The joystick auto-orients from the accelerometer, so directions stay physically
correct however the Pi is placed (0/90/180/270):

- `detect_rotation()` reads gravity and returns the rotation. The axis **signs
  were calibrated to this board** (vertical axis: `180 if y<0 else 0`).
- `rotate_dir()` maps raw joystick directions into the upright frame by rotating
  by the **inverse** of the rotation (`-deg`). Verified on-device across all four
  orientations (every physical push → correct action).
- Lying **flat** (gravity straight down) → can't be sensed → falls back to
  `DEFAULT_ROTATION` from `config.env`.

Calibration knobs in `config.env`: `AUTO_ORIENT`, `DEFAULT_ROTATION`,
`ROTATION_OFFSET`. If directions ever come out turned, adjust `ROTATION_OFFSET`
(multiple of 90) before touching code.

### Adjusting things later

- **Change publish rate / temperature offset / topics:** edit `config.env`,
  then `sudo systemctl restart sensehat-ha`.
- **Target different lights/blinds:** edit `ha-automation.yaml` — it targets the
  HA **`bedroom` area**; swap `area_id: bedroom` for explicit `entity_id:`s if
  you prefer. Reload automations in HA afterwards.
- **Remap the knob:** edit `KNOB_MAP` in `sensehat_ha.py`; the joystick is
  `STICK_MAP` just below it.
- **View logs:** `journalctl -u sensehat-ha -f` (every action is logged as
  `action: blinds_open` etc, so you can tell Pi-side from HA-side problems).
- **Stop / start:** `sudo systemctl stop|start sensehat-ha`

### Gotchas learned the hard way

- The LED matrix is **5-bit per channel** with a gamma curve that crushes dim
  values — dim *and* pastel isn't possible; dim colours must stay saturated.
  (Relevant only if you re-enable the matrix.)
- HA MQTT triggers fire on messages arriving **while subscribed**; a retained
  message published before the automation existed won't trigger it.

---

## Status

- [x] MQTT broker set up in HA (Mosquitto add-on, login `pi_sensehat`)
- [x] I2C enabled + packages installed + service running on boot
- [x] Joystick orientation calibrated across all four orientations
- [x] USB volume knob supported (auto-detected, debounced, hot-plug tolerant)
- [x] Sense HAT made optional — refit it and it is picked up automatically
- [x] Targets confirmed: "Bedroom" area + `cover.bedroom_blinds`
- [ ] Updated `ha-automation.yaml` pasted into HA (replaces the old
      "Sense HAT joystick lights" automation — delete that one)
- [ ] Knob tested end-to-end against HA
