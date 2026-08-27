#!/bin/sh
set -eu

CONTROL_URL=""
ENROLLMENT_TOKEN=""
DEVICE_ID=""
BUNDLE_URL=""
BUNDLE_SHA256=""
PUBLIC_KEY_URL=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --control-url) CONTROL_URL="$2"; shift 2 ;;
    --enrollment-token) ENROLLMENT_TOKEN="$2"; shift 2 ;;
    --device-id) DEVICE_ID="$2"; shift 2 ;;
    --bundle-url) BUNDLE_URL="$2"; shift 2 ;;
    --bundle-sha256) BUNDLE_SHA256="$2"; shift 2 ;;
    --public-key-url) PUBLIC_KEY_URL="$2"; shift 2 ;;
    *) echo "Argumento desconhecido: $1" >&2; exit 2 ;;
  esac
done

if [ "$(id -u)" -ne 0 ]; then
  echo "Execute este instalador com sudo." >&2
  exit 1
fi
if [ "$(uname -m)" != "aarch64" ]; then
  echo "A primeira versao suporta apenas Raspberry Pi OS arm64." >&2
  exit 1
fi
if [ -z "$CONTROL_URL" ] || [ -z "$ENROLLMENT_TOKEN" ] || [ -z "$DEVICE_ID" ] || [ -z "$BUNDLE_URL" ] || [ -z "$BUNDLE_SHA256" ] || [ -z "$PUBLIC_KEY_URL" ]; then
  echo "Use --control-url, --device-id, --enrollment-token, --bundle-url, --bundle-sha256 e --public-key-url." >&2
  exit 2
fi
case "$DEVICE_ID" in
  ????????-????-????-????-????????????) ;;
  *) echo "--device-id deve ser um UUID." >&2; exit 2 ;;
esac
case "$BUNDLE_SHA256" in
  *[!0-9a-fA-F]*|'') echo "--bundle-sha256 invalido." >&2; exit 2 ;;
esac
if [ "${#BUNDLE_SHA256}" -ne 64 ]; then
  echo "--bundle-sha256 deve ter 64 caracteres." >&2
  exit 2
fi

apt-get update
apt-get install -y --no-install-recommends avahi-daemon ca-certificates curl python3 python3-venv unattended-upgrades
id pdvbridge >/dev/null 2>&1 || useradd --system --home /var/lib/pdv-device-bridge --shell /usr/sbin/nologin pdvbridge
usermod -a -G dialout,lp pdvbridge
install -d -m 0755 /opt/pdv-device-bridge/releases /etc/pdv-device-bridge
install -d -o pdvbridge -g pdvbridge -m 0750 /var/lib/pdv-device-bridge

TEMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TEMP_DIR"' EXIT
curl --fail --location --silent --show-error "$BUNDLE_URL" -o "$TEMP_DIR/release.tar.gz"
printf '%s  %s\n' "$BUNDLE_SHA256" "$TEMP_DIR/release.tar.gz" | sha256sum --check --status
mkdir "$TEMP_DIR/release"
tar -xzf "$TEMP_DIR/release.tar.gz" -C "$TEMP_DIR/release"
python3 -m venv "$TEMP_DIR/release/.venv"
"$TEMP_DIR/release/.venv/bin/python" -m pip install --no-index --find-links "$TEMP_DIR/release/wheelhouse" pdv-device-bridge
VERSION="$("$TEMP_DIR/release/.venv/bin/python" -c 'import pdv_device_bridge; print(pdv_device_bridge.__version__)')"
mv "$TEMP_DIR/release" "/opt/pdv-device-bridge/releases/$VERSION"
ln -sfn "/opt/pdv-device-bridge/releases/$VERSION" /opt/pdv-device-bridge/current

SHARE_DIR="/opt/pdv-device-bridge/current/.venv/share/pdv-device-bridge"
install -m 0644 "$SHARE_DIR/systemd/pdv-device-bridge.service" /etc/systemd/system/pdv-device-bridge.service
install -m 0644 "$SHARE_DIR/systemd/pdv-device-agent.service" /etc/systemd/system/pdv-device-agent.service
install -m 0644 "$SHARE_DIR/avahi/pdv-device-bridge.service" /etc/avahi/services/pdv-device-bridge.service
install -o pdvbridge -g pdvbridge -m 0640 "$SHARE_DIR/config.example.toml" /var/lib/pdv-device-bridge/config.toml
sed -i 's#cors_allowed_origins = \["http://localhost:8080"\]#cors_allowed_origins = ["*"]#' /var/lib/pdv-device-bridge/config.toml
sed "s#https://devices.example.com#$CONTROL_URL#" "$SHARE_DIR/agent.example.toml" > /etc/pdv-device-bridge/agent.toml
curl --fail --location --silent --show-error "$PUBLIC_KEY_URL" -o /etc/pdv-device-bridge/release-public-key.pem
chmod 0644 /etc/pdv-device-bridge/release-public-key.pem
PROVISION_PATH="/boot/firmware/pdv-device-bridge.json"
install -d -m 0755 /boot/firmware
CODE="$(printf '%s' "$DEVICE_ID" | tr -d '-' | cut -c1-6 | tr '[:lower:]' '[:upper:]')"
HOSTNAME="freeline-bridge-$(printf '%s' "$CODE" | tr '[:upper:]' '[:lower:]')"
printf '{"device_id":"%s","code":"%s","hostname":"%s","enrollment_token":"%s"}\n' "$DEVICE_ID" "$CODE" "$HOSTNAME" "$ENROLLMENT_TOKEN" > "$PROVISION_PATH"
chmod 0600 "$PROVISION_PATH"
STATE_PROVISION_PATH="/var/lib/pdv-device-bridge/identity-provision.json"
install -o pdvbridge -g pdvbridge -m 0600 "$PROVISION_PATH" "$STATE_PROVISION_PATH"
runuser -u pdvbridge -- "/opt/pdv-device-bridge/current/.venv/bin/python" -c \
  'from pdv_device_bridge.identity import load_or_create_identity; load_or_create_identity("/var/lib/pdv-device-bridge/identity.json", "/var/lib/pdv-device-bridge/identity-provision.json")'
rm -f "$PROVISION_PATH"
hostnamectl set-hostname "$HOSTNAME"

systemctl daemon-reload
systemctl enable --now avahi-daemon pdv-device-bridge.service
systemctl enable --now pdv-device-agent.service
echo "PDV Device Bridge $VERSION instalado."
