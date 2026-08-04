from __future__ import annotations

import argparse
import os
from pathlib import Path

import uvicorn

from .app_runtime import BridgeRuntime
from .config import ConfigError, DEFAULT_CONFIG_PATH, load_config
from .http_api import create_app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Daemon HTTP para bridge serial de balanca/impressora.")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("PDV_DEVICE_BRIDGE_CONFIG", DEFAULT_CONFIG_PATH)),
        help="Caminho do arquivo TOML de configuracao.",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    try:
        bridge_config = load_config(args.config)
    except FileNotFoundError:
        parser.error(f"Arquivo de configuracao nao encontrado: {args.config}")
    except ConfigError as exc:
        parser.error(f"Configuracao invalida: {exc}")

    runtime = BridgeRuntime.from_config(bridge_config)
    app = create_app(runtime)

    uvicorn.run(
        app,
        host=bridge_config.server.host,
        port=bridge_config.server.port,
        log_level=bridge_config.server.log_level,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
