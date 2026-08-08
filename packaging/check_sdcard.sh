#!/usr/bin/env bash
#
# Inspect a freshly-flashed Raspberry Pi card before it ever boots.
#
#     bash packaging/check_sdcard.sh
#
# Everything here is checkable from the Mac, while the card's boot partition is still
# mounted, and every one of these problems is dramatically cheaper to fix now than later.
#
# Two of them are the CEC traps that `tub3.cec check` can only *report* once the Pi is
# running — by which point the fix costs an edit, a reboot, and a second diagnosis. The
# rest are the headless-install traps, where the failure mode is a Pi that boots fine and
# is simply unreachable, with no screen to find out why.
#
# Read-only by default. `--fix` applies the corrections it is confident about.

set -uo pipefail

WANT_KEY_BODY="AAAAC3NzaC1lZDI1NTE5AAAAIEiliTd2069z224qQEaME/n9YYmk1icSO88JPVqJG9V8"
WANT_HOST="boobtube"
WANT_USER="andrew"
WANT_TZ="America/Vancouver"

FIX=0
[[ "${1:-}" == "--fix" ]] && FIX=1

pass() { printf '  \033[1;32m✓\033[0m %s\n' "$1"; }
fail() { printf '  \033[1;31m✗\033[0m %s\n' "$1"; PROBLEMS=$((PROBLEMS + 1)); }
warn() { printf '  \033[1;33m!\033[0m %s\n' "$1"; }
note() { printf '      %s\n' "$1"; }
PROBLEMS=0

# ------------------------------------------------------------------ find the card ---
BOOT=""
for candidate in /Volumes/bootfs /Volumes/boot; do
    [[ -d "$candidate" ]] && BOOT="$candidate" && break
done

if [[ -z "$BOOT" ]]; then
    echo
    echo "  No Raspberry Pi boot partition mounted."
    echo
    echo "  Imager unmounts the card while writing and again when it verifies. Wait for"
    echo "  \"Write Successful\", then pull the card out and put it back — macOS will mount"
    echo "  the small FAT partition as /Volumes/bootfs. Then run this again."
    echo
    exit 1
fi

echo
echo "  Checking $BOOT"
echo

# ------------------------------------------------------------------ the settings ---
# Imager 1.9+ writes a single custom.toml. Older versions wrote firstrun.sh, userconf.txt
# and an empty `ssh` marker. Rather than guess the version, search whatever is there.
SETTINGS="$(cat "$BOOT"/custom.toml "$BOOT"/firstrun.sh "$BOOT"/userconf.txt 2>/dev/null)"

if [[ -z "$SETTINGS" ]]; then
    fail "No Imager customisation found on the card"
    note "You clicked Write without opening the gear (⚙) dialog. A Lite image with no"
    note "customisation has no user account and no SSH — it cannot be logged into at all."
    note "Re-flash, and set hostname, user, SSH key and wifi in the gear dialog."
    echo
    exit 1
fi

# --- SSH, and the right key -------------------------------------------------------
# Match the whole `ssh-ed25519 <body>` form, not just the body. sshd parses each
# authorized_keys line as "<type> <base64> [comment]" and silently skips anything that does
# not start with an algorithm name — so a paste of the base64 alone looks completely correct
# to a human, and fails at the only moment it matters, over a network needing that key.
if grep -q "ssh-ed25519[[:space:]]\+$WANT_KEY_BODY" <<<"$SETTINGS"; then
    pass "SSH authorised key is the BoobTube key"
elif grep -q "$WANT_KEY_BODY" <<<"$SETTINGS"; then
    fail "The BoobTube key is on the card, but WITHOUT its 'ssh-ed25519 ' prefix"
    note "sshd skips a line that does not begin with an algorithm name, so this card"
    note "would boot and refuse your key."
    if [[ $FIX -eq 1 && -f "$BOOT/custom.toml" ]]; then
        # Prepend the algorithm name to the bare body wherever it appears. Anchored on the
        # body itself so an already-correct key elsewhere in the file is left alone.
        sed -i '' "s|\"$WANT_KEY_BODY|\"ssh-ed25519 $WANT_KEY_BODY|g" "$BOOT/custom.toml"
        if grep -q "ssh-ed25519[[:space:]]\+$WANT_KEY_BODY" "$BOOT/custom.toml"; then
            note "→ repaired in custom.toml; no re-flash needed"
            PROBLEMS=$((PROBLEMS - 1))
        else
            note "→ could not repair automatically; edit $BOOT/custom.toml by hand"
        fi
    else
        note "Re-run with --fix to repair it in place, or edit $BOOT/custom.toml so the"
        note "authorized_keys entry reads:"
        note "  \"ssh-ed25519 $WANT_KEY_BODY andrew@MacBook-Pro-2-boobtube\""
    fi
elif grep -qE 'ssh-(ed25519|rsa)|ssh_import_id' <<<"$SETTINGS"; then
    fail "SSH key present, but it is NOT ~/.ssh/boobtube.pub"
    note "Imager pre-filled a different key from this Mac. You would still get in if that"
    note "key's private half is here, but the ssh config entry names ~/.ssh/boobtube and"
    note "sets IdentitiesOnly, so \`ssh boobtube\` will refuse. Re-flash with the right key."
