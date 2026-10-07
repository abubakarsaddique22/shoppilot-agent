#!/bin/bash
# infra/aws/deploy.sh : deploy one version of ShopPilot on the EC2 host. The same script is the rollback.
#
#   bash infra/aws/deploy.sh <git-tag-or-sha>          deploy that version
#   bash infra/aws/deploy.sh <previous-tag-or-sha>     rollback = deploy the previous version
#
# What it does, in order: fetch the code, read the secrets from SSM, build the image tagged with the Git SHA,
# start the stack, run the database migrations, wait until /ready answers, remove old images.
# It never prints a secret. Run it as the "ubuntu" user (the deploy workflow does this through SSM Run Command).
set -euo pipefail

REF="${1:?usage: bash infra/aws/deploy.sh <git-tag-or-sha>}"
APP_DIR="${APP_DIR:-/opt/shoppilot}"
KEEP_IMAGES="${KEEP_IMAGES:-3}"

cd "$APP_DIR"
PREVIOUS="$(git rev-parse --short HEAD 2>/dev/null || echo none)"

dc() { docker compose -f docker-compose.prod.yml --env-file /run/shoppilot.env "$@"; }

echo "==> fetch code ($PREVIOUS -> $REF)"
git fetch --tags --prune origin
git checkout --force --detach "$REF"
export GIT_SHA="$(git rev-parse --short HEAD)"

echo "==> secrets from SSM"
bash infra/aws/fetch_secrets.sh

echo "==> build and start (image shoppilot-api:$GIT_SHA)"
dc up -d --build --remove-orphans

echo "==> database migrations"
dc exec -T api alembic upgrade head

echo "==> wait for /ready"
ready=0
for _ in $(seq 1 30); do
  if dc exec -T api python -c "import urllib.request as u; print(u.urlopen('http://localhost:8000/ready', timeout=5).read().decode())"; then
    ready=1
    break
  fi
  sleep 4
done
if [[ "$ready" -ne 1 ]]; then
  echo "ERROR: /ready did not answer after the deploy. Last API logs:" >&2
  dc logs --tail 60 api >&2 || true
  echo "Rollback: bash infra/aws/deploy.sh $PREVIOUS" >&2
  exit 1
fi

echo "==> remove old images (keep the newest $KEEP_IMAGES)"
docker images shoppilot-api --format '{{.Tag}}' | tail -n +"$((KEEP_IMAGES + 1))" |
  xargs -r -I{} docker rmi "shoppilot-api:{}" || true
docker image prune -f >/dev/null || true

echo "deployed $GIT_SHA (previous: $PREVIOUS)"
