from __future__ import annotations

import asyncio
import os
import socket


class SystemdNotifier:
    def __init__(self) -> None:
        self._notify_socket = os.environ.get("NOTIFY_SOCKET")
        self._watchdog_usec = int(os.environ.get("WATCHDOG_USEC", "0") or "0")
        self._watchdog_task: asyncio.Task[None] | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._notify_socket)

    async def start(self) -> None:
        if not self.enabled:
            return

        self.notify("READY=1")
        self.notify("STATUS=pdv-device-bridge iniciado")

        if self._watchdog_usec > 0 and self._watchdog_task is None:
            interval = max(1.0, (self._watchdog_usec / 1_000_000) / 2)
            self._watchdog_task = asyncio.create_task(self._run_watchdog(interval), name="systemd-watchdog")

    async def stop(self) -> None:
        if self._watchdog_task:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass
            self._watchdog_task = None

        if self.enabled:
            self.notify("STOPPING=1")

    def notify(self, message: str) -> None:
        notify_socket = self._notify_socket
        if not notify_socket:
            return

        address = notify_socket
        if notify_socket.startswith("@"):
            address = f"\0{notify_socket[1:]}"

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sock.connect(address)
            sock.sendall(message.encode("utf-8"))
        finally:
            sock.close()

    async def _run_watchdog(self, interval_seconds: float) -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            self.notify("WATCHDOG=1")
