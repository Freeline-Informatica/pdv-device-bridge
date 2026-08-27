from pdv_device_bridge.configuration_store import DeviceAssignment, apply_assignments
from pdv_device_bridge.config import load_config


def test_apply_assignments_preserves_base_config_and_writes_backup(tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("""
[server]
port = 8787

[security]
require_auth = false

[devices]
[[devices.printers]]
id = "old"
path = "/dev/ttyUSB9"
""", encoding="utf-8")

    backup = apply_assignments(config_path, [
        DeviceAssignment(
            device_id="printer-main",
            kind="printer",
            profile_id="printer-escpos-115200-8n1",
            path="/dev/serial/by-id/usb-printer",
            usb_vid=0x04B8,
            usb_pid=0x0202,
        ),
    ])

    config = load_config(config_path)
    assert backup.exists()
    assert config.printers[0].device_id == "printer-main"
    assert config.printers[0].baudrate == 115200
    assert config.printers[0].usb_vid == 0x04B8


def test_apply_assignments_rejects_wrong_profile_kind(tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("[server]\nport = 8787\n", encoding="utf-8")

    try:
        apply_assignments(config_path, [
            DeviceAssignment("scale-1", "scale", "printer-escpos-115200-8n1", "/dev/ttyUSB0"),
        ])
    except ValueError as error:
        assert "nao pertence" in str(error)
    else:  # pragma: no cover
        raise AssertionError("Perfil incompatível deveria falhar")
