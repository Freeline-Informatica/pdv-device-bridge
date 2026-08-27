from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import tempfile
import time

import httpx

from . import __version__
from .agent_config import DEFAULT_AGENT_CONFIG_PATH, AgentConfig, load_agent_config
from .configuration_store import DeviceAssignment, apply_assignments
from .identity import BridgeIdentity, load_or_create_identity, local_addresses
from .updater import AtomicUpdater, ReleaseManifest, UpdateError


@dataclass(slots=True)
class AgentState:
    device_token: str | None = None
    pairing_token: str | None = None
    last_applied_version: str | None = None
    last_configuration_revision: int = 0


class ControlAgent:
    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        self.identity: BridgeIdentity = load_or_create_identity(config.identity_path)
        self.state = _load_state(config.state_path)
        self.client = httpx.AsyncClient(base_url=config.control_url, timeout=20)
        self.updater = AtomicUpdater(
            public_key_path=config.public_key_path,
            releases_root=config.releases_root,
            current_symlink=config.current_symlink,
            service_name=config.bridge_service,
            health_url=config.bridge_health_url,
        )

    async def run_forever(self) -> None:
        self.updater.recover_interrupted_update()
        while True:
            try:
                await self.run_once()
            except Exception as exc:
                print(f"pdv-device-agent: {exc}", flush=True)
            await asyncio.sleep(self.config.poll_interval_seconds)

    async def run_once(self) -> None:
        if not self.state.device_token:
            await self._enroll()
        headers = self._headers()
        heartbeat = self._heartbeat_payload()
        local_status = await self._local_status()
        heartbeat["peripherals"] = local_status.get("devices", [])
        response = await self.client.post("/api/v1/device/heartbeat", headers=headers, json=heartbeat)
        response.raise_for_status()
        control = response.json()
        await self._handle_credentials(control)
        await self._handle_configuration(control)
        desired = control.get("desired_release")
        if desired:
            await self._handle_release(ReleaseManifest.from_payload(desired))

    async def close(self) -> None:
        await self.client.aclose()

    async def _enroll(self) -> None:
        if not self.identity.enrollment_token:
            raise RuntimeError("Dispositivo sem enrollment_token no provisionamento.")
        response = await self.client.post("/api/v1/device/enroll", json={
            "device_id": self.identity.device_id,
            "code": self.identity.code,
            "hostname": self.identity.hostname,
            "site_id": self.identity.site_id,
            "enrollment_token": self.identity.enrollment_token,
            "architecture": platform.machine().lower(),
            "version": __version__,
        })
        response.raise_for_status()
        payload = response.json()
        self.state.device_token = str(payload["device_token"])
        self.state.pairing_token = str(payload["pairing_token"])
        _save_state(self.config.state_path, self.state)
        _enable_bridge_security(self.config.bridge_config_path, self.state.pairing_token)
        await asyncio.to_thread(
            self.updater.command_runner,
            ["systemctl", "restart", self.config.bridge_service],
            check=True,
        )

    async def _handle_release(self, manifest: ReleaseManifest) -> None:
        self.updater.verify_manifest(manifest, current_version=__version__)
        await asyncio.to_thread(self.updater.stage, manifest, bearer_token=str(self.state.device_token))
        health = await self._local_health()
        activation_due = manifest.urgent or datetime.now().hour == self.config.activation_hour
        if activation_due and health.get("idle") is True:
            try:
                await asyncio.to_thread(self.updater.activate, manifest)
                self.state.last_applied_version = manifest.version
                _save_state(self.config.state_path, self.state)
                await self._report_update(manifest.version, "activated", None)
                await asyncio.to_thread(
                    self.updater.command_runner,
                    [
                        "systemd-run",
                        "--unit=pdv-device-agent-restart",
                        "--collect",
                        "--on-active=5s",
                        "/bin/systemctl",
                        "restart",
                        "pdv-device-agent.service",
                    ],
                    check=True,
                )
            except Exception as exc:
                await self._report_update(manifest.version, "rolled_back", str(exc))
                raise
        else:
            await self._report_update(manifest.version, "staged", None)

    async def _handle_credentials(self, control: dict[str, object]) -> None:
        pairing_token = str(control.get("pairing_token") or "").strip()
        if pairing_token and pairing_token != self.state.pairing_token:
            health = await self._local_health()
            if health.get("idle") is not True:
                return
            self.state.pairing_token = pairing_token
            _enable_bridge_security(self.config.bridge_config_path, pairing_token)
            _save_state(self.config.state_path, self.state)
            await asyncio.to_thread(
                self.updater.command_runner,
                ["systemctl", "restart", self.config.bridge_service],
                check=True,
            )

        if control.get("credential_rotation_required") is True:
            response = await self.client.post(
                "/api/v1/device/credentials/renew",
                headers=self._headers(),
                json={"rotate_lan_token": False},
            )
            response.raise_for_status()
            self.state.device_token = str(response.json()["device_token"])
            _save_state(self.config.state_path, self.state)

    async def _handle_configuration(self, control: dict[str, object]) -> None:
        revision = int(control.get("configuration_revision") or 0)
        desired = control.get("desired_configuration")
        if revision <= self.state.last_configuration_revision or not isinstance(desired, dict):
            return
        raw_devices = desired.get("devices")
        if not isinstance(raw_devices, list):
            raise RuntimeError("Configuracao desejada sem lista de dispositivos.")
        health = await self._local_health()
        if health.get("idle") is not True:
            return
        assignments = [DeviceAssignment(**item) for item in raw_devices if isinstance(item, dict)]
        await asyncio.to_thread(apply_assignments, self.config.bridge_config_path, assignments)
        await asyncio.to_thread(
            self.updater.command_runner,
            ["systemctl", "restart", self.config.bridge_service],
            check=True,
        )
        self.state.last_configuration_revision = revision
        _save_state(self.config.state_path, self.state)

    async def _local_health(self) -> dict[str, object]:
        async with httpx.AsyncClient(timeout=3) as local:
            response = await local.get(self.config.bridge_health_url)
            response.raise_for_status()
            return response.json()

    async def _local_status(self) -> dict[str, object]:
        token = self.state.pairing_token
        if not token:
            return {}
        status_url = self.config.bridge_health_url.rsplit("/health", 1)[0] + "/v1/status"
        async with httpx.AsyncClient(timeout=3) as local:
            response = await local.get(status_url, headers={"Authorization": f"Bearer {token}"})
            response.raise_for_status()
            return response.json()

    async def _report_update(self, version: str, status: str, message: str | None) -> None:
        response = await self.client.post("/api/v1/device/update-events", headers=self._headers(), json={
            "version": version,
            "status": status,
            "message": message,
        })
        response.raise_for_status()

    def _heartbeat_payload(self) -> dict[str, object]:
        disk = shutil.disk_usage("/")
        return {
            "version": __version__,
            "architecture": platform.machine().lower(),
            "os": platform.platform(),
            "hostname": self.identity.hostname,
            "ip_addresses": local_addresses(),
            "uptime_seconds": _uptime_seconds(),
            "disk_total_bytes": disk.total,
            "disk_free_bytes": disk.free,
            "memory_total_bytes": _meminfo_value("MemTotal") * 1024,
            "memory_available_bytes": _meminfo_value("MemAvailable") * 1024,
            "cpu_temperature_c": _cpu_temperature(),
            "pairing_token_fingerprint": hashlib.sha256(self.state.pairing_token.encode()).hexdigest()
            if self.state.pairing_token else None,
        }

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.state.device_token}"}


