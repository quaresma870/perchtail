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
# The host keys live in their own volume, so a rebuilt or recreated
# container keeps them and PerchTail's pinned key still matches.
mkdir -p /etc/ssh/keys
for type in ed25519 rsa; do
  [ -f "/etc/ssh/keys/ssh_host_${type}_key" ]     || ssh-keygen -q -t "$type" -N '' -f "/etc/ssh/keys/ssh_host_${type}_key"
done
exec /usr/sbin/sshd -D -e
