import asyncio
import os
import threading
import time

import pytest
import serial

from pdv_device_bridge.config import ScaleRuntimeConfig, SerialDeviceConfig
from pdv_device_bridge.scale_worker import ScaleReadError, ScaleWorker
from pdv_device_bridge.serial_io import ScaleSerialSession


class FakeRegistry:
    def __init__(self, descriptor: SerialDeviceConfig, path: str) -> None:
        self._descriptor = descriptor
        self._path = path

    def get_descriptor(self, kind: str, device_id: str) -> SerialDeviceConfig:
        assert kind == "scale"
        assert device_id == self._descriptor.device_id
        return self._descriptor

    async def resolve_path(self, kind: str, device_id: str) -> str:
        assert kind == "scale"
        assert device_id == self._descriptor.device_id
        return self._path


@pytest.mark.asyncio
async def test_scale_worker_reads_from_virtual_tty_and_uses_cache() -> None:
    master_fd, slave_fd = os.openpty()
    slave_path = os.ttyname(slave_fd)

    descriptor = SerialDeviceConfig(
        device_id="scale-1",
        path=slave_path,
        baudrate=9600,
        bytesize=8,
        parity="N",
        stopbits=1.0,
    )
    registry = FakeRegistry(descriptor, slave_path)
    worker = ScaleWorker(
        registry,
        ScaleRuntimeConfig(
            read_timeout_ms=800,
            cache_max_age_ms=1500,
            max_read_bytes=200,
            command_bytes=b"\x04\x05",
        ),
    )

    received_commands: list[bytes] = []

    def device_emulator() -> None:
        try:
            command = os.read(master_fd, 2)
            received_commands.append(command)
            if command == b"\x04\x05":
                os.write(master_fd, b"ST,GS,+0,245kg\r\n")
        except OSError:
            return

    emulator_thread = threading.Thread(target=device_emulator, daemon=True)
    emulator_thread.start()

    try:
        started_at = time.monotonic()
        first = await worker.read("scale-1", max_age_ms=0)
        elapsed = time.monotonic() - started_at
        second = await worker.read("scale-1", max_age_ms=1500)

        assert first["source"] == "device"
        assert first["grams"] == 245
        assert first["stable"] is True
        assert elapsed < 0.5

        assert second["source"] == "cache"
        assert second["grams"] == 245
        assert received_commands == [b"\x04\x05"]
    finally:
        await worker.stop()
        emulator_thread.join(timeout=1)
        os.close(master_fd)
        os.close(slave_fd)


@pytest.mark.asyncio
async def test_virtual_tty_recovers_from_missing_response_to_weight() -> None:
    master_fd, slave_fd = os.openpty()
    slave_path = os.ttyname(slave_fd)
    descriptor = SerialDeviceConfig(device_id="scale-silent", path=slave_path)
    worker = ScaleWorker(
        FakeRegistry(descriptor, slave_path),
        ScaleRuntimeConfig(read_timeout_ms=100),
    )

    def device_emulator() -> None:
        try:
            os.read(master_fd, 2)
            os.read(master_fd, 2)
            os.write(master_fd, b"ST,+0.485kg\r")
        except OSError:
            pass

    emulator_thread = threading.Thread(target=device_emulator, daemon=True)
    emulator_thread.start()
    try:
        with pytest.raises(ScaleReadError, match="sem resposta serial"):
            await worker.read("scale-silent", max_age_ms=0)
        weight = await worker.read("scale-silent", max_age_ms=0)
        assert weight["state"] == "weight"
        assert weight["grams"] == 485
    finally:
        await worker.stop()
        emulator_thread.join(timeout=1)
        os.close(master_fd)
        os.close(slave_fd)


@pytest.mark.asyncio
async def test_scale_worker_fails_for_invalid_payload() -> None:
    master_fd, slave_fd = os.openpty()
    slave_path = os.ttyname(slave_fd)

    descriptor = SerialDeviceConfig(
        device_id="scale-2",
        path=slave_path,
        baudrate=9600,
        bytesize=8,
        parity="N",
        stopbits=1.0,
    )
    registry = FakeRegistry(descriptor, slave_path)
    worker = ScaleWorker(registry, ScaleRuntimeConfig())

    def device_emulator() -> None:
        try:
            _ = os.read(master_fd, 2)
            os.write(master_fd, b"@@@@")
        except OSError:
            return

    emulator_thread = threading.Thread(target=device_emulator, daemon=True)
    emulator_thread.start()

    try:
        with pytest.raises(ScaleReadError):
            await worker.read("scale-2", max_age_ms=0)
    finally:
        await worker.stop()
        emulator_thread.join(timeout=1)
        os.close(master_fd)
        os.close(slave_fd)


