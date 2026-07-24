# Serial Hardware (generic USB-serial)

mockingbuoy is hardware-agnostic. Any USB-serial adapter that presents a `/dev/ttyUSB*` (or
`/dev/ttyACM*`) node works. A channel's electrical nature is irrelevant to the software beyond three
config knobs: `baud`, `framing` (8N1), and `direction`.

## Simplex vs full-duplex

- **Simplex (TX-only):** `direction: "tx"`. The channel only transmits; no reader thread is started.
  Typical for one-way talker outputs (e.g. differential RS-422 drive).
- **Full-duplex (TX+RX):** `direction: "both"`. A bidirectional adapter can also receive; the RX reader
  parses inbound sentences (see architecture.md). Nothing else changes — same class, same config.
- **Inbound-only:** `direction: "rx"`.

Switching simplex↔duplex is a config change, never a code change. If an adapter has a hardware
mode/termination switch (RS-232/422/485), set it per the adapter's own manual; the software is unaware.

## Persistent device naming (critical)

`/dev/ttyUSB*` enumeration order is **not stable** across reboots/replug when multiple adapters are
present. Bind every channel by a stable path:

- Prefer **`/dev/serial/by-id/...`** (includes the per-unit serial string) — robust when each adapter has
  a unique serial number.
- If adapters share a serial (or have none), fall back to **`/dev/serial/by-path/...`** (physical USB
  port) or a udev `SYMLINK` rule keyed on the port path. Label the physical ports.

Discover attributes:
```bash
ls -l /dev/serial/by-id/  /dev/serial/by-path/
udevadm info -a -n /dev/ttyUSB0        # ATTRS{serial}, KERNELS, ATTRS{devpath}
udevadm info -q property -n /dev/ttyUSB0
```

Example udev rules (`/etc/udev/rules.d/99-mockingbuoy.rules`) — serial-based:
```udev
SUBSYSTEM=="tty", ATTRS{serial}=="<UNIT_A_SERIAL>", SYMLINK+="nmea-gps"
SUBSYSTEM=="tty", ATTRS{serial}=="<UNIT_B_SERIAL>", SYMLINK+="nmea-heading"
SUBSYSTEM=="tty", ATTRS{serial}=="<UNIT_C_SERIAL>", SYMLINK+="nmea-ais"
```
Port-path fallback (identical/serial-less adapters):
```udev
SUBSYSTEM=="tty", DRIVERS=="ftdi_sio", KERNELS=="<BUS-PORT>", SYMLINK+="nmea-gps"
```
Apply: `sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=tty`.

## Gotchas

- **Custom-PID devices:** some USB-serial gateways use a vendor-specific FTDI PID that older kernels
  don't auto-bind. Register it via a udev rule or `new_id`:
  `echo <VID> <PID> | sudo tee /sys/bus/usb-serial/drivers/ftdi_sio/new_id`. Modern kernels usually
  bind common PIDs automatically.
- **FTDI latency timer** defaults to **16 ms**, adding jitter. Lower to 1 ms:
  ```udev
  ACTION=="add", SUBSYSTEM=="usb-serial", DRIVER=="ftdi_sio", ATTR{latency_timer}="1"
  ```
- **`brltty`** (braille daemon on Debian/Bookworm) grabs some USB-serial adapters, making the tty appear
  then vanish. Mask/remove it: `sudo systemctl mask brltty-udev.service brltty.service` or
  `sudo apt-get purge brltty`.
- **Permissions:** serial nodes are group `dialout`. Run as a non-root user in `dialout` (the systemd
  unit uses `SupplementaryGroups=dialout` + explicit `DeviceAllow=`).
- **CRLF:** the app writes raw `b"\r\n"` on a binary port — never rely on text-mode newline translation.

## Verifying output

```bash
# Read a channel with a serial terminal:
minicom -D /dev/serial/by-id/<...> -b 4800
screen /dev/serial/by-id/<...> 4800
cat /dev/serial/by-id/<...>
```
Or point a second machine / pyserial reader at the line and parse it back with `pynmea2` / `pyais`.
