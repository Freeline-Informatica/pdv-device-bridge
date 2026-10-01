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
    serial_port = _create_serial_port(descriptor, path, timeout_ms=timeout_ms, write_timeout_ms=write_timeout_ms)

    try:
        yield serial_port
    finally:
        if serial_port.is_open:
            serial_port.close()


def _create_serial_port(
    descriptor: SerialDeviceConfig,
    path: str,
    *,
    timeout_ms: int,
    write_timeout_ms: int | None = None,
):
    return serial.Serial(
        port=path,
        baudrate=descriptor.baudrate,
        bytesize=_BYTESIZE_MAP[descriptor.bytesize],
        parity=_PARITY_MAP[descriptor.parity],
        stopbits=_STOPBITS_MAP[descriptor.stopbits],
        timeout=max(0.01, timeout_ms / 1000),
        write_timeout=(None if write_timeout_ms is None else max(0.01, write_timeout_ms / 1000)),
    )


class ScaleSerialSession:
    """Mantem a porta da balanca aberta durante as leituras sucessivas."""

    def __init__(self, descriptor: SerialDeviceConfig, path: str) -> None:
        self.descriptor = descriptor
        self.path = path
        self._port = None
        self._aborted = False

    def read(self, *, command_bytes: bytes, timeout_ms: int, response_quiet_ms: int, max_read_bytes: int) -> bytes:
        if self._port is None or not self._port.is_open:
            self._port = _create_serial_port(
                self.descriptor, self.path, timeout_ms=timeout_ms, write_timeout_ms=timeout_ms,
            )

        try:
            if self._aborted:
                raise serial.SerialException("Leitura serial cancelada apos timeout.")
            self._port.timeout = max(0.01, timeout_ms / 1000)
            payload = _read_scale_payload(
                self._port,
                command_bytes=command_bytes,
                timeout_ms=timeout_ms,
                response_quiet_ms=response_quiet_ms,
                max_read_bytes=max_read_bytes,
            )
            if self._aborted:
                raise serial.SerialException("Leitura serial cancelada apos timeout.")
            return payload
        except (OSError, serial.SerialException):
            self.close()
            raise

    def abort(self) -> None:
        """Interrompe uma leitura/escrita pendente sem fechar a porta em outra thread."""
        self._aborted = True
        port = self._port
        if port is None or not port.is_open:
            return
        for method_name in ("cancel_read", "cancel_write"):
            method = getattr(port, method_name, None)
            if callable(method):
                try:
                    method()
                except (OSError, serial.SerialException):
                    pass

    def close(self) -> None:
        port = self._port
        self._port = None
        if port is not None and port.is_open:
            port.close()


def read_scale_once(
    descriptor: SerialDeviceConfig,
    *,
    path: str,
    command_bytes: bytes,
    timeout_ms: int,
    response_quiet_ms: int,
    max_read_bytes: int,
) -> bytes:
    with open_serial_port(descriptor, path, timeout_ms=timeout_ms, write_timeout_ms=timeout_ms) as serial_port:
        return _read_scale_payload(
            serial_port,
            command_bytes=command_bytes,
            timeout_ms=timeout_ms,
            response_quiet_ms=response_quiet_ms,
            max_read_bytes=max_read_bytes,
        )


def _read_scale_payload(
    serial_port,
    *,
    command_bytes: bytes,
    timeout_ms: int,
    response_quiet_ms: int,
    max_read_bytes: int,
) -> bytes:
    serial_port.reset_input_buffer()
    written = serial_port.write(command_bytes)
    if written != len(command_bytes):
        raise serial.SerialTimeoutException("Comando da balanca enviado parcialmente.")

    max_bytes = max(1, int(max_read_bytes))
    deadline = time.monotonic() + (max(10, int(timeout_ms)) / 1000)
    payload = bytearray(serial_port.read(1))
    if not payload:
        return b""

    quiet_timeout = max(1, int(response_quiet_ms)) / 1000
    while len(payload) < max_bytes:
        if payload[-1] in b"\r\n":
            break

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break

        serial_port.timeout = min(quiet_timeout, remaining)
        next_byte = serial_port.read(1)
        if not next_byte:
            break

        payload.extend(next_byte)

    return bytes(payload)


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
