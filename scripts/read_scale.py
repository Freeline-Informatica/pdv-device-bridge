#!/usr/bin/env python3
"""Le a balanca pelo bridge ou, com o servico parado, diretamente pela serial."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import serial

from pdv_device_bridge.config import ConfigError, DEFAULT_CONFIG_PATH, BridgeConfig, load_config
from pdv_device_bridge.device_registry import DeviceRegistry, DeviceRegistryError
from pdv_device_bridge.scale_parser import parse_weight_payload
from pdv_device_bridge.serial_io import read_scale_once


def bridge_is_active() -> bool:
    try:
        result = subprocess.run(
            ("systemctl", "is-active", "--quiet", "pdv-device-bridge"),
            capture_output=True,
            timeout=3,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def read_api(config: BridgeConfig, scale_id: str) -> dict[str, object]:
    url = f"http://127.0.0.1:{config.server.port}/v1/scales/{quote(scale_id, safe='')}/read?max_age_ms=0"
    headers = {}
    if config.security.require_auth and config.security.pairing_token:
        headers["Authorization"] = f"Bearer {config.security.pairing_token}"
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=max(4, config.scale.operation_timeout_ms / 1000 + 1)) as response:
            return json.load(response)
    except HTTPError as exc:
        try:
            detail = json.load(exc).get("detail", exc.reason)
        except (ValueError, AttributeError):
            detail = exc.reason
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except (URLError, TimeoutError, ValueError) as exc:
        raise RuntimeError(f"Falha na API local: {exc}") from exc


def read_direct(config: BridgeConfig, scale_id: str) -> dict[str, object]:
    registry = DeviceRegistry(config)
    descriptor = registry.get_descriptor("scale", scale_id)
    path = asyncio.run(registry.resolve_path("scale", scale_id))
    payload = read_scale_once(
        descriptor,
        path=path,
        command_bytes=config.scale.command_bytes,
        timeout_ms=config.scale.read_timeout_ms,
        response_quiet_ms=config.scale.response_quiet_ms,
        max_read_bytes=config.scale.max_read_bytes,
    )
    if not payload:
        raise RuntimeError("Sem resposta serial (0 bytes); isto nao comprova prato vazio.")
    parsed = parse_weight_payload(payload)
    if parsed is None:
        raise RuntimeError(f"Resposta serial invalida: hex={payload.hex()}")
    return {
        "grams": parsed.grams,
        "state": "empty" if parsed.grams == 0 else "weight",
        "stable": parsed.stable,
        "raw": parsed.raw_text,
        "raw_hex": parsed.raw_hex,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--scale-id", help="ID da balanca (obrigatorio se houver mais de uma).")
    parser.add_argument("--direct", action="store_true", help="Le a serial; exige bridge parado.")
    parser.add_argument("--watch", action="store_true", help="Repete leituras ate Ctrl+C.")
    parser.add_argument("--interval", type=float, default=1.0, help="Segundos entre leituras no modo --watch (padrao: 1).")
    args = parser.parse_args(argv)
    if args.interval < 0.2:
        parser.error("--interval deve ser de pelo menos 0.2 segundo.")

    try:
        config = load_config(args.config)
    except (OSError, ConfigError, ValueError) as exc:
        parser.error(f"Nao foi possivel carregar {args.config}: {exc}")

    scales = [item for item in config.scales if args.scale_id is None or item.device_id == args.scale_id]
    if len(scales) != 1:
        parser.error(f"Informe --scale-id. IDs: {', '.join(item.device_id for item in config.scales) or '(nenhum)'}")
    scale_id = scales[0].device_id

    print(f"Balanca: {scale_id}; modo: {'serial direta' if args.direct else 'API local'}; Ctrl+C encerra o monitor.", flush=True)
    had_error = False
    try:
        while True:
            if args.direct and bridge_is_active():
                parser.error("O bridge esta ativo. Pare pdv-device-bridge antes de usar --direct; use a leitura pela API durante a operacao.")
            started = time.monotonic()
            timestamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
            try:
                result = read_direct(config, scale_id) if args.direct else read_api(config, scale_id)
                elapsed = (time.monotonic() - started) * 1000
                print(f"{timestamp} OK {result.get('grams')} g state={result.get('state')} "
                      f"stable={result.get('stable')} duracao={elapsed:.0f} ms "
                      f"raw={result.get('raw', '')!r}" +
                      (f" hex={result['raw_hex']}" if 'raw_hex' in result else ""), flush=True)
            except (DeviceRegistryError, OSError, serial.SerialException, RuntimeError) as exc:
                had_error = True
                elapsed = (time.monotonic() - started) * 1000
                print(f"{timestamp} ERRO duracao={elapsed:.0f} ms {exc}", flush=True)
            if not args.watch:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nMonitor encerrado.")
    return 1 if had_error else 0


if __name__ == "__main__":
    sys.exit(main())
