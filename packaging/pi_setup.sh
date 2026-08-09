#!/usr/bin/env bash
#
# Provision a Raspberry Pi as an 8008TUB3 appliance.
#
# Idempotent — safe to re-run. Run on a fresh Raspberry Pi OS Lite (Bookworm, 64-bit):
#
#     sudo bash packaging/pi_setup.sh
#
# Two services, deliberately split, and the split is the point. The build side needs moviepy,
# ffmpeg-python and FieldStation42, and FieldStation42 calls sys.exit() at module scope when
# ffmpeg is missing — an import that can kill the process. The tuner must never die, so it
# gets mpv and the standard library and nothing else. A build failure cannot take the
# television down because they do not share an interpreter.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="${SUDO_USER:-$(id -un)}"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"

say() { printf '\n\033[1;32m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m  ! %s\033[0m\n' "$1"; }

[[ $EUID -eq 0 ]] || { echo "Run with sudo."; exit 1; }

# ---------------------------------------------------------------- packages ---
say "Installing packages"
apt-get update -qq
apt-get install -y --no-install-recommends \
    mpv ffmpeg python3 python3-venv python3-pip git \
    v4l-utils libcec6 cec-utils \
    fonts-dejavu-core \
    samba samba-common-bin wsdd avahi-daemon

# v4l-utils gives cec-ctl, which is the kernel CEC interface. cec-utils gives cec-client,
# kept as a fallback because some TVs answer one and not the other.

# ------------------------------------------------------------------- python ---
say "Creating the build virtualenv"
sudo -u "$RUN_USER" python3 -m venv "$REPO/.venv-build"
sudo -u "$RUN_USER" "$REPO/.venv-build/bin/pip" -q install --upgrade pip
# Four packages, not the forty in FieldStation42's requirements.txt. The rest are for its
# own player, OSD, web server and TUI, none of which we use — and PySide6 alone is hundreds
# of megabytes on a Pi.
sudo -u "$RUN_USER" "$REPO/.venv-build/bin/pip" -q install \
    moviepy ffmpeg-python mutagen jsonschema

if [[ ! -d "$REPO/vendor/FieldStation42" ]]; then
    say "Cloning FieldStation42 at the pinned commit"
    PIN="$(grep -oE '[0-9a-f]{40}' "$REPO/UPSTREAM.md" | head -1)"
    sudo -u "$RUN_USER" git clone -q https://github.com/shane-mason/FieldStation42.git \
        "$REPO/vendor/FieldStation42"
    sudo -u "$RUN_USER" git -C "$REPO/vendor/FieldStation42" checkout -q "$PIN"
fi

# ---------------------------------------------------------------------- CEC ---
say "Checking HDMI-CEC"
if ! grep -q '^dtoverlay=vc4-kms-v3d' /boot/firmware/config.txt 2>/dev/null; then
    warn "vc4-kms-v3d overlay missing from config.txt — CEC will not initialise"
fi
# The one-character trap: a trailing D on a video= setting forces DVI, and DVI has no CEC
# line, so the whole subsystem silently never starts.
if grep -qE 'video=HDMI-A-[0-9]:[^ ]*D( |$)' /boot/firmware/cmdline.txt 2>/dev/null; then
    warn "cmdline.txt forces DVI mode (trailing 'D') — this disables CEC entirely"
fi
usermod -aG video,input,render "$RUN_USER" || true
# Without this, /dev/cec0 and /dev/input/event* need root and the tuner would have to run as
# root to read a remote.
cat > /etc/udev/rules.d/99-tub3-input.rules <<'RULES'
KERNEL=="cec[0-9]*", GROUP="video", MODE="0660"
SUBSYSTEM=="input", GROUP="input", MODE="0660"
RULES
udevadm control --reload-rules || true

# ------------------------------------------------------------------ services ---
say "Installing services"

cat > /etc/systemd/system/tub3-tuner.service <<SERVICE
[Unit]
Description=8008TUB3 tuner
After=network-online.target
Wants=network-online.target
# A television comes back on its own. Never give up restarting it.
#
# This key belongs in [Unit], not [Service]. systemd parses it there and silently ignores
# it anywhere else — logging "Unknown key" once per boot and then applying the default,
# which gives up after 5 restarts in 10 seconds. For an appliance that is the whole point
# of the setting, and the failure is invisible: the box simply stops coming back one day.
StartLimitIntervalSec=0

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$REPO
Environment=PYTHONUNBUFFERED=1
# The tuner uses mpv and the standard library only. No virtualenv, so a broken build
# environment cannot stop the television starting.
ExecStart=/usr/bin/python3 -m tub3.app --fullscreen --headless
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
SERVICE

# The settings page is its own service, deliberately. It must be reachable when the
# television is *not* working — which is exactly when you need it — so it cannot be a
# thread inside the process that is failing.
cat > /etc/systemd/system/tub3-web.service <<SERVICE
[Unit]
Description=8008TUB3 settings page
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$REPO
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 -m tub3.web --host 0.0.0.0 --port 8008
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
SERVICE