@pytest.mark.asyncio
async def test_scale_worker_zero_max_age_always_bypasses_cache(monkeypatch) -> None:
    descriptor = SerialDeviceConfig(device_id="scale-fresh", path="/dev/ttyUSB0")
    registry = FakeRegistry(descriptor, "/dev/ttyUSB0")
    worker = ScaleWorker(registry, ScaleRuntimeConfig())
    responses = iter([b"ST,+0.245kg\r", b"ST,+0.485kg\r"])

    monkeypatch.setattr(
        "pdv_device_bridge.scale_worker.ScaleSerialSession.read",
        lambda *_args, **_kwargs: next(responses),
    )

    first = await worker.read("scale-fresh", max_age_ms=0)
    second = await worker.read("scale-fresh", max_age_ms=0)

    assert first["source"] == "device"
    assert first["grams"] == 245
    assert second["source"] == "device"
    assert second["grams"] == 485


@pytest.mark.asyncio
async def test_scale_worker_reports_serial_open_error_and_recovers(monkeypatch) -> None:
    descriptor = SerialDeviceConfig(device_id="scale-eio", path="/dev/ttyUSB0")
    worker = ScaleWorker(FakeRegistry(descriptor, "/dev/ttyUSB0"), ScaleRuntimeConfig())
    responses = iter([serial.SerialException(5, "could not open port /dev/ttyUSB0: Input/output error"), b"ST,+0.245kg\r"])

    def read_once(*_args, **_kwargs) -> bytes:
        result = next(responses)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr("pdv_device_bridge.scale_worker.ScaleSerialSession.read", read_once)
    monkeypatch.setattr("pdv_device_bridge.scale_worker.ScaleSerialSession.close", lambda *_args: None)

    with pytest.raises(ScaleReadError, match="could not open port"):
        await worker.read("scale-eio", max_age_ms=0)
    assert "could not open port" in worker.health_snapshot()["scale-eio"]["last_error"]

    result = await worker.read("scale-eio", max_age_ms=0)
    assert result["grams"] == 245
    assert worker.health_snapshot()["scale-eio"]["last_error"] is None


@pytest.mark.asyncio
async def test_silence_does_not_clear_a_serial_failure(monkeypatch) -> None:
    descriptor = SerialDeviceConfig(device_id="scale-fault", path="/dev/ttyUSB0")
    worker = ScaleWorker(FakeRegistry(descriptor, "/dev/ttyUSB0"), ScaleRuntimeConfig())
    responses = iter([serial.SerialException("EPIPE"), b"", b"ST,+0.000kg\r"])

    def read_once(*_args, **_kwargs) -> bytes:
        result = next(responses)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr("pdv_device_bridge.scale_worker.ScaleSerialSession.read", read_once)
    monkeypatch.setattr("pdv_device_bridge.scale_worker.ScaleSerialSession.close", lambda *_args: None)

    with pytest.raises(ScaleReadError, match="EPIPE"):
        await worker.read("scale-fault", max_age_ms=0)
    with pytest.raises(ScaleReadError, match="sem resposta serial"):
        await worker.read("scale-fault", max_age_ms=0)
    assert "sem resposta serial" in worker.health_snapshot()["scale-fault"]["last_error"]
    await worker.read("scale-fault", max_age_ms=0)
    assert worker.health_snapshot()["scale-fault"]["last_error"] is None


@pytest.mark.asyncio
async def test_scale_worker_rejects_no_bytes_and_reopens_port(monkeypatch) -> None:
    descriptor = SerialDeviceConfig(device_id="scale-empty", path="/dev/ttyUSB0")
    worker = ScaleWorker(FakeRegistry(descriptor, "/dev/ttyUSB0"), ScaleRuntimeConfig())
    sessions = []

    class FakeSession:
        def __init__(self, descriptor, path):
            self.descriptor = descriptor
            self.path = path
            self.closed = False
            sessions.append(self)

        def read(self, **_kwargs):
            return b"" if len(sessions) == 1 else b"ST,+0.458kg\r"

        def close(self):
            self.closed = True

    monkeypatch.setattr("pdv_device_bridge.scale_worker.ScaleSerialSession", FakeSession)

    with pytest.raises(ScaleReadError, match="sem resposta serial"):
        await worker.read("scale-empty", max_age_ms=0)
    assert "sem resposta serial" in worker.health_snapshot()["scale-empty"]["last_error"]
    result = await worker.read("scale-empty", max_age_ms=0)

    assert result["grams"] == 458
    assert len(sessions) == 2
    assert sessions[0].closed
    assert not sessions[1].closed
    await worker.stop()
    assert sessions[1].closed


@pytest.mark.asyncio
async def test_serial_silence_blocks_cached_weight_until_valid_zero(monkeypatch) -> None:
    descriptor = SerialDeviceConfig(device_id="scale-silence", path="/dev/ttyUSB0")
    worker = ScaleWorker(FakeRegistry(descriptor, "/dev/ttyUSB0"), ScaleRuntimeConfig())
    responses = iter([b"ST,+0.438kg\r", b"", b"ST,+0.000kg\r"])
    monkeypatch.setattr(
        "pdv_device_bridge.scale_worker.ScaleSerialSession.read",
        lambda *_args, **_kwargs: next(responses),
    )
    monkeypatch.setattr("pdv_device_bridge.scale_worker.ScaleSerialSession.close", lambda *_args: None)

    weight = await worker.read("scale-silence", max_age_ms=0)
    assert weight["grams"] == 438
    with pytest.raises(ScaleReadError, match="sem resposta serial"):
        await worker.read("scale-silence", max_age_ms=0)

    zero = await worker.read("scale-silence", max_age_ms=1500)
    assert zero["state"] == "empty"
    assert zero["grams"] == 0
    assert zero["source"] == "device"
    assert worker.health_snapshot()["scale-silence"]["last_error"] is None


