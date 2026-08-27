from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import time

from .config import load_config
from .device_profiles import get_profile


@dataclass(slots=True, frozen=True)
class DeviceAssignment:
    device_id: str
    kind: str
    profile_id: str
    path: str
    usb_vid: int | None = None
    usb_pid: int | None = None
    usb_serial: str | None = None


def apply_assignments(config_path: str | Path, assignments: list[DeviceAssignment]) -> Path:
    target = Path(config_path)
    if not assignments:
        raise ValueError("Informe ao menos um periferico.")

    seen: set[tuple[str, str]] = set()
    lines = _base_config_lines(target)
    lines.extend(["", "[devices]"])

    for assignment in assignments:
        key = (assignment.kind, assignment.device_id)
        if key in seen:
            raise ValueError(f"Dispositivo duplicado: {assignment.kind}/{assignment.device_id}")
        seen.add(key)

        if assignment.kind not in {"scale", "printer"}:
            raise ValueError(f"Tipo de dispositivo invalido: {assignment.kind}")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", assignment.device_id):
            raise ValueError(f"ID de dispositivo invalido: {assignment.device_id}")
        if not assignment.path.startswith("/dev/"):
            raise ValueError("A porta serial deve estar dentro de /dev.")

        profile = get_profile(assignment.profile_id)
        if profile.kind != assignment.kind:
            raise ValueError(f"Perfil {profile.profile_id} nao pertence ao tipo {assignment.kind}.")

        plural = "scales" if assignment.kind == "scale" else "printers"
        lines.extend([
            "",
            f"[[devices.{plural}]]",
            f'id = "{_toml_string(assignment.device_id)}"',
            f'path = "{_toml_string(assignment.path)}"',
            f"baudrate = {profile.baudrate}",
            f"bytesize = {profile.bytesize}",
            f'parity = "{profile.parity}"',
            f"stopbits = {profile.stopbits:g}",
        ])
        if assignment.usb_vid is not None:
            lines.append(f'usb_vid = "0x{assignment.usb_vid:04X}"')
        if assignment.usb_pid is not None:
            lines.append(f'usb_pid = "0x{assignment.usb_pid:04X}"')
        if assignment.usb_serial:
            lines.append(f'usb_serial = "{_toml_string(assignment.usb_serial)}"')

    target.parent.mkdir(parents=True, exist_ok=True)
    backup = target.with_name(f"{target.name}.bak-{int(time.time())}")
    if target.exists():
        shutil.copy2(target, backup)

    temporary = target.with_suffix(f"{target.suffix}.tmp")
    temporary.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    os.chmod(temporary, 0o640)
    if target.exists():
        target_stat = target.stat()
        os.chown(temporary, target_stat.st_uid, target_stat.st_gid)
    try:
        load_config(temporary)
        os.replace(temporary, target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return backup


def _base_config_lines(path: Path) -> list[str]:
    if not path.exists():
        return [
            "[server]",
            'host = "0.0.0.0"',
            "port = 8787",
            'cors_allowed_origins = ["*"]',
        ]

    raw = path.read_text(encoding="utf-8")
    marker = re.search(r"(?m)^\[devices\]\s*$", raw)
    return raw[: marker.start()].rstrip().splitlines() if marker else raw.rstrip().splitlines()


def _toml_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
