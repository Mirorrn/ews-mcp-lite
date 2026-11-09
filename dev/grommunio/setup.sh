#!/usr/bin/env bash
# Local grommunio test server with EWS, built from the official gromox-container
# repo at a pinned commit, plus a small patch so it runs natively on Apple
# Silicon. Creates alice@/bob@grommunio.test and seeds alice's mailbox.
#
#   dev/grommunio/setup.sh        # build + start + users + seed (idempotent)
#   dev/grommunio/teardown.sh     # stop and DELETE all data
#
# Needs: git, Docker with Compose >= 2.24, uv. First run takes ~10 minutes.
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
EWS_URL="https://127.0.0.1/EWS/Exchange.asmx"

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
cat > "$WORK/docker-compose.override.yml" <<'YML'
# grommunio-office has no aarch64 build: run that one image as x86_64.
services:
  gromox-office:
    platform: linux/amd64
YML

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
(cd "$HERE/../.." && ALICE_PASS="$ALICE_PASS" BOB_PASS="$BOB_PASS" uv run python dev/grommunio/seed.py)

cat <<DONE

grommunio is up.
  EWS        $EWS_URL
  Webmail    https://127.0.0.1/web/
  Admin UI   https://127.0.0.1:8443/   (admin / $ADMIN_PASS)
  Users      alice@$DOMAIN / $ALICE_PASS     bob@$DOMAIN / $BOB_PASS
NOTE: the stack publishes its ports on ALL interfaces (443, 25, 993, 8443 ...).
DONE