@pytest.mark.asyncio
async def test_scale_worker_accepts_explicit_zero_and_rejects_invalid_bytes(monkeypatch) -> None:
    descriptor = SerialDeviceConfig(device_id="scale-zero", path="/dev/ttyUSB0")
    worker = ScaleWorker(FakeRegistry(descriptor, "/dev/ttyUSB0"), ScaleRuntimeConfig())
    responses = iter([b"ST,+0.000kg\r", b"@@@@"])
    monkeypatch.setattr(
        "pdv_device_bridge.scale_worker.ScaleSerialSession.read",
        lambda *_args, **_kwargs: next(responses),
    )
    monkeypatch.setattr("pdv_device_bridge.scale_worker.ScaleSerialSession.close", lambda *_args: None)

    zero = await worker.read("scale-zero", max_age_ms=0)
    assert zero["state"] == "empty"
    assert zero["grams"] == 0
    with pytest.raises(ScaleReadError, match="Resposta invalida"):
        await worker.read("scale-zero", max_age_ms=0)
    assert worker.health_snapshot()["scale-zero"]["last_error"]


@pytest.mark.asyncio
async def test_scale_stream_shares_one_read_between_two_subscribers(monkeypatch) -> None:
    descriptor = SerialDeviceConfig(device_id="scale-stream", path="/dev/ttyUSB0")
    worker = ScaleWorker(FakeRegistry(descriptor, "/dev/ttyUSB0"), ScaleRuntimeConfig())
    reads = 0

    async def fake_read(*_args, **_kwargs):
        nonlocal reads
        reads += 1
        return {"state": "empty", "grams": 0, "kilograms": 0.0, "read_at": "now"}

    monkeypatch.setattr(worker, "read", fake_read)
    first = worker.stream("scale-stream")
    second = worker.stream("scale-stream")
    try:
        events = await asyncio.gather(anext(first), anext(second))
        assert reads == 1
        assert [event["state"] for event in events] == ["empty", "empty"]
    finally:
        await first.aclose()
        await second.aclose()
        await asyncio.sleep(0.12)
        assert "scale-stream" not in worker._stream_tasks
        await worker.stop()

    assert not worker._subscribers


@pytest.mark.asyncio
async def test_scale_worker_times_out_stuck_serial_read_without_starting_another(monkeypatch) -> None:
    descriptor = SerialDeviceConfig(device_id="scale-stuck", path="/dev/ttyUSB0")
    worker = ScaleWorker(
        FakeRegistry(descriptor, "/dev/ttyUSB0"),
        ScaleRuntimeConfig(operation_timeout_ms=100),
    )
    release = threading.Event()
    calls = 0

    def blocked_read(*_args, **_kwargs) -> bytes:
        nonlocal calls
        calls += 1
        if calls == 1:
            release.wait(timeout=2)
        return b"ST,+0.245kg\r"

    monkeypatch.setattr("pdv_device_bridge.scale_worker.ScaleSerialSession.read", blocked_read)
    monkeypatch.setattr("pdv_device_bridge.scale_worker.ScaleSerialSession.close", lambda *_args: None)

    try:
        started_at = time.monotonic()
        with pytest.raises(ScaleReadError, match="Tempo limite"):
            await worker.read("scale-stuck", max_age_ms=0)
        assert time.monotonic() - started_at < 0.5

        with pytest.raises(ScaleReadError, match="anterior ainda esta travada"):
            await worker.read("scale-stuck", max_age_ms=0)
        assert calls == 1
        assert worker.health_snapshot()["scale-stuck"]["serial_read_in_progress"] is True
        assert "scale-stuck" not in worker._sessions
    finally:
        release.set()

    await asyncio.sleep(0.05)
    result = await worker.read("scale-stuck", max_age_ms=0)
    assert result["grams"] == 245
    assert calls == 2


@pytest.mark.asyncio
async def test_stop_aborts_an_active_serial_read() -> None:
    descriptor = SerialDeviceConfig(device_id="scale-stop", path="/dev/ttyUSB0")
    worker = ScaleWorker(FakeRegistry(descriptor, "/dev/ttyUSB0"), ScaleRuntimeConfig())
    session = ScaleSerialSession(descriptor, "/dev/ttyUSB0")
    active_read = asyncio.create_task(asyncio.sleep(10))
    worker._sessions["scale-stop"] = session
    worker._serial_reads["scale-stop"] = active_read

    await worker.stop()

    assert session._aborted
    assert not worker._sessions
    active_read.cancel()
    await asyncio.gather(active_read, return_exceptions=True)