cat > /etc/systemd/system/tub3-build.service <<SERVICE
[Unit]
Description=8008TUB3 schedule build
After=network-online.target

[Service]
Type=oneshot
User=$RUN_USER
WorkingDirectory=$REPO
ExecStart=$REPO/.venv-build/bin/python -m tub3.supervisor --once
SERVICE

cat > /etc/systemd/system/tub3-build.timer <<'TIMER'
[Unit]
Description=Keep the schedule ahead of the clock

[Timer]
OnBootSec=2min
OnUnitActiveSec=6h
Persistent=true

[Install]
WantedBy=timers.target
TIMER

# --------------------------------------------------------------- network storage ---
say "Installing the storage helper"
apt-get install -y --no-install-recommends cifs-utils smbclient

# Mounting is root's job and the settings server is an unauthenticated HTTP listener, so it
# must not be root. This helper is the only bridge: one program, four verbs, no shell path.
install -m 0755 "$REPO/packaging/tub3-nas" /usr/local/sbin/tub3-nas

# Scoped to exactly that binary. Not a general NOPASSWD grant — the web user gets the
# ability to mount a share, and nothing else.
cat > /etc/sudoers.d/tub3-nas <<SUDO
$RUN_USER ALL=(root) NOPASSWD: /usr/local/sbin/tub3-nas
SUDO
chmod 0440 /etc/sudoers.d/tub3-nas
# A malformed sudoers file locks everyone out of sudo, so validate before trusting it.
if ! visudo -c -f /etc/sudoers.d/tub3-nas >/dev/null 2>&1; then
    rm -f /etc/sudoers.d/tub3-nas
    warn "sudoers snippet was rejected and has been removed — NAS mounting from the web UI"
    warn "will not work. Everything else is unaffected."
fi
# Shutting down from the sofa. The box lives behind a television and the only clean shutdown
# before this was an ssh session, which is not a thing anyone has to hand while unplugging a
# Pi to move it — so the alternative was pulling power from a running Linux box, which is how
# a filesystem gets corrupted.
#
# Three exact command lines, same reasoning as the storage helper above: not a general grant.
# `systemctl` with an argument is safe to name here precisely because sudoers matches the
# whole command line, so this cannot be turned into "restart anything".
cat > /etc/sudoers.d/tub3-power <<SUDO
$RUN_USER ALL=(root) NOPASSWD: /bin/systemctl poweroff, /bin/systemctl reboot, /bin/systemctl restart tub3-tuner
SUDO
chmod 0440 /etc/sudoers.d/tub3-power
if ! visudo -c -f /etc/sudoers.d/tub3-power >/dev/null 2>&1; then
    rm -f /etc/sudoers.d/tub3-power
    warn "sudoers snippet for power was rejected and has been removed — the on-screen"
    warn "Shut down and Restart items will not work. Everything else is unaffected."
fi

mkdir -p /mnt/tub3

systemctl daemon-reload
systemctl enable tub3-tuner.service tub3-web.service tub3-build.timer >/dev/null
systemctl restart tub3-web.service >/dev/null 2>&1 || true

# ------------------------------------------------------------------- sharing ---
say "Setting up the drop share"
DROP="$RUN_HOME/tub3/Commercials"
sudo -u "$RUN_USER" mkdir -p "$DROP"/{Kids,Family,Late,Unsorted}

if ! grep -q '\[COMMERCIALS\]' /etc/samba/smb.conf; then
    cat >> /etc/samba/smb.conf <<SHARE

[COMMERCIALS]
   path = $DROP
   browseable = yes
   read only = no
   valid users = $RUN_USER
SHARE
fi
# Guest-writable shares do not work: Windows 10 1709+ disables SMB2/3 guest access and
# Windows 11 24H2 requires signing. A real account is the only thing that works everywhere.
warn "Set the share password:  sudo smbpasswd -a $RUN_USER"
# wsdd is what makes the share visible in Windows Explorer's network browser. Without it the
# share works by \\hostname but is invisible, which reads as broken.
systemctl enable --now wsdd avahi-daemon >/dev/null 2>&1 || true
systemctl restart smbd >/dev/null 2>&1 || true

say "Done"
cat <<NEXT

  Next:
    1.  sudo smbpasswd -a $RUN_USER          set the drop-share password
    2.  python3 -m tub3.cec check            diagnose HDMI-CEC
    3.  python3 -m tub3.remote list          find your remote
    4.  open http://\$(hostname -I | cut -d' ' -f1):8008   point at your media

  Then, once it all works and not before:

    sudo bash packaging/silent_boot.sh       power on -> mark -> television

  That hides the boot diagnostics, which are the thing you want visible while you
  are still finding out whether it boots. It has an --undo.

  The tuner starts on boot. Drop commercials into Unsorted over the network —
  they count as Late, so nothing unsorted can reach a kids channel.

NEXT
