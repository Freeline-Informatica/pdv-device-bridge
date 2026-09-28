import os
import pty

from pdv_device_bridge.config import SerialDeviceConfig
from scripts.troubleshoot_scale import kernel_findings, probe_open


def test_kernel_findings_identifies_cp210x_uart_failure() -> None:
    log = """cp210x ttyUSB0: failed set request 0x0 status: -32
cp210x ttyUSB0: cp210x_open - Unable to enable UART
usb 1-1.2: USB disconnect, device number 4"""

    findings = kernel_findings(log)

    assert any("CP210x" in finding and "-32/EPIPE" in finding for finding in findings)
    assert any("desconexao USB" in finding for finding in findings)


def test_probe_opens_pty_without_sending_data(capsys) -> None:
    master, slave = pty.openpty()
    try:
        descriptor = SerialDeviceConfig(device_id="scale-test")
        assert probe_open(descriptor, os.ttyname(slave)) is True
        assert "Porta abriu" in capsys.readouterr().out
    finally:
        os.close(master)
        os.close(slave)
