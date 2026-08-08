#!/usr/bin/env bash
#
# Inspect a freshly-flashed Raspberry Pi card before it ever boots.
#
#     bash packaging/check_sdcard.sh            # report only
#     bash packaging/check_sdcard.sh --fix      # correct what is safely correctable
#
# Everything here is checkable from the Mac while the card's boot partition is still
# mounted, and every one of these problems is dramatically cheaper to fix now than later.
# Two are the CEC traps that `tub3.cec check` can only *report* once the Pi is running, by
# which point the fix costs a boot, a diagnosis and a reboot. The rest are the headless
# traps, where the failure is a Pi that boots perfectly and is simply unreachable, with no
# screen to find out why.
#
# Every check here has already caught a real error on a real card:
#   - the Desktop image flashed instead of Lite
#   - a public key pasted without its "ssh-ed25519 " prefix, alongside ssh_pwauth: false
#   - the wifi regulatory domain set to Seychelles instead of Canada
#
# Two customisation formats exist and the script must read both. Bookworm-era Imager writes
# custom.toml; Trixie-era writes cloud-init (user-data / meta-data / network-config). Which
# one is present is also the most reliable way to tell the two releases apart from the card.

set -uo pipefail

WANT_KEY_BODY="AAAAC3NzaC1lZDI1NTE5AAAAIEiliTd2069z224qQEaME/n9YYmk1icSO88JPVqJG9V8"
WANT_HOST="boobtube"
WANT_USER="andrew"
WANT_TZ="America/Vancouver"
WANT_REGDOM="CA"

FIX=0
[[ "${1:-}" == "--fix" ]] && FIX=1

pass() { printf '  \033[1;32m✓\033[0m %s\n' "$1"; }
fail() { printf '  \033[1;31m✗\033[0m %s\n' "$1"; PROBLEMS=$((PROBLEMS + 1)); }
warn() { printf '  \033[1;33m!\033[0m %s\n' "$1"; }
note() { printf '      %s\n' "$1"; }
PROBLEMS=0

# ------------------------------------------------------------------ find the card ---
BOOT=""
# BOOTFS is an override so the checks can be exercised against a mock card. Every branch
# below is destructive-ish on a real card; none of it should go untested for want of one.
for candidate in ${BOOTFS:-} /Volumes/bootfs /Volumes/boot; do
    [[ -d "$candidate" ]] && BOOT="$candidate" && break
done

if [[ -z "$BOOT" ]]; then
    cat <<'MSG'

  No Raspberry Pi boot partition mounted.

  Imager unmounts the card while writing and again when it verifies. Wait for
  "Write Successful", then pull the card and reinsert it — macOS mounts the small
  FAT partition as /Volumes/bootfs. Then run this again.

MSG
    exit 1
fi

echo
echo "  Checking $BOOT"
echo

# --------------------------------------------------------------- which image is it ---
# pi-gen build stages: stage2 is Lite, stage4 is the full Desktop image. The Desktop image
# auto-logins to a compositor that owns the display, and the tuner draws straight to DRM —
# both claiming the screen means the television comes up black.
STAGE="$(grep -oE 'stage[0-9]' "$BOOT/issue.txt" 2>/dev/null | tail -1)"
case "$STAGE" in
    stage2) pass "Raspberry Pi OS Lite (pi-gen $STAGE)" ;;
    stage3|stage4|stage5)
        fail "This is a DESKTOP image (pi-gen $STAGE), not Lite"
        note "The desktop auto-logins to a compositor that owns the display. The tuner"
        note "draws direct to DRM, so both would fight for the screen. Re-flash with"
        note "Raspberry Pi OS Lite (64-bit) — it is under 'Raspberry Pi OS (other)'." ;;
    *)      warn "Could not read the build stage from issue.txt" ;;
esac

# --------------------------------------------------------------- the settings files ---
# Read whichever format is present rather than guessing the Imager version.
SETTINGS=""
RELEASE=""
if [[ -f "$BOOT/custom.toml" ]]; then
    SETTINGS="$(cat "$BOOT/custom.toml")"
    RELEASE="Bookworm"
elif [[ -f "$BOOT/user-data" ]]; then
    SETTINGS="$(cat "$BOOT/user-data" "$BOOT/network-config" 2>/dev/null)"
    RELEASE="Trixie"
elif [[ -f "$BOOT/firstrun.sh" || -f "$BOOT/userconf.txt" ]]; then
    SETTINGS="$(cat "$BOOT/firstrun.sh" "$BOOT/userconf.txt" 2>/dev/null)"
    RELEASE="Bookworm or older"
fi

if [[ -z "$SETTINGS" ]]; then
    fail "No Imager customisation found on the card"
    note "You wrote without opening the gear (⚙) dialog. A Lite image with no"
    note "customisation has no user account and no SSH — it cannot be logged into."
    note "Re-flash, setting hostname, user, SSH key, wifi and timezone."
    echo
    exit 1
