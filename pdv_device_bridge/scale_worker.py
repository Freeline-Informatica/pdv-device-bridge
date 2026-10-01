from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import time

from .config import ScaleRuntimeConfig
from .device_registry import DeviceRegistry
from .scale_parser import parse_weight_payload
from .serial_io import ScaleSerialSession


class ScaleReadError(RuntimeError):
    pass


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CachedScaleReading:
    grams: int | None
    kilograms: float | None
    stable: bool | None
    raw: str
    read_at_epoch_ms: int
    state: str = "weight"

    def to_payload(self) -> dict[str, object]:
        return {
            "grams": self.grams,
            "kilograms": self.kilograms,
            "stable": self.stable,
            "raw": self.raw,
            "read_at": _epoch_ms_to_iso(self.read_at_epoch_ms),
            "state": self.state,
        }


class ScaleWorker:
    def __init__(self, registry: DeviceRegistry, config: ScaleRuntimeConfig) -> None:
        self._registry = registry
        self._config = config
        self._cache: dict[str, CachedScaleReading] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._serial_reads: dict[str, asyncio.Task[bytes]] = {}
        self._last_errors: dict[str, str] = {}
        self._sessions: dict[str, ScaleSerialSession] = {}
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, object]]]] = {}
        self._stream_tasks: dict[str, asyncio.Task[None]] = {}
        self._history: dict[str, deque[dict[str, object]]] = {}

    async def read(self, scale_id: str, *, max_age_ms: int | None = None) -> dict[str, object]:
        effective_max_age_ms = self._config.cache_max_age_ms if max_age_ms is None else max(0, int(max_age_ms))

        cached = self._cache.get(scale_id)
        now_ms = _now_epoch_ms()
        if effective_max_age_ms > 0 and cached and not self._last_errors.get(scale_id) and (now_ms - cached.read_at_epoch_ms) <= effective_max_age_ms:
            payload = cached.to_payload()
            payload["source"] = "cache"
            return payload

        lock = self._locks.setdefault(scale_id, asyncio.Lock())
        async with lock:
            # Evita corrida entre leitores simultaneos do mesmo dispositivo.
            cached = self._cache.get(scale_id)
            now_ms = _now_epoch_ms()
            if effective_max_age_ms > 0 and cached and not self._last_errors.get(scale_id) and (now_ms - cached.read_at_epoch_ms) <= effective_max_age_ms:
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
            session = self._sessions.get(scale_id)
            if session is None or session.path != path or session.descriptor != descriptor:
                if session is not None:
                    await asyncio.to_thread(session.close)
                session = ScaleSerialSession(descriptor, path)
                self._sessions[scale_id] = session

            serial_read = asyncio.create_task(asyncio.to_thread(
                session.read,
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
                self._sessions.pop(scale_id, None)
                session.abort()
                raise ScaleReadError(message) from exc
            except Exception as exc:
                message = f"Falha na leitura serial da balanca: {exc}"
                self._last_errors[scale_id] = message
                logger.exception("scale read failed: scale_id=%s path=%s", scale_id, path)
                raise ScaleReadError(message) from exc

            if not payload_bytes:
                if descriptor.no_response_state == "no_reading":
                    previous_error = self._last_errors.get(scale_id)
                    if previous_error:
                        await asyncio.to_thread(session.close)
                        self._sessions.pop(scale_id, None)
                        raise ScaleReadError(previous_error)
                    reading = CachedScaleReading(
                        grams=None,
                        kilograms=None,
                        stable=None,
                        raw="",
                        read_at_epoch_ms=_now_epoch_ms(),
                        state="no_reading",
                    )
                    self._cache[scale_id] = reading
                    self._history.setdefault(scale_id, deque(maxlen=500)).appendleft(reading.to_payload())
                    result = reading.to_payload()
                    result["source"] = "device"
                    return result
                await asyncio.to_thread(session.close)
                self._sessions.pop(scale_id, None)
                message = "Balanca sem resposta serial; confira a conexao USB."
                self._last_errors[scale_id] = message
                logger.warning("scale did not respond: scale_id=%s path=%s", scale_id, path)
                raise ScaleReadError(message)

            parsed = parse_weight_payload(payload_bytes)
            if parsed is None:
                await asyncio.to_thread(session.close)
                self._sessions.pop(scale_id, None)
                logger.warning("invalid scale payload: scale_id=%s raw_hex=%s", scale_id, payload_bytes.hex())
                message = "Resposta invalida retornada pela balanca."
                self._last_errors[scale_id] = message
                raise ScaleReadError(message)

            reading = CachedScaleReading(
                grams=parsed.grams,
                kilograms=parsed.kilograms,
                stable=parsed.stable,
                raw=parsed.raw_text,
                read_at_epoch_ms=_now_epoch_ms(),
                state="negative" if parsed.grams < 0 else ("empty" if parsed.grams == 0 else "weight"),
            )

            self._cache[scale_id] = reading
            history = self._history.setdefault(scale_id, deque(maxlen=500))
            history.appendleft(reading.to_payload())
            self._last_errors.pop(scale_id, None)
            result = reading.to_payload()
            result["source"] = "device"
            return result

    async def stream(self, scale_id: str):
        self._registry.get_descriptor("scale", scale_id)
        queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=1)
        subscribers = self._subscribers.setdefault(scale_id, set())
        subscribers.add(queue)
        task = self._stream_tasks.get(scale_id)
        if task is None or task.done():
            self._stream_tasks[scale_id] = asyncio.create_task(self._stream_loop(scale_id))
        try:
            while True:
                yield await queue.get()
        finally:
            subscribers.discard(queue)
            if not subscribers:
                self._subscribers.pop(scale_id, None)
                task = self._stream_tasks.pop(scale_id, None)
                if task and not task.done():
                    task.cancel()

    async def _stream_loop(self, scale_id: str) -> None:
        try:
            while self._subscribers.get(scale_id):
                try:
                    payload = await self.read(scale_id, max_age_ms=0)
                    delay = 0.1
                except Exception as exc:
                    payload = {"state": "error", "message": str(exc), "read_at": _epoch_ms_to_iso(_now_epoch_ms())}
                    delay = 1.0
                for queue in tuple(self._subscribers.get(scale_id, ())):
                    if queue.full():
                        queue.get_nowait()
                    queue.put_nowait(payload)
                if self._subscribers.get(scale_id):
                    await asyncio.sleep(delay)
        finally:
            if self._stream_tasks.get(scale_id) is asyncio.current_task():
                self._stream_tasks.pop(scale_id, None)

    async def stop(self) -> None:
        tasks = tuple(self._stream_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._stream_tasks.clear()
        for scale_id, session in list(self._sessions.items()):
            active_read = self._serial_reads.get(scale_id)
            if active_read and not active_read.done():
                session.abort()
                continue
            await asyncio.to_thread(session.close)
        self._sessions.clear()

    def health_snapshot(self) -> dict[str, dict[str, object]]:
        result: dict[str, dict[str, object]] = {}
        for scale_id in self._cache.keys() | self._serial_reads.keys() | self._last_errors.keys():
            reading = self._cache.get(scale_id)
            serial_read = self._serial_reads.get(scale_id)
            result[scale_id] = {
                "last_read_at": _epoch_ms_to_iso(reading.read_at_epoch_ms) if reading else None,
                "grams": reading.grams if reading else None,
                "stable": reading.stable if reading else None,
                "state": reading.state if reading else None,
                "serial_read_in_progress": bool(serial_read and not serial_read.done()),
                "last_error": self._last_errors.get(scale_id),
            }

        return result

    def recent_readings(self, scale_id: str, *, limit: int = 50) -> list[dict[str, object]]:
        return list(self._history.get(scale_id, ()))[:max(1, min(int(limit), 500))]

    def is_idle(self) -> bool:
        return not any(self._subscribers.values()) and not any(
            not task.done() for task in self._serial_reads.values()
        )

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
