from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib


DEFAULT_AGENT_CONFIG_PATH = Path("/etc/pdv-device-bridge/agent.toml")


@dataclass(slots=True, frozen=True)
class AgentConfig:
    control_url: str
    state_path: Path
    identity_path: Path
    bridge_config_path: Path
    public_key_path: Path
    releases_root: Path
    current_symlink: Path
    poll_interval_seconds: int = 60
    activation_hour: int = 3
    bridge_service: str = "pdv-device-bridge.service"
    bridge_health_url: str = "http://127.0.0.1:8787/health"


def load_agent_config(path: Path | str) -> AgentConfig:
    raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    control = raw.get("control", {})
    update = raw.get("update", {})
    local = raw.get("local", {})
    control_url = str(control.get("base_url", "")).strip().rstrip("/")
    if not control_url.startswith("https://"):
        raise ValueError("control.base_url deve usar HTTPS.")
    activation_hour = int(update.get("activation_hour", 3))
    if activation_hour < 0 or activation_hour > 23:
        raise ValueError("update.activation_hour deve estar entre 0 e 23.")
    return AgentConfig(
        control_url=control_url,
        state_path=Path(control.get("state_path", "/var/lib/pdv-device-bridge/agent-state.json")),
        identity_path=Path(local.get("identity_path", "/var/lib/pdv-device-bridge/identity.json")),
        bridge_config_path=Path(local.get("bridge_config_path", "/var/lib/pdv-device-bridge/config.toml")),
        public_key_path=Path(update.get("public_key_path", "/etc/pdv-device-bridge/release-public-key.pem")),
        releases_root=Path(update.get("releases_root", "/opt/pdv-device-bridge/releases")),
        current_symlink=Path(update.get("current_symlink", "/opt/pdv-device-bridge/current")),
        poll_interval_seconds=max(15, int(control.get("poll_interval_seconds", 60))),
        activation_hour=activation_hour,
        bridge_service=str(local.get("bridge_service", "pdv-device-bridge.service")),
        bridge_health_url=str(local.get("bridge_health_url", "http://127.0.0.1:8787/health")),
    )
