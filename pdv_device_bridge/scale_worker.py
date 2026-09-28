from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import time

from .config import ScaleRuntimeConfig
from .device_registry import DeviceRegistry
from .scale_parser import parse_weight_payload
from .serial_io import read_scale_once


class ScaleReadError(RuntimeError):
    pass


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CachedScaleReading:
    grams: int
    kilograms: float
    stable: bool
    raw: str
    read_at_epoch_ms: int

    def to_payload(self) -> dict[str, object]:
        return {
            "grams": self.grams,
            "kilograms": self.kilograms,
            "stable": self.stable,
            "raw": self.raw,
            "read_at": _epoch_ms_to_iso(self.read_at_epoch_ms),
        }


class ScaleWorker:
    def __init__(self, registry: DeviceRegistry, config: ScaleRuntimeConfig) -> None:
        self._registry = registry
        self._config = config
        self._cache: dict[str, CachedScaleReading] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._serial_reads: dict[str, asyncio.Task[bytes]] = {}
        self._last_errors: dict[str, str] = {}

    async def read(self, scale_id: str, *, max_age_ms: int | None = None) -> dict[str, object]:
        effective_max_age_ms = self._config.cache_max_age_ms if max_age_ms is None else max(0, int(max_age_ms))

        cached = self._cache.get(scale_id)
        now_ms = _now_epoch_ms()
        if effective_max_age_ms > 0 and cached and (now_ms - cached.read_at_epoch_ms) <= effective_max_age_ms:
            payload = cached.to_payload()
            payload["source"] = "cache"
            return payload

        lock = self._locks.setdefault(scale_id, asyncio.Lock())
        async with lock:
            # Evita corrida entre leitores simultaneos do mesmo dispositivo.
            cached = self._cache.get(scale_id)
            now_ms = _now_epoch_ms()
            if effective_max_age_ms > 0 and cached and (now_ms - cached.read_at_epoch_ms) <= effective_max_age_ms:
                payload = cached.to_payload()
                payload["source"] = "cache"
                return payload

            active_read = self._serial_reads.get(scale_id)
            if active_read and not active_read.done():
                message = "Leitura serial anterior ainda esta travada; verifique a balanca e reinicie o bridge."
                self._last_errors[scale_id] = message
                raise ScaleReadError(message)

            descriptor = self._registry.get_descriptor("scale", scale_id)
            path = await self._registry.resolve_path("scale", scale_id)

            serial_read = asyncio.create_task(asyncio.to_thread(
                read_scale_once,
                descriptor,
                path=path,
                command_bytes=self._config.command_bytes,
                timeout_ms=self._config.read_timeout_ms,
                response_quiet_ms=self._config.response_quiet_ms,
                max_read_bytes=self._config.max_read_bytes,
            ))
            self._serial_reads[scale_id] = serial_read
            serial_read.add_done_callback(lambda task: self._finish_serial_read(scale_id, task))

            try:
                payload_bytes = await asyncio.wait_for(
                    asyncio.shield(serial_read),
                    timeout=max(0.1, self._config.operation_timeout_ms / 1000),
                )
            except asyncio.TimeoutError as exc:
                message = "Tempo limite da leitura serial da balanca excedido."
                self._last_errors[scale_id] = message
                logger.warning("scale read timed out: scale_id=%s path=%s", scale_id, path)
                raise ScaleReadError(message) from exc
            except Exception as exc:
                message = f"Falha na leitura serial da balanca: {exc}"
                self._last_errors[scale_id] = message
                logger.exception("scale read failed: scale_id=%s path=%s", scale_id, path)
                raise ScaleReadError(message) from exc

            parsed = parse_weight_payload(payload_bytes)
            if parsed is None:
                message = "Leitura vazia ou invalida retornada pela balanca."
                self._last_errors[scale_id] = message
                raise ScaleReadError(message)

            reading = CachedScaleReading(
                grams=parsed.grams,
                kilograms=parsed.kilograms,
                stable=parsed.stable,
                raw=parsed.raw_text,
                read_at_epoch_ms=_now_epoch_ms(),
            )

            self._cache[scale_id] = reading
            self._last_errors.pop(scale_id, None)
            result = reading.to_payload()
            result["source"] = "device"
            return result

    def health_snapshot(self) -> dict[str, dict[str, object]]:
        result: dict[str, dict[str, object]] = {}
        for scale_id in self._cache.keys() | self._serial_reads.keys() | self._last_errors.keys():
            reading = self._cache.get(scale_id)
            serial_read = self._serial_reads.get(scale_id)
            result[scale_id] = {
                "last_read_at": _epoch_ms_to_iso(reading.read_at_epoch_ms) if reading else None,
                "grams": reading.grams if reading else None,
                "stable": reading.stable if reading else None,
                "serial_read_in_progress": bool(serial_read and not serial_read.done()),
                "last_error": self._last_errors.get(scale_id),
            }

        return result

    def _finish_serial_read(self, scale_id: str, task: asyncio.Task[bytes]) -> None:
        if self._serial_reads.get(scale_id) is task:
            self._serial_reads.pop(scale_id, None)
        if not task.cancelled():
            task.exception()


def _now_epoch_ms() -> int:
    return int(time.time() * 1000)


def _epoch_ms_to_iso(value: int) -> str:
    dt = datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")
