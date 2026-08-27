from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("dist/device-bridge-release"))
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    package_version = subprocess.check_output(
        [sys.executable, "-c", "import pdv_device_bridge; print(pdv_device_bridge.__version__)"],
        cwd=root,
        text=True,
    ).strip()
    if package_version != args.version:
        raise SystemExit(f"Versao solicitada {args.version} difere do pacote {package_version}.")
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pdv-device-release-") as temporary:
        bundle_root = Path(temporary) / "bundle"
        wheelhouse = bundle_root / "wheelhouse"
        wheelhouse.mkdir(parents=True)
        subprocess.run([sys.executable, "-m", "build", "--wheel", "--outdir", str(wheelhouse), str(root)], check=True)
        own_wheel = next(wheelhouse.glob("pdv_device_bridge-*.whl"))
        for python_version, abi in (("311", "cp311"), ("313", "cp313")):
            subprocess.run([
                sys.executable, "-m", "pip", "download", "--only-binary=:all:",
                "--platform", "manylinux2014_aarch64", "--implementation", "cp",
                "--python-version", python_version, "--abi", abi, "--dest", str(wheelhouse),
                str(own_wheel),
            ], check=True)
        archive = args.output / f"pdv-device-bridge-{args.version}-arm64.tar.gz"
        with tarfile.open(archive, "w:gz") as target:
            target.add(bundle_root, arcname=".")

    sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()
    signed = f"{args.version}\narm64\n{sha256}".encode()
    key = serialization.load_pem_private_key(args.private_key.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise SystemExit("A chave de release deve ser Ed25519.")
    manifest = {
        "version": args.version,
        "architecture": "arm64",
        "sha256": sha256,
        "signature": base64.b64encode(key.sign(signed)).decode(),
        "artifact": archive.name,
    }
    (args.output / f"pdv-device-bridge-{args.version}-arm64.json").write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
