from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import time
import uuid

from .config import PrinterRuntimeConfig
from .device_registry import DeviceRegistry
from .serial_io import send_printer_payload


class PrinterWorkerError(RuntimeError):
    pass


class QueueLimitReachedError(PrinterWorkerError):
    pass


class JobNotFoundError(PrinterWorkerError):
    pass


@dataclass(slots=True)
class PrinterJob:
    job_id: str
    printer_id: str
    request_id: str | None
    status: str
    message: str
    attempts: int
    created_at_epoch_ms: int
    updated_at_epoch_ms: int
    retry_of: str | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "printer_id": self.printer_id,
            "request_id": self.request_id,
            "status": self.status,
            "message": self.message,
            "attempts": self.attempts,
            "created_at": _epoch_ms_to_iso(self.created_at_epoch_ms),
            "updated_at": _epoch_ms_to_iso(self.updated_at_epoch_ms),
            "retry_of": self.retry_of,
        }


@dataclass(slots=True)
class _QueuedPrinterJob:
    job_id: str
    payload: bytes


class PrinterWorker:
    def __init__(self, registry: DeviceRegistry, config: PrinterRuntimeConfig, printer_ids: list[str]) -> None:
        self._registry = registry
        self._config = config
        self._printer_ids = printer_ids
        self._queues = {printer_id: asyncio.Queue[_QueuedPrinterJob](maxsize=config.queue_size) for printer_id in printer_ids}
        self._jobs: dict[tuple[str, str], PrinterJob] = {}
        self._payloads: dict[tuple[str, str], bytes] = {}
        self._active_jobs: dict[str, str] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._locks = {printer_id: asyncio.Lock() for printer_id in printer_ids}

    async def start(self) -> None:
        for printer_id in self._printer_ids:
            if printer_id in self._workers:
                continue
            self._workers[printer_id] = asyncio.create_task(self._run_worker(printer_id), name=f"printer-worker:{printer_id}")

    async def stop(self) -> None:
        for task in self._workers.values():
            task.cancel()

        for task in self._workers.values():
            try:
                await task
            except asyncio.CancelledError:
                pass

        self._workers.clear()

    def submit_job(self, printer_id: str, payload: bytes, request_id: str | None = None) -> dict[str, object]:
        queue = self._queues.get(printer_id)
        if queue is None:
            raise PrinterWorkerError(f"Impressora '{printer_id}' nao configurada.")

        job_id = uuid.uuid4().hex
        now_ms = _now_epoch_ms()
        job = PrinterJob(
            job_id=job_id,
            printer_id=printer_id,
            request_id=request_id,
            status="queued",
            message="Job enfileirado para envio.",
            attempts=0,
            created_at_epoch_ms=now_ms,
            updated_at_epoch_ms=now_ms,
        )
        self._jobs[(printer_id, job_id)] = job
        self._payloads[(printer_id, job_id)] = payload

        try:
            queue.put_nowait(_QueuedPrinterJob(job_id=job_id, payload=payload))
        except asyncio.QueueFull as exc:
            self._update_job(
                printer_id,
                job_id,
                status="failed",
                message="Fila cheia para esta impressora.",
            )
            raise QueueLimitReachedError("Fila de impressao cheia.") from exc

        self._prune_history(printer_id)
        return job.to_payload()

    def get_job(self, printer_id: str, job_id: str) -> dict[str, object]:
        job = self._jobs.get((printer_id, job_id))
        if not job:
            raise JobNotFoundError(f"Job '{job_id}' nao encontrado para a impressora '{printer_id}'.")
        return job.to_payload()

    def list_jobs(self, printer_id: str, *, limit: int = 50) -> list[dict[str, object]]:
        if printer_id not in self._queues:
            raise PrinterWorkerError(f"Impressora '{printer_id}' nao configurada.")
        jobs = [job for (owner, _), job in self._jobs.items() if owner == printer_id]
        jobs.sort(key=lambda job: job.created_at_epoch_ms, reverse=True)
        return [job.to_payload() for job in jobs[:max(1, min(int(limit), 500))]]

    def retry_job(self, printer_id: str, job_id: str) -> dict[str, object]:
        original = self._jobs.get((printer_id, job_id))
        if original is None:
            raise JobNotFoundError(f"Job '{job_id}' nao encontrado para a impressora '{printer_id}'.")
        if original.status != "failed":
            raise PrinterWorkerError("Somente jobs com falha podem ser reenviados.")
        payload = self._payloads.get((printer_id, job_id))
        if payload is None:
            raise PrinterWorkerError("Payload original indisponivel para reenvio.")
        created = self.submit_job(printer_id, payload, request_id=original.request_id)
        retry_job = self._jobs[(printer_id, str(created["job_id"]))]
        retry_job.retry_of = job_id
        retry_job.message = f"Reenvio do job {job_id} enfileirado. Pode gerar impressao duplicada se o envio anterior chegou ao equipamento."
        return retry_job.to_payload()

    def health_snapshot(self) -> dict[str, dict[str, object]]:
        snapshot: dict[str, dict[str, object]] = {}
        for printer_id, queue in self._queues.items():
            snapshot[printer_id] = {
                "queue_size": queue.qsize(),
                "queue_limit": queue.maxsize,
                "active_job_id": self._active_jobs.get(printer_id),
                "running": printer_id in self._workers and not self._workers[printer_id].done(),
            }

        return snapshot

    async def _run_worker(self, printer_id: str) -> None:
        queue = self._queues[printer_id]

        while True:
            item = await queue.get()
            self._active_jobs[printer_id] = item.job_id

            try:
                await self._process_job(printer_id, item)
            finally:
                self._active_jobs.pop(printer_id, None)
                queue.task_done()

    async def _process_job(self, printer_id: str, queued_job: _QueuedPrinterJob) -> None:
        self._update_job(printer_id, queued_job.job_id, status="printing", message="Enviando para impressora...")

        delays = tuple(self._config.retry_delays_ms)
        max_attempts = len(delays) + 1
        last_error: Exception | None = None

        for attempt_number in range(1, max_attempts + 1):
            self._update_job(
                printer_id,
                queued_job.job_id,
                status="printing",
                message=f"Tentativa {attempt_number}/{max_attempts}.",
                attempts=attempt_number,
            )

            try:
                await self._send_once(printer_id, queued_job.payload)
                self._update_job(
                    printer_id,
                    queued_job.job_id,
                    status="printed",
                    message="Impresso com sucesso.",
                    attempts=attempt_number,
                )
                return
            except Exception as exc:  # pragma: no cover - validado por testes de fila/retry
                last_error = exc
                if attempt_number >= max_attempts:
                    break

                await asyncio.sleep(delays[attempt_number - 1] / 1000)

        self._update_job(
            printer_id,
            queued_job.job_id,
            status="failed",
            message=str(last_error) if last_error else "Falha ao imprimir.",
        )

    async def _send_once(self, printer_id: str, payload: bytes) -> None:
        descriptor = self._registry.get_descriptor("printer", printer_id)
        path = await self._registry.resolve_path("printer", printer_id)

        lock = self._locks[printer_id]
        async with lock:
            await asyncio.to_thread(
                send_printer_payload,
                descriptor,
                path=path,
                payload=payload,
                timeout_ms=self._config.write_timeout_ms,
                write_timeout_ms=self._config.write_timeout_ms,
                chunk_size=self._config.serial_chunk_size,
                chunk_delay_ms=self._config.serial_chunk_delay_ms,
                print_settle_ms=self._config.print_settle_ms,
            )

    def _update_job(
        self,
        printer_id: str,
        job_id: str,
        *,
        status: str,
        message: str,
        attempts: int | None = None,
    ) -> None:
        key = (printer_id, job_id)
        job = self._jobs.get(key)
        if not job:
            return

        job.status = status
        job.message = message
        if attempts is not None:
            job.attempts = attempts
        job.updated_at_epoch_ms = _now_epoch_ms()
        self._prune_history(printer_id)

    def _prune_history(self, printer_id: str) -> None:
        jobs = [job for (owner, _), job in self._jobs.items() if owner == printer_id]
        excess = len(jobs) - 500
        if excess <= 0:
            return
        removable = sorted(
            (job for job in jobs if job.status in {"printed", "failed"}),
            key=lambda job: job.created_at_epoch_ms,
        )
        for job in removable[:excess]:
            key = (printer_id, job.job_id)
            self._jobs.pop(key, None)
            self._payloads.pop(key, None)


def _now_epoch_ms() -> int:
    return int(time.time() * 1000)


def _epoch_ms_to_iso(value: int) -> str:
    dt = datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")
