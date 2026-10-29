#!/usr/bin/env bash
# Build the Bitpin bot image on this machine and ship it to the server with rsync.
# The server can't download Python wheels, so nothing is built there.
#
# Usage (from anywhere): deploy/bitpin/deploy.sh
#   DEPLOY_HOST   ssh target, e.g. ubuntu@1.2.3.4 (default: contents of deploy/bitpin/.host, gitignored)
#   DEPLOY_DIR    directory on the server (default /opt/freqtrade-bitpin)
#   SKIP_BUILD=1  sync strategies, config, data and UI only; keep the image
#                 already on the server (use when nothing under freqtrade/ changed)
#   NO_START=1    upload only, don't (re)start the bots
#   SERVICES=...  only (re)start these compose services, e.g. SERVICES=nobitex
#                 (default: all - the Bitpin bot "freqtrade" and the Nobitex bot "nobitex")
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

HOST="${DEPLOY_HOST:-$(cat deploy/bitpin/.host 2>/dev/null || true)}"
DIR="${DEPLOY_DIR:-/opt/freqtrade-bitpin}"
if [ -z "$HOST" ]; then
    echo "Set DEPLOY_HOST or put the ssh target in deploy/bitpin/.host" >&2
    exit 1
fi

# Commit + hash of the image recipe, so lock/Dockerfile changes get a new tag.
RECIPE="$(cat deploy/bitpin/Dockerfile deploy/bitpin/requirements.lock deploy/bitpin/constraints.txt | shasum | cut -c1-6)"
TAG="$(git rev-parse --short HEAD)-$RECIPE"
if [ -n "$(git status --porcelain --untracked-files=no -- freqtrade)" ]; then
    TAG="$TAG-dirty"
fi
IMAGE="freqtrade-bitpin:$TAG"
TARBALL="dist/freqtrade-bitpin-$TAG.tar.gz"

ssh "$HOST" "mkdir -p $DIR/images $DIR/user_data/logs"

if [ -z "${SKIP_BUILD:-}" ]; then
    echo "==> Building $IMAGE (linux/amd64)"
    docker buildx build --platform linux/amd64 -f deploy/bitpin/Dockerfile -t "$IMAGE" --load .

    mkdir -p dist
    if [ ! -f "$TARBALL" ]; then
        echo "==> Saving $TARBALL"
        docker save "$IMAGE" | gzip -1 > "$TARBALL.tmp"
        mv "$TARBALL.tmp" "$TARBALL"
    fi

    echo "==> Uploading image"
    rsync -a --partial --progress "$TARBALL" "$HOST:$DIR/images/"
    ssh "$HOST" "docker load -i $DIR/images/$(basename "$TARBALL")"
fi

echo "==> Syncing compose file and user_data"
rsync -ah deploy/bitpin/docker-compose.yml "$HOST:$DIR/"
# Only what the bot needs. Never secrets, databases, logs or backtest output.
# No --delete: the server's database and logs live in the same directory.
rsync -a \
    --include='/config_bitpin.json' \
    --include='/strategies/' --include='/strategies/*.py' \
    --include='/data/' --include='/data/bitpin/' --include='/data/bitpin/**' \
    --exclude='*' \
    user_data/ "$HOST:$DIR/user_data/"
# The UI folder holds nothing else, so it can mirror the local install exactly.
rsync -a --delete --exclude='.gitkeep' --exclude='*.map' \
    freqtrade/rpc/api_server/ui/installed/ "$HOST:$DIR/ui/"
# The local Nobitex config holds the API key and the dashboard login - ship it without them.
# On the server they come from nobitex.env.
if [ -f user_data/config_nobitex.json ]; then
    jq '.exchange.key = "" | .exchange.secret = ""
        | del(.api_server.username, .api_server.password,
              .api_server.jwt_secret_key, .api_server.ws_token)' \
        user_data/config_nobitex.json \
        | ssh "$HOST" "umask 077 && cat > $DIR/user_data/config_nobitex.json"
fi

# .env only holds IMAGE_TAG so a plain `docker compose up -d` on the server
# uses the deployed image; secrets live in bot.env / nobitex.env.
ssh "$HOST" "cd $DIR && touch bot.env && chmod 600 bot.env"
# Nobitex: generate the dashboard login on the server once; the exchange key stays empty
# (dry-run needs none) until it's filled in on the server.
ssh "$HOST" "cd $DIR && if [ ! -s nobitex.env ]; then umask 077 && { \
    echo FREQTRADE__EXCHANGE__KEY=; echo FREQTRADE__EXCHANGE__SECRET=; \
    echo FREQTRADE__API_SERVER__USERNAME=nbx-\$(openssl rand -hex 3); \
    echo FREQTRADE__API_SERVER__PASSWORD=\$(openssl rand -base64 24 | tr -d '/+='); \
    echo FREQTRADE__API_SERVER__JWT_SECRET_KEY=\$(openssl rand -hex 32); \
    echo FREQTRADE__API_SERVER__WS_TOKEN=\$(openssl rand -hex 16); } > nobitex.env; fi"
if [ -z "${SKIP_BUILD:-}" ]; then
    ssh "$HOST" "echo IMAGE_TAG=$TAG > $DIR/.env"
else
    IMAGE="freqtrade-bitpin:$(ssh "$HOST" "sed -n 's/^IMAGE_TAG=//p' $DIR/.env")"
fi

if [ -n "${NO_START:-}" ]; then
    echo "==> Uploaded $IMAGE; not started (NO_START set)"
    exit 0
fi

echo "==> Restarting ${SERVICES:-all bots} with $IMAGE"
# Recreate so strategy/config changes load even when the image is unchanged.
ssh "$HOST" "cd $DIR && docker compose up -d --force-recreate ${SERVICES:-} && docker compose ps"

# Keep the current and previous image/tarball for rollback.
ssh "$HOST" "cd $DIR/images && ls -t freqtrade-bitpin-*.tar.gz | tail -n +3 | xargs -r rm -f; \
    docker images freqtrade-bitpin --format '{{.Tag}}' | tail -n +3 | xargs -r -I{} docker rmi freqtrade-bitpin:{}" || true
ls -t dist/freqtrade-bitpin-*.tar.gz 2>/dev/null | tail -n +3 | xargs rm -f
echo "==> Done. Logs: ssh $HOST 'cd $DIR && docker compose logs -f'"
