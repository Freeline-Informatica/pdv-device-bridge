from __future__ import annotations

from contextlib import contextmanager
import time

import serial

from .config import SerialDeviceConfig

_PARITY_MAP = {
    "N": serial.PARITY_NONE,
    "E": serial.PARITY_EVEN,
    "O": serial.PARITY_ODD,
    "M": serial.PARITY_MARK,
    "S": serial.PARITY_SPACE,
}

_BYTESIZE_MAP = {
    5: serial.FIVEBITS,
    6: serial.SIXBITS,
    7: serial.SEVENBITS,
    8: serial.EIGHTBITS,
}

_STOPBITS_MAP = {
    1.0: serial.STOPBITS_ONE,
    1.5: serial.STOPBITS_ONE_POINT_FIVE,
    2.0: serial.STOPBITS_TWO,
}


@contextmanager
def open_serial_port(
    descriptor: SerialDeviceConfig,
    path: str,
    *,
    timeout_ms: int,
    write_timeout_ms: int | None = None,
):
    serial_port = serial.Serial(
        port=path,
        baudrate=descriptor.baudrate,
        bytesize=_BYTESIZE_MAP[descriptor.bytesize],
        parity=_PARITY_MAP[descriptor.parity],
        stopbits=_STOPBITS_MAP[descriptor.stopbits],
        timeout=max(0.01, timeout_ms / 1000),
        write_timeout=(None if write_timeout_ms is None else max(0.01, write_timeout_ms / 1000)),
    )

    try:
        yield serial_port
    finally:
        if serial_port.is_open:
            serial_port.close()


def read_scale_once(
    descriptor: SerialDeviceConfig,
    *,
    path: str,
    command_bytes: bytes,
    timeout_ms: int,
    max_read_bytes: int,
) -> bytes:
    with open_serial_port(descriptor, path, timeout_ms=timeout_ms) as serial_port:
        serial_port.reset_input_buffer()
        serial_port.write(command_bytes)
        serial_port.flush()
        return bytes(serial_port.read(max_read_bytes))


def send_printer_payload(
    descriptor: SerialDeviceConfig,
    *,
    path: str,
    payload: bytes,
    timeout_ms: int,
    write_timeout_ms: int | None = None,
    chunk_size: int = 512,
    chunk_delay_ms: int = 15,
    print_settle_ms: int = 1000,
) -> None:
    effective_chunk_size = max(1, int(chunk_size))
    effective_chunk_delay = max(0, int(chunk_delay_ms)) / 1000
    effective_settle = max(0, int(print_settle_ms)) / 1000

    with open_serial_port(
        descriptor,
        path,
        timeout_ms=timeout_ms,
        write_timeout_ms=write_timeout_ms if write_timeout_ms is not None else timeout_ms,
    ) as serial_port:
        for offset in range(0, len(payload), effective_chunk_size):
            chunk = payload[offset:offset + effective_chunk_size]
            written = serial_port.write(chunk)

            if written != len(chunk):
                raise serial.SerialTimeoutException(
                    f"Escrita serial incompleta: {written}/{len(chunk)} bytes enviados.",
                )

            serial_port.flush()

            if effective_chunk_delay > 0 and offset + effective_chunk_size < len(payload):
                time.sleep(effective_chunk_delay)

        serial_port.flush()

        if effective_settle > 0:
            time.sleep(effective_settle)
