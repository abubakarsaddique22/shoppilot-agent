#!/bin/bash
# infra/aws/fetch_secrets.sh : read the secrets from SSM Parameter Store into /run/shoppilot.env
#
#   bash infra/aws/fetch_secrets.sh
#
# /run is tmpfs (memory), so the file is gone after a reboot: run this again after every reboot (deploy.sh does it too).
# No access key is needed on the server: the EC2 instance role proves who we are (blueprint section 12).
# Every parameter /shoppilot/prod/NAME is written twice:
#   SHOP_NAME=value   read by the app settings (core/config.py uses the SHOP_ prefix)
#   NAME=value        read by Compose (${POSTGRES_PASSWORD}) and by the LangSmith SDK (LANGSMITH_API_KEY)
# Secret values are never printed.
set -euo pipefail

REGION="${AWS_REGION:-ap-south-1}"
SSM_PATH="${SSM_PATH:-/shoppilot/prod/}"
OUT="${SECRETS_FILE:-/run/shoppilot.env}"
REQUIRED=(POSTGRES_PASSWORD JWT_SECRET LLM_API_KEY)

umask 077
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

aws ssm get-parameters-by-path --region "$REGION" --path "$SSM_PATH" --with-decryption \
  --query "Parameters[].[Name,Value]" --output text |
while IFS=$'\t' read -r name value; do
  n="$(basename "$name")"
  # An env file cannot hold these characters safely (Compose would read $ and quotes as syntax).
  case "$value" in
    *[[:space:]]* | *'$'* | *'#'* | *'"'* | *"'"* | *'\'*)
      echo "ERROR: the value of $n has a space or one of \$ # \" ' \\ . Store it without that character." >&2
      exit 1
      ;;
  esac
  echo "SHOP_${n}=${value}"
  echo "${n}=${value}"
done > "$tmp"

if [[ ! -s "$tmp" ]]; then
  echo "ERROR: no parameters found under $SSM_PATH in $REGION (wrong region, wrong path, or the role has no access)" >&2
  exit 1
fi
for key in "${REQUIRED[@]}"; do
  if ! grep -q "^${key}=" "$tmp"; then
    echo "ERROR: the required parameter ${SSM_PATH}${key} is missing" >&2
    exit 1
  fi
done

# /run can only be written by root. The file is handed to the user who started this script (Compose reads it as that
# user) and nobody else can read it.
SUDO=()
if [[ $EUID -ne 0 ]]; then
  SUDO=(sudo)
fi
"${SUDO[@]}" install -m 600 -o "$(id -un)" -g "$(id -gn)" "$tmp" "$OUT"
echo "wrote $OUT ($(grep -c . "$tmp") lines from $SSM_PATH)"
