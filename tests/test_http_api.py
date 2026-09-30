from types import SimpleNamespace
import json

import pytest

from pdv_device_bridge.config import BridgeConfig
from pdv_device_bridge.http_api import create_app


@pytest.mark.asyncio
async def test_health_reports_scale_error_even_when_usb_path_exists() -> None:
    scale_health = {
        "scale-1": {
            "last_read_at": None,
            "grams": None,
            "stable": None,
            "serial_read_in_progress": False,
            "last_error": "could not open port /dev/ttyUSB0: Input/output error",
        },
    }
    runtime = SimpleNamespace(
        config=BridgeConfig(),
        registry=SimpleNamespace(snapshot=lambda: [{"available": True}]),
        scale_worker=SimpleNamespace(health_snapshot=lambda: scale_health),
        printer_worker=SimpleNamespace(health_snapshot=lambda: {}),
        uptime_seconds=lambda: 1.0,
        is_idle=lambda: True,
    )
    app = create_app(runtime)
    health = next(route.endpoint for route in app.routes if route.path == "/health")

    response = await health()
    assert response["status"] == "degraded"
    assert response["idle"] is True
    assert response["workers"]["scale"]["scale-1"]["last_error"] == scale_health["scale-1"]["last_error"]

    scale_health["scale-1"]["last_error"] = None
    response = await health()
    assert response["status"] == "ok"


@pytest.mark.asyncio
async def test_scale_events_uses_sse_contract() -> None:
    async def scale_stream(_scale_id):
        yield {"state": "empty", "grams": 0, "kilograms": 0.0}

    runtime = SimpleNamespace(
        config=BridgeConfig(),
        registry=SimpleNamespace(get_descriptor=lambda *_args: object()),
        scale_worker=SimpleNamespace(stream=scale_stream),
    )
    app = create_app(runtime)
    endpoint = next(route.endpoint for route in app.routes if route.path == "/v1/scales/{scale_id}/events")
    response = await endpoint("scale-1")
    chunk = await anext(response.body_iterator)
    await response.body_iterator.aclose()

    assert response.media_type == "text/event-stream"
    assert chunk.startswith("event: scale\ndata: ")
    assert json.loads(chunk.split("data: ", 1)[1])["state"] == "empty"
