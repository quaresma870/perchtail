#!/bin/sh
set -eu
: "${LT_SOURCE_USER:?LT_SOURCE_USER is required}"
: "${LT_SOURCE_PASSWORD:?LT_SOURCE_PASSWORD is required}"
if [ "$LT_SOURCE_USER" != "lt-src" ]; then
  sed -i "s/^AllowUsers .*/AllowUsers $LT_SOURCE_USER/" /etc/ssh/sshd_config
fi
if ! id -u "$LT_SOURCE_USER" >/dev/null 2>&1; then
  useradd --no-create-home --home-dir /srv/logs --shell /usr/sbin/nologin "$LT_SOURCE_USER"
fi
printf '%s:%s\n' "$LT_SOURCE_USER" "$LT_SOURCE_PASSWORD" | chpasswd
ssh-keygen -A >/dev/null
exec /usr/sbin/sshd -D -e
