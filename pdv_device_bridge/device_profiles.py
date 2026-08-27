from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(slots=True, frozen=True)
class DeviceProfile:
    profile_id: str
    kind: str
    label: str
    baudrate: int
    bytesize: int = 8
    parity: str = "N"
    stopbits: float = 1.0

    def to_payload(self) -> dict[str, object]:
        return asdict(self)


PROFILES: tuple[DeviceProfile, ...] = (
    DeviceProfile("scale-generic-9600-8n2", "scale", "Balança serial 9600 8N2", 9600, stopbits=2.0),
    DeviceProfile("scale-generic-9600-8n1", "scale", "Balança serial 9600 8N1", 9600),
    DeviceProfile("printer-escpos-115200-8n1", "printer", "Impressora ESC/POS 115200 8N1", 115200),
    DeviceProfile("printer-escpos-9600-8n1", "printer", "Impressora ESC/POS 9600 8N1", 9600),
)


def profiles_payload() -> list[dict[str, object]]:
    return [profile.to_payload() for profile in PROFILES]


def get_profile(profile_id: str) -> DeviceProfile:
    for profile in PROFILES:
        if profile.profile_id == profile_id:
            return profile
    raise ValueError(f"Perfil serial desconhecido: {profile_id}")
