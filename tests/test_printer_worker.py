import asyncio
from types import MethodType

import pytest

from pdv_device_bridge.config import PrinterRuntimeConfig, SerialDeviceConfig
from pdv_device_bridge.printer_worker import PrinterWorker, QueueLimitReachedError


class DummyRegistry:
    def get_descriptor(self, *_args, **_kwargs):  # pragma: no cover
        raise AssertionError("Nao deveria chamar get_descriptor quando _send_once e mockado")

    async def resolve_path(self, *_args, **_kwargs):  # pragma: no cover
        raise AssertionError("Nao deveria chamar resolve_path quando _send_once e mockado")


class SendRegistry:
    def __init__(self, descriptor: SerialDeviceConfig, path: str) -> None:
        self.descriptor = descriptor
        self.path = path

    def get_descriptor(self, kind: str, device_id: str) -> SerialDeviceConfig:
        assert kind == "printer"
        assert device_id == self.descriptor.device_id

        return self.descriptor

    async def resolve_path(self, kind: str, device_id: str) -> str:
        assert kind == "printer"
        assert device_id == self.descriptor.device_id

        return self.path


async def _wait_for_terminal_state(worker: PrinterWorker, printer_id: str, job_id: str) -> dict[str, object]:
    for _ in range(200):
        job = worker.get_job(printer_id, job_id)
        if job["status"] in {"printed", "failed"}:
            return job
        await asyncio.sleep(0.01)

    raise AssertionError("Timeout aguardando job finalizar")


@pytest.mark.asyncio
async def test_printer_worker_retries_and_marks_printed() -> None:
    worker = PrinterWorker(
        registry=DummyRegistry(),
        config=PrinterRuntimeConfig(queue_size=10, retry_delays_ms=(1, 1, 1)),
        printer_ids=["printer-1"],
    )

    calls = {"count": 0}

    async def fake_send_once(self, printer_id: str, payload: bytes) -> None:
        assert printer_id == "printer-1"
        assert payload == b"hello"
        calls["count"] += 1
        if calls["count"] < 3:
            raise RuntimeError("serial offline")

    worker._send_once = MethodType(fake_send_once, worker)

    await worker.start()
    try:
        created = worker.submit_job("printer-1", b"hello", request_id="req-1")
        done = await _wait_for_terminal_state(worker, "printer-1", str(created["job_id"]))

        assert done["status"] == "printed"
        assert done["attempts"] == 3
        assert calls["count"] == 3
    finally:
        await worker.stop()


@pytest.mark.asyncio
async def test_printer_worker_marks_failed_after_retries() -> None:
    worker = PrinterWorker(
        registry=DummyRegistry(),
        config=PrinterRuntimeConfig(queue_size=10, retry_delays_ms=(1, 1, 1)),
        printer_ids=["printer-1"],
    )

    calls = {"count": 0}

    async def fake_send_once(self, _printer_id: str, _payload: bytes) -> None:
        calls["count"] += 1
        raise RuntimeError("device busy")

    worker._send_once = MethodType(fake_send_once, worker)

    await worker.start()
    try:
        created = worker.submit_job("printer-1", b"hello")
        done = await _wait_for_terminal_state(worker, "printer-1", str(created["job_id"]))

        assert done["status"] == "failed"
        assert done["attempts"] == 4
        assert calls["count"] == 4
    finally:
        await worker.stop()


@pytest.mark.asyncio
async def test_printer_worker_respects_queue_limit() -> None:
    worker = PrinterWorker(
        registry=DummyRegistry(),
        config=PrinterRuntimeConfig(queue_size=2, retry_delays_ms=(1000, 1000, 1000)),
        printer_ids=["printer-1"],
    )

    send_gate = asyncio.Event()

    async def fake_send_once(self, _printer_id: str, _payload: bytes) -> None:
        await send_gate.wait()

    worker._send_once = MethodType(fake_send_once, worker)

    await worker.start()
    try:
        worker.submit_job("printer-1", b"job-1")
        worker.submit_job("printer-1", b"job-2")

        with pytest.raises(QueueLimitReachedError):
            worker.submit_job("printer-1", b"job-3")
    finally:
        send_gate.set()
        await worker.stop()


@pytest.mark.asyncio
async def test_printer_worker_passes_serial_pacing_config(monkeypatch) -> None:
    descriptor = SerialDeviceConfig(device_id="printer-1", path="/dev/ttyUSB0", baudrate=115200)
    worker = PrinterWorker(
        registry=SendRegistry(descriptor, "/dev/ttyUSB0"),
        config=PrinterRuntimeConfig(
            queue_size=10,
            retry_delays_ms=(1,),
            serial_chunk_size=256,
            serial_chunk_delay_ms=20,
            print_settle_ms=1200,
            write_timeout_ms=4500,
        ),
        printer_ids=["printer-1"],
    )
    captured = {}

    def fake_send_printer_payload(
        passed_descriptor,
        *,
        path,
        payload,
        timeout_ms,
        write_timeout_ms,
        chunk_size,
        chunk_delay_ms,
        print_settle_ms,
    ) -> None:
        captured.update({
            "descriptor": passed_descriptor,
            "path": path,
            "payload": payload,
            "timeout_ms": timeout_ms,
            "write_timeout_ms": write_timeout_ms,
            "chunk_size": chunk_size,
            "chunk_delay_ms": chunk_delay_ms,
            "print_settle_ms": print_settle_ms,
        })

    monkeypatch.setattr("pdv_device_bridge.printer_worker.send_printer_payload", fake_send_printer_payload)

    await worker._send_once("printer-1", b"escpos")

    assert captured == {
        "descriptor": descriptor,
        "path": "/dev/ttyUSB0",
        "payload": b"escpos",
        "timeout_ms": 4500,
        "write_timeout_ms": 4500,
        "chunk_size": 256,
        "chunk_delay_ms": 20,
        "print_settle_ms": 1200,
    }
