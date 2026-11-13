"""
Configuración central del servicio.

Todos los valores se pueden sobreescribir con variables de entorno, lo que
permite ajustar límites sin tocar el código (por ejemplo desde docker-compose).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


def _env_float(name: str, default: float) -> float:
    return float(os.getenv(name, default))


@dataclass(frozen=True)
class Settings:
    # --- Límites de ejecución -------------------------------------------
    # Tiempo máximo (segundos de reloj) que puede vivir un script.
    timeout_seconds: float = _env_float("RUN_TIMEOUT", 10.0)
    # Tiempo máximo cuando el programa abre una ventana (Tkinter). Se activa
    # en cuanto aparece la primera ventana; antes rige RUN_TIMEOUT.
    gui_timeout_seconds: float = _env_float("GUI_TIMEOUT", 120.0)
    # Segundos de CPU que puede consumir el script. Una app Tkinter esperando
    # eventos casi no gasta CPU, pero un bucle infinito sí: este límite lo
    # detiene aunque haya una ventana abierta.
    cpu_limit_seconds: int = _env_int("CPU_LIMIT", 20)
    # Tamaño máximo del código recibido (bytes).
    max_code_bytes: int = _env_int("MAX_CODE_BYTES", 100_000)
    # Máximo de bytes de salida (stdout + stderr) antes de cortar el proceso.
    max_output_bytes: int = _env_int("MAX_OUTPUT_BYTES", 200_000)
    # Memoria virtual máxima por proceso (MB). Xvfb también la hereda.
    memory_limit_mb: int = _env_int("MEMORY_LIMIT_MB", 1024)
    # Tamaño máximo de archivo que el script puede escribir (MB).
    file_size_limit_mb: int = _env_int("FILE_SIZE_LIMIT_MB", 10)
    # Límite de procesos del usuario (0 = no aplicar). Úsalo solo si el
    # sandbox corre con un usuario dedicado; en Docker preferimos pids_limit.
    nproc_limit: int = _env_int("NPROC_LIMIT", 0)

    # --- Concurrencia -----------------------------------------------------
    # Ejecuciones simultáneas permitidas (cada una con su propio display X).
    max_concurrent_runs: int = _env_int("MAX_CONCURRENT_RUNS", 4)
    # Segundos que una petición espera un hueco libre antes de devolver 503.
    queue_wait_seconds: float = _env_float("QUEUE_WAIT_SECONDS", 5.0)
    # Primer número de display virtual (:100, :101, ...).
    display_base: int = _env_int("DISPLAY_BASE", 100)

    # --- Pantalla virtual -------------------------------------------------
    screen: str = os.getenv("XVFB_SCREEN", "1024x768x24")
    # Cada cuántos segundos se captura la pantalla virtual.
    frame_interval: float = _env_float("FRAME_INTERVAL", 0.4)

    # --- Rutas y red ------------------------------------------------------
    runner_path: Path = Path(__file__).resolve().parent.parent / "sandbox" / "runner.py"
    frontend_dir: Path = Path(
        os.getenv("FRONTEND_DIR", Path(__file__).resolve().parents[2] / "frontend")
    )
    cors_origins: list[str] = field(
        default_factory=lambda: [
            o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()
        ]
    )


settings = Settings()
