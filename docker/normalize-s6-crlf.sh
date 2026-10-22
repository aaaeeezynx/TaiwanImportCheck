#!/bin/sh
# Normalize Hermes runtime scripts when the image was built from a Windows
# checkout. This changes only the container filesystem, never the host source.
set -eu

for file in \
    /etc/cont-init.d/* \
    /etc/s6-overlay/s6-rc.d/*/type \
    /etc/s6-overlay/s6-rc.d/*/run \
    /etc/s6-overlay/s6-rc.d/*/finish \
    /opt/hermes/docker/*.sh \
    /opt/hermes/bin/hermes
do
    [ -f "$file" ] || continue
    sed -i 's/\r$//' "$file"
done

exec /init /opt/hermes/docker/main-wrapper.sh "$@"
