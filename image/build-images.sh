#!/bin/sh
set -eu

if [ "$#" -ne 4 ]; then
  echo "Uso: $0 CONTROL_URL BUNDLE_URL BUNDLE_SHA256 PUBLIC_KEY_URL" >&2
  exit 2
fi

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
SOURCE_ROOT="$SCRIPT_DIR"
IMAGE_GEN_DIR="${RPI_IMAGE_GEN_DIR:-$SCRIPT_DIR/.rpi-image-gen}"

if [ ! -d "$IMAGE_GEN_DIR/.git" ]; then
  git clone --depth 1 https://github.com/raspberrypi/rpi-image-gen.git "$IMAGE_GEN_DIR"
  "$IMAGE_GEN_DIR/install_deps.sh"
fi

for device in pi3 pi4 pi5; do
  "$IMAGE_GEN_DIR/rpi-image-gen" build -S "$SOURCE_ROOT" -c "$SCRIPT_DIR/config/$device.yaml" -- \
    "IGconf_bridge_control_url=$1" \
    "IGconf_bridge_bundle_url=$2" \
    "IGconf_bridge_bundle_sha256=$3" \
    "IGconf_bridge_public_key_url=$4"
done
