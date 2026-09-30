from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time

from .config import BridgeConfig
from .device_registry import DeviceRegistry
from .identity import BridgeIdentity, load_or_create_identity
from .printer_worker import PrinterWorker
from .scale_worker import ScaleWorker
from .systemd_watchdog import SystemdNotifier


@dataclass(slots=True)
class BridgeRuntime:
    config: BridgeConfig
    registry: DeviceRegistry
    scale_worker: ScaleWorker
    printer_worker: PrinterWorker
    started_at_epoch_s: float
    notifier: SystemdNotifier
    identity: BridgeIdentity | None = None
    config_path: Path | None = None

    @classmethod
    def from_config(cls, config: BridgeConfig, *, config_path: Path | str | None = None) -> "BridgeRuntime":
        registry = DeviceRegistry(config)
        scale_worker = ScaleWorker(registry, config.scale)
        printer_worker = PrinterWorker(
            registry,
            config.printer,
            [descriptor.device_id for descriptor in config.printers],
        )

        return cls(
            config=config,
            registry=registry,
            scale_worker=scale_worker,
            printer_worker=printer_worker,
            started_at_epoch_s=time.time(),
            notifier=SystemdNotifier(),
            identity=load_or_create_identity(config.identity.state_path, config.identity.provision_path) if config_path else None,
            config_path=Path(config_path) if config_path else None,
        )

    async def start(self) -> None:
        await self.registry.start()
        await self.printer_worker.start()
        await self.notifier.start()

    async def stop(self) -> None:
        await self.notifier.stop()
        await self.printer_worker.stop()
        await self.scale_worker.stop()
        await self.registry.stop()

    def uptime_seconds(self) -> float:
        return max(0.0, time.time() - self.started_at_epoch_s)

    def is_idle(self) -> bool:
        return self.printer_worker.is_idle() and self.scale_worker.is_idle()
