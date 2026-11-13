"""
Motor de ejecución de código Python con pantalla virtual (Xvfb).

Flujo de una ejecución:

    1. Se reserva un número de display libre del pool (:100, :101, ...).
    2. Se crea un directorio temporal aislado con main.py y frames/.
    3. Se lanza:  xvfb-run -n N -s "-screen 0 WxHxD ..." python -I runner.py ...
       con límites de recursos (setrlimit), entorno mínimo y sesión propia.
    4. Se leen stdout / stderr en paralelo y se emiten como eventos.
    5. Se vigila frames/latest.png y se emite cada captura nueva (base64).
    6. Si vence el timeout o la salida es excesiva, se mata TODO el grupo de
       procesos (script + Xvfb): primero SIGTERM, luego SIGKILL. Cuando el
       programa abre una ventana, el límite pasa de RUN_TIMEOUT a GUI_TIMEOUT
       (el límite de CPU sigue frenando los bucles infinitos).
    7. Se libera el display y se borra el directorio temporal.

La API pública son dos funciones:
    - stream_execution(code)  -> AsyncIterator[dict]   (eventos en tiempo real)
    - run_to_completion(code) -> dict                  (resultado agregado)
"""
from __future__ import annotations

import asyncio
import base64
import codecs
import contextlib
import json
import os
import re
import secrets
import shutil
import signal
import sys
import tempfile
import time
from pathlib import Path
from typing import AsyncIterator

from .config import settings

try:  # `resource` solo existe en Unix
    import resource
except ImportError:  # pragma: no cover
    resource = None  # type: ignore[assignment]


class ServerBusyError(RuntimeError):
    """No hay displays libres en el tiempo de espera configurado."""


class RunNotFoundError(LookupError):
    """La ejecución no existe o ya terminó."""


# ---------------------------------------------------------------------------
# Entrada del usuario (ratón y teclado) hacia las ventanas
# ---------------------------------------------------------------------------
# run_id (secreto, solo lo conoce el cliente que lanzó la ejecución) -> archivo
# input.jsonl que el proceso capture.py lee e inyecta en la pantalla virtual.
_active_runs: dict[str, dict] = {}

MAX_INPUT_BYTES_PER_RUN = 2_000_000
_KEY_NAME = re.compile(r"^[A-Za-z0-9_]{1,24}$")
_MODS = {"Shift", "Control", "Alt", "Meta"}


def _clean_event(ev: dict) -> dict | None:
    """Valida y normaliza una orden de entrada. Devuelve None si no es válida."""
    if not isinstance(ev, dict):
        return None
    kind = ev.get("t")
    try:
        if kind in ("move", "down", "up", "wheel"):
            out = {"t": kind, "x": int(ev["x"]), "y": int(ev["y"])}
            if not (-10_000 < out["x"] < 10_000 and -10_000 < out["y"] < 10_000):
                return None
            if kind in ("down", "up"):
                out["b"] = int(ev.get("b", 1))
                if out["b"] not in (1, 2, 3):
                    return None
            if kind == "wheel":
                out["dy"] = -1 if float(ev.get("dy", 1)) < 0 else 1
            return out
        if kind == "key":
            k = str(ev["k"])
            if not (len(k) == 1 or _KEY_NAME.match(k)):
                return None
            mods = [m for m in ev.get("mods", []) if m in _MODS]
            return {"t": "key", "k": k, "mods": mods}
        if kind == "text":
            text = str(ev["s"])[:500]
            return {"t": "text", "s": text} if text else None
    except (KeyError, TypeError, ValueError):
        return None
    return None


def send_input(run_id: str, events: list) -> int:
    """Añade órdenes válidas al input.jsonl de la ejecución. Devuelve cuántas."""
    run = _active_runs.get(run_id)
    if run is None:
        raise RunNotFoundError("La ejecución no existe o ya terminó.")
    lines = [json.dumps(c, ensure_ascii=False) for c in map(_clean_event, events) if c]
    if not lines:
        return 0
    data = ("\n".join(lines) + "\n").encode("utf-8")
    if run["bytes"] + len(data) > MAX_INPUT_BYTES_PER_RUN:
        raise RunNotFoundError("Límite de entrada alcanzado para esta ejecución.")
    with open(run["path"], "ab") as fh:
        fh.write(data)
    run["bytes"] += len(data)
    return len(lines)


