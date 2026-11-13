"""
API REST del intérprete Python online.

Endpoints:
    GET  /api/health        -> estado del servicio y de Xvfb
    POST /api/run           -> ejecuta y devuelve el resultado completo (JSON)
    POST /api/run/stream    -> ejecuta y emite eventos en tiempo real (NDJSON)
    POST /api/input/{id}    -> envía clics/teclas a las ventanas de una ejecución
    GET  /                  -> sirve el frontend estático (si existe)

Arranque en desarrollo:
    uvicorn app.main:app --reload --port 8000      (desde la carpeta backend/)
"""
from __future__ import annotations

import json
import shutil
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .config import settings
from .executor import (
    RunNotFoundError,
    ServerBusyError,
    run_to_completion,
    send_input,
    stream_execution,
)

app = FastAPI(
    title="PyRunner – Intérprete Python con Tkinter",
    version="1.0.0",
    description="Ejecuta código Python (incluido Tkinter) sobre una pantalla virtual Xvfb.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


# ---------------------------------------------------------------------------
# Modelos
# ---------------------------------------------------------------------------
class RunRequest(BaseModel):
    code: str = Field(..., description="Código fuente Python a ejecutar")

    @field_validator("code")
    @classmethod
    def check_size(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("El código está vacío.")
        if len(value.encode("utf-8")) > settings.max_code_bytes:
            raise ValueError(f"El código supera {settings.max_code_bytes} bytes.")
        if "\x00" in value:
            raise ValueError("El código contiene bytes nulos.")
        return value


class InputRequest(BaseModel):
    # Órdenes de ratón/teclado; executor._clean_event valida cada una
    events: list[dict] = Field(..., max_length=200)


class RunResult(BaseModel):
    stdout: str
    stderr: str
    exit_code: int | None
    timed_out: bool
    cpu_exceeded: bool = False
    truncated: bool
    duration: float
    screenshot: str | None = Field(None, description="Última captura PNG en base64")


# ---------------------------------------------------------------------------
# Rutas
# ---------------------------------------------------------------------------
@app.get("/api/health")
async def health() -> dict:
    return {
        "status": "ok",
        "xvfb": shutil.which("xvfb-run") is not None,
        "timeout_seconds": settings.timeout_seconds,
        "gui_timeout_seconds": settings.gui_timeout_seconds,
        "max_concurrent_runs": settings.max_concurrent_runs,
    }


@app.post("/api/run", response_model=RunResult)
async def run(req: RunRequest) -> RunResult:
    """Ejecución simple: espera a que termine y devuelve todo de una vez."""
    try:
        return RunResult(**await run_to_completion(req.code))
    except ServerBusyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/run/stream")
async def run_stream(req: RunRequest) -> StreamingResponse:
    """
    Ejecución en tiempo real. La respuesta es NDJSON: un objeto JSON por línea.
    El cliente lo lee con fetch() + ReadableStream. Si el cliente aborta la
    petición, el generador se cancela y el proceso se mata automáticamente.
    """
    events = stream_execution(req.code)

    # Arrancamos el generador aquí para que "servidor ocupado" pueda responder
    # con un 503 real en lugar de un stream que falla a mitad.
    try:
        first = await events.__anext__()
    except ServerBusyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    async def ndjson() -> AsyncIterator[bytes]:
        try:
            yield (json.dumps(first, ensure_ascii=False) + "\n").encode()
            async for event in events:
                yield (json.dumps(event, ensure_ascii=False) + "\n").encode()
        finally:
            await events.aclose()  # garantiza la limpieza del proceso

    return StreamingResponse(
        ndjson(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},  # sin buffer en nginx
    )


@app.post("/api/input/{run_id}")
async def run_input(run_id: str, req: InputRequest) -> dict:
    """
    Recibe clics, movimientos, rueda y teclas del navegador y los reenvía a la
    pantalla virtual de esa ejecución. El run_id llega en el evento "start".
    """
    try:
        return {"accepted": send_input(run_id, req.events)}
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Frontend estático (opcional): monta / después de las rutas de la API
# ---------------------------------------------------------------------------
if settings.frontend_dir.is_dir():
    app.mount("/", StaticFiles(directory=settings.frontend_dir, html=True), name="frontend")


__all__ = ["app"]
