import base64
import hashlib
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pdv_device_bridge.updater import AtomicUpdater, ReleaseManifest, UpdateError


def _updater(tmp_path, public_key_path):
    return AtomicUpdater(
        public_key_path=public_key_path,
        releases_root=tmp_path / "releases",
        current_symlink=tmp_path / "current",
        service_name="bridge.service",
        health_url="http://127.0.0.1/health",
    )


def test_updater_accepts_valid_ed25519_manifest(tmp_path, monkeypatch) -> None:
    private = Ed25519PrivateKey.generate()
    public_path = tmp_path / "public.pem"
    public_path.write_bytes(private.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ))
    sha256 = hashlib.sha256(b"bundle").hexdigest()
    signed = f"0.2.0\narm64\n{sha256}".encode()
    manifest = ReleaseManifest(
        version="0.2.0",
        architecture="arm64",
        artifact_url="https://devices.example/release",
        sha256=sha256,
        signature=base64.b64encode(private.sign(signed)).decode(),
    )
    monkeypatch.setattr("pdv_device_bridge.updater.platform.machine", lambda: "aarch64")

    _updater(tmp_path, public_path).verify_manifest(manifest, current_version="0.1.0")


def test_updater_rejects_tampered_manifest(tmp_path, monkeypatch) -> None:
    private = Ed25519PrivateKey.generate()
    public_path = tmp_path / "public.pem"
    public_path.write_bytes(private.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ))
    manifest = ReleaseManifest(
        version="0.2.0",
        architecture="arm64",
        artifact_url="https://devices.example/release",
        sha256="0" * 64,
        signature=base64.b64encode(private.sign(b"different")).decode(),
    )
    monkeypatch.setattr("pdv_device_bridge.updater.platform.machine", lambda: "aarch64")

    with pytest.raises(UpdateError, match="Assinatura"):
        _updater(tmp_path, public_path).verify_manifest(manifest, current_version="0.1.0")


def test_recovery_restores_previous_release_after_power_loss(tmp_path) -> None:
    private = Ed25519PrivateKey.generate()
    public_path = tmp_path / "public.pem"
    public_path.write_bytes(private.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ))
    releases = tmp_path / "releases"
    previous = releases / "0.1.0"
    current_release = releases / "0.2.0"
    previous.mkdir(parents=True)
    current_release.mkdir()
    current = tmp_path / "current"
    current.symlink_to(current_release)
    commands = []
    updater = AtomicUpdater(
        public_key_path=public_path,
        releases_root=releases,
        current_symlink=current,
        service_name="bridge.service",
        health_url="http://127.0.0.1/health",
        command_runner=lambda command, **_: commands.append(command),
    )
    updater.pending_path.write_text(json.dumps({"version": "0.2.0", "previous": str(previous)}))

    assert updater.recover_interrupted_update() is True
    assert current.resolve() == previous.resolve()
    assert commands == [["systemctl", "restart", "bridge.service"]]
    assert not updater.pending_path.exists()
