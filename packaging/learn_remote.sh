#!/usr/bin/env bash
# Teach a Flirc dongle the buttons this television understands.
#
# Flirc is not a kernel IR receiver. Its own firmware turns infrared into USB keystrokes, so
# once it is taught, the box needs no IR support at all — EvdevDriver reads it as an ordinary
# keyboard, which is why nothing in tuner/ knows this script exists.
#
# Run it on the Pi, or over ssh with a terminal:  ssh -t boobtube 'bash -s' < learn_remote.sh
#
# Buttons are recorded one at a time and each can be retried or skipped, because a remote
# whose battery is going will record a button that then never repeats the same code.
set -uo pipefail

FLIRC=${FLIRC:-/usr/local/bin/flirc_util}
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"

# prompt|flirc key|what the box does with it
#
# vol_up/vol_down/mute are the HID consumer keys; they arrive on the same evdev node as the
# letters, which is why one device is enough. There is deliberately no `power` here: Flirc's
# vocabulary has no KEY_POWER, and its `suspend` would put the Pi to sleep rather than the
# television, so POWER is an F-key that EVDEV_MAP translates.
BUTTONS=(
  "1|1|channel 1"                       "2|2|channel 2"
  "3|3|channel 3"                       "4|4|channel 4"
  "5|5|channel 5"                       "6|6|channel 6"
  "7|7|channel 7"                       "8|8|channel 8"
  "9|9|channel 9"                       "0|0|channel 0"
  "CHANNEL UP|up|next channel"          "CHANNEL DOWN|down|previous channel"
  "VOLUME UP|vol_up|louder (repeats when held)"
  "VOLUME DOWN|vol_down|quieter (repeats when held)"
  "MUTE|mute|mute"
  "OK / SELECT|enter|select"            "MENU|tab|open the menu"
  "EXIT / BACK|escape|back"
  "POWER|F12|television on/off"
  "LAST CHANNEL|F1|jump to the previous channel"
  "GUIDE|F2|jump to the guide"
  "INFO / DISPLAY|F3|show the channel bug again"
)

echo
echo "  Teaching the Flirc. Point the remote at it and press the button named."
echo "  Enter = record it,  s = skip,  q = stop."
echo

recorded=0; skipped=0
for entry in "${BUTTONS[@]}"; do
  IFS='|' read -r label key does <<< "$entry"
  while true; do
    printf "  %-16s -> %-9s (%s)  [Enter/s/q] " "$label" "$key" "$does"
    read -r answer </dev/tty || answer=q
    case "$answer" in
      q|Q) echo; echo "  stopped."; break 2 ;;
      s|S) skipped=$((skipped+1)); break ;;
    esac
    echo "      press $label on the remote now..."
    if $SUDO "$FLIRC" record "$key" 2>&1 | sed 's/^/      /'; then
      recorded=$((recorded+1)); break
    fi
    echo "      nothing learned — try again, or s to skip."
  done
done

echo
echo "  recorded $recorded, skipped $skipped"
echo
$SUDO "$FLIRC" settings 2>&1 | sed -n '/Recorded Keys/,$p'
