#!/bin/bash

set -eu

if [ ! -f /var/syncthing/config/config.xml ]; then
    echo "Starting Syncthing"
    su-exec "${PUID}:${PGID}" env HOME="$HOME" "$@" &

    echo "Waiting for Syncthing to come up"
    until [[ $(curl -i -b /tmp/cookie.txt -c /tmp/cookie.txt -L http://localhost:8384 | grep '200') ]]; do
        printf '.'
        sleep 1
    done

    CSRF_TOKEN=$(grep CSRF /tmp/cookie.txt | awk '{print $7}')
    CSRF_NAME=$(grep CSRF /tmp/cookie.txt | awk '{print tolower($6)}')
    echo "Using CSRF x-${CSRF_NAME}: ${CSRF_TOKEN}"
    echo "Setting password"
    until [[ $(curl -i 'http://localhost:8384/rest/system/config' -H "x-${CSRF_NAME}: ${CSRF_TOKEN}" -b /tmp/cookie.txt -c /tmp/cookie.txt -H 'accept: application/json, text/plain, */*' -H 'content-type: application/json' -H 'accept-language: en-US,en;q=0.9' --data-binary '{"gui":{"enabled":true,"address":"127.0.0.1:8384","unixSocketPermissions":"","user":"'${USERNAME}'","password":"'${PASSWORD}'","authMode":"static","useTLS":false,"insecureAdminAccess":false,"theme":"default","debugging":false,"insecureSkipHostcheck":false,"insecureAllowFrameLoading":false}}' --compressed | grep '403') ]]; do
        printf '.'
        sleep 1
    done
    pkill -9 syncthing
fi

sed -i -E "s#<listenAddress>.*</listenAddress>#<listenAddress>tcp://0.0.0.0:${TCP_PORT}</listenAddress>#g" /var/syncthing/config/config.xml
sed -i -E "s#<localAnnouncePort>[0-9]*</localAnnouncePort>#<localAnnouncePort>${UDP_PORT}</localAnnouncePort>#g" /var/syncthing/config/config.xml
sed -i -E "s#8384]:[0-9]*#8384]:${UDP_PORT}#g" /var/syncthing/config/config.xml

# Limit concurrent folder operations to reduce I/O during sync
# https://docs.syncthing.net/advanced/option-max-concurrency.html
MAX_FOLDER_CONCURRENCY="${MAX_FOLDER_CONCURRENCY:-2}"
if grep -q "<maxFolderConcurrency>" /var/syncthing/config/config.xml; then
    sed -i -E "s#<maxFolderConcurrency>[0-9]*</maxFolderConcurrency>#<maxFolderConcurrency>${MAX_FOLDER_CONCURRENCY}</maxFolderConcurrency>#g" /var/syncthing/config/config.xml
else
    sed -i -E "s#</options>#<maxFolderConcurrency>${MAX_FOLDER_CONCURRENCY}</maxFolderConcurrency></options>#g" /var/syncthing/config/config.xml
fi

# Limit concurrent file writes to reduce I/O during sync (default 16 -> 4)
MAX_CONCURRENT_WRITES="${MAX_CONCURRENT_WRITES:-4}"
if grep -q "<maxConcurrentWrites>" /var/syncthing/config/config.xml; then
    sed -i -E "s#<maxConcurrentWrites>[0-9]*</maxConcurrentWrites>#<maxConcurrentWrites>${MAX_CONCURRENT_WRITES}</maxConcurrentWrites>#g" /var/syncthing/config/config.xml
else
    sed -i -E "s#</options>#<maxConcurrentWrites>${MAX_CONCURRENT_WRITES}</maxConcurrentWrites></options>#g" /var/syncthing/config/config.xml
fi

until [[ $(curl -i -H "Accept: application/json" -H "Content-Type:application/json" -X POST "${APPBOX_CALLBACK_BASE:-https://api.cylo.io}/v1/apps/installed/${INSTANCE_ID}" | grep '200') ]]
    do
    sleep 5
done

chown "${PUID}:${PGID}" "${HOME}" \
  && exec su-exec "${PUID}:${PGID}" \
     env HOME="$HOME" "$@"
