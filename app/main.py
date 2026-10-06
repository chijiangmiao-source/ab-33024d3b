"""轨道载荷升级服务：双槽镜像升级、断电恢复裁决与并发代次仲裁的 HTTP API。"""
from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from .recovery import adjudicate
from .store import ConflictError, NotFoundError, Store, sha256_hex

BASE_DIR = Path(__file__).resolve().parent.parent
DIST_INDEX = BASE_DIR / "frontend" / "dist" / "index.html"
SRC_DIR = BASE_DIR / "frontend" / "src"


class CreateDeviceReq(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    version: int = Field(ge=0)
    image: str = Field(min_length=1, max_length=65536)


class CandidateReq(BaseModel):
    version: int = Field(ge=0)
    image: str = Field(min_length=1, max_length=65536)
    expected_generation: int = Field(ge=1)


class ConfirmReq(BaseModel):
    expected_generation: int = Field(ge=1)


class PowerLossReq(BaseModel):
    corrupt_candidate: bool = False


def slot_view(slot: dict) -> dict:
    image = slot.get("image")
    actual = sha256_hex(image) if image else None
    expected = slot.get("expected_digest")
    return {
        "slot": slot["slot"],
        "state": slot["state"],
        "version": slot["version"],
        "expected_digest": expected,
        "actual_digest": actual,
        "digest_ok": expected is not None and actual == expected,
        "image_size": len(image) if image else 0,
        "confirmed_generation": slot["confirmed_generation"],
        "diagnostics": json.loads(slot["diagnostics"]) if slot["diagnostics"] else None,
        "updated_at": slot["updated_at"],
    }


def device_view(payload: dict) -> dict:
    device, slots = payload["device"], payload["slots"]
    return {
        "id": device["id"],
        "name": device["name"],
        "generation": device["generation"],
        "active_slot": device["active_slot"],
        "created_at": device["created_at"],
        "slots": [slot_view(s) for s in slots],
        "recovery": adjudicate(device, slots),
    }


def create_app(db_path: str | None = None) -> FastAPI:
    store = Store(db_path or os.environ.get("APP_DB_PATH", "data/app.db"))
    app = FastAPI(title="轨道载荷升级服务", version="1.0.0")
    app.state.store = store

    @app.exception_handler(ConflictError)
    async def conflict_handler(_, exc: ConflictError):
        return JSONResponse(
            status_code=409,
            content={"error": {"code": exc.code, "message": exc.message, **exc.extra}},
        )

    @app.exception_handler(NotFoundError)
    async def not_found_handler(_, exc: NotFoundError):
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "DEVICE_NOT_FOUND", "message": exc.message}},
        )

    @app.get("/healthz")
    def healthz():
        store.ping()
        return {"status": "ok"}

    # ---- 页面 ----

    @app.get("/", include_in_schema=False)
    def index():
        if DIST_INDEX.exists():
            return FileResponse(DIST_INDEX)
        return FileResponse(SRC_DIR / "index.html")

    @app.get("/styles.css", include_in_schema=False)
    def styles():
        return FileResponse(SRC_DIR / "styles.css")

    @app.get("/app.js", include_in_schema=False)
    def script():
        return FileResponse(SRC_DIR / "app.js")

    # ---- 设备与升级 API ----

    @app.post("/api/devices")
    def create_device(req: CreateDeviceReq):
        return device_view(store.create_device(req.name, req.version,
                                               req.image.encode("utf-8")))

    @app.get("/api/devices")
    def list_devices():
        return {"devices": store.list_devices()}

    @app.get("/api/devices/{device_id}")
    def get_device(device_id: str):
        # 每次读取即"重新打开设备视图"：基于持久化清单实时裁决
        return device_view(store.get_device(device_id))

    @app.post("/api/devices/{device_id}/candidates")
    def submit_candidate(device_id: str, req: CandidateReq):
        return device_view(store.submit_candidate(
            device_id, req.version, req.image.encode("utf-8"),
            req.expected_generation))

    @app.post("/api/devices/{device_id}/verify")
    def verify_candidate(device_id: str):
        return device_view(store.verify_candidate(device_id))

    @app.post("/api/devices/{device_id}/confirm")
    def confirm_switch(device_id: str, req: ConfirmReq):
        return device_view(store.confirm_switch(device_id, req.expected_generation))

    @app.post("/api/devices/{device_id}/power-loss")
    def power_loss(device_id: str, req: PowerLossReq):
        return device_view(store.power_loss(device_id, req.corrupt_candidate))

    @app.get("/api/devices/{device_id}/events")
    def list_events(device_id: str):
        return {"events": store.list_events(device_id)}

    return app


app = create_app()
