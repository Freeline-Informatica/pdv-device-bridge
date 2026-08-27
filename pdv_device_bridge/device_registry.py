from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
import time
from typing import Literal

from serial.tools import list_ports

from .config import BridgeConfig, SerialDeviceConfig

DeviceKind = Literal["scale", "printer"]


class DeviceRegistryError(RuntimeError):
    pass


class DeviceNotFoundError(DeviceRegistryError):
    pass


class DeviceUnavailableError(DeviceRegistryError):
    pass


@dataclass(slots=True)
class DeviceRuntimeState:
    kind: DeviceKind
    device_id: str
    configured_path: str | None
    resolved_path: str | None
    available: bool
    last_seen_at: float | None
    usb_vid: int | None
    usb_pid: int | None
    usb_serial: str | None


class DeviceRegistry:
    def __init__(self, config: BridgeConfig) -> None:
        self._descriptors: dict[tuple[DeviceKind, str], SerialDeviceConfig] = {}
        self._states: dict[tuple[DeviceKind, str], DeviceRuntimeState] = {}
        self._refresh_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._discovery_task: asyncio.Task[None] | None = None
        self._discovery_interval = max(0.2, config.discovery_interval_ms / 1000)

        for descriptor in config.scales:
            self._descriptors[("scale", descriptor.device_id)] = descriptor

        for descriptor in config.printers:
            self._descriptors[("printer", descriptor.device_id)] = descriptor

    async def start(self) -> None:
        await self.refresh()
        if self._discovery_task is None:
            self._stop_event.clear()
            self._discovery_task = asyncio.create_task(self._discovery_loop(), name="device-registry-loop")

    async def stop(self) -> None:
        self._stop_event.set()

        if self._discovery_task:
            self._discovery_task.cancel()
            try:
                await self._discovery_task
            except asyncio.CancelledError:
                pass
            self._discovery_task = None

    async def refresh(self) -> None:
        async with self._refresh_lock:
            ports = await asyncio.to_thread(list_ports.comports)
            now = time.time()

            next_states: dict[tuple[DeviceKind, str], DeviceRuntimeState] = {}
            for key, descriptor in self._descriptors.items():
                resolved_path = self._resolve_descriptor_path(descriptor, ports)
                available = bool(resolved_path)

                previous_state = self._states.get(key)
                if available:
                    last_seen_at = now
                else:
                    last_seen_at = previous_state.last_seen_at if previous_state else None

                kind, device_id = key
                next_states[key] = DeviceRuntimeState(
                    kind=kind,
                    device_id=device_id,
                    configured_path=descriptor.path,
                    resolved_path=resolved_path,
                    available=available,
                    last_seen_at=last_seen_at,
                    usb_vid=descriptor.usb_vid,
                    usb_pid=descriptor.usb_pid,
                    usb_serial=descriptor.usb_serial,
                )

            self._states = next_states

    async def resolve_path(self, kind: DeviceKind, device_id: str) -> str:
        key = (kind, device_id)
        if key not in self._descriptors:
            raise DeviceNotFoundError(f"{kind} '{device_id}' nao configurado.")

        state = self._states.get(key)
        if state and state.available and state.resolved_path:
            return state.resolved_path

        await self.refresh()

        state = self._states.get(key)
        if state and state.available and state.resolved_path:
            return state.resolved_path

        raise DeviceUnavailableError(f"{kind} '{device_id}' indisponivel no momento.")

    def get_descriptor(self, kind: DeviceKind, device_id: str) -> SerialDeviceConfig:
        key = (kind, device_id)
        descriptor = self._descriptors.get(key)
        if not descriptor:
            raise DeviceNotFoundError(f"{kind} '{device_id}' nao configurado.")
        return descriptor

    def snapshot(self) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for key in sorted(self._descriptors.keys()):
            state = self._states.get(key)
            if not state:
                continue

            rows.append(
                {
                    "kind": state.kind,
                    "device_id": state.device_id,
                    "available": state.available,
                    "configured_path": state.configured_path,
                    "resolved_path": state.resolved_path,
                    "last_seen_at": _to_iso_timestamp(state.last_seen_at),
                    "usb_vid": state.usb_vid,
                    "usb_pid": state.usb_pid,
                    "usb_serial": state.usb_serial,
                },
            )

        return rows

    def serial_ports_snapshot(self) -> list[dict[str, object]]:
        configured_paths = {
            str(state.resolved_path or state.configured_path)
            for state in self._states.values()
            if state.resolved_path or state.configured_path
        }
        rows: list[dict[str, object]] = []
        for port in list_ports.comports():
            rows.append({
                "path": str(port.device),
                "name": str(getattr(port, "name", "") or ""),
                "description": str(getattr(port, "description", "") or ""),
                "manufacturer": str(getattr(port, "manufacturer", "") or "") or None,
                "product": str(getattr(port, "product", "") or "") or None,
                "vid": getattr(port, "vid", None),
                "pid": getattr(port, "pid", None),
                "serial_number": str(getattr(port, "serial_number", "") or "") or None,
                "configured": str(port.device) in configured_paths,
            })
        return sorted(rows, key=lambda item: str(item["path"]))

    async def _discovery_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.refresh()
            except Exception:
                # Nao interrompe o loop de descoberta por erro pontual.
                pass

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._discovery_interval)
            except asyncio.TimeoutError:
                continue

    def _resolve_descriptor_path(self, descriptor: SerialDeviceConfig, ports: list) -> str | None:
        if descriptor.path:
            preferred = Path(descriptor.path)
            if preferred.exists():
                return str(preferred.resolve())

        for candidate in Path("/dev/serial/by-id").glob("*"):
            if not candidate.exists():
                continue

            resolved = candidate.resolve()
            if not resolved.exists():
                continue

            text = str(candidate).lower()
            if descriptor.usb_serial and descriptor.usb_serial.lower() in text:
                return str(resolved)

            if descriptor.usb_vid is not None and descriptor.usb_pid is not None:
                vid = format(descriptor.usb_vid, "04x")
                pid = format(descriptor.usb_pid, "04x")
                if vid in text and pid in text:
                    return str(resolved)

        has_usb_match_criteria = (
            descriptor.usb_vid is not None
            or descriptor.usb_pid is not None
            or bool(descriptor.usb_serial)
        )
        if not has_usb_match_criteria:
            return None

        for port in ports:
            if descriptor.usb_vid is not None and port.vid != descriptor.usb_vid:
                continue

            if descriptor.usb_pid is not None and port.pid != descriptor.usb_pid:
                continue

            if descriptor.usb_serial:
                serial_number = str(getattr(port, "serial_number", "") or "")
                if serial_number.lower() != descriptor.usb_serial.lower():
                    continue

            return str(port.device)

        return None


def _to_iso_timestamp(value: float | None) -> str | None:
    if value is None:
        return None

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value))
