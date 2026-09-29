#!/usr/bin/env python3
"""Diagnostica a porta USB/serial e a leitura HTTP de uma balanca configurada."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import urlopen

import serial
from serial.tools import list_ports

from pdv_device_bridge.config import ConfigError, DEFAULT_CONFIG_PATH, SerialDeviceConfig, load_config


SERVICE = "pdv-device-bridge"


def kernel_findings(log: str) -> list[str]:
    """Classifica mensagens do kernel, sem tratá-las como estado atual."""
    findings: list[str] = []
    lowered = log.lower()
    if "cp210x_open - unable to enable uart" in lowered or (
        "cp210x" in lowered and "failed set request" in lowered and "status: -32" in lowered
    ):
        findings.append(
            "CP210x falhou ao habilitar a UART (-32/EPIPE): falha na comunicacao USB; "
            "teste reconectar, outra porta USB e outro adaptador."
        )
    if "usb disconnect" in lowered:
        findings.append("O kernel registrou desconexao USB; confira a hora do evento e a conexao fisica.")
    return findings


def run_command(*args: str) -> tuple[int, str]:
    try:
        completed = subprocess.run(args, capture_output=True, text=True, timeout=5, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return completed.returncode, (completed.stdout or completed.stderr).strip()


def show_port(descriptor: SerialDeviceConfig) -> str | None:
    print(f"Configuracao: id={descriptor.device_id}, path={descriptor.path or '(ausente)'}, "
          f"serial={descriptor.baudrate} {descriptor.bytesize}{descriptor.parity}{descriptor.stopbits:g}")
    if not descriptor.path:
        print("[AVISO] Sem path configurado; confira usb_vid, usb_pid e usb_serial.")
        return None

    configured = Path(descriptor.path)
    if not configured.exists():
        print(f"[FALHA] Caminho nao existe ou link quebrado: {configured}")
        return None

    resolved = configured.resolve()
    mode = resolved.stat().st_mode
    if not stat.S_ISCHR(mode):
        print(f"[FALHA] {resolved} nao e um dispositivo de caracteres.")
        return None

    access = os.access(resolved, os.R_OK | os.W_OK)
    print(f"Porta: {configured} -> {resolved}; acesso leitura/escrita para uid={os.geteuid()}: "
          f"{'sim' if access else 'nao'}")
    if not access:
        print("[FALHA] Permissao insuficiente. Confira o usuario/grupos do servico e o grupo dialout.")

    matching = [port for port in list_ports.comports() if port.device == str(resolved)]
    if matching:
        port = matching[0]
        vid_pid = (
            f"{port.vid:04x}:{port.pid:04x}"
            if port.vid is not None and port.pid is not None else "desconhecido"
        )
        print(f"USB: VID:PID={vid_pid}, serial={port.serial_number or '-'}, "
              f"descricao={port.description}")
    else:
        print("[AVISO] Porta nao apareceu na enumeracao do pySerial.")
    return str(resolved)


def show_kernel_events(device_path: str | None, *, since: str | None = None) -> None:
    status, output = run_command("journalctl", "-k", "-b", "--since", since or "15 minutes ago", "-n", "200", "--no-pager", "-o", "short-iso")
    if status != 0:
        print(f"[AVISO] Nao foi possivel ler o journal do kernel: {output}")
        return

    name = Path(device_path).name.lower() if device_path else "ttyusb"
    relevant = [line for line in output.splitlines() if any(token in line.lower() for token in (name, "cp210x", "usb disconnect"))]
    period = "durante este teste" if since else "nos ultimos 15 minutos (historico)"
    print(f"Eventos USB/serial {period} ({len(relevant)} encontrados; ultimos 8):")
    for line in relevant[-8:]:
        print(f"  {line}")
    for finding in kernel_findings("\n".join(relevant)):
        print(f"[{'AVISO' if since else 'HISTORICO'}] {finding}")


def probe_open(descriptor: SerialDeviceConfig, path: str) -> bool:
    try:
        with serial.Serial(
            port=path,
            baudrate=descriptor.baudrate,
            bytesize=descriptor.bytesize,
            parity=descriptor.parity,
            stopbits=descriptor.stopbits,
            timeout=1,
        ):
            pass
    except (OSError, serial.SerialException, ValueError) as exc:
        print(f"[FALHA] Abertura direta da porta: {exc}")
        return False
    print("[OK] Porta abriu e fechou sem enviar comandos; resposta da balanca ainda nao testada.")
    return True


def read_bridge(port: int, scale_id: str, *, attempts: int = 3) -> bool:
    url = f"http://127.0.0.1:{port}/v1/scales/{quote(scale_id, safe='')}/read?max_age_ms=0"
    failures: list[str] = []
    for attempt in range(1, max(1, attempts) + 1):
        started_at = time.monotonic()
        try:
            with urlopen(url, timeout=4) as response:
                payload = json.load(response)
        except HTTPError as exc:
            detail = exc.read(500).decode("utf-8", errors="replace")
            failures.append(f"HTTP {exc.code} em {(time.monotonic() - started_at) * 1000:.0f} ms: {detail}")
        except (URLError, TimeoutError, ValueError) as exc:
            failures.append(f"{exc} em {(time.monotonic() - started_at) * 1000:.0f} ms")
        else:
            if failures:
                print(f"[AVISO] {len(failures)} tentativa(s) falharam antes desta leitura valida: {'; '.join(failures)}")
            print(f"[OK] Leitura do dispositivo: {payload.get('grams')} g; "
                  f"estado={payload.get('state')}; origem={payload.get('source')}; "
                  f"stable={payload.get('stable')}; HTTP={(time.monotonic() - started_at) * 1000:.0f} ms")
            print(f"Resposta bruta: {str(payload.get('raw', ''))[:200]!r}")
            print("Nota: stable=True pode ser padrao do parser quando o protocolo nao informa estabilidade.")
            return True

        if attempt < attempts:
            time.sleep(0.5)

    print(f"[FALHA] Nenhuma das {max(1, attempts)} tentativas retornou uma leitura valida.")
    for attempt, failure in enumerate(failures, start=1):
        print(f"  Tentativa {attempt}: {failure}")
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--scale-id", help="Obrigatorio quando ha mais de uma balanca configurada.")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--open-port", action="store_true", help="Abre e fecha a serial sem enviar bytes; pare o servico antes.")
    action.add_argument("--read-api", action="store_true", help="Pede uma leitura nova ao bridge HTTP (envia o comando da balanca).")
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except (OSError, ConfigError, ValueError) as exc:
        parser.error(f"Nao foi possivel carregar {args.config}: {exc}")

    scales = [item for item in config.scales if args.scale_id is None or item.device_id == args.scale_id]
    if len(scales) != 1:
        parser.error(f"Selecione uma balanca com --scale-id. IDs: {', '.join(item.device_id for item in config.scales) or '(nenhum)'}")
    descriptor = scales[0]

    print(f"Diagnostico da balanca {descriptor.device_id}")
    print(f"Usuario: {os.geteuid()}; grupos: {','.join(str(group) for group in os.getgroups())}")
    service_status, output = run_command("systemctl", "is-active", SERVICE)
    active = service_status == 0 and output == "active"
    print(f"Servico {SERVICE}: {output or 'indisponivel'}")
    _, service_identity = run_command("systemctl", "show", SERVICE, "-p", "User", "-p", "Group", "-p", "SupplementaryGroups", "--no-pager")
    if service_identity and "=" in service_identity:
        print(f"Identidade do servico: {service_identity.replace(chr(10), '; ')}")

    path = show_port(descriptor)
    result = 0 if path else 1
    test_started_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S") if (args.open_port or args.read_api) else None

    if args.open_port:
        if active:
            print("[AVISO] Abertura direta ignorada: pare o servico para evitar disputa pela serial.")
            result = 2
        elif not path or not probe_open(descriptor, path):
            result = 1

    if args.read_api and not read_bridge(config.server.port, descriptor.device_id):
        result = 1

    show_kernel_events(path, since=test_started_at)

    if not args.open_port and not args.read_api:
        print("Para testar abertura: pare o servico e execute com --open-port.")
        print("Para testar peso: execute com --read-api enquanto o servico estiver ativo.")
    return result


if __name__ == "__main__":
    sys.exit(main())
