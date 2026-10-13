ARG GOVERSION=latest

#
# Maybe build Syncthing. This is a bit ugly as we can't make an entire
# section of the Dockerfile conditional, so we end up always pulling the
# golang image as builder. Then we check if the executable we need already
# exists (pre-built) otherwise we build it.
#

FROM golang:$GOVERSION AS builder
ARG BUILD_USER
ARG BUILD_HOST
ARG TARGETARCH=amd64
# Build contexts come from git archive (no .git), so pin the release version.
ARG VERSION=v2.1.6

WORKDIR /src
COPY . .

ENV CGO_ENABLED=0
RUN if [ ! -f syncthing-linux-amd64 ] ; then \
    VERSION=$VERSION go run build.go -no-upgrade build syncthing ; \
    mv syncthing syncthing-linux-amd64 ; \
  fi

#
# The rest of the Dockerfile uses the binary from the builder, prebuilt or
# not.
#

FROM alpine

EXPOSE 8384

RUN apk add --no-cache ca-certificates curl libcap su-exec tzdata bash

COPY --from=builder /src/syncthing-linux-amd64 /bin/syncthing
COPY --from=builder /src/scripts/entrypoint.sh /bin/entrypoint.sh

ENV PUID=1000 PGID=1000 HOME=/var/syncthing

HEALTHCHECK --interval=1m --timeout=10s \
  CMD curl -fkLsS -m 2 127.0.0.1:8384/rest/noauth/health | grep -o --color=never OK || exit 1

ENV STGUIADDRESS=0.0.0.0:8384
ENV STHOMEDIR=/var/syncthing/config
RUN chmod 755 /bin/entrypoint.sh
ENTRYPOINT ["/bin/entrypoint.sh", "/bin/syncthing"]
