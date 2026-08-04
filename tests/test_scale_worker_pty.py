import os
import threading

import pytest

from pdv_device_bridge.config import ScaleRuntimeConfig, SerialDeviceConfig
from pdv_device_bridge.scale_worker import ScaleReadError, ScaleWorker


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
        first = await worker.read("scale-1", max_age_ms=0)
        second = await worker.read("scale-1", max_age_ms=1500)

        assert first["source"] == "device"
        assert first["grams"] == 245
        assert first["stable"] is True

        assert second["source"] == "cache"
        assert second["grams"] == 245
        assert received_commands == [b"\x04\x05"]
    finally:
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
        emulator_thread.join(timeout=1)
        os.close(master_fd)
        os.close(slave_fd)
