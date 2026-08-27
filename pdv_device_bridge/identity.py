from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import socket
import uuid


@dataclass(slots=True, frozen=True)
class BridgeIdentity:
    device_id: str
    code: str
    hostname: str
    site_id: str | None
    enrollment_token: str | None
    created_at: str

    def public_payload(self, *, version: str, api_version: str = "v1") -> dict[str, object]:
        return {
            "device_id": self.device_id,
            "code": self.code,
            "hostname": self.hostname,
            "site_id": self.site_id,
            "version": version,
            "api_version": api_version,
        }


def load_or_create_identity(state_path: str | Path, provision_path: str | Path | None = None) -> BridgeIdentity:
    state = Path(state_path)
    if state.exists():
        return _parse_identity(json.loads(state.read_text(encoding="utf-8")))

    provision: dict[str, object] = {}
    provision_file = Path(provision_path) if provision_path else None
    if provision_file and provision_file.exists():
        provision = json.loads(provision_file.read_text(encoding="utf-8"))

    device_id = str(provision.get("device_id") or uuid.uuid4()).strip().lower()
    uuid.UUID(device_id)
    code = _normalize_code(str(provision.get("code") or device_id.replace("-", "")[:6]))
    hostname = _normalize_hostname(str(provision.get("hostname") or f"freeline-bridge-{code.lower()}"))
    identity = BridgeIdentity(
        device_id=device_id,
        code=code,
        hostname=hostname,
        site_id=str(provision.get("site_id") or "").strip() or None,
        enrollment_token=str(provision.get("enrollment_token") or "").strip() or None,
        created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    )

    state.parent.mkdir(parents=True, exist_ok=True)
    temporary = state.with_suffix(f"{state.suffix}.tmp")
    temporary.write_text(json.dumps(asdict(identity), indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, state)

    if provision_file and provision_file.exists():
        provision_file.unlink()

    return identity


def local_addresses() -> list[str]:
    addresses: set[str] = set()
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None):
            address = str(item[4][0]).split("%", 1)[0]
            if address and not address.startswith("127.") and address != "::1":
                addresses.add(address)
    except OSError:
        pass
    return sorted(addresses)


def _parse_identity(raw: dict[str, object]) -> BridgeIdentity:
    device_id = str(raw.get("device_id") or "").strip().lower()
    uuid.UUID(device_id)
    return BridgeIdentity(
        device_id=device_id,
        code=_normalize_code(str(raw.get("code") or device_id.replace("-", "")[:6])),
        hostname=_normalize_hostname(str(raw.get("hostname") or "")),
        site_id=str(raw.get("site_id") or "").strip() or None,
        enrollment_token=str(raw.get("enrollment_token") or "").strip() or None,
        created_at=str(raw.get("created_at") or ""),
    )


def _normalize_code(value: str) -> str:
    code = re.sub(r"[^A-Za-z0-9]", "", value).upper()[:12]
    if len(code) < 6:
        raise ValueError("Codigo do bridge deve ter ao menos 6 caracteres.")
    return code


def _normalize_hostname(value: str) -> str:
    hostname = re.sub(r"[^a-z0-9-]", "-", value.strip().lower()).strip("-")[:63]
    if not hostname:
        raise ValueError("Hostname do bridge invalido.")
    return hostname
