#!/usr/bin/env bash
# One-time EC2 setup for habla-core.
# Usage:
#   sudo APP_DIR=/opt/habla-core DOMAIN=your-domain.example.com ./deploy/ec2/bootstrap_server.sh

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/habla-core}"
SERVICE_NAME="${SERVICE_NAME:-habla-core}"
SERVICE_USER="${SERVICE_USER:-ubuntu}"
SERVICE_GROUP="${SERVICE_GROUP:-ubuntu}"
DOMAIN="${DOMAIN:-_}"

if [[ $EUID -ne 0 ]]; then
  echo "Run as root (sudo)." >&2
  exit 1
fi

echo "==> Installing OS packages"
apt-get update
apt-get install -y --no-install-recommends \
  python3 python3-venv python3-pip nginx git curl rsync

echo "==> Ensuring app directory: ${APP_DIR}"
mkdir -p "${APP_DIR}"
chown -R "${SERVICE_USER}:${SERVICE_GROUP}" "${APP_DIR}"

echo "==> Installing systemd service"
cp deploy/ec2/habla-core.service /etc/systemd/system/${SERVICE_NAME}.service
sed -i "s|__APP_DIR__|${APP_DIR}|g" /etc/systemd/system/${SERVICE_NAME}.service
sed -i "s|__SERVICE_USER__|${SERVICE_USER}|g" /etc/systemd/system/${SERVICE_NAME}.service
sed -i "s|__SERVICE_GROUP__|${SERVICE_GROUP}|g" /etc/systemd/system/${SERVICE_NAME}.service

echo "==> Installing nginx site config"
cp deploy/ec2/nginx-habla-core.conf /etc/nginx/sites-available/${SERVICE_NAME}
sed -i "s|__DOMAIN__|${DOMAIN}|g" /etc/nginx/sites-available/${SERVICE_NAME}
ln -sf /etc/nginx/sites-available/${SERVICE_NAME} /etc/nginx/sites-enabled/${SERVICE_NAME}
rm -f /etc/nginx/sites-enabled/default

nginx -t
systemctl daemon-reload
systemctl enable ${SERVICE_NAME}
systemctl restart nginx

echo "==> Bootstrap complete"
echo "Next steps:"
echo "1) Deploy app files to ${APP_DIR}"
echo "2) Create ${APP_DIR}/.env with production secrets"
echo "3) sudo systemctl restart ${SERVICE_NAME}"
