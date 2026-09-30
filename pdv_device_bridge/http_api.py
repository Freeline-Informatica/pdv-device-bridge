from __future__ import annotations

import base64
import binascii
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hmac
import json

from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from . import __version__
from .app_runtime import BridgeRuntime
from .device_registry import DeviceNotFoundError, DeviceUnavailableError
from .configuration_store import DeviceAssignment, apply_assignments
from .device_profiles import profiles_payload
from .printer_worker import JobNotFoundError, PrinterWorkerError, QueueLimitReachedError
from .scale_worker import ScaleReadError


class PrintJobCreateRequest(BaseModel):
    payload_base64: str = Field(min_length=1)
    content_type: str = Field(default="escpos_raw")
    request_id: str | None = Field(default=None, max_length=120)


class PrintJobCreateResponse(BaseModel):
    job_id: str
    status: str
    message: str


class DeviceAssignmentRequest(BaseModel):
    device_id: str
    kind: str
    profile_id: str
    path: str
    usb_vid: int | None = None
    usb_pid: int | None = None
    usb_serial: str | None = None


class ConfigurationApplyRequest(BaseModel):
    devices: list[DeviceAssignmentRequest] = Field(min_length=1, max_length=32)


def create_app(runtime: BridgeRuntime) -> FastAPI:
    bearer = HTTPBearer(auto_error=False)

    def require_lan_access(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> None:
        if not runtime.config.security.require_auth:
            return
        supplied = credentials.credentials if credentials and credentials.scheme.lower() == "bearer" else ""
        expected = runtime.config.security.pairing_token or ""
        if not supplied or not hmac.compare_digest(supplied, expected):
            raise HTTPException(status_code=401, detail="Credencial LAN invalida.")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(
        title="pdv-device-bridge",
        version=__version__,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(runtime.config.server.cors_allowed_origins),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    async def health() -> dict[str, object]:
        devices = runtime.registry.snapshot()
        available_count = sum(1 for item in devices if item["available"])
        scale_health = runtime.scale_worker.health_snapshot()
        degraded = available_count < len(devices) or any(
            item["last_error"] for item in scale_health.values()
        )

        return {
            "status": "degraded" if degraded else "ok",
            "version": app.version,
            "timestamp": _utc_now_iso(),
            "uptime_seconds": round(runtime.uptime_seconds(), 3),
            "idle": runtime.is_idle(),
            "devices": devices,
            "workers": {
                "scale": scale_health,
                "printer": runtime.printer_worker.health_snapshot(),
            },
        }

    @app.get("/v1/identity", dependencies=[Depends(require_lan_access)])
    async def identity() -> dict[str, object]:
        if runtime.identity is None:
            raise HTTPException(status_code=503, detail="Identidade indisponivel.")
        return runtime.identity.public_payload(version=app.version)

    @app.get("/v1/status", dependencies=[Depends(require_lan_access)])
    async def status() -> dict[str, object]:
        return {
            "version": app.version,
            "identity": runtime.identity.public_payload(version=app.version) if runtime.identity else None,
            "idle": runtime.is_idle(),
            "devices": runtime.registry.snapshot(),
            "workers": {
                "scale": runtime.scale_worker.health_snapshot(),
                "printer": runtime.printer_worker.health_snapshot(),
            },
        }

    @app.get("/v1/devices", dependencies=[Depends(require_lan_access)])
    async def list_devices() -> dict[str, object]:
        rows = runtime.registry.snapshot()
        return {
            "items": rows,
            "count": len(rows),
        }

    @app.get("/v1/serial-ports", dependencies=[Depends(require_lan_access)])
    async def serial_ports() -> dict[str, object]:
        return {"items": runtime.registry.serial_ports_snapshot(), "profiles": profiles_payload()}

    @app.put("/v1/configuration", dependencies=[Depends(require_lan_access)])
    async def configure(payload: ConfigurationApplyRequest) -> dict[str, object]:
        if runtime.config_path is None or not runtime.is_idle():
            raise HTTPException(status_code=409, detail="Bridge ocupado ou sem arquivo de configuracao.")
        try:
            backup = apply_assignments(runtime.config_path, [
                DeviceAssignment(**item.model_dump()) for item in payload.devices
            ])
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"status": "accepted", "restart_required": True, "backup_path": str(backup)}

    @app.get("/v1/scales/{scale_id}/read", dependencies=[Depends(require_lan_access)])
    async def read_scale(scale_id: str, max_age_ms: int = Query(default=1500, ge=0, le=60000)) -> dict[str, object]:
        try:
            return await runtime.scale_worker.read(scale_id, max_age_ms=max_age_ms)
        except DeviceNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except DeviceUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ScaleReadError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/v1/scales/{scale_id}/readings", dependencies=[Depends(require_lan_access)])
    async def list_scale_readings(
        scale_id: str,
        limit: int = Query(default=50, ge=1, le=500),
    ) -> dict[str, object]:
        try:
            runtime.registry.get_descriptor("scale", scale_id)
        except DeviceNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        items = runtime.scale_worker.recent_readings(scale_id, limit=limit)
        return {"items": items, "count": len(items)}

    @app.get("/v1/scales/{scale_id}/events", dependencies=[Depends(require_lan_access)])
    async def stream_scale(scale_id: str) -> StreamingResponse:
        try:
            runtime.registry.get_descriptor("scale", scale_id)
        except DeviceNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        async def events():
            subscription = runtime.scale_worker.stream(scale_id)
            try:
                async for payload in subscription:
                    yield f"event: scale\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            finally:
                await subscription.aclose()

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/v1/printers/{printer_id}/jobs", response_model=PrintJobCreateResponse, dependencies=[Depends(require_lan_access)])
    async def create_print_job(printer_id: str, payload: PrintJobCreateRequest) -> PrintJobCreateResponse:
        if payload.content_type != "escpos_raw":
            raise HTTPException(status_code=422, detail="content_type deve ser 'escpos_raw'.")

        try:
            raw_payload = base64.b64decode(payload.payload_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(status_code=422, detail="payload_base64 invalido.") from exc

        if not raw_payload:
            raise HTTPException(status_code=422, detail="payload_base64 vazio.")

        try:
            created = runtime.printer_worker.submit_job(
                printer_id,
                raw_payload,
                request_id=payload.request_id,
            )
        except QueueLimitReachedError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except PrinterWorkerError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        return PrintJobCreateResponse(
            job_id=str(created["job_id"]),
            status=str(created["status"]),
            message=str(created["message"]),
        )

    @app.get("/v1/printers/{printer_id}/jobs/{job_id}", dependencies=[Depends(require_lan_access)])
    async def get_print_job(printer_id: str, job_id: str) -> dict[str, object]:
        try:
            return runtime.printer_worker.get_job(printer_id, job_id)
        except JobNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/v1/printers/{printer_id}/jobs", dependencies=[Depends(require_lan_access)])
    async def list_print_jobs(
        printer_id: str,
        limit: int = Query(default=50, ge=1, le=500),
    ) -> dict[str, object]:
        try:
            items = runtime.printer_worker.list_jobs(printer_id, limit=limit)
        except PrinterWorkerError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"items": items, "count": len(items), "queue": runtime.printer_worker.health_snapshot().get(printer_id)}

    @app.post("/v1/printers/{printer_id}/jobs/{job_id}/retry", dependencies=[Depends(require_lan_access)])
    async def retry_print_job(printer_id: str, job_id: str) -> PrintJobCreateResponse:
        try:
            created = runtime.printer_worker.retry_job(printer_id, job_id)
        except JobNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except QueueLimitReachedError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except PrinterWorkerError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return PrintJobCreateResponse(
            job_id=str(created["job_id"]),
            status=str(created["status"]),
            message=str(created["message"]),
        )

    return app


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
