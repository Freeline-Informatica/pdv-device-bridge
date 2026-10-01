from io import BytesIO

import pytest

from pdv_device_bridge.config import BridgeConfig, ScaleRuntimeConfig, SecurityConfig, SerialDeviceConfig
from scripts import read_scale


def test_api_read_requests_fresh_weight_with_local_auth(monkeypatch) -> None:
    config = BridgeConfig(
        security=SecurityConfig(require_auth=True, pairing_token="secret"),
        scales=(SerialDeviceConfig(device_id="scale-horti-1"),),
    )

    def fake_urlopen(request, *, timeout):
        assert request.full_url.endswith("/v1/scales/scale-horti-1/read?max_age_ms=0")
        assert request.get_header("Authorization") == "Bearer secret"
        assert timeout >= 4
        return BytesIO(b'{"grams":438,"state":"weight","raw":"PESO L: 0.438kg"}')

    monkeypatch.setattr(read_scale, "urlopen", fake_urlopen)

    assert read_scale.read_api(config, "scale-horti-1")["grams"] == 438


def test_direct_read_does_not_call_silence_empty(monkeypatch) -> None:
    config = BridgeConfig(
        scale=ScaleRuntimeConfig(),
        scales=(SerialDeviceConfig(device_id="scale-horti-1", path="/dev/ttyUSB0"),),
    )

    async def fake_resolve_path(self, kind, device_id):
        assert (kind, device_id) == ("scale", "scale-horti-1")
        return "/dev/ttyUSB0"

    monkeypatch.setattr(read_scale.DeviceRegistry, "resolve_path", fake_resolve_path)
    monkeypatch.setattr(read_scale, "read_scale_once", lambda *_args, **_kwargs: b"")

    with pytest.raises(RuntimeError, match="0 bytes"):
        read_scale.read_direct(config, "scale-horti-1")


def test_direct_urano_silence_has_no_numeric_weight(monkeypatch) -> None:
    config = BridgeConfig(scales=(SerialDeviceConfig(
        device_id="scale-horti-1", path="/dev/ttyUSB0", no_response_state="no_reading",
    ),))

    async def fake_resolve_path(self, kind, device_id):
        return "/dev/ttyUSB0"

    monkeypatch.setattr(read_scale.DeviceRegistry, "resolve_path", fake_resolve_path)
    monkeypatch.setattr(read_scale, "read_scale_once", lambda *_args, **_kwargs: b"")

    reading = read_scale.read_direct(config, "scale-horti-1")
    assert reading["state"] == "no_reading"
    assert reading["grams"] is None


def test_direct_mode_refuses_active_bridge(monkeypatch, tmp_path, capsys) -> None:
    config = BridgeConfig(scales=(SerialDeviceConfig(device_id="scale-horti-1"),))
    monkeypatch.setattr(read_scale, "load_config", lambda _path: config)
    monkeypatch.setattr(read_scale, "bridge_is_active", lambda: True)
    monkeypatch.setattr(read_scale, "read_direct", lambda *_args: pytest.fail("serial abriu com bridge ativo"))

    with pytest.raises(SystemExit, match="2"):
        read_scale.main(["--config", str(tmp_path / "config.toml"), "--direct"])

    assert "bridge esta ativo" in capsys.readouterr().err


def test_single_api_read_prints_valid_zero(monkeypatch, tmp_path, capsys) -> None:
    config = BridgeConfig(scales=(SerialDeviceConfig(device_id="scale-horti-1"),))
    monkeypatch.setattr(read_scale, "load_config", lambda _path: config)
    monkeypatch.setattr(read_scale, "read_api", lambda *_args: {"grams": 0, "state": "empty", "stable": True, "raw": "PESO L: 0.000kg"})

    assert read_scale.main(["--config", str(tmp_path / "config.toml")]) == 0

    output = capsys.readouterr().out
    assert "LEITURA 0 g state=empty" in output
    assert "raw='PESO L: 0.000kg'" in output
