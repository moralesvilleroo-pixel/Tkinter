"""
Proceso "envoltorio" que se ejecuta DENTRO de xvfb-run.

    xvfb-run -n <display> python -I runner.py <script.py> <frames_dir> <intervalo> <fd_stderr>

Responsabilidades:
  1. Ejecutar el script del usuario como si fuera `__main__`.
  2. En un hilo en segundo plano, capturar periódicamente la pantalla virtual
     (Xvfb) y guardarla como PNG en <frames_dir>/latest.png. El backend vigila
     ese archivo y lo envía al navegador: así el usuario "ve" su ventana Tkinter.
  3. Imprimir los tracebacks limpios (sin las líneas internas de este runner).

Este archivo NO aplica medidas de seguridad: los límites (tiempo, memoria,
procesos, etc.) los impone el backend antes de lanzar el proceso.
"""
from __future__ import annotations

import hashlib
import io
import os
import runpy
import sys
import threading
import time
import traceback


def _capture_loop(frames_dir: str, interval: float) -> None:
    """Captura la pantalla del display X actual cada `interval` segundos."""
    try:
        from PIL import ImageGrab  # Pillow con soporte XCB
    except Exception:  # pragma: no cover - depende del entorno
        sys.__stderr__.write("[sandbox] Pillow no disponible: vista gráfica desactivada\n")
        return

    display = os.environ.get("DISPLAY")
    target = os.path.join(frames_dir, "latest.png")
    tmp = os.path.join(frames_dir, ".latest.tmp")
    last_digest = None

    while True:
        time.sleep(interval)
        try:
            img = ImageGrab.grab(xdisplay=display)
            # Sin gestor de ventanas el fondo es negro (-br): recortamos al
            # área ocupada por las ventanas para que la vista sea compacta.
            bbox = img.getbbox()
            if bbox is None:
                continue  # pantalla vacía, nada que mostrar todavía
            img = img.crop(bbox)

            buf = io.BytesIO()
            img.save(buf, format="PNG", optimize=False)
            data = buf.getvalue()

            # Solo escribimos si la imagen cambió (ahorra ancho de banda).
            digest = hashlib.blake2b(data, digest_size=16).digest()
            if digest == last_digest:
                continue
            last_digest = digest

            # Escritura atómica: el backend nunca lee un PNG a medio escribir.
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, target)
        except Exception:
            # El display puede desaparecer al terminar; no es un error del usuario.
            continue


def _print_user_traceback(exc: BaseException) -> None:
    """Imprime el traceback omitiendo los frames de runner.py y runpy."""
    tb = exc.__traceback__
    def is_internal(filename: str) -> bool:
        return (
            os.path.abspath(filename) in (os.path.abspath(__file__), runpy.__file__)
            or filename.startswith("<frozen runpy")
        )

    while tb is not None and is_internal(tb.tb_frame.f_code.co_filename):
        tb = tb.tb_next
    traceback.print_exception(type(exc), exc, tb, file=sys.stderr)


def main() -> int:
    if len(sys.argv) != 5:
        sys.stderr.write("uso: runner.py <script> <frames_dir> <intervalo> <fd_stderr>\n")
        return 2

    script, frames_dir, interval = sys.argv[1], sys.argv[2], float(sys.argv[3])

    # xvfb-run redirige stderr a stdout ("2>&1"). Restauramos un stderr propio
    # usando el pipe que nos pasa el backend; dup2 afecta también a la salida
    # de librerías en C (por ejemplo los errores de Tcl/Tk).
    stderr_fd = int(sys.argv[4])
    sys.stderr.flush()
    os.dup2(stderr_fd, 2)
    os.close(stderr_fd)

    threading.Thread(
        target=_capture_loop, args=(frames_dir, interval), daemon=True, name="frame-capture"
    ).start()

    # El script ve un entorno "normal": argv limpio, su carpeta como cwd y en
    # sys.path, y nombre relativo (los tracebacks muestran "main.py").
    workdir, filename = os.path.split(os.path.abspath(script))
    os.chdir(workdir)
    sys.argv = [filename]
    sys.path.insert(0, workdir)

    try:
        runpy.run_path(filename, run_name="__main__")
    except SystemExit as exc:  # respeta sys.exit(n) del usuario
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        print(code, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except BaseException as exc:  # noqa: BLE001 - queremos reportar todo
        _print_user_traceback(exc)
        return 1
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
