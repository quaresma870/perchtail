# A throwaway SMB server for load tests: Samba with NTLM password login for
# one account and a read-only share "ltlogs" of /srv/logs (mounted by
# compose.sources.yml).
FROM debian:trixie-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2

RUN apt-get update \
    && apt-get install -y --no-install-recommends samba iproute2 \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /srv/logs

COPY smb.conf /etc/samba/smb.conf
COPY entrypoint.sh /entrypoint.sh
RUN chmod 755 /entrypoint.sh

EXPOSE 445
ENTRYPOINT ["/entrypoint.sh"]
