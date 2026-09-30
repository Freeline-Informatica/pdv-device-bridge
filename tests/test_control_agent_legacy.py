import asyncio
from types import SimpleNamespace

import httpx

from pdv_device_bridge import control_agent


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