# ---------------------------------------------------------------------------
# Pool de displays virtuales
# ---------------------------------------------------------------------------
class DisplayPool:
    """
    Asigna números de display únicos a cada ejecución concurrente.

    `xvfb-run -a` busca un display libre por sí solo, pero tiene una carrera
    conocida cuando se lanzan varias instancias a la vez. Gestionar el pool
    nosotros mismos lo evita y, de paso, limita la concurrencia.
    """

    def __init__(self, base: int, size: int) -> None:
        self._base = base
        self._size = size
        self._queue: asyncio.Queue[int] | None = None

    def _ensure(self) -> asyncio.Queue[int]:
        # Se crea perezosamente para quedar ligado al event loop activo.
        if self._queue is None:
            self._queue = asyncio.Queue()
            for n in range(self._base, self._base + self._size):
                self._queue.put_nowait(n)
        return self._queue

    async def acquire(self, wait: float) -> int:
        try:
            return await asyncio.wait_for(self._ensure().get(), timeout=wait)
        except asyncio.TimeoutError as exc:
            raise ServerBusyError("Servidor ocupado, inténtalo en unos segundos.") from exc

    def release(self, display: int) -> None:
        self._ensure().put_nowait(display)


display_pool = DisplayPool(settings.display_base, settings.max_concurrent_runs)


# ---------------------------------------------------------------------------
# Preparación del proceso hijo
# ---------------------------------------------------------------------------
def _apply_limits() -> None:
    """
    Se ejecuta en el proceso hijo justo antes de `exec` (preexec_fn).
    Los límites se heredan por xvfb-run, Xvfb y el script del usuario.
    """
    if resource is None:
        return
    mb = 1024 * 1024
    cpu = settings.cpu_limit_seconds  # segundos de CPU por proceso

    limits = [
        # Límite blando < duro: al superar el blando llega SIGXCPU (detectable)
        (resource.RLIMIT_CPU, (cpu, cpu + 2)),
        (resource.RLIMIT_AS, (settings.memory_limit_mb * mb,) * 2),
        (resource.RLIMIT_FSIZE, (settings.file_size_limit_mb * mb,) * 2),
        (resource.RLIMIT_CORE, (0, 0)),
    ]
    if settings.nproc_limit > 0:
        limits.append((resource.RLIMIT_NPROC, (settings.nproc_limit,) * 2))

    for res, value in limits:
        with contextlib.suppress(ValueError, OSError):
            resource.setrlimit(res, value)
    os.umask(0o077)


def _build_env(workdir: str) -> dict[str, str]:
    """Entorno mínimo: no filtramos variables del servidor (tokens, claves...)."""
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": workdir,
        "TMPDIR": workdir,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONUNBUFFERED": "1",          # stdout sin buffer => tiempo real
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def _build_command(display: int, script: Path, frames_dir: Path, stderr_fd: int) -> list[str]:
    xvfb_args = f"-screen 0 {settings.screen} -nolisten tcp -br -noreset"
    return [
        "xvfb-run",
        "-n", str(display),
        "-s", xvfb_args,
        sys.executable, "-I",              # -I: modo aislado (ignora PYTHON*, site de usuario)
        str(settings.runner_path),
        str(script),
        str(frames_dir),
        str(settings.frame_interval),
        str(stderr_fd),
    ]


async def _terminate_group(proc: asyncio.subprocess.Process) -> None:
    """
    Mata el grupo de procesos completo. SIGTERM primero para que Xvfb borre
    su /tmp/.X<n>-lock (si no, el display quedaría "ocupado" para siempre).
    """
    if proc.returncode is not None:
        return
    for sig, grace in ((signal.SIGTERM, 1.5), (signal.SIGKILL, 2.0)):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, sig)
        try:
            await asyncio.wait_for(proc.wait(), timeout=grace)
            return
        except asyncio.TimeoutError:
            continue


