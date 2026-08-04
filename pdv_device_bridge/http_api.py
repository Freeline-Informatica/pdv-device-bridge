from __future__ import annotations

import base64
import binascii
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .app_runtime import BridgeRuntime
from .device_registry import DeviceNotFoundError, DeviceUnavailableError
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


def create_app(runtime: BridgeRuntime) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(
        title="pdv-device-bridge",
        version="0.1.0",
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
        degraded = available_count < len(devices)

        return {
            "status": "degraded" if degraded else "ok",
            "timestamp": _utc_now_iso(),
            "uptime_seconds": round(runtime.uptime_seconds(), 3),
            "devices": devices,
            "workers": {
                "scale": runtime.scale_worker.health_snapshot(),
                "printer": runtime.printer_worker.health_snapshot(),
            },
        }

    @app.get("/v1/devices")
    async def list_devices() -> dict[str, object]:
        rows = runtime.registry.snapshot()
        return {
            "items": rows,
            "count": len(rows),
        }

    @app.get("/v1/scales/{scale_id}/read")
    async def read_scale(scale_id: str, max_age_ms: int = Query(default=1500, ge=0, le=60000)) -> dict[str, object]:
        try:
            return await runtime.scale_worker.read(scale_id, max_age_ms=max_age_ms)
        except DeviceNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except DeviceUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ScaleReadError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/v1/printers/{printer_id}/jobs", response_model=PrintJobCreateResponse)
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

    @app.get("/v1/printers/{printer_id}/jobs/{job_id}")
    async def get_print_job(printer_id: str, job_id: str) -> dict[str, object]:
        try:
            return runtime.printer_worker.get_job(printer_id, job_id)
        except JobNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return app


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
