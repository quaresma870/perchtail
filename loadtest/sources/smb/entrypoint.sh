#!/bin/sh
set -eu
: "${LT_SOURCE_USER:?LT_SOURCE_USER is required}"
: "${LT_SOURCE_PASSWORD:?LT_SOURCE_PASSWORD is required}"
if ! id -u "$LT_SOURCE_USER" >/dev/null 2>&1; then
  useradd --no-create-home --home-dir /srv/logs --shell /usr/sbin/nologin "$LT_SOURCE_USER"
fi
printf '%s\n%s\n' "$LT_SOURCE_PASSWORD" "$LT_SOURCE_PASSWORD" | smbpasswd -s -a "$LT_SOURCE_USER" >/dev/null
smbpasswd -e "$LT_SOURCE_USER" >/dev/null
exec smbd --foreground --no-process-group
