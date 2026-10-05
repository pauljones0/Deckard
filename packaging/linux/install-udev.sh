#!/bin/sh
set -eu
[ "$(id -u)" -eq 0 ] || { echo 'USB rules require administrator authentication.' >&2; exit 1; }
bundle="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
install -m644 "$bundle/share/deckard/60-deckard.rules" /etc/udev/rules.d/60-deckard.rules
udevadm control --reload-rules
udevadm trigger --subsystem-match=usb
udevadm trigger --subsystem-match=hidraw
