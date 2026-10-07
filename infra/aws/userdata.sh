#!/bin/bash
# infra/aws/userdata.sh : EC2 "User data" (runs once, as root, at the first boot). Ubuntu Server LTS, x86_64.
#
# Before you paste it into the launch form: put your own repository address in REPO_URL.
# The repository must be public (or use a read-only deploy key, see docs/runbook.md).
# The log of this script is /var/log/cloud-init-output.log
set -euxo pipefail

REPO_URL="https://github.com/YOURUSER/shoppilot.git"
TZ_NAME="Asia/Karachi"      # cron uses server time, so 08:00 here means 08:00 in Pakistan

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y ca-certificates curl git unzip jq docker.io docker-compose-v2
systemctl enable --now docker
usermod -aG docker ubuntu
timedatectl set-timezone "$TZ_NAME"

# 2 GB swap: a 1 GB instance needs it to build the image and to run Postgres next to the API
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# AWS CLI v2 (use awscli-exe-linux-aarch64.zip if you choose an ARM instance type)
curl -fsS https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip -o /tmp/awscliv2.zip
unzip -q -o /tmp/awscliv2.zip -d /tmp
/tmp/aws/install --update

# the application
if [[ ! -d /opt/shoppilot/.git ]]; then
  git clone "$REPO_URL" /opt/shoppilot
fi
chown -R ubuntu:ubuntu /opt/shoppilot

# a short command for the ubuntu user: dc ps, dc logs -f api, dc exec api ...
cat >> /home/ubuntu/.bashrc <<'EOF'
alias dc='docker compose -f /opt/shoppilot/docker-compose.prod.yml --env-file /run/shoppilot.env'
export AWS_REGION=ap-south-1
EOF

echo "userdata finished: next step is docs/runbook.md, section 'First deploy'"
