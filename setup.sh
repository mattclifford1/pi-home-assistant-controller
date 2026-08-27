#!/usr/bin/env bash
# One-shot setup for the Sense HAT -> Home Assistant bridge.
# Run with sudo:   sudo bash /home/matt/sensehat-ha/setup.sh
set -euo pipefail

echo "==> Enabling I2C (needed by the Sense HAT)"
raspi-config nonint do_i2c 0

echo "==> Installing packages (Sense HAT library + MQTT client)"
apt-get update
apt-get install -y sense-hat python3-paho-mqtt

echo "==> Installing systemd service"
install -m 644 /home/matt/sensehat-ha/sensehat-ha.service \
  /etc/systemd/system/sensehat-ha.service
systemctl daemon-reload
systemctl enable sensehat-ha.service

echo
echo "Setup done."
echo "I2C was just enabled, so REBOOT before starting the service:"
echo "    sudo reboot"
echo
echo "After reboot the service starts automatically. Check it with:"
echo "    systemctl status sensehat-ha"
echo "    journalctl -u sensehat-ha -f"
