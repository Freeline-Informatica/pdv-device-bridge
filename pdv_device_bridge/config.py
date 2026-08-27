from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
import tomllib

DEFAULT_CONFIG_PATH = Path("/var/lib/pdv-device-bridge/config.toml")


@dataclass(slots=True, frozen=True)
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8787
    log_level: str = "info"
    cors_allowed_origins: tuple[str, ...] = ("*",)


@dataclass(slots=True, frozen=True)
class IdentityConfig:
    state_path: str = "/var/lib/pdv-device-bridge/identity.json"
    provision_path: str = "/boot/firmware/pdv-device-bridge.json"


@dataclass(slots=True, frozen=True)
class SecurityConfig:
    require_auth: bool = False
    pairing_token: str | None = None


@dataclass(slots=True, frozen=True)
class ScaleRuntimeConfig:
    read_timeout_ms: int = 800
    response_quiet_ms: int = 30
    cache_max_age_ms: int = 1500
    max_read_bytes: int = 200
    command_bytes: bytes = b"\x04\x05"


@dataclass(slots=True, frozen=True)
class PrinterRuntimeConfig:
    queue_size: int = 100
    retry_delays_ms: tuple[int, ...] = (200, 500, 1000)
    serial_chunk_size: int = 512
    serial_chunk_delay_ms: int = 15
    print_settle_ms: int = 1000
    write_timeout_ms: int = 3000


@dataclass(slots=True, frozen=True)
class SerialDeviceConfig:
    device_id: str
    path: str | None = None
    baudrate: int = 9600
    bytesize: int = 8
    parity: str = "N"
    stopbits: float = 1.0
    usb_vid: int | None = None
    usb_pid: int | None = None
    usb_serial: str | None = None


