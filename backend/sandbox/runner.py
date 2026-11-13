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

import ctypes
import ctypes.util
import hashlib
import io
import os
import runpy
import sys
import threading
import time
import traceback


# ---------------------------------------------------------------------------
# Captura de pantalla
# ---------------------------------------------------------------------------
class XlibGrabber:
    """
    Captura la pantalla con libX11 vía ctypes (XGetImage).

    No depende de que Pillow se haya compilado con soporte XCB: usa la misma
    libX11 que ya carga Tkinter, así que si Tk puede abrir una ventana, esto
    puede capturarla. Pillow solo se usa para codificar el PNG.
    """

    ZPIXMAP = 2
    ALL_PLANES = ctypes.c_ulong(0xFFFFFFFF)

    class _XImage(ctypes.Structure):
        _fields_ = [
            ("width", ctypes.c_int), ("height", ctypes.c_int),
            ("xoffset", ctypes.c_int), ("format", ctypes.c_int),
            ("data", ctypes.c_void_p),
            ("byte_order", ctypes.c_int), ("bitmap_unit", ctypes.c_int),
            ("bitmap_bit_order", ctypes.c_int), ("bitmap_pad", ctypes.c_int),
            ("depth", ctypes.c_int), ("bytes_per_line", ctypes.c_int),
            ("bits_per_pixel", ctypes.c_int),
            ("red_mask", ctypes.c_ulong), ("green_mask", ctypes.c_ulong),
            ("blue_mask", ctypes.c_ulong), ("obdata", ctypes.c_void_p),
            # struct funcs: create_image, destroy_image, get_pixel, put_pixel,
            # sub_image, add_pixel
            ("f_create", ctypes.c_void_p), ("f_destroy", ctypes.c_void_p),
            ("f_get", ctypes.c_void_p), ("f_put", ctypes.c_void_p),
            ("f_sub", ctypes.c_void_p), ("f_add", ctypes.c_void_p),
        ]

    def __init__(self, display: str | None) -> None:
        from PIL import Image  # solo para construir/codificar la imagen

        self._Image = Image
        name = ctypes.util.find_library("X11") or "libX11.so.6"
        x = ctypes.CDLL(name)
        x.XOpenDisplay.restype = ctypes.c_void_p
        x.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x.XDefaultScreen.argtypes = [ctypes.c_void_p]
        x.XRootWindow.restype = ctypes.c_ulong
        x.XRootWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        x.XDisplayWidth.argtypes = [ctypes.c_void_p, ctypes.c_int]
        x.XDisplayHeight.argtypes = [ctypes.c_void_p, ctypes.c_int]
        x.XGetImage.restype = ctypes.POINTER(self._XImage)
        x.XGetImage.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
            ctypes.c_uint, ctypes.c_uint, ctypes.c_ulong, ctypes.c_int,
        ]
        self._x = x

        self._dpy = x.XOpenDisplay(display.encode() if display else None)
        if not self._dpy:
            raise RuntimeError(f"no se pudo abrir el display {display!r}")
        screen = x.XDefaultScreen(self._dpy)
        self._root = x.XRootWindow(self._dpy, screen)
        self._w = x.XDisplayWidth(self._dpy, screen)
        self._h = x.XDisplayHeight(self._dpy, screen)

    def grab(self):
        ptr = self._x.XGetImage(self._dpy, self._root, 0, 0, self._w, self._h,
                                self.ALL_PLANES, self.ZPIXMAP)
        if not ptr:
            raise RuntimeError("XGetImage devolvió NULL")
        img = ptr.contents
        try:
            if img.bits_per_pixel != 32:
                raise RuntimeError(f"profundidad no soportada: {img.bits_per_pixel} bpp")
            raw = ctypes.string_at(img.data, img.bytes_per_line * img.height)
            # Xvfb a 24 bits guarda los píxeles como B, G, R, X (little endian)
            return self._Image.frombuffer(
                "RGB", (img.width, img.height), raw, "raw", "BGRX", img.bytes_per_line, 1
            )
        finally:
            # XDestroyImage es una macro de C: llamamos al puntero de la estructura
            destroy = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p)(img.f_destroy)
            destroy(ctypes.cast(ptr, ctypes.c_void_p))


class PillowGrabber:
    """Respaldo: ImageGrab de Pillow (requiere Pillow compilado con XCB)."""

    def __init__(self, display: str | None) -> None:
        from PIL import ImageGrab

        self._grab = ImageGrab.grab
        self._display = display
        self.grab()  # falla aquí si Pillow no tiene soporte XCB

    def grab(self):
        return self._grab(xdisplay=self._display)


def _make_grabber(display: str | None):
    errors = []
    for cls in (XlibGrabber, PillowGrabber):
        try:
            return cls(display)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{cls.__name__}: {exc}")
    raise RuntimeError("; ".join(errors))


def _warn(msg: str) -> None:
    """Aviso del sandbox por stderr (visible en la consola del usuario)."""
    try:
        sys.stderr.write(f"[sandbox] {msg}\n")
        sys.stderr.flush()
    except Exception:
        pass


def _capture_loop(frames_dir: str, interval: float) -> None:
    """Captura la pantalla del display X actual cada `interval` segundos."""
    display = os.environ.get("DISPLAY")
    target = os.path.join(frames_dir, "latest.png")
    tmp = os.path.join(frames_dir, ".latest.tmp")
    last_digest = None
    grabber = None
    failures = 0

    while True:
        time.sleep(interval)
        try:
            if grabber is None:
                grabber = _make_grabber(display)

            img = grabber.grab()
            # Sin gestor de ventanas el fondo es negro (-br): recortamos al
            # área ocupada por las ventanas para que la vista sea compacta.
            bbox = img.getbbox()
            if bbox is None:
                continue  # pantalla vacía, nada que mostrar todavía
            img = img.crop(bbox)

            buf = io.BytesIO()
            img.save(buf, format="PNG", compress_level=3)
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
            failures = 0
        except Exception as exc:  # noqa: BLE001
            # Al terminar el programa el display desaparece: eso es normal.
            # Si falla varias veces seguidas, avisamos una vez y desistimos.
            failures += 1
            grabber = None
            if failures == 3:
                _warn(f"vista gráfica no disponible ({exc})")
                return


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