# ---------------------------------------------------------------------------
# Ejecución en streaming
# ---------------------------------------------------------------------------
async def stream_execution(code: str) -> AsyncIterator[dict]:
    """
    Ejecuta `code` y produce eventos:

        {"type": "start",  "run_id": "...", "display": 100, "timeout": 10}
        {"type": "stdout", "data": "..."}
        {"type": "stderr", "data": "..."}
        {"type": "gui",    "timeout": 120}          # se detectó una ventana
        {"type": "frame",  "data": "<png base64>" | None, "width": W, "height": H,
                           "left": X0, "top": Y0,
                           "windows": [{"title", "x", "y", "w", "h", "decorated"}]}
        {"type": "exit",   "exit_code": 0, "timed_out": False, "cpu_exceeded": False,
                           "truncated": False, "duration": 1.23}

    Si el consumidor deja de iterar (p. ej. el cliente cierra la conexión),
    el bloque `finally` mata el proceso y limpia todo.
    """
    if shutil.which("xvfb-run") is None:
        yield {"type": "stderr", "data": "Error del servidor: xvfb-run no está instalado.\n"}
        yield {"type": "exit", "exit_code": -1, "timed_out": False, "cpu_exceeded": False,
               "truncated": False, "duration": 0.0}
        return

    display = await display_pool.acquire(settings.queue_wait_seconds)
    workdir = tempfile.mkdtemp(prefix="pyrun-")
    run_id = secrets.token_urlsafe(18)
    proc: asyncio.subprocess.Process | None = None
    stderr_transport: asyncio.ReadTransport | None = None
    events: asyncio.Queue[dict | None] = asyncio.Queue()
    tasks: list[asyncio.Task] = []

    try:
        script = Path(workdir) / "main.py"
        script.write_text(code, encoding="utf-8")
        frames_dir = Path(workdir) / "frames"
        frames_dir.mkdir()
        _active_runs[run_id] = {"path": frames_dir / "input.jsonl", "bytes": 0}

        # xvfb-run ejecuta el comando con "2>&1", lo que mezclaría stderr con
        # stdout. Para conservarlos separados abrimos un pipe extra que el
        # runner usa como su stderr real (os.dup2 sobre el descriptor 2).
        err_read, err_write = os.pipe()

        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *_build_command(display, script, frames_dir, err_write),
                stdin=asyncio.subprocess.DEVNULL,   # input() recibe EOF en vez de colgarse
                stdout=asyncio.subprocess.PIPE,     # stdout del usuario
                stderr=asyncio.subprocess.PIPE,     # mensajes propios de xvfb-run
                cwd=workdir,
                env=_build_env(workdir),
                pass_fds=(err_write,),
                start_new_session=True,             # grupo propio => killpg mata todo
                preexec_fn=_apply_limits,
            )
        finally:
            os.close(err_write)  # el padre solo conserva el extremo de lectura

        loop = asyncio.get_running_loop()
        user_stderr = asyncio.StreamReader()
        stderr_transport, _ = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(user_stderr),
            os.fdopen(err_read, "rb", buffering=0),
        )
        yield {"type": "start", "run_id": run_id, "display": display,
               "timeout": settings.timeout_seconds,
               "gui_timeout": settings.gui_timeout_seconds}

        state = {"bytes": 0, "truncated": False}
        output_limit_hit = asyncio.Event()
        gui_detected = asyncio.Event()

        async def pump(stream: asyncio.StreamReader, kind: str) -> None:
            """Lee un pipe en bloques y lo reenvía como eventos de texto."""
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    tail = decoder.decode(b"", final=True)
                    if tail:
                        await events.put({"type": kind, "data": tail})
                    return
                if state["truncated"]:
                    continue  # seguimos drenando el pipe para no bloquear al hijo
                remaining = settings.max_output_bytes - state["bytes"]
                if len(chunk) > remaining:
                    chunk = chunk[:max(remaining, 0)]
                    state["truncated"] = True
                    output_limit_hit.set()
                state["bytes"] += len(chunk)
                text = decoder.decode(chunk)
                if text:
                    await events.put({"type": kind, "data": text})

        async def watch_frames() -> None:
            """Envía cada nueva captura (imagen + ventanas) de la pantalla virtual."""
            latest = frames_dir / "latest.json"
            last_mtime = 0
            while True:
                await asyncio.sleep(0.05)  # stat() es barato: baja latencia
                try:
                    mtime = latest.stat().st_mtime_ns
                except FileNotFoundError:
                    continue
                if mtime == last_mtime:
                    continue
                last_mtime = mtime
                try:
                    frame = json.loads(latest.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if frame.get("windows"):
                    gui_detected.set()
                await events.put({
                    "type": "frame",
                    "data": frame.get("png"),
                    "width": frame.get("width", 0),
                    "height": frame.get("height", 0),
                    "left": frame.get("left", 0),
                    "top": frame.get("top", 0),
                    "windows": frame.get("windows", []),
                })

        async def supervise() -> None:
            """
            Espera el fin del proceso, el exceso de salida o el timeout. El
            plazo empieza en RUN_TIMEOUT y se amplía a GUI_TIMEOUT (contado
            desde el inicio) cuando aparece la primera ventana.
            """
            waiter = asyncio.create_task(proc.wait())
            limiter = asyncio.create_task(output_limit_hit.wait())
            gui_task = asyncio.create_task(gui_detected.wait())
            deadline = started + settings.timeout_seconds
            timed_out = False

            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                watch = {waiter, limiter} | ({gui_task} if not gui_task.done() else set())
                done, _ = await asyncio.wait(watch, timeout=remaining,
                                             return_when=asyncio.FIRST_COMPLETED)
                if waiter in done or limiter in done:
                    break
                if gui_task in done:
                    deadline = max(deadline, started + settings.gui_timeout_seconds)
                    await events.put({"type": "gui",
                                      "timeout": settings.gui_timeout_seconds})

            if not waiter.done():
                await _terminate_group(proc)
            for t in (limiter, gui_task):
                t.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)  # vaciar pipes

            # xvfb-run devuelve 128 + señal cuando el script muere por una señal
            cpu_exceeded = not timed_out and proc.returncode in (
                128 + signal.SIGXCPU, -signal.SIGXCPU)

            if timed_out:
                limit = (settings.gui_timeout_seconds if gui_detected.is_set()
                         else settings.timeout_seconds)
                await events.put({"type": "stderr", "data":
                    f"\n⏱ Tiempo límite excedido ({limit:g} s). Proceso detenido.\n"})
            if cpu_exceeded:
                await events.put({"type": "stderr", "data":
                    f"\n🔥 Límite de CPU excedido ({settings.cpu_limit_seconds} s de cálculo). "
                    "¿Hay un bucle infinito?\n"})
            if state["truncated"]:
                await events.put({"type": "stderr", "data":
                    "\n✂ Salida demasiado larga: ejecución detenida.\n"})
            await events.put({
                "type": "exit",
                "exit_code": proc.returncode,
                "timed_out": timed_out,
                "cpu_exceeded": cpu_exceeded,
                "truncated": state["truncated"],
                "duration": round(time.monotonic() - started, 3),
            })
            await events.put(None)  # fin del stream

        pumps = [
            asyncio.create_task(pump(proc.stdout, "stdout")),
            asyncio.create_task(pump(user_stderr, "stderr")),
            asyncio.create_task(pump(proc.stderr, "stderr")),
        ]
        frame_task = asyncio.create_task(watch_frames())
        tasks = [*pumps, frame_task, asyncio.create_task(supervise())]

        while (event := await events.get()) is not None:
            if event["type"] == "exit":
                frame_task.cancel()  # no enviar capturas después del final
            yield event

    finally:
        _active_runs.pop(run_id, None)
        for t in tasks:
            t.cancel()
        if proc is not None:
            await _terminate_group(proc)
        if stderr_transport is not None:
            stderr_transport.close()
        shutil.rmtree(workdir, ignore_errors=True)
        display_pool.release(display)


async def run_to_completion(code: str) -> dict:
    """Versión no interactiva: acumula todo y devuelve un único resultado."""
    stdout: list[str] = []
    stderr: list[str] = []
    last_frame: str | None = None
    result: dict = {}

    async for event in stream_execution(code):
        kind = event["type"]
        if kind == "stdout":
            stdout.append(event["data"])
        elif kind == "stderr":
            stderr.append(event["data"])
        elif kind == "frame" and event.get("data"):
            last_frame = event["data"]
        elif kind == "exit":
            result = event

    return {
        "stdout": "".join(stdout),
        "stderr": "".join(stderr),
        "exit_code": result.get("exit_code"),
        "timed_out": result.get("timed_out", False),
        "cpu_exceeded": result.get("cpu_exceeded", False),
        "truncated": result.get("truncated", False),
        "duration": result.get("duration", 0.0),
        "screenshot": last_frame,  # PNG en base64 (o None si no hubo GUI)
    }
