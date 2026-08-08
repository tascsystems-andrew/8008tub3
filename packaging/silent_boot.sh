#!/usr/bin/env bash
#
# Make the Pi boot like an appliance: power on, mark, television. No Linux anywhere.
#
#     sudo bash packaging/silent_boot.sh          # apply
#     sudo bash packaging/silent_boot.sh --undo   # put it all back
#
# RUN THIS AFTER THE BOX WORKS, not during bring-up. Everything here exists to hide boot
# diagnostics, which are exactly what you want visible while you are still finding out
# whether the thing boots. --undo is here because that trade reverses the moment something
# goes wrong at 9pm.
#
# There is no single "quiet boot" switch. Five different components each draw to the screen
# and each is silenced differently:
#
#   1. Firmware rainbow square       config.txt      disable_splash=1
#   2. Kernel's four raspberries     cmdline.txt     logo.nologo
#   3. Kernel message wall           cmdline.txt     quiet loglevel=0
#   4. Blinking console cursor       cmdline.txt     vt.global_cursor_default=0
#   5. systemd/cloud-init chatter    cmdline.txt     console=tty3
#
# (5) is the one people miss. `quiet` suppresses the kernel, not userspace — without moving
# the console to an unused virtual terminal, service start-up text still scrolls over the
# splash. tty3 is chosen because nothing else uses it and Ctrl-Alt-F3 still reaches it.
#
# Plymouth then draws the mark over the top for the whole of boot. Lite does not ship it,
# so it is installed here.

set -euo pipefail

THEME_NAME="boobtube"
THEME_DIR="/usr/share/plymouth/themes/$THEME_NAME"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SPLASH_SRC="$REPO/packaging/assets/splash.png"

# Bookworm moved these under /boot/firmware. Support both rather than assume.
if [[ -d /boot/firmware ]]; then
    CFG=/boot/firmware/config.txt
    CMD=/boot/firmware/cmdline.txt
else
    CFG=/boot/config.txt
    CMD=/boot/cmdline.txt
fi

CMD_FLAGS=(logo.nologo quiet loglevel=0 vt.global_cursor_default=0 splash
           plymouth.ignore-serial-consoles)

say() { printf '\n\033[1;32m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m  ! %s\033[0m\n' "$1"; }

[[ $EUID -eq 0 ]] || { echo "Run with sudo."; exit 1; }

# ------------------------------------------------------------------------ undo ---
if [[ "${1:-}" == "--undo" ]]; then
    say "Restoring a normal, noisy boot"
    for flag in "${CMD_FLAGS[@]}"; do
        sed -i "s/ ${flag//./\\.}//g" "$CMD"
    done
    sed -i 's/console=tty3/console=tty1/' "$CMD"
    sed -i '/^disable_splash=1/d' "$CFG"
    plymouth-set-default-theme -R pix >/dev/null 2>&1 || true
    say "Done — reboot to see boot messages again"
    exit 0
fi

# --------------------------------------------------------------------- plymouth ---
say "Installing Plymouth"
apt-get update -qq
apt-get install -y --no-install-recommends plymouth plymouth-themes

if [[ ! -f "$SPLASH_SRC" ]]; then
    echo "No splash at $SPLASH_SRC — run 'python3 packaging/make_splash.py' on the Mac"
    echo "and re-deploy. It is a committed asset, so it should have arrived with the code."
    exit 1
fi

say "Installing the $THEME_NAME theme"
mkdir -p "$THEME_DIR"
install -m 0644 "$SPLASH_SRC" "$THEME_DIR/splash.png"

cat > "$THEME_DIR/$THEME_NAME.plymouth" <<THEME
[Plymouth Theme]
Name=BoobTube
Description=Mark on black, nothing else
ModuleName=script

[script]
ImageDir=$THEME_DIR
ScriptFile=$THEME_DIR/$THEME_NAME.script
THEME

# Deliberately no progress bar, no spinner, no messages. A television does not report its
# own progress. The splash is centred by measured size rather than assumed resolution, so
# it lands correctly whether the TV negotiates 1080p or 4K.
cat > "$THEME_DIR/$THEME_NAME.script" <<'SCRIPT'
Window.SetBackgroundTopColor(0, 0, 0);
Window.SetBackgroundBottomColor(0, 0, 0);

logo.image = Image("splash.png");
logo.sprite = Sprite(logo.image);
logo.sprite.SetX(Window.GetWidth()  / 2 - logo.image.GetWidth()  / 2);
logo.sprite.SetY(Window.GetHeight() / 2 - logo.image.GetHeight() / 2);

fun refresh_callback() { }
Plymouth.SetRefreshFunction(refresh_callback);

# Swallow the password and message callbacks. Without these, plymouth falls back to drawing
# text prompts over the splash for things like a filesystem check.
fun display_normal_callback() { }
Plymouth.SetDisplayNormalFunction(display_normal_callback);
SCRIPT

plymouth-set-default-theme -R "$THEME_NAME"

# ------------------------------------------------------------------ boot flags ---
say "Silencing the boot chain"

if ! grep -q '^disable_splash=1' "$CFG"; then
    printf '\n# No firmware rainbow square at power-on.\ndisable_splash=1\n' >> "$CFG"
fi

# cmdline.txt must stay a single line — a newline in it makes everything after the break
# invisible to the kernel, and the usual symptom is an unbootable Pi with no explanation.
LINE="$(tr -d '\n' < "$CMD")"
for flag in "${CMD_FLAGS[@]}"; do
    grep -qw -- "$flag" <<<"$LINE" || LINE="$LINE $flag"
done
LINE="${LINE//console=tty1/console=tty3}"
printf '%s\n' "$LINE" > "$CMD"

say "Done"
cat <<NEXT

  Reboot to see it. The chain is now:

    power  →  mark on black  →  television

  If anything goes wrong and you need the boot messages back:

    sudo bash packaging/silent_boot.sh --undo

  Boot text now lives on tty3 — Ctrl-Alt-F3 with a keyboard attached, or just read
  'journalctl -b' over ssh, which is unaffected by any of this.

NEXT
