import time
import uuid

import httpx
import pytest

from pdv_device_bridge.remote_mqtt import RemoteMqttClient


def test_remote_mqtt_uses_websockets_when_provisioned(monkeypatch, tmp_path):
    monkeypatch.setattr("paho.mqtt.client.Client.tls_set", lambda *_args, **_kwargs: None)
    device_id = str(uuid.uuid4())
    client = RemoteMqttClient(
        device_id=device_id,
        credentials={"username": device_id, "password": "secret", "host": "mqtt.test", "transport": "websockets"},
        pairing_token="token",
        health_url="http://127.0.0.1:8787/health",
        ledger_path=tmp_path / "jobs.sqlite",
    )

    assert client.client.transport == "websockets"
    client.ledger.close()


@pytest.mark.asyncio
async def test_print_command_is_not_repeated_after_duplicate_or_restart(monkeypatch, tmp_path):
    monkeypatch.setattr("paho.mqtt.client.Client.tls_set", lambda *_args, **_kwargs: None)
    posts = []
    published = []

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"job_id": "local-job", "status": "printed", "message": "ok"}

    class FakeHttp:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, **kwargs):
            posts.append((url, kwargs))
            return FakeResponse()

        async def get(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeHttp)
    device_id = str(uuid.uuid4())
    credentials = {"username": device_id, "password": "secret", "host": "mqtt.test"}
    ledger = tmp_path / "jobs.sqlite"

    def make_client():
        client = RemoteMqttClient(device_id=device_id, credentials=credentials,
                                  pairing_token="token", health_url="http://127.0.0.1:8787/health", ledger_path=ledger)
        monkeypatch.setattr(client.client, "publish", lambda *args, **kwargs: published.append(args))
        return client

    command = {"job_id": str(uuid.uuid4()), "printer_id": "printer-1", "payload_base64": "QQ==",
               "expires_at": time.time() + 10}
    first = make_client()
    await first._print(command)
    await first._print(command)
    first.ledger.close()
    second = make_client()
    await second._print(command)
    second.ledger.close()

    assert len(posts) == 1
    assert len(published) >= 3


@pytest.mark.asyncio
async def test_expired_print_command_is_not_sent(monkeypatch, tmp_path):
    monkeypatch.setattr("paho.mqtt.client.Client.tls_set", lambda *_args, **_kwargs: None)
    device_id = str(uuid.uuid4())
    client = RemoteMqttClient(device_id=device_id, credentials={"username": device_id, "password": "secret", "host": "mqtt.test"},
                              pairing_token="token", health_url="http://127.0.0.1:8787/health", ledger_path=tmp_path / "jobs.sqlite")
    await client._print({"job_id": str(uuid.uuid4()), "printer_id": "printer-1", "payload_base64": "QQ==",
                         "expires_at": time.time() - 1})
    assert client.ledger.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    client.ledger.close()