def _load_state(path: Path) -> AgentState:
    if not path.exists():
        return AgentState()
    raw = json.loads(path.read_text(encoding="utf-8"))
    defaults = asdict(AgentState())
    return AgentState(**{key: raw[key] if key in raw and raw[key] is not None else value for key, value in defaults.items()})


def _save_state(path: Path, state: AgentState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as handle:
        json.dump(asdict(state), handle, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _enable_bridge_security(config_path: Path, token: str) -> None:
    raw = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    lines = raw.splitlines()
    start = next((index for index, line in enumerate(lines) if line.strip() == "[security]"), None)
    if start is not None:
        end = next((index for index in range(start + 1, len(lines)) if lines[index].startswith("[")), len(lines))
        del lines[start:end]
    lines.extend(["", "[security]", "require_auth = true", f'pairing_token = "{token}"'])
    temporary = config_path.with_suffix(f"{config_path.suffix}.security.tmp")
    temporary.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    os.chmod(temporary, 0o640)
    if config_path.exists():
        target_stat = config_path.stat()
        os.chown(temporary, target_stat.st_uid, target_stat.st_gid)
    os.replace(temporary, config_path)


def _uptime_seconds() -> int | None:
    try:
        return int(float(Path("/proc/uptime").read_text().split()[0]))
    except (OSError, ValueError, IndexError):
        return None


def _meminfo_value(name: str) -> int:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith(f"{name}:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0


def _cpu_temperature() -> float | None:
    try:
        return round(int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000, 1)
    except (OSError, ValueError):
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Agente de controle e atualizacao do PDV Device Bridge.")
    parser.add_argument("--config", type=Path, default=DEFAULT_AGENT_CONFIG_PATH)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    async def run() -> None:
        agent = ControlAgent(load_agent_config(args.config))
        try:
            if args.once:
                await agent.run_once()
            else:
                await agent.run_forever()
        finally:
            await agent.close()

    asyncio.run(run())


if __name__ == "__main__":
    main()
