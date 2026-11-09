#!/usr/bin/env bash
# Stop the local grommunio stack and DELETE all of its data volumes.
set -euo pipefail
WORK="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/.work/gromox-container"
[ -d "$WORK" ] || { echo "nothing to tear down ($WORK missing)"; exit 0; }
(cd "$WORK" && docker compose down -v)
