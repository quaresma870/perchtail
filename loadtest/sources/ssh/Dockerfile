# A throwaway SFTP server for load tests: OpenSSH with password login for
# one unprivileged account and the built-in SFTP subsystem, serving
# /srv/logs read-only (mounted by compose.sources.yml).
FROM debian:trixie-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2

RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-server iproute2 \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /run/sshd /srv/logs

COPY sshd_config /etc/ssh/sshd_config
COPY entrypoint.sh /entrypoint.sh
RUN chmod 755 /entrypoint.sh

EXPOSE 22
ENTRYPOINT ["/entrypoint.sh"]
