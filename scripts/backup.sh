#!/bin/bash
# scripts/backup.sh : nightly Postgres dump to S3 (cron runs it, see infra/aws/crontab.txt)
#
#   bash scripts/backup.sh
#
# Writes s3://<bucket>/backups/postgres/<date-time>.sql.gz. The bucket lifecycle rule deletes backups after 30 days.
# The server has no access key: the EC2 role may only put and get objects in this one bucket (blueprint section 12).
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/shoppilot}"
ENV_FILE="${SECRETS_FILE:-/run/shoppilot.env}"
export AWS_DEFAULT_REGION="${AWS_REGION:-ap-south-1}"

cd "$APP_DIR"
# /run is tmpfs: after a reboot the file is gone until fetch_secrets.sh writes it again
[[ -s "$ENV_FILE" ]] || bash infra/aws/fetch_secrets.sh

BUCKET="$(grep '^S3_BUCKET=' "$ENV_FILE" | head -n1 | cut -d= -f2-)"
if [[ -z "$BUCKET" ]]; then
  echo "ERROR: S3_BUCKET is missing in $ENV_FILE (create the SSM parameter /shoppilot/prod/S3_BUCKET)" >&2
  exit 1
fi

STAMP="$(date +%F-%H%M)"
KEY="backups/postgres/${STAMP}.sql.gz"

docker compose -f docker-compose.prod.yml --env-file "$ENV_FILE" exec -T db \
  pg_dump -U shop -d shoppilot | gzip | aws s3 cp - "s3://${BUCKET}/${KEY}"

# an empty or missing object means the backup failed silently
SIZE="$(aws s3api head-object --bucket "$BUCKET" --key "$KEY" --query ContentLength --output text)"
if [[ "$SIZE" -lt 1000 ]]; then
  echo "ERROR: backup $KEY is only $SIZE bytes" >&2
  exit 1
fi
echo "backup ok: s3://${BUCKET}/${KEY} (${SIZE} bytes)"
