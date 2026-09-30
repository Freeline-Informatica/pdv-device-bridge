"""Outbound MQTT session for live scale subscriptions and confirmed print jobs."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import quote

import httpx
from paho.mqtt import client as mqtt


VALID_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


class RemoteMqttClient:
    def __init__(self, *, device_id: str, credentials: dict, pairing_token: str, health_url: str, ledger_path: Path) -> None:
        self.device_id = device_id
        self.credentials = credentials
        self.pairing_token = pairing_token
        self.base_url = health_url.rsplit("/health", 1)[0]
        self.loop: asyncio.AbstractEventLoop | None = None
        self.connected = False
        self.scales: dict[str, asyncio.Task[None]] = {}
        self.scale_leases: dict[str, float] = {}
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self.ledger = sqlite3.connect(ledger_path)
        self.ledger.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, status TEXT NOT NULL, message TEXT NOT NULL)")
        self.ledger.commit()
        self.client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                                  client_id=device_id, protocol=mqtt.MQTTv5,
                                  transport=str(credentials.get("transport", "tcp")))
        self.client.username_pw_set(str(credentials["username"]), str(credentials["password"]))
        self.client.tls_set()
        self.client.will_set(self.topic("events/presence"), json.dumps({"online": False}), qos=1, retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

    def topic(self, suffix: str) -> str:
        return f"device/{self.device_id}/{suffix}"

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.client.connect_async(str(self.credentials["host"]), int(self.credentials.get("port", 8883)), 15)
        self.client.loop_start()

    async def stop(self) -> None:
        for task in self.scales.values():
            task.cancel()
        if self.scales:
            await asyncio.gather(*self.scales.values(), return_exceptions=True)
        self.scales.clear()
        self.client.publish(self.topic("events/presence"), json.dumps({"online": False}), qos=1, retain=True)
        self.client.disconnect()
        self.client.loop_stop()
        self.ledger.close()

    def _on_connect(self, client, _userdata, _flags, reason_code, _properties) -> None:
        if reason_code.is_failure:
            return
        client.subscribe(self.topic("commands/#"), qos=1)
        if self.loop:
            self.loop.call_soon_threadsafe(self._set_connected, True)

    def _on_disconnect(self, _client, _userdata, _flags, _reason_code, _properties) -> None:
        if self.loop:
            self.loop.call_soon_threadsafe(self._set_connected, False)

    def _set_connected(self, value: bool) -> None:
        self.connected = value
        if not value:
            for task in self.scales.values():
                task.cancel()
            self.scale_leases.clear()

    def _on_message(self, _client, _userdata, message) -> None:
        try:
            payload = json.loads(message.payload)
        except (ValueError, UnicodeDecodeError):
            return
        if self.loop:
            self.loop.call_soon_threadsafe(self._handle_command, message.topic, payload)

    def _handle_command(self, topic: str, payload: dict) -> None:
        if topic == self.topic("commands/scale"):
            scale_id = str(payload.get("scale_id", ""))
            if not VALID_ID.fullmatch(scale_id):
                return
            if payload.get("action") == "start":
                self.scale_leases[scale_id] = time.monotonic() + 15
                if scale_id not in self.scales or self.scales[scale_id].done():
                    self.scales[scale_id] = asyncio.create_task(self._stream_scale(scale_id))
            elif payload.get("action") == "stop":
                self.scale_leases.pop(scale_id, None)
                task = self.scales.pop(scale_id, None)
                if task:
                    task.cancel()
        elif topic == self.topic("commands/print"):
            asyncio.create_task(self._print(payload))

    async def run_presence(self) -> None:
        while True:
            if self.connected:
                self.client.publish(self.topic("events/presence"), json.dumps({"online": True, "at": time.time()}), qos=1, retain=True)
            await asyncio.sleep(5)

    async def _stream_scale(self, scale_id: str) -> None:
        url = f"{self.base_url}/v1/scales/{quote(scale_id, safe='')}/events"
        while self.connected and time.monotonic() < self.scale_leases.get(scale_id, 0):
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(5, read=3)) as local:
                    async with local.stream("GET", url, headers={"Authorization": f"Bearer {self.pairing_token}"}) as response:
                        response.raise_for_status()
                        async for line in response.aiter_lines():
                            if not self.connected or time.monotonic() >= self.scale_leases.get(scale_id, 0):
                                return
                            if line.startswith("data: "):
                                reading = json.loads(line[6:])
                                self.client.publish(self.topic(f"events/scale/{scale_id}"),
                                                    json.dumps(reading), qos=0, retain=False)
            except (httpx.HTTPError, ValueError):
                await asyncio.sleep(1)
        self.scales.pop(scale_id, None)

    def _record(self, job_id: str, status: str, message: str) -> None:
        self.ledger.execute("UPDATE jobs SET status=?, message=? WHERE id=?", (status, message, job_id))
        self.ledger.commit()
        self.client.publish(self.topic(f"events/print/{job_id}"),
                            json.dumps({"status": status, "message": message}), qos=1, retain=False)

    async def _print(self, payload: dict) -> None:
        job_id = str(payload.get("job_id", ""))
        printer_id = str(payload.get("printer_id", ""))
        if not VALID_ID.fullmatch(printer_id) or not re.fullmatch(r"[0-9a-f-]{36}", job_id):
            return
        prior = self.ledger.execute("SELECT status, message FROM jobs WHERE id=?", (job_id,)).fetchone()
        if prior:
            self._record(job_id, prior[0], prior[1])
            return
        if float(payload.get("expires_at", 0)) < time.time():
            return
        self.ledger.execute("INSERT INTO jobs (id, status, message) VALUES (?, 'unknown', 'Resultado indeterminado.')", (job_id,))
        self.ledger.commit()
        url = f"{self.base_url}/v1/printers/{quote(printer_id, safe='')}/jobs"
        try:
            async with httpx.AsyncClient(timeout=5) as local:
                response = await local.post(url, headers={"Authorization": f"Bearer {self.pairing_token}"},
                                            json={"payload_base64": payload["payload_base64"],
                                                  "content_type": "escpos_raw", "request_id": job_id})
                response.raise_for_status()
                local_id = response.json()["job_id"]
                self._record(job_id, "printing", "Aguardando confirmação da impressora.")
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    result = await local.get(f"{url}/{quote(local_id, safe='')}")
                    result.raise_for_status()
                    state = result.json()
                    if state.get("status") in ("printed", "failed"):
                        self._record(job_id, state["status"], str(state.get("message", "")))
                        return
                    await asyncio.sleep(0.5)
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            self._record(job_id, "unknown", f"Sem confirmação do bridge: {exc}")
            return
        self._record(job_id, "unknown", "Tempo limite sem confirmação da impressora.")
