# Deploying to a Raspberry Pi

**Status: untested.** These steps have not yet been run on a real Pi. Expect small
fixes on the first run.

Target: Raspberry Pi 4/5, Raspberry Pi OS (Bookworm, 64-bit) **with desktop** (needed for
the on-screen kiosk browser). Python 3.11 ships with Bookworm.

## 1. Install the app

```sh
sudo useradd --system --home /opt/macprinter --shell /usr/sbin/nologin macprinter
sudo git clone https://github.com/leftoverjacksons/macPrinter.git /opt/macprinter
sudo chown -R macprinter: /opt/macprinter
sudo -u macprinter python3 -m venv /opt/macprinter/.venv
sudo -u macprinter /opt/macprinter/.venv/bin/pip install -e /opt/macprinter
```

Update later with `sudo -u macprinter git -C /opt/macprinter pull && sudo systemctl restart macprinter`.

## 2. Network setup for dongles under test

```sh
sudo install -m 600 deploy/macprinter-dongle.nmconnection /etc/NetworkManager/system-connections/
sudo install -m 644 deploy/10-macprinter-usb-nic.link /etc/systemd/network/
sudo nmcli connection reload
# .link files apply when a device appears: re-plug dongles (or reboot) after installing
```

Check with a dongle plugged in and cabled:
`nmcli device` (dongle connected via `macprinter-dongle`) and `ip route` (its default
route has metric 900, the Pi's own uplink a lower one).

## 3. Run as a service

```sh
sudo install -m 644 deploy/macprinter.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now macprinter
journalctl -u macprinter -f
```

Dashboard: `http://<pi-address>:8000` from any PC on the LAN, or `http://localhost:8000` on the Pi.

## 4. Kiosk screen

Open Chromium full-screen at login. On current Bookworm (labwc), add this line to
`~/.config/labwc/autostart` of the desktop user (older Wayfire images: an `[autostart]`
entry in `~/.config/wayfire.ini`):

```sh
chromium-browser --kiosk --noerrdialogs --disable-infobars --no-first-run http://localhost:8000 &
```

(The binary may be named `chromium` on newer images.)

## 5. Printer (later)

Printing is not wired up yet. When it is: install Canon's UFR II driver (`cnrdrvcups-lb`,
arm64 build), add the MF3010 queue in CUPS, and add `macprinter` to the `lpadmin` group.
