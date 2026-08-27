import asyncio
from decimal import Decimal
from pathlib import Path

import pytest

from pdv_device_bridge.config import SerialDeviceConfig
from pdv_device_bridge.scale_parser import parse_weight_payload
from pdv_device_bridge.serial_io import read_scale_once
from scripts.scale_simulator import (
    ScaleReading,
    ScaleSimulator,
    build_scale_response,
    parse_command_hex,
    parse_kilograms,
)


def test_build_scale_response_supports_stable_and_unstable_readings() -> None:
    stable = build_scale_response(ScaleReading(Decimal("0.350"), stable=True))
    unstable = build_scale_response(ScaleReading(Decimal("1.250"), stable=False))

    assert stable == b"ST,GS,+0.350kg\r\n"
    assert unstable == b"US,GS,+1.250kg\r\n"
    assert parse_weight_payload(stable).grams == 350
    assert parse_weight_payload(unstable).stable is False


def test_parse_simulator_arguments() -> None:
    assert parse_kilograms("1,250") == Decimal("1.250")
    assert parse_command_hex("04 05") == b"\x04\x05"


@pytest.mark.asyncio
async def test_simulator_answers_bridge_serial_reads(tmp_path: Path) -> None:
    link_path = tmp_path / "scale-sim"
    descriptor = SerialDeviceConfig(
        device_id="scale-sim",
        path=str(link_path),
        baudrate=9600,
        bytesize=8,
        parity="N",
        stopbits=1.0,
    )

    with ScaleSimulator(link_path=link_path) as simulator:
        simulator.set_reading(ScaleReading(Decimal("0.485"), stable=True))

        payload = await asyncio.to_thread(
            read_scale_once,
            descriptor,
            path=str(link_path),
            command_bytes=b"\x04\x05",
            timeout_ms=800,
            response_quiet_ms=30,
            max_read_bytes=200,
        )

        parsed = parse_weight_payload(payload)
        assert parsed is not None
        assert parsed.grams == 485
        assert parsed.stable is True

    assert not link_path.exists()
