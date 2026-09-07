# Bedroom controls → Home Assistant

Bridges physical controls on a Raspberry Pi 3 to Home Assistant over MQTT.

Two input devices are supported, **either or both**. Whichever is plugged in is
used; the service starts fine with one missing and picks it up automatically if
you swap hardware — no code or config change needed.

| Action                     | USB volume knob    | Sense HAT joystick |
|----------------------------|--------------------|--------------------|
| Open the blinds            | volume **down**    | up                 |
| Close the blinds           | volume **up**      | down               |
| **Stop** the blinds        | *opposite way, while moving* |          |
| Toggle bedroom lights      | mute press         | click              |

The knob's up/down is deliberately the opposite way round to the joystick's —
turning the knob "up" winds the blinds *down*.

**Stopping mid-travel:** while the blinds are moving, turning the control the
opposite way **stops them where they are** instead of reversing them. See
[Blinds interlock](#blinds-interlock).

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

You want `MQTT connected: Success`. Then add the HA automations so the controls
actually drive the bedroom — **two separate automations**:

1. `ha-automation.yaml` — lights + blinds open/close/stop. Required.
2. `ha-automation-blinds-state.yaml` — reports the blinds' state back to the Pi.
   Optional, but makes the [blinds interlock](#blinds-interlock) exact instead
   of timer-based.

---

## How it works (for future tweaking)

- `sensehat_ha.py` — the service. Reads the knob and/or Sense HAT, publishes
  sensors, and talks MQTT.
- `config.env` — broker credentials, command topics, and tuning knobs. Edit,
  then `sudo systemctl restart sensehat-ha`.
- `sensehat-ha.service` — systemd unit (autostart + restart on failure).
- `ha-automation.yaml` — paste into Home Assistant so the joystick commands
  actually drive the lights/blinds.
- `ha-automation-blinds-state.yaml` — second automation, reporting the blinds'
  state back to the Pi so the interlock knows when a move has finished.
  Optional; without it the Pi falls back to a timer.

### MQTT topics

| Topic                          | Payload            | Effect                    |
|--------------------------------|--------------------|---------------------------|
| `sensehat/sensors/state`       | JSON, retained     | Sensor readings → HA      |
| `sensehat/bedroom/lights/set`  | `toggle`           | Toggle bedroom lights     |
| `sensehat/bedroom/blinds/set`  | `open`/`close`/`stop` | Move / halt blinds     |
| `sensehat/bedroom/blinds/state`| cover state, retained | HA → Pi: is it moving? |

Commands are published **non-retained** — they're one-shot actions, so HA won't
replay the last one on restart.

**Every input source publishes these same topics.** The USB knob and the Sense
HAT joystick are interchangeable front-ends for the same commands, so swapping
hardware never requires an HA change. The one HA automation handles whatever is
plugged in.

**The automation in HA is called "Bedroom controls (Pi)."** It was renamed from
"Sense HAT bedroom controls" on 2026-09-07 — that name predated the USB knob and
was misleading, since the one automation serves both input devices.

Its identifiers have *not* kept up with the renames, which makes it hard to find:

| What            | Value                                  |
|-----------------|----------------------------------------|
| entity_id       | `automation.sense_hat_joystick_lights` |
| numeric id      | `1782305747696`                        |
| YAML `alias`    | whatever was last saved in the editor  |
| displayed name  | entity-registry override, if one is set |

The entity_id is frozen at the *original* 2024 name and will never change.
Renaming via the entity dialog only sets a display-name override, which leaves
the YAML `alias` untouched — so the list can show one name while the YAML shows
another. To rename it properly, edit `alias:` in the YAML editor.
Read the real config with:

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  http://192.168.1.221:8123/api/config/automation/config/1782305747696
```

If the automation ever seems dead, check the topic names first: they were
renamed from `sensehat/lights/set` to `sensehat/bedroom/...` when this was
retargeted from "all lights" to the bedroom. An HA automation still listening
on the old topic will simply never fire.

### Blinds interlock

The blinds motor queues whatever it is told. Sending "open" while it is closing
used to mean it finished closing and *then* opened again — rarely what you
wanted, and irritating. So the Pi refuses to queue a reversal:

| While the blinds are... | Turn the control...   | What happens                     |
|-------------------------|-----------------------|----------------------------------|
| moving                  | the **opposite** way  | **stop** where they are          |
| moving                  | the **same** way      | ignored (already going that way) |
| stopped, < 1s ago       | either way            | ignored (settling)               |
| idle                    | either way            | the move starts                  |

Rules, in `request_blinds()`:

1. **Only a stop interrupts a move.** An open/close arriving mid-travel is
   either turned into `stop` (opposite direction) or dropped (same direction);
   it is never queued.
2. **After a stop, a 1s lockout** (`BLINDS_STOP_LOCKOUT`) ignores open/close, so
   the tail of the same knob spin that stopped the blinds doesn't start them
   off again. Keep spinning past that second and it will start the new move —
   which is the intended way to reverse deliberately.
3. This applies to **both input devices**; the joystick goes through the same
   gate.
4. **Lights are unaffected** — the mute press/click works whatever the blinds
   are doing.

#### Why the HA automation must be `mode: parallel`

The Tuiss cover's open/close service calls **block for the whole travel**. Under
`mode: queued` the stop is serialised behind the very move it is meant to
interrupt, so it always arrives too late. `mode: parallel` lets it run
alongside. This is not optional — with `queued`, everything above works and the
blinds still won't stop.

#### Knowing when a move has finished

The Pi tracks this two ways, and needs at least the first:

- **A travel timer** (`BLINDS_TRAVEL_TIME`, default 30s). Set it a little
  *longer* than a real full open or close. Too short and an unwanted reversal
  can slip through near the end of a move; too long and the controls stay
  locked out after the blinds have already arrived. This is a backstop as much
  as anything: it guarantees the interlock can never latch on permanently, even
  if HA goes away mid-move.
- **State feedback from HA** (optional, more accurate). Add the second
  automation, `ha-automation-blinds-state.yaml`: it publishes the cover's state
  to `sensehat/bedroom/blinds/state`, and the Pi releases the interlock the
  moment the blinds actually arrive rather than waiting out the timer.
  - Some covers only ever report `open`/`closed`, never `opening`/`closing`.
    The Pi detects that (`reports_travel`) and ignores their terminal states,
    falling back to the timer — otherwise the interlock would clear the instant
    a move began, which is the bug this whole section exists to prevent.
  - A bonus: a move started from the HA app or a schedule also registers, so
    the knob can stop *that* too.

Tune both in `config.env`, then restart the service.

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
  sends a single "open"/"close" rather than a burst. The
  [interlock](#blinds-interlock) then drops anything the debounce let through
  while the blinds are still moving.
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
- **Blinds stopping/locking out wrongly:** tune `BLINDS_TRAVEL_TIME` and
  `BLINDS_STOP_LOCKOUT` in `config.env` — see
  [Blinds interlock](#blinds-interlock).
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
- **The Tuiss cover's `open_cover`/`close_cover` block for the entire travel**
  (~40s), instead of returning as soon as the command is sent. Most integrations
  return immediately, so this is easy to get wrong. Consequences:
  - The automation **must be `mode: parallel`**. Under `mode: queued` a `stop`
    waits behind the still-running open/close and only fires once the move has
    finished — the stop button appears completely dead. This cost an evening of
    debugging: `cover.stop_cover` was blamed first, but it works fine; it was
    simply never running in time.
  - Don't judge a blocking service call as "broken". Measure it: a
    `close_cover` that returns in 11.2s returned *because* something stopped
    the blind at 11.2s.
- **The Tuiss cover's `state` doesn't track `current_position` sensibly** once
  stopped part-way: stopped at 82% it reported `open`, stopped at 18% it
  reported `closed`. Don't infer position from the state string — read
  `current_position`. The interlock is unaffected, since it treats `open`,
  `closed` and `stopped` alike as "not moving".

---

## Status

Done:

- [x] MQTT broker set up in HA (Mosquitto add-on, login `pi_sensehat`)
- [x] I2C enabled, packages installed, service runs on boot
- [x] Sense HAT joystick orientation calibrated across all four orientations
- [x] USB volume knob supported (auto-detected, debounced, hot-plug tolerant)
- [x] Sense HAT made optional — refit it and it is picked up automatically
- [x] Targets confirmed: "Bedroom" area + `cover.bedroom_blinds`
- [x] **HA automation pasted in** — done at the bedroom retarget (confirmed).
      The knob needed no HA change; it publishes the same topics.
- [x] Blinds interlock — opposite way while moving stops them; no more queued
      reversals firing after the move finishes.

Outstanding:

- [x] End-to-end test — knob confirmed working
- [x] **Re-paste `ha-automation.yaml`** in HA — it has gained a `stop` branch,
      which the live copy doesn't have (confirmed by reading the live YAML).
      Without it the Pi publishes `stop`, no branch matches, and the blinds
      carry on to the end of their travel.
      Done 2026-09-07, and the automation renamed to "Bedroom controls (Pi)"
      at the same time (it had been "Sense HAT bedroom controls").
- [ ] **Add `ha-automation-blinds-state.yaml`** as a second automation, and
      check the Pi logs show the interlock releasing when the blinds arrive
      rather than 30s later. Optional, but it's what makes the timing exact.
- [x] **Time a full open/close** — the entity reports `traversal_speed` 2.496
      %/sec, i.e. ~40s end to end. `BLINDS_TRAVEL_TIME` set to 45s.
- [x] **Confirmed the blinds support stop** — `supported_features: 15`
      (OPEN|CLOSE|SET_POSITION|STOP). Verified live: a `stop_cover` mid-close
      halted it at 82% and it held. The earlier failure was the queued-mode
      problem above, not the cover.
- [x] **Automation set to `mode: parallel` in HA** — and verified end to end:
      publishing `open` then `stop` mid-travel halted the blind at 18% and it
      held. This was the actual fix.
- [x] **State feedback automation created** (`Bedroom blinds state -> Pi`,
      id `1788809147553`). Verified publishing `opening` then `closed`, two
      messages per move — the trigger condition suppresses the position-only
      updates during travel.
- [ ] Check how the live HA automation targets the blinds. It was pasted around
      the same time the blinds were identified as "Bedroom Blinds", so it may
      use `area_id: bedroom` (the earlier version) rather than
      `entity_id: cover.bedroom_blinds` (this repo's version). Both work **if**
      the blinds are assigned to the Bedroom area; only the entity version works
      if they are not. If the blinds don't respond, this is the first thing to
      check.

### Testing end to end

Watch the Pi while using the knob:

```bash
journalctl -u sensehat-ha -f
```

Each input logs a line like `action: blinds_open`. That splits the problem
cleanly in two:

- **No log line** → the Pi isn't seeing the input (knob/device problem).
- **Log line but nothing happens** → the Pi sent it fine; the issue is on the
  HA side (automation missing, wrong topic, or wrong entity/area).

The interlock logs its decisions too, so you can see why nothing happened:

```
action: blinds_close                     <- move started
blinds: already closing — ignored        <- same way again, dropped
action: blinds_stop                      <- opposite way, stopped it
blinds: settling after stop — ignored    <- inside the 1s lockout
```

To test HA without touching the hardware, publish a command by hand:

```bash
python3 -c "import paho.mqtt.client as m,time; c=m.Client(m.CallbackAPIVersion.VERSION2); c.username_pw_set('pi_sensehat','pi3'); c.connect('192.168.1.221',1883); c.loop_start(); c.publish('sensehat/bedroom/blinds/set','open'); time.sleep(1)"
```

---

## History — what was built, and why

Roughly in order, so the odd-looking decisions have context:

1. **Sensors → HA over MQTT.** Chose MQTT discovery over the REST API so the
   entities create themselves and survive HA restarts.
2. **Mosquitto** had to be installed first — a network scan found HA on
   `192.168.1.221` but no broker anywhere. The login lives in the add-on's own
   **Logins** list (an HA *user* account also works, but wasn't what we used).
3. **LED matrix dashboard** (light + temp/humidity/pressure bars) was built,
   then later **scrapped**; the matrix is now off. Kept in git history and
   `sensehat_ha.py.matrix-version.bak`.
   - Learned along the way: the matrix is **5-bit per channel** with a gamma
     curve that crushes dim values, so colours can be dim *or* pastel, not
     both. Dim colours must stay saturated or they all render grey.
4. **Joystick orientation** was calibrated on-device. Several wrong guesses
   (mirrored axes) were resolved by logging raw events in all four
   orientations; the real fix was rotating by the **inverse** of the display
   rotation (`-deg`), not a reflection.
5. **Retargeted** from "all lights + brightness/colour" to **bedroom lights +
   blinds**, which renamed the MQTT topics. This is the only reason HA needs a
   new automation.
6. **Commands made non-retained.** Retained commands are wrong for one-shot
   actions — HA would replay the last one on restart. (A stale retained message
   on the old `sensehat/lights/set` topic was cleared.)
7. **Sense HAT swapped for a USB volume knob.** The HAT's absence was
   crash-looping the service, so the HAT became optional and the knob was added
   as a second front-end publishing the same commands.
   - Read via raw `input_event` structs rather than `evdev`, so there is no
     package to install (there is no passwordless sudo on this box).
   - Debounced, because one knob detent = one key event; a quick spin would
     otherwise fire a burst of identical commands at HA.
8. **Blinds interlock added.** Turning the knob back while the blinds were
   moving didn't stop them — the motor finished the current move and *then*
   ran the reversal, which was almost never what you wanted. Fixed Pi-side
   rather than in HA: the Pi now models whether the blinds are moving, converts
   an opposite-direction command into `cover.stop_cover`, drops same-direction
   repeats, and locks out new moves for a second afterwards.
   - The Pi has no direct view of the cover, so "is it moving?" is a **timer**
     (`BLINDS_TRAVEL_TIME`), optionally corrected by **state fed back from HA**
     on `sensehat/bedroom/blinds/state`. The timer is kept even with feedback
     available, as a backstop against the interlock latching on if HA goes
     quiet mid-move.
   - Feedback is only trusted to *end* a move once the cover has been seen to
     report `opening`/`closing` at least once — covers that only report
     `open`/`closed` would otherwise clear the interlock the moment a move
     started, reintroducing the original bug.
