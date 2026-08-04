import serial
import pytest

from pdv_device_bridge.config import SerialDeviceConfig
from pdv_device_bridge.serial_io import send_printer_payload


class FakeSerial:
    def __init__(self, write_lengths=None) -> None:
        self.write_lengths = list(write_lengths or [])
        self.writes = []
        self.flush_count = 0
        self.closed = False
        self.is_open = True
        self.kwargs = {}

    def write(self, chunk: bytes) -> int:
        self.writes.append(bytes(chunk))
        if self.write_lengths:
            return self.write_lengths.pop(0)

        return len(chunk)

    def flush(self) -> None:
        self.flush_count += 1

    def close(self) -> None:
        self.closed = True
        self.is_open = False


def test_send_printer_payload_writes_chunks_and_waits(monkeypatch) -> None:
    fake = FakeSerial()
    sleeps = []

    def serial_factory(**kwargs):
        fake.kwargs = kwargs

        return fake

    monkeypatch.setattr("pdv_device_bridge.serial_io.serial.Serial", serial_factory)
    monkeypatch.setattr("pdv_device_bridge.serial_io.time.sleep", lambda seconds: sleeps.append(seconds))

    send_printer_payload(
        SerialDeviceConfig(device_id="printer-1", baudrate=115200),
        path="/dev/ttyUSB0",
        payload=b"abcdef",
        timeout_ms=3000,
        write_timeout_ms=4000,
        chunk_size=2,
        chunk_delay_ms=15,
        print_settle_ms=1000,
    )

    assert fake.kwargs["port"] == "/dev/ttyUSB0"
    assert fake.kwargs["baudrate"] == 115200
    assert fake.kwargs["write_timeout"] == 4.0
    assert fake.writes == [b"ab", b"cd", b"ef"]
    assert fake.flush_count == 4
    assert sleeps == [0.015, 0.015, 1.0]
    assert fake.closed


def test_send_printer_payload_fails_on_incomplete_serial_write(monkeypatch) -> None:
    fake = FakeSerial(write_lengths=[1])

    monkeypatch.setattr("pdv_device_bridge.serial_io.serial.Serial", lambda **kwargs: fake)
    monkeypatch.setattr("pdv_device_bridge.serial_io.time.sleep", lambda _seconds: None)

    with pytest.raises(serial.SerialTimeoutException):
        send_printer_payload(
            SerialDeviceConfig(device_id="printer-1"),
            path="/dev/ttyUSB0",
            payload=b"abc",
            timeout_ms=3000,
            chunk_size=3,
        )

    assert fake.closed
