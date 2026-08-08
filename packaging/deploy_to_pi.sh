#!/usr/bin/env bash
#
# Send this repo to the Pi.
#
#     bash packaging/deploy_to_pi.sh            # to `boobtube`
#     bash packaging/deploy_to_pi.sh other-host
#
# Safe to re-run: that is how you push a change after editing on the Mac.
#
# Uses tar over ssh rather than rsync, because Raspberry Pi OS Lite does not ship rsync and
# a deploy tool that needs installing before it can deploy is not a deploy tool.
#
# `vendor/` is excluded deliberately — 298 MB of FieldStation42 that pi_setup.sh clones
# itself, at the SHA pinned in UPSTREAM.md. Copying it would be slower *and* would risk the
# Pi running a different upstream commit than the pin claims.
#
# `.git` IS included. It is under a megabyte, and having history on the box means a bad
# change can be backed out at 9pm without a laptop.

set -euo pipefail

HOST="${1:-boobtube}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="8008tub3"

say() { printf '\n\033[1;32m==>\033[0m %s\n' "$1"; }

cd "$REPO"

if [[ -n "$(git status --porcelain)" ]]; then
    printf '\033[1;33m  ! Uncommitted changes — sending the working tree as it is.\033[0m\n'
    git status --short | sed 's/^/      /'
fi

say "Checking $HOST is reachable"
if ! ssh -o ConnectTimeout=8 -o BatchMode=yes "$HOST" true 2>/dev/null; then
    cat <<MSG

  Cannot reach $HOST over ssh.

    - Still booting? First boot resizes the filesystem and reboots. Give it 2 minutes.
    - mDNS not resolving? Try the IP directly:  bash $0 andrew@192.168.x.x
      Find it on the router, or:  ping boobtube.local
    - Key refused? The card was flashed with a different public key than
      ~/.ssh/boobtube. That is not fixable over the network — re-flash.

MSG
    exit 1
fi

say "Sending source"
# --exclude before the path, BSD tar and GNU tar both honour it there.
tar --exclude='./vendor' --exclude='./.venv' --exclude='./.venv-build' \
    --exclude='./__pycache__' --exclude='*.pyc' --exclude='./runtime' \
    -cf - . | ssh "$HOST" "mkdir -p ~/$DEST && tar -xf - -C ~/$DEST"

SENT="$(ssh "$HOST" "find ~/$DEST -type f ! -path '*/.git/*' | wc -l" | tr -d ' ')"
say "$SENT file(s) on $HOST:~/$DEST"

cat <<NEXT

  Next, on the Pi:

    ssh $HOST
    sudo bash ~/$DEST/packaging/pi_setup.sh

  That takes a few minutes — it installs mpv, ffmpeg and samba, builds the build-side
  virtualenv, clones FieldStation42 at the pin, and enables the tuner service.

NEXT
