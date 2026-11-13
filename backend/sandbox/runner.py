"""
Proceso "envoltorio" que se ejecuta DENTRO de xvfb-run.

    xvfb-run -n <display> python -I runner.py <script.py> <frames_dir> <intervalo> <fd_stderr>

Responsabilidades:
  1. Ejecutar el script del usuario como si fuera `__main__`.
  2. Lanzar capture.py, un proceso aparte que inspecciona la pantalla virtual
     (ventanas abiertas, títulos, posiciones y una captura) y lo guarda en
     <frames_dir>/latest.json. El backend vigila ese archivo y lo envía al
     navegador, que dibuja cada ventana con su barra de título.
  3. Imprimir los tracebacks limpios (sin las líneas internas de este runner).

Este archivo NO aplica medidas de seguridad: los límites (tiempo, memoria,
procesos, etc.) los impone el backend antes de lanzar el proceso.
"""
from __future__ import annotations

import os
import runpy
import subprocess
import sys
import traceback

CAPTURE_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "capture.py")

# ---------------------------------------------------------------------------
# Ejecución del script del usuario
# ---------------------------------------------------------------------------
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

    # Captura de pantalla en un proceso separado (ver capture.py)
    capture = subprocess.Popen(
        [sys.executable, "-I", CAPTURE_SCRIPT, frames_dir, str(interval)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,  # sus avisos van a stderr (consola del usuario)
    )

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
        capture.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