fi

# The customisation format is the reliable release tell: cloud-init arrived with Trixie.
if [[ "$RELEASE" == "Trixie" ]]; then
    fail "This looks like Trixie (cloud-init customisation)"
    note "FieldStation42 documents Trixie support as incomplete, and Trixie is Python"
    note "3.13 where everything here is tested on 3.11. Re-flash with"
    note "'Raspberry Pi OS (Legacy, 64-bit) Lite', which is Bookworm."
else
    pass "$RELEASE-era customisation"
fi

# --- SSH, and the right key -------------------------------------------------------
# sshd parses authorized_keys as "<type> <base64> [comment]" and silently skips any line
# not starting with an algorithm name. A body-only paste looks perfectly correct to a
# human and fails at the one moment it matters — over a network that needs that key.
if grep -q "ssh-ed25519[[:space:]]\+$WANT_KEY_BODY" <<<"$SETTINGS"; then
    pass "SSH authorised key is the BoobTube key, correctly formed"
    # Imager's key box accumulates entries across sessions, so an earlier bad paste can
    # still be sitting above a later good one. Both get installed. The malformed line is
    # inert — sshd skips it and it grants nothing — but it is exactly the artefact that
    # wastes twenty minutes when you are debugging something else months later.
    STRAY="$(grep -cE "(^|['\"[:space:]])$WANT_KEY_BODY" <<<"$SETTINGS")"
    GOOD="$(grep -cE "ssh-ed25519[[:space:]]+$WANT_KEY_BODY" <<<"$SETTINGS")"
    if [[ "$STRAY" -gt "$GOOD" ]]; then
        warn "There is also a bare copy of the key with no 'ssh-ed25519 ' prefix"
        note "Harmless — sshd skips it — but worth removing. Clear the old entry from"
        note "Imager's key box next time; it keeps what you pasted before."
    fi
elif grep -q "$WANT_KEY_BODY" <<<"$SETTINGS"; then
    fail "The BoobTube key is on the card WITHOUT its 'ssh-ed25519 ' prefix"
    note "sshd would skip the line, so this card boots and refuses your key."
    if [[ $FIX -eq 1 ]]; then
        for f in "$BOOT/custom.toml" "$BOOT/user-data"; do
            [[ -f "$f" ]] && sed -i '' "s|\"$WANT_KEY_BODY|\"ssh-ed25519 $WANT_KEY_BODY|g" "$f"
        done
        if grep -q "ssh-ed25519[[:space:]]\+$WANT_KEY_BODY" \
             "$BOOT/custom.toml" "$BOOT/user-data" 2>/dev/null; then
            note "→ repaired in place; no re-flash needed"
            PROBLEMS=$((PROBLEMS - 1))
        else
            note "→ could not repair automatically; edit the file by hand"
        fi
    else
        note "Re-run with --fix to repair it in place."
    fi
elif grep -qE 'ssh-(ed25519|rsa)' <<<"$SETTINGS"; then
    fail "An SSH key is present, but it is NOT ~/.ssh/boobtube.pub"
    note "Imager pre-filled a different key. ~/.ssh/config names ~/.ssh/boobtube and"
    note "sets IdentitiesOnly, so 'ssh boobtube' will refuse. Re-flash."
else
    fail "No SSH public key on the card"
fi

# Password auth off + a broken key = unreachable. Worth saying together.
if grep -qE 'ssh_pwauth:[[:space:]]*false|password_authentication[[:space:]]*=[[:space:]]*false' \
     <<<"$SETTINGS"; then
    note "(password SSH is disabled on this card, so the key is the only way in)"
fi

# --- hostname, user, timezone -----------------------------------------------------
grep -q "$WANT_HOST" <<<"$SETTINGS" \
    && pass "Hostname is $WANT_HOST" \
    || { fail "Hostname is not '$WANT_HOST'"
         note "~/.ssh/config points 'boobtube' at boobtube.local."; }

# Three spellings across the three formats: cloud-init `name: andrew`, custom.toml
# `name = "andrew"`, and firstrun.sh `usermod -l "andrew"` / `userconf 'andrew'`. Missing
# the third made a correct card report a wrong username.
if grep -qE "name:[[:space:]]*$WANT_USER|name[[:space:]]*=[[:space:]]*\"?$WANT_USER|usermod -l \"$WANT_USER\"|userconf '$WANT_USER'" \
     <<<"$SETTINGS"; then
    pass "User is $WANT_USER"
else
    fail "User is not '$WANT_USER' — the ssh config entry assumes it"
fi

if grep -q "$WANT_TZ" <<<"$SETTINGS"; then
    pass "Timezone is $WANT_TZ"
else
    fail "Timezone is not $WANT_TZ"
    note "Not cosmetic: channels are a virtual clock built against local time, so the"
    note "wrong zone puts the dinner strip at the wrong hour."
    if [[ $FIX -eq 1 ]]; then
        for f in "$BOOT/custom.toml" "$BOOT/user-data"; do
            [[ -f "$f" ]] && sed -i '' \
                -e "s|timezone = \".*\"|timezone = \"$WANT_TZ\"|" \
                -e "s|^timezone:.*|timezone: $WANT_TZ|" "$f"
        done
        note "→ set to $WANT_TZ"
        PROBLEMS=$((PROBLEMS - 1))
    fi
fi

# --- wifi, and the region that decides whether it works ---------------------------
if grep -qiE '^\[wlan\]|ssid|access-points' <<<"$SETTINGS"; then
    pass "Wifi is configured"
    REGDOM="$(grep -oE 'regulatory-domain:[[:space:]]*"?[A-Z]{2}|country[[:space:]]*=[[:space:]]*"?[A-Z]{2}' \
              <<<"$SETTINGS" | grep -oE '[A-Z]{2}$' | head -1)"
    if [[ -z "$REGDOM" ]]; then
        warn "No wifi regulatory domain set"
    elif [[ "$REGDOM" == "$WANT_REGDOM" ]]; then
        pass "Wifi region is $REGDOM"
    else
        fail "Wifi region is $REGDOM, not $WANT_REGDOM"
        note "The region decides which channels the radio may use. If the AP sits on a"
        note "channel that region disallows, the Pi never sees the network — it boots"
        note "fine and is simply invisible."
        if [[ $FIX -eq 1 ]]; then
            for f in "$BOOT/network-config" "$BOOT/custom.toml" "$BOOT/cmdline.txt"; do
                [[ -f "$f" ]] && sed -i '' \
                    -e "s|regulatory-domain: \"$REGDOM\"|regulatory-domain: \"$WANT_REGDOM\"|" \
                    -e "s|country = \"$REGDOM\"|country = \"$WANT_REGDOM\"|" \
                    -e "s|ieee80211_regdom=$REGDOM|ieee80211_regdom=$WANT_REGDOM|" "$f"
            done
            note "→ set to $WANT_REGDOM in network-config and cmdline.txt"
            PROBLEMS=$((PROBLEMS - 1))
        fi
    fi
else
    warn "No wifi on the card — safe only if it goes straight onto ethernet"
    note "There is no screen on Lite. If ethernet does not come up, the only fix is"
    note "to re-flash."
fi

echo

# ------------------------------------------------------------------------- CEC ---
CFG="$BOOT/config.txt"
CMD="$BOOT/cmdline.txt"

if [[ -f "$CFG" ]] && grep -q '^dtoverlay=vc4-kms-v3d' "$CFG"; then
    pass "config.txt loads vc4-kms-v3d (CEC needs the KMS driver)"
elif [[ -f "$CFG" ]]; then
    fail "config.txt is missing dtoverlay=vc4-kms-v3d — CEC will never initialise"
    if [[ $FIX -eq 1 ]]; then
        printf '\ndtoverlay=vc4-kms-v3d\n' >> "$CFG" && note "→ appended"
    fi
else
    warn "No config.txt at $BOOT — check the card mounted fully"
fi

# One character: a trailing D on a video= setting forces DVI, and DVI has no CEC line.
if [[ -f "$CMD" ]] && grep -qE 'video=HDMI-A-[0-9]:[^ ]*D( |$)' "$CMD"; then
    fail "cmdline.txt forces DVI mode (a trailing 'D') — this disables CEC entirely"
    if [[ $FIX -eq 1 ]]; then
        sed -i '' -E 's|(video=HDMI-A-[0-9]:[^ ]*)D( |$)|\1\2|g' "$CMD" \
            && note "→ trailing D removed"
    fi
elif [[ -f "$CMD" ]]; then
    pass "cmdline.txt does not force DVI"
fi

echo
if [[ $PROBLEMS -eq 0 ]]; then
    printf '  \033[1;32mCard looks right.\033[0m Eject it and boot the Pi.\n\n'
    cat <<'NEXT'
  HDMI goes in the port NEAREST THE USB-C socket. That is the only port with CEC on
  a Pi 5, and the wrong one fails silently — the remote just does nothing.

  First boot resizes the filesystem and reboots itself; give it two minutes. Then:

    ssh boobtube
    bash packaging/deploy_to_pi.sh     (from the Mac, first)

NEXT
else
    printf '  \033[1;31m%d problem(s).\033[0m ' "$PROBLEMS"
    [[ $FIX -eq 0 ]] && echo "Re-run with --fix to correct what is correctable." \
                     || echo "Anything still listed needs a re-flash."
    echo
fi
