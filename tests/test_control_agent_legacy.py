import asyncio
from types import SimpleNamespace

import httpx

from pdv_device_bridge import control_agent
from pdv_device_bridge.agent_config import load_agent_config


def test_legacy_bridge_health_supplies_peripherals_when_status_route_is_missing(monkeypatch):
    requests = []

    def respond(request):
        requests.append(request.url.path)
        if request.url.path == "/v1/status":
            return httpx.Response(404)
        return httpx.Response(200, json={"devices": [{"id": "scale-horti-1", "kind": "scale"}]})

    transport = httpx.MockTransport(respond)
    original_client = httpx.AsyncClient
    monkeypatch.setattr(control_agent.httpx, "AsyncClient", lambda **kwargs: original_client(transport=transport, **kwargs))
    agent = object.__new__(control_agent.ControlAgent)
    agent.config = SimpleNamespace(bridge_health_url="http://127.0.0.1:8787/health")
    agent.state = SimpleNamespace(pairing_token="test-token")

    status = asyncio.run(agent._local_status())

    assert requests == ["/v1/status", "/health"]
    assert status == {"devices": [{"id": "scale-horti-1", "kind": "scale"}]}
    assert "identity" not in status


def test_observation_mode_skips_bridge_configuration_and_release(monkeypatch, tmp_path):
    config_path = tmp_path / "agent.toml"
    config_path.write_text('[control]\nbase_url = "https://control.example.test"\n[local]\nmanage_bridge = false\n')
    config = load_agent_config(config_path)
    assert config.manage_bridge is False

    agent = object.__new__(control_agent.ControlAgent)
    agent.config = config
    agent.state = SimpleNamespace(device_token="device-token", mqtt=None, pairing_token="pairing-token")
    heartbeats = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"configuration_revision": 1, "desired_configuration": {"devices": []}, "desired_release": {"version": "9.0.0"}}

    async def post(_path, *, headers, json):
        heartbeats.append(json)
        return Response()

    async def local_status():
        return {"devices": [{"id": "scale-horti-1"}]}

    async def fail(*_args):
        raise AssertionError("Observation mode must not change the bridge")

    async def no_op(*_args):
        pass

    agent.client = SimpleNamespace(post=post)
    monkeypatch.setattr(agent, "_headers", lambda: {})
    monkeypatch.setattr(agent, "_heartbeat_payload", lambda: {})
    monkeypatch.setattr(agent, "_local_status", local_status)
    monkeypatch.setattr(agent, "_handle_credentials", no_op)
    monkeypatch.setattr(agent, "_handle_configuration", fail)
    monkeypatch.setattr(agent, "_handle_release", fail)
    monkeypatch.setattr(agent, "_sync_bridge_identity", fail)

    asyncio.run(agent.run_once())

    assert heartbeats == [{"peripherals": [{"id": "scale-horti-1"}]}]
