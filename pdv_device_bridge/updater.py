from __future__ import annotations

from dataclasses import dataclass
import base64
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
import tempfile
import time
from typing import Callable
from urllib.request import Request, urlopen

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from packaging.version import Version


@dataclass(slots=True, frozen=True)
class ReleaseManifest:
    version: str
    architecture: str
    artifact_url: str
    sha256: str
    signature: str
    urgent: bool = False

    @classmethod
    def from_payload(cls, raw: dict[str, object]) -> "ReleaseManifest":
        return cls(
            version=str(raw["version"]),
            architecture=str(raw["architecture"]),
            artifact_url=str(raw["artifact_url"]),
            sha256=str(raw["sha256"]).lower(),
            signature=str(raw["signature"]),
            urgent=bool(raw.get("urgent", False)),
        )

    def signed_bytes(self) -> bytes:
        return f"{self.version}\n{self.architecture}\n{self.sha256}".encode()


class UpdateError(RuntimeError):
    pass


class AtomicUpdater:
    def __init__(
        self,
        *,
        public_key_path: Path,
        releases_root: Path,
        current_symlink: Path,
        service_name: str,
        health_url: str,
        command_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ) -> None:
        self.public_key_path = public_key_path
        self.releases_root = releases_root
        self.current_symlink = current_symlink
        self.service_name = service_name
        self.health_url = health_url
        self.command_runner = command_runner
        self.pending_path = releases_root / ".pending-update.json"

    def verify_manifest(self, manifest: ReleaseManifest, *, current_version: str) -> None:
        machine = platform.machine().lower()
        accepted = {"aarch64", "arm64"} if machine in {"aarch64", "arm64"} else {machine}
        if manifest.architecture.lower() not in accepted:
            raise UpdateError(f"Artefato {manifest.architecture} incompativel com {machine}.")
        if Version(manifest.version) < Version(current_version):
            raise UpdateError("Downgrade nao autorizado.")
        key = serialization.load_pem_public_key(self.public_key_path.read_bytes())
        if not isinstance(key, Ed25519PublicKey):
            raise UpdateError("Chave publica de release nao e Ed25519.")
        try:
            key.verify(base64.b64decode(manifest.signature, validate=True), manifest.signed_bytes())
        except Exception as exc:
            raise UpdateError("Assinatura da release invalida.") from exc

    def stage(self, manifest: ReleaseManifest, *, bearer_token: str) -> Path:
        self.releases_root.mkdir(parents=True, exist_ok=True)
        final = self.releases_root / manifest.version
        if final.exists():
            return final

        with tempfile.TemporaryDirectory(prefix="pdv-bridge-update-") as temp_dir:
            archive = Path(temp_dir) / "release.tar.gz"
            request = Request(manifest.artifact_url, headers={"Authorization": f"Bearer {bearer_token}"})
            digest = hashlib.sha256()
            with urlopen(request, timeout=120) as response, archive.open("wb") as target:
                while chunk := response.read(1024 * 1024):
                    digest.update(chunk)
                    target.write(chunk)
            if digest.hexdigest() != manifest.sha256:
                raise UpdateError("Checksum SHA-256 do artefato invalido.")

            staging = self.releases_root / f".{manifest.version}.staging"
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(mode=0o755)
            with tarfile.open(archive, "r:gz") as bundle:
                _safe_extract(bundle, staging)
            wheelhouse = staging / "wheelhouse"
            if not wheelhouse.is_dir():
                raise UpdateError("Bundle sem wheelhouse.")
            self.command_runner(["python3", "-m", "venv", str(staging / ".venv")], check=True)
            self.command_runner([
                str(staging / ".venv/bin/python"), "-m", "pip", "install", "--no-index",
                "--find-links", str(wheelhouse), f"pdv-device-bridge=={manifest.version}",
            ], check=True)
            os.replace(staging, final)
        return final

    def activate(self, manifest: ReleaseManifest, *, health_timeout_seconds: int = 60) -> None:
        target = self.releases_root / manifest.version
        if not target.exists():
            raise UpdateError("Release ainda nao foi preparada.")
        previous = self.current_symlink.resolve() if self.current_symlink.is_symlink() else None
        self.pending_path.write_text(json.dumps({
            "version": manifest.version,
            "previous": str(previous) if previous else None,
        }), encoding="utf-8")
        temporary_link = self.current_symlink.with_name(f".{self.current_symlink.name}.next")
        temporary_link.unlink(missing_ok=True)
        temporary_link.symlink_to(target)
        os.replace(temporary_link, self.current_symlink)
        try:
            self._restart_and_wait(health_timeout_seconds)
        except Exception as activation_error:
            if previous:
                rollback = self.current_symlink.with_name(f".{self.current_symlink.name}.rollback")
                rollback.unlink(missing_ok=True)
                rollback.symlink_to(previous)
                os.replace(rollback, self.current_symlink)
                self._restart_and_wait(health_timeout_seconds)
                self.pending_path.unlink(missing_ok=True)
            raise activation_error
        else:
            self.pending_path.unlink(missing_ok=True)

    def recover_interrupted_update(self) -> bool:
        if not self.pending_path.exists():
            return False
        raw = json.loads(self.pending_path.read_text(encoding="utf-8"))
        previous_value = str(raw.get("previous") or "").strip()
        previous = Path(previous_value) if previous_value else None
        if previous and previous.exists():
            replacement = self.current_symlink.with_name(f".{self.current_symlink.name}.recovery")
            replacement.unlink(missing_ok=True)
            replacement.symlink_to(previous)
            os.replace(replacement, self.current_symlink)
            self.command_runner(["systemctl", "restart", self.service_name], check=True)
        self.pending_path.unlink(missing_ok=True)
        return True

    def _restart_and_wait(self, timeout_seconds: int) -> None:
        self.command_runner(["systemctl", "restart", self.service_name], check=True)
        deadline = time.monotonic() + timeout_seconds
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                with urlopen(self.health_url, timeout=2) as response:
                    payload = json.loads(response.read())
                    if response.status == 200 and payload.get("status") == "ok":
                        return
            except Exception as exc:
                last_error = exc
            time.sleep(2)
        raise UpdateError(f"Bridge nao confirmou saude apos ativacao: {last_error}")


def _safe_extract(bundle: tarfile.TarFile, target: Path) -> None:
    root = target.resolve()
    for member in bundle.getmembers():
        destination = (target / member.name).resolve()
        if root not in destination.parents and destination != root:
            raise UpdateError("Bundle contem caminho inseguro.")
        if member.issym() or member.islnk():
            raise UpdateError("Bundle nao pode conter links simbolicos.")
    bundle.extractall(target, filter="data")
