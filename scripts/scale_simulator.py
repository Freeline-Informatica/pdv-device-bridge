#!/usr/bin/env python3
"""Simulador interativo de balanca para o PDV Device Bridge."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import os
from pathlib import Path
import pty
import select
import threading
import tty


DEFAULT_LINK_PATH = Path("/var/lib/pdv-device-bridge/scale-sim")
DEFAULT_COMMAND = b"\x04\x05"


@dataclass(slots=True, frozen=True)
class ScaleReading:
    kilograms: Decimal
    stable: bool = True


def build_scale_response(reading: ScaleReading) -> bytes:
    """Gera uma resposta compativel com o parser do bridge."""
    status = "ST" if reading.stable else "US"
    sign = "+" if reading.kilograms >= 0 else "-"
    amount = f"{abs(reading.kilograms):.3f}"
    return f"{status},GS,{sign}{amount}kg\r\n".encode("ascii")


def parse_kilograms(value: str) -> Decimal:
    try:
        kilograms = Decimal(value.strip().replace(",", "."))
    except InvalidOperation as exc:
        raise ValueError(f"Peso invalido: {value!r}.") from exc

    if not kilograms.is_finite():
        raise ValueError(f"Peso invalido: {value!r}.")

    return kilograms


def parse_command_hex(value: str) -> bytes:
    cleaned = "".join(character for character in value if character in "0123456789abcdefABCDEF")
    if not cleaned:
        raise argparse.ArgumentTypeError("O comando serial nao pode ser vazio.")
    if len(cleaned) % 2:
        cleaned = f"0{cleaned}"
    return bytes.fromhex(cleaned)


class ScaleSimulator:
    def __init__(
        self,
        *,
        link_path: Path = DEFAULT_LINK_PATH,
        command: bytes = DEFAULT_COMMAND,
        initial_reading: ScaleReading | None = None,
    ) -> None:
        if not command:
            raise ValueError("O comando serial nao pode ser vazio.")

        self.link_path = link_path
        self.command = command
        self._reading = initial_reading or ScaleReading(Decimal("0.000"))
        self._reading_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._master_fd: int | None = None
        self._slave_fd: int | None = None
        self._slave_path: str | None = None
        self._thread: threading.Thread | None = None

    @property
    def reading(self) -> ScaleReading:
        with self._reading_lock:
            return self._reading

    def set_reading(self, reading: ScaleReading) -> None:
        with self._reading_lock:
            self._reading = reading

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("O simulador ja esta em execucao.")

        master_fd, slave_fd = pty.openpty()
        tty.setraw(slave_fd)
        slave_path = os.ttyname(slave_fd)

        try:
            _install_link(self.link_path, slave_path)
        except Exception:
            os.close(master_fd)
            os.close(slave_fd)
            raise

        self._master_fd = master_fd
        self._slave_fd = slave_fd
        self._slave_path = slave_path
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._emulate_device, name="scale-simulator", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop_event.set()

        if self._thread is not None:
            self._thread.join(timeout=1)

        for descriptor in (self._master_fd, self._slave_fd):
            if descriptor is None:
                continue
            try:
                os.close(descriptor)
            except OSError:
                pass

        _remove_owned_link(self.link_path, self._slave_path)
        self._thread = None
        self._master_fd = None
        self._slave_fd = None
        self._slave_path = None

    def __enter__(self) -> ScaleSimulator:
        self.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _emulate_device(self) -> None:
        master_fd = self._master_fd
        if master_fd is None:
            return

        pending = bytearray()
        while not self._stop_event.is_set():
            try:
                ready, _, _ = select.select([master_fd], [], [], 0.2)
                if not ready:
                    continue
                chunk = os.read(master_fd, 64)
            except (OSError, ValueError):
                return

            if not chunk:
                continue

            pending.extend(chunk)
            while True:
                command_index = pending.find(self.command)
                if command_index < 0:
                    break

                del pending[:command_index + len(self.command)]
                response = build_scale_response(self.reading)
                try:
                    os.write(master_fd, response)
                except OSError:
                    return
                print(f"\nLeitura enviada: {response.decode('ascii').strip()}", flush=True)

            maximum_pending = max(64, len(self.command) * 4)
            if len(pending) > maximum_pending:
                bytes_to_keep = len(self.command) - 1
                if bytes_to_keep > 0:
                    del pending[:-bytes_to_keep]
                else:
                    pending.clear()


def _install_link(link_path: Path, slave_path: str) -> None:
    link_path.parent.mkdir(parents=True, exist_ok=True)

    if os.path.lexists(link_path):
        if link_path.is_symlink() and not link_path.exists():
            link_path.unlink()
        else:
            raise FileExistsError(
                f"{link_path} ja existe. Encerre o outro simulador ou remova o caminho manualmente.",
            )

    link_path.symlink_to(slave_path)


def _remove_owned_link(link_path: Path, slave_path: str | None) -> None:
    if slave_path is None or not link_path.is_symlink():
        return

    try:
        if os.readlink(link_path) == slave_path:
            link_path.unlink()
    except FileNotFoundError:
        pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Simula uma balanca serial para validar o fluxo de pesagem do PDV.",
    )
    parser.add_argument(
        "--link",
        type=Path,
        default=DEFAULT_LINK_PATH,
        help=f"Caminho serial estavel exposto ao bridge (padrao: {DEFAULT_LINK_PATH}).",
    )
    parser.add_argument(
        "--initial-kg",
        default="0.000",
        help="Peso inicial em quilogramas (padrao: 0.000).",
    )
    parser.add_argument(
        "--command-hex",
        type=parse_command_hex,
        default=DEFAULT_COMMAND,
        help="Comando serial que solicita uma leitura (padrao: 04 05).",
    )
    return parser


def _show_help() -> None:
    print("Comandos:")
    print("  0.350    define 0,350 kg estavel")
    print("  s 1.250  define 1,250 kg estavel")
    print("  u 1.250  define 1,250 kg instavel")
    print("  s         marca o peso atual como estavel")
    print("  u         marca o peso atual como instavel")
    print("  zero      zera a balanca")
    print("  status    mostra a proxima leitura")
    print("  q         encerra o simulador")


def _interactive_loop(simulator: ScaleSimulator) -> None:
    _show_help()

    while True:
        try:
            raw_command = input("peso> ").strip()
        except EOFError:
            return

        if not raw_command:
            continue

        parts = raw_command.lower().split(maxsplit=1)
        action = parts[0]

        if action in {"q", "quit", "exit"}:
            return
        if action in {"help", "ajuda", "?"}:
            _show_help()
            continue
        if action == "status":
            _print_reading(simulator.reading)
            continue
        if action == "zero":
            simulator.set_reading(ScaleReading(Decimal("0.000"), stable=True))
            _print_reading(simulator.reading)
            continue

        stable = True
        weight_text = raw_command
        if action in {"s", "u"}:
            stable = action == "s"
            if len(parts) == 1:
                simulator.set_reading(ScaleReading(simulator.reading.kilograms, stable=stable))
                _print_reading(simulator.reading)
                continue
            weight_text = parts[1]

        try:
            kilograms = parse_kilograms(weight_text)
        except ValueError as exc:
            print(f"{exc} Exemplo valido: 0.350")
            continue

        simulator.set_reading(ScaleReading(kilograms, stable=stable))
        _print_reading(simulator.reading)


def _print_reading(reading: ScaleReading) -> None:
    situation = "estavel" if reading.stable else "instavel"
    print(f"Proxima leitura: {reading.kilograms:.3f} kg ({situation}).")


def main() -> int:
    args = build_parser().parse_args()

    try:
        initial_reading = ScaleReading(parse_kilograms(args.initial_kg))
        with ScaleSimulator(
            link_path=args.link,
            command=args.command_hex,
            initial_reading=initial_reading,
        ) as simulator:
            print(f"Balanca virtual disponivel em: {simulator.link_path}")
            print("Configure esse caminho em devices.scales.path e reinicie o bridge.")
            _print_reading(simulator.reading)
            _interactive_loop(simulator)
    except (OSError, ValueError) as exc:
        print(f"Erro: {exc}")
        return 1
    except KeyboardInterrupt:
        print("\nSimulador encerrado.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
