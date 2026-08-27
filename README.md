# Sense HAT → Home Assistant

Bridges the Raspberry Pi 3 Sense HAT to Home Assistant over MQTT.

- **Sensors** (temperature, humidity, pressure) are published to HA via MQTT
  discovery, so they appear automatically as `sensor.*` entities.
- **Joystick** controls the bedroom (directions are auto-corrected for however
  the Pi is physically oriented — see "Joystick orientation"):
  - **Click** → toggle the bedroom lights
  - **Up** → open the bedroom blinds
  - **Down** → close the bedroom blinds
  - **Left / Right** → unused for now
- **LED matrix** is **off**. (It previously showed a light/sensor bar dashboard;
  that code is kept in git history and in `sensehat_ha.py.matrix-version.bak`
  if you ever want it back.)

## Hardware / host facts (this machine)

- Raspberry Pi 3 Model B Rev 1.2, Sense HAT attached (**v1** — no light/colour
  sensor; the only other onboard sensors are the IMU: accel/gyro/magnetometer).
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

- `sensehat_ha.py` — the service. Reads sensors, handles joystick events, and
  talks MQTT.
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

### Joystick orientation (auto)

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
- **View logs:** `journalctl -u sensehat-ha -f` (joystick actions are logged).
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
- [x] Sensors publishing to HA via MQTT discovery
- [x] Joystick orientation calibrated across all four orientations
- [x] Stripped back to: matrix off, click = bedroom lights, up/down = blinds
- [ ] Updated `ha-automation.yaml` pasted into HA (replaces the old
      "Sense HAT joystick lights" automation — delete that one)
- [x] Targets confirmed: "Bedroom" area + cover.bedroom_blinds
- [ ] Temperature offset sanity-checked (`TEMP_OFFSET=17`)