else
    fail "No SSH public key on the card"
    note "Headless Lite with no key and no password is unreachable. Re-flash."
fi

if grep -qiE '^[[:space:]]*enabled[[:space:]]*=[[:space:]]*true' <<<"$SETTINGS" \
   || [[ -e "$BOOT/ssh" ]] || grep -q 'systemctl enable ssh' <<<"$SETTINGS"; then
    pass "SSH is enabled"
else
    warn "Could not confirm SSH is enabled — check the gear dialog's Services tab"
fi

# --- hostname and user ------------------------------------------------------------
if grep -q "\"$WANT_HOST\"\|=$WANT_HOST\|$WANT_HOST" <<<"$SETTINGS"; then
    pass "Hostname is $WANT_HOST"
else
    fail "Hostname is not '$WANT_HOST'"
    note "~/.ssh/config points 'boobtube' at boobtube.local. A different hostname means"
    note "you have to find its IP address on every connection."
fi

if grep -qE "(^|[^a-z])$WANT_USER([^a-z]|$)" <<<"$SETTINGS"; then
    pass "User is $WANT_USER"
else
    fail "User is not '$WANT_USER' — the ssh config entry assumes it"
fi

# --- wifi -------------------------------------------------------------------------
if grep -qiE '^\[wlan\]|ssid' <<<"$SETTINGS"; then
    pass "Wifi is configured"
else
    warn "No wifi on the card — this is only safe if it is going straight onto ethernet"
    note "There is no screen on a Lite install. If ethernet does not come up, the only"
    note "way to fix it is to re-flash."
fi

# --- timezone ---------------------------------------------------------------------
if grep -q "$WANT_TZ" <<<"$SETTINGS"; then
    pass "Timezone is $WANT_TZ"
else
    TZ_FOUND="$(grep -oE '[A-Za-z]+/[A-Za-z_]+' <<<"$SETTINGS" | grep -v ssh | head -1)"
    fail "Timezone is ${TZ_FOUND:-unset}, not $WANT_TZ"
    note "This one is not cosmetic. Channels are a virtual clock — the schedule is built"
    note "against local time, so the wrong zone puts the dinner strip at the wrong hour."
    if [[ $FIX -eq 1 && -f "$BOOT/custom.toml" ]]; then
        sed -i '' "s|timezone = \".*\"|timezone = \"$WANT_TZ\"|" "$BOOT/custom.toml" \
            && note "→ fixed in custom.toml"
    fi
fi

echo

# ------------------------------------------------------------------------- CEC ---
# The two silent killers. Both are one line in a text file, and both are far cheaper to
# correct here than after a boot, a diagnosis and a reboot.

CFG="$BOOT/config.txt"
CMD="$BOOT/cmdline.txt"

if [[ -f "$CFG" ]] && grep -q '^dtoverlay=vc4-kms-v3d' "$CFG"; then
    pass "config.txt loads vc4-kms-v3d (CEC needs the KMS driver)"
elif [[ -f "$CFG" ]]; then
    fail "config.txt is missing dtoverlay=vc4-kms-v3d — CEC will never initialise"
    if [[ $FIX -eq 1 ]]; then
        printf '\ndtoverlay=vc4-kms-v3d\n' >> "$CFG" && note "→ appended"
    else
        note "Add:  dtoverlay=vc4-kms-v3d"
    fi
else
    warn "No config.txt found at $BOOT — unusual; check the card mounted fully"
fi

if [[ -f "$CMD" ]] && grep -qE 'video=HDMI-A-[0-9]:[^ ]*D( |$)' "$CMD"; then
    fail "cmdline.txt forces DVI mode (a trailing 'D') — this disables CEC entirely"
    note "DVI has no CEC line, so the subsystem is skipped and the remote does nothing."
    if [[ $FIX -eq 1 ]]; then
        sed -i '' -E 's|(video=HDMI-A-[0-9]:[^ ]*)D( |$)|\1\2|g' "$CMD" \
            && note "→ trailing D removed"
    else
        note "Delete the trailing D from the video= setting."
    fi
elif [[ -f "$CMD" ]]; then
    pass "cmdline.txt does not force DVI"
fi

echo
if [[ $PROBLEMS -eq 0 ]]; then
    printf '  \033[1;32mCard looks right.\033[0m Eject it and boot the Pi.\n\n'
    echo "  HDMI goes in the port NEAREST THE USB-C socket. That is the only port with"
    echo "  CEC on a Pi 5, and the wrong one fails silently — the remote just does nothing."
    echo
    echo "  Then:   ssh boobtube"
    echo
else
    printf '  \033[1;31m%d problem(s).\033[0m ' "$PROBLEMS"
    if [[ $FIX -eq 0 ]]; then
        echo "Re-run with --fix to correct the ones that are safe to."
    else
        echo "Anything still listed needs a re-flash."
    fi
    echo
fi