@dataclass(slots=True, frozen=True)
class BridgeConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    identity: IdentityConfig = field(default_factory=IdentityConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    scale: ScaleRuntimeConfig = field(default_factory=ScaleRuntimeConfig)
    printer: PrinterRuntimeConfig = field(default_factory=PrinterRuntimeConfig)
    discovery_interval_ms: int = 2000
    scales: tuple[SerialDeviceConfig, ...] = ()
    printers: tuple[SerialDeviceConfig, ...] = ()


class ConfigError(ValueError):
    pass


def load_config(path: Path | str) -> BridgeConfig:
    config_path = Path(path)
    raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    server_raw = raw.get("server", {})

    server = ServerConfig(
        host=str(server_raw.get("host", "0.0.0.0")).strip() or "0.0.0.0",
        port=int(server_raw.get("port", 8787)),
        log_level=str(server_raw.get("log_level", "info")).strip() or "info",
        cors_allowed_origins=_parse_string_tuple(server_raw.get("cors_allowed_origins"), ("*",), "server.cors_allowed_origins"),
    )

    identity_raw = raw.get("identity", {})
    identity = IdentityConfig(
        state_path=str(identity_raw.get("state_path", "/var/lib/pdv-device-bridge/identity.json")),
        provision_path=str(identity_raw.get("provision_path", "/boot/firmware/pdv-device-bridge.json")),
    )

    security_raw = raw.get("security", {})
    pairing_token = str(security_raw.get("pairing_token", "")).strip() or None
    security = SecurityConfig(
        require_auth=bool(security_raw.get("require_auth", False)),
        pairing_token=pairing_token,
    )
    if security.require_auth and not security.pairing_token:
        raise ConfigError("security.pairing_token e obrigatorio quando require_auth=true.")

    scale_raw = raw.get("scale", {})
    scale = ScaleRuntimeConfig(
        read_timeout_ms=int(scale_raw.get("read_timeout_ms", 800)),
        response_quiet_ms=int(scale_raw.get("response_quiet_ms", 30)),
        cache_max_age_ms=int(scale_raw.get("cache_max_age_ms", 1500)),
        max_read_bytes=int(scale_raw.get("max_read_bytes", 200)),
        command_bytes=_parse_hex_bytes(str(scale_raw.get("command_hex", "0405"))),
    )

    printer_raw = raw.get("printer", {})
    printer = PrinterRuntimeConfig(
        queue_size=int(printer_raw.get("queue_size", 100)),
        retry_delays_ms=tuple(int(value) for value in printer_raw.get("retry_delays_ms", [200, 500, 1000])),
        serial_chunk_size=int(printer_raw.get("serial_chunk_size", 512)),
        serial_chunk_delay_ms=int(printer_raw.get("serial_chunk_delay_ms", 15)),
        print_settle_ms=int(printer_raw.get("print_settle_ms", 1000)),
        write_timeout_ms=int(printer_raw.get("write_timeout_ms", 3000)),
    )

    discovery_interval_ms = int(raw.get("discovery", {}).get("interval_ms", 2000))

    devices = raw.get("devices", {})
    scales = tuple(_parse_device(item, "scale") for item in devices.get("scales", []))
    printers = tuple(_parse_device(item, "printer") for item in devices.get("printers", []))

    if not scales and not printers:
        raise ConfigError("Nenhum dispositivo configurado. Defina [devices.scales] e/ou [devices.printers].")

    return BridgeConfig(
        server=server,
        identity=identity,
        security=security,
        scale=scale,
        printer=printer,
        discovery_interval_ms=discovery_interval_ms,
        scales=scales,
        printers=printers,
    )


def _parse_device(raw: dict, kind: str) -> SerialDeviceConfig:
    device_id = str(raw.get("id", "")).strip()
    if not device_id:
        raise ConfigError(f"Dispositivo {kind} sem id.")

    path = raw.get("path")
    path_value = str(path).strip() if path else None

    parity = str(raw.get("parity", "N")).strip().upper() or "N"
    if parity not in {"N", "E", "O", "M", "S"}:
        raise ConfigError(f"Paridade invalida para {device_id}: {parity}")

    bytesize = int(raw.get("bytesize", 8))
    if bytesize not in {5, 6, 7, 8}:
        raise ConfigError(f"Bytesize invalido para {device_id}: {bytesize}")

    stopbits = float(raw.get("stopbits", 1))
    if stopbits not in {1.0, 1.5, 2.0}:
        raise ConfigError(f"Stopbits invalido para {device_id}: {stopbits}")

    usb_vid = raw.get("usb_vid")
    usb_pid = raw.get("usb_pid")

    return SerialDeviceConfig(
        device_id=device_id,
        path=path_value,
        baudrate=int(raw.get("baudrate", 9600)),
        bytesize=bytesize,
        parity=parity,
        stopbits=stopbits,
        usb_vid=_parse_int_or_none(usb_vid),
        usb_pid=_parse_int_or_none(usb_pid),
        usb_serial=str(raw.get("usb_serial", "")).strip() or None,
    )


def _parse_int_or_none(value: object) -> int | None:
    if value is None:
        return None

    if isinstance(value, int):
        return value

    text = str(value).strip().lower()
    if not text:
        return None

    if text.startswith("0x"):
        return int(text, 16)

    return int(text)


def _parse_hex_bytes(raw: str) -> bytes:
    cleaned = re.sub(r"[^0-9a-fA-F]", "", raw)
    if not cleaned:
        raise ConfigError("command_hex vazio para leitura da balanca.")

    if len(cleaned) % 2 != 0:
        cleaned = f"0{cleaned}"

    return bytes.fromhex(cleaned)


def _parse_string_tuple(value: object, default: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    if value is None:
        return default

    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple)):
        values = [str(item) for item in value]
    else:
        raise ConfigError(f"{field_name} deve ser string ou lista de strings.")

    cleaned = tuple(item.strip() for item in values if item.strip())
    if not cleaned:
        raise ConfigError(f"{field_name} nao pode ser vazio.")

    return cleaned
