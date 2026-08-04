from pdv_device_bridge.config import load_config


def test_load_config_reads_printer_serial_pacing_fields(tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[printer]
queue_size = 5
retry_delays_ms = [10, 20]
serial_chunk_size = 256
serial_chunk_delay_ms = 25
print_settle_ms = 1500
write_timeout_ms = 4500

[devices]
[[devices.printers]]
id = "printer-1"
path = "/dev/ttyUSB0"
baudrate = 115200
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.printer.queue_size == 5
    assert config.printer.retry_delays_ms == (10, 20)
    assert config.printer.serial_chunk_size == 256
    assert config.printer.serial_chunk_delay_ms == 25
    assert config.printer.print_settle_ms == 1500
    assert config.printer.write_timeout_ms == 4500


def test_load_config_keeps_printer_serial_pacing_defaults_for_old_configs(tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[devices]
[[devices.printers]]
id = "printer-1"
path = "/dev/ttyUSB0"
baudrate = 115200
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.printer.serial_chunk_size == 512
    assert config.printer.serial_chunk_delay_ms == 15
    assert config.printer.print_settle_ms == 1000
    assert config.printer.write_timeout_ms == 3000
