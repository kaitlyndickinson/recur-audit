#!/usr/bin/env bash
# Installs and (re)starts the app as a user service. Safe to rerun after a git pull.
set -euo pipefail
D=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

if [ "$D" != "$HOME/dev/projects/household-app" ]; then
  echo "household-app.service expects the checkout at ~/dev/projects/household-app, not $D" >&2
  exit 1
fi

[ -d "$D/.venv" ] || python3 -m venv "$D/.venv"
"$D/.venv/bin/pip" install -q -e "$D"
mkdir -p "$D/data"
chmod 700 "$D/data"

systemctl --user link "$D/deploy/household-app.service"
systemctl --user daemon-reload
systemctl --user enable household-app.service
systemctl --user restart household-app.service

if [ "$(loginctl show-user "$USER" -p Linger --value)" != yes ]; then
  echo "Note: user services stop when you log out. To keep the app running: loginctl enable-linger $USER"
fi
