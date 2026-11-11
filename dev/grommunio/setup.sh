#!/usr/bin/env bash
# Local grommunio test server with EWS, built from the official gromox-container
# repo at a pinned commit, plus a small patch so it runs natively on Apple
# Silicon. Creates alice@/bob@grommunio.test and seeds alice's mailbox.
#
#   dev/grommunio/setup.sh        # build + start + users + seed (idempotent)
#   dev/grommunio/teardown.sh     # stop and DELETE all data
#
# Needs: git, uv and Docker Compose or podman (podman-compose works, no API
# socket needed). First run takes ~10 minutes.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$HERE/.work/gromox-container"
REPO="https://github.com/grommunio/gromox-container"
COMMIT="${GROMMUNIO_COMMIT:-dbdb4a3}"

DOMAIN="grommunio.test"
FQDN="mail.$DOMAIN"
ADMIN_PASS="${GROMMUNIO_ADMIN_PASS:-GrommAdmin-4711}"
ALICE_PASS="${ALICE_PASS:-Al1cePass!}"
BOB_PASS="${BOB_PASS:-B0bPass!!}"
EWS_URL="https://127.0.0.1:10443/EWS/Exchange.asmx"

log() { printf '\033[1m==> %s\033[0m\n' "$*"; }

# 1. upstream checkout at the pinned commit -----------------------------------
if [ ! -d "$WORK/.git" ]; then
  log "cloning $REPO"
  mkdir -p "$(dirname "$WORK")"
  git clone -q "$REPO" "$WORK"
fi
git -C "$WORK" fetch -q origin || true
git -C "$WORK" checkout -q --force "$COMMIT"
git -C "$WORK" clean -q -fd -e var.local.env -e gromox_letsencrypt

# 2. patch: grommunio-chat's aarch64 package has an unresolvable dependency ---
log "patching gromox-core (drop grommunio-chat)"
perl -pi -e 's/ grommunio-chat / /g' "$WORK/gromox-core/Dockerfile"
rm -f "$WORK/gromox-core/supervisor.d/grommunio-chat.conf"

# 3. local configuration -------------------------------------------------------
cat > "$WORK/var.local.env" <<ENV
FQDN=$FQDN
DOMAIN=$DOMAIN
ADMIN_PASS=$ADMIN_PASS
CHAT_ADMIN_PASS=dev-chat-unused
FILES_ADMIN_PASS=dev-files-4711
SSL_INSTALL_TYPE=0
ENABLE_CHAT=false
TIMEZONE=Europe/Berlin
ENV
# Patch the upstream compose file in place (step 1 resets it on every run) instead of
# using docker-compose.override.yml: that keeps the stack startable with
# podman-compose, which cannot replace a port list from an override file.
rm -f "$WORK/docker-compose.override.yml"
# gromox-core: publish on 127.0.0.1 only, at host port = standard port + 10000
# (25 -> 10025, 443 -> 10443, admin 8443 -> 18443), so rootless podman can bind
# them (no ports < 1024) and nothing is exposed to the network.
perl -pi -e 's/^(\s*- )"(\d+):(\d+)"/sprintf(q{%s"127.0.0.1:%d:%d"}, $1, $2 + 10000, $3)/e' \
  "$WORK/docker-compose.yml"
# grommunio-office has no aarch64 build: run that one image as x86_64.
if [ "$(uname -m)" != "x86_64" ]; then
  perl -pi -e 's/^(  gromox-office:\n)/$1    platform: linux\/amd64\n/' "$WORK/docker-compose.yml"
fi

# 4. build + start -------------------------------------------------------------
log "docker compose up -d --build (first run is slow)"
(cd "$WORK" && docker compose up -d --build)

log "waiting for EWS at $EWS_URL"
for _ in $(seq 1 120); do
  code=$(curl -sk -m 5 -o /dev/null -w '%{http_code}' "$EWS_URL" || true)
  [ "$code" = "405" ] && break   # EWS is POST-only: 405 on GET means it is up
  sleep 5
done
[ "$code" = "405" ] || { echo "EWS did not come up (last HTTP $code)"; exit 1; }

# 5. domain + users ------------------------------------------------------------
log "creating domain and users"
admin() { docker exec gromox-core grommunio-admin "$@"; }
admin domain create -u 100 "$DOMAIN" >/dev/null 2>&1 || true
for spec in "alice:Alice Test:$ALICE_PASS" "bob:Bob Test:$BOB_PASS"; do
  name="${spec%%:*}"; rest="${spec#*:}"; display="${rest%%:*}"; pass="${rest#*:}"
  admin user create --lang en_US --pop3-imap yes --smtp yes \
    --property displayname="$display" "$name@$DOMAIN" >/dev/null 2>&1 || true
  admin passwd --password "$pass" "$name@$DOMAIN" >/dev/null
done

# 6. seed ----------------------------------------------------------------------
log "seeding test data"
(cd "$HERE/../.." && EWS_URL="$EWS_URL" ALICE_PASS="$ALICE_PASS" BOB_PASS="$BOB_PASS" uv run python dev/grommunio/seed.py)

cat <<DONE

grommunio is up.
  EWS        $EWS_URL
  Webmail    https://127.0.0.1:10443/web/
  Admin UI   https://127.0.0.1:18443/   (admin / $ADMIN_PASS)
  Users      alice@$DOMAIN / $ALICE_PASS     bob@$DOMAIN / $BOB_PASS
Ports are published on 127.0.0.1 only (standard port + 10000; admin UI on 18443).
DONE
