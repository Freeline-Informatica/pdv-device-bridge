from io import BytesIO
import os
import pty
from urllib.error import HTTPError

from pdv_device_bridge.config import SerialDeviceConfig
from scripts.troubleshoot_scale import kernel_findings, probe_open, read_bridge, show_kernel_events


def test_kernel_findings_identifies_cp210x_uart_failure() -> None:
    log = """cp210x ttyUSB0: failed set request 0x0 status: -32
cp210x ttyUSB0: cp210x_open - Unable to enable UART
usb 1-1.2: USB disconnect, device number 4"""

    findings = kernel_findings(log)

    assert any("CP210x" in finding and "-32/EPIPE" in finding for finding in findings)
    assert any("desconexao USB" in finding for finding in findings)


def test_kernel_findings_identifies_comm_status_failure() -> None:
    findings = kernel_findings("cp210x ttyUSB0: failed to get comm status: -121")
    assert any("CP210x" in finding and "-121" in finding for finding in findings)


def test_probe_opens_pty_without_sending_data(capsys) -> None:
    master, slave = pty.openpty()
    try:
        descriptor = SerialDeviceConfig(device_id="scale-test")
        assert probe_open(descriptor, os.ttyname(slave)) is True
        assert "Porta abriu" in capsys.readouterr().out
    finally:
        os.close(master)
        os.close(slave)


def test_read_bridge_reports_recovery_after_one_failed_sample(monkeypatch, capsys) -> None:
    responses = iter([
        HTTPError("http://127.0.0.1:8787/read", 502, "Bad Gateway", {}, BytesIO(b'{"detail":"Leitura vazia"}')),
        BytesIO(b'{"grams":382,"kilograms":0.382,"source":"device","stable":true}'),
    ])

    def fake_urlopen(*_args, **_kwargs):
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr("scripts.troubleshoot_scale.urlopen", fake_urlopen)
    monkeypatch.setattr("scripts.troubleshoot_scale.time.sleep", lambda _seconds: None)

    assert read_bridge(8787, "scale-horti-1") is True
    output = capsys.readouterr().out
    assert "[AVISO] 1 tentativa(s) falharam" in output
    assert "[OK] Leitura do dispositivo: 382 g" in output


def test_read_bridge_fails_when_every_sample_fails(monkeypatch, capsys) -> None:
    def unavailable(*_args, **_kwargs):
        raise HTTPError("http://127.0.0.1:8787/read", 502, "Bad Gateway", {}, BytesIO(b"sem resposta"))

    monkeypatch.setattr("scripts.troubleshoot_scale.urlopen", unavailable)
    monkeypatch.setattr("scripts.troubleshoot_scale.time.sleep", lambda _seconds: None)

    assert read_bridge(8787, "scale-horti-1") is False
    assert "Nenhuma das 3 tentativas" in capsys.readouterr().out


def test_read_bridge_reports_no_reading_without_fake_zero(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "scripts.troubleshoot_scale.urlopen",
        lambda *_args, **_kwargs: BytesIO(b'{"state":"no_reading","grams":null,"raw":""}'),
    )

    assert read_bridge(8787, "scale-horti-1") is True
    output = capsys.readouterr().out
    assert "[AGUARDO]" in output
    assert "None g" not in output


def test_kernel_events_use_test_start_instead_of_old_history(monkeypatch, capsys) -> None:
    commands = []

    def fake_run_command(*args):
        commands.append(args)
        return 0, ""

    monkeypatch.setattr("scripts.troubleshoot_scale.run_command", fake_run_command)

    show_kernel_events("/dev/ttyUSB0", since="2026-09-28 12:00:00")

    assert commands[0][commands[0].index("--since") + 1] == "2026-09-28 12:00:00"
    assert "durante este teste (0 encontrados" in capsys.readouterr().out
