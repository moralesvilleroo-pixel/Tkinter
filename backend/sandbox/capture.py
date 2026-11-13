"""
Proceso de captura de la pantalla virtual. Lo lanza runner.py:

    python -I capture.py <frames_dir> <intervalo>

Corre en un proceso APARTE del script del usuario por dos motivos:
  * El manejador de errores de Xlib es global por proceso; instalar el nuestro
    junto al de Tkinter podría romper el manejo de errores de Tk.
  * Su consumo de CPU no cuenta contra el límite de CPU del script.

Cada `intervalo` segundos escribe <frames_dir>/latest.json con:
    {"png": <base64>, "width": W, "height": H, "left": X0, "top": Y0,
     "windows": [{"id", "title", "x", "y", "w", "h", "decorated"}, ...]}
(x/y de cada ventana son relativos a left/top, el origen de la captura).

Además, un hilo lee <frames_dir>/input.jsonl, donde el backend añade los
clics y teclas del navegador, y los inyecta con XTEST (ver x11input.py).
Tras cada interacción se captura enseguida para que la respuesta se note.

Termina solo cuando el display desaparece o su proceso padre muere.
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.util
import hashlib
import io
import json
import os
import sys
import threading
import time

# Con -I (modo aislado) la carpeta del script no está en sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------------------
# Acceso a X11 mediante ctypes
# ---------------------------------------------------------------------------
c_ulong, c_int, c_uint, c_long, c_void_p = (
    ctypes.c_ulong, ctypes.c_int, ctypes.c_uint, ctypes.c_long, ctypes.c_void_p
)


class _XImage(ctypes.Structure):
    _fields_ = [
        ("width", c_int), ("height", c_int), ("xoffset", c_int), ("format", c_int),
        ("data", c_void_p),
        ("byte_order", c_int), ("bitmap_unit", c_int), ("bitmap_bit_order", c_int),
        ("bitmap_pad", c_int), ("depth", c_int), ("bytes_per_line", c_int),
        ("bits_per_pixel", c_int),
        ("red_mask", c_ulong), ("green_mask", c_ulong), ("blue_mask", c_ulong),
        ("obdata", c_void_p),
        # struct funcs { create_image, destroy_image, get_pixel, put_pixel, sub_image, add_pixel }
        ("f_create", c_void_p), ("f_destroy", c_void_p), ("f_get", c_void_p),
        ("f_put", c_void_p), ("f_sub", c_void_p), ("f_add", c_void_p),
    ]


class _XWindowAttributes(ctypes.Structure):
    _fields_ = [
        ("x", c_int), ("y", c_int), ("width", c_int), ("height", c_int),
        ("border_width", c_int), ("depth", c_int), ("visual", c_void_p),
        ("root", c_ulong), ("class_", c_int), ("bit_gravity", c_int),
        ("win_gravity", c_int), ("backing_store", c_int), ("backing_planes", c_ulong),
        ("backing_pixel", c_ulong), ("save_under", c_int), ("colormap", c_ulong),
        ("map_installed", c_int), ("map_state", c_int), ("all_event_masks", c_long),
        ("your_event_mask", c_long), ("do_not_propagate_mask", c_long),
        ("override_redirect", c_int), ("screen", c_void_p),
    ]


IS_VIEWABLE = 2
ZPIXMAP = 2
ALL_PLANES = 0xFFFFFFFF


class X11Screen:
    """
    Lee la pantalla virtual con libX11 (la misma librería que usa Tkinter).

    Sin gestor de ventanas, cada ventana principal de Tk (tk.Tk / Toplevel) es
    hija directa de la ventana raíz, así que basta con recorrer esos hijos.
    """

    def __init__(self, display: str | None) -> None:
        from PIL import Image  # solo para construir/codificar la imagen

        self._Image = Image
        x = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
        P = ctypes.POINTER

        x.XOpenDisplay.restype = c_void_p
        x.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x.XDefaultScreen.argtypes = [c_void_p]
        x.XRootWindow.restype = c_ulong
        x.XRootWindow.argtypes = [c_void_p, c_int]
        x.XDisplayWidth.argtypes = [c_void_p, c_int]
        x.XDisplayHeight.argtypes = [c_void_p, c_int]
        x.XQueryTree.argtypes = [c_void_p, c_ulong, P(c_ulong), P(c_ulong),
                                 P(P(c_ulong)), P(c_uint)]
        x.XGetWindowAttributes.argtypes = [c_void_p, c_ulong, P(_XWindowAttributes)]
        x.XInternAtom.restype = c_ulong
        x.XInternAtom.argtypes = [c_void_p, ctypes.c_char_p, c_int]
        x.XGetWindowProperty.argtypes = [
            c_void_p, c_ulong, c_ulong, c_long, c_long, c_int, c_ulong,
            P(c_ulong), P(c_int), P(c_ulong), P(c_ulong), P(c_void_p),
        ]
        x.XFetchName.argtypes = [c_void_p, c_ulong, P(c_void_p)]
        x.XFree.argtypes = [c_void_p]
        x.XGetImage.restype = P(_XImage)
        x.XGetImage.argtypes = [c_void_p, c_ulong, c_int, c_int, c_uint, c_uint,
                                c_ulong, c_int]
        x.XSetErrorHandler.restype = c_void_p
        self._x = x

        # Una ventana puede desaparecer mientras la leemos: ignoramos esos
        # errores de X (BadWindow, BadMatch) en lugar de abortar el proceso.
        handler_type = ctypes.CFUNCTYPE(c_int, c_void_p, c_void_p)
        self._error_handler = handler_type(lambda _d, _e: 0)  # mantener referencia
        x.XSetErrorHandler(self._error_handler)

        self._dpy = x.XOpenDisplay(display.encode() if display else None)
        if not self._dpy:
            raise RuntimeError(f"no se pudo abrir el display {display!r}")
        screen = x.XDefaultScreen(self._dpy)
        self.root = x.XRootWindow(self._dpy, screen)
        self.width = x.XDisplayWidth(self._dpy, screen)
        self.height = x.XDisplayHeight(self._dpy, screen)
        self._net_wm_name = x.XInternAtom(self._dpy, b"_NET_WM_NAME", 0)
        self._utf8 = x.XInternAtom(self._dpy, b"UTF8_STRING", 0)

    # ------------------------------------------------------------------ títulos
    def _title(self, win: int) -> str:
        """Título de la ventana: _NET_WM_NAME (UTF-8) o, si no, WM_NAME."""
        x, dpy = self._x, self._dpy
        actual_type, actual_format = c_ulong(), c_int()
        nitems, after, prop = c_ulong(), c_ulong(), c_void_p()
        ok = x.XGetWindowProperty(dpy, win, self._net_wm_name, 0, 1024, 0, self._utf8,
                                  ctypes.byref(actual_type), ctypes.byref(actual_format),
                                  ctypes.byref(nitems), ctypes.byref(after),
                                  ctypes.byref(prop))
        if ok == 0 and prop.value:
            try:
                if nitems.value:
                    return ctypes.string_at(prop.value, nitems.value).decode("utf-8", "replace")
            finally:
                x.XFree(prop)

        name = c_void_p()
        if x.XFetchName(dpy, win, ctypes.byref(name)) and name.value:
            try:
                return ctypes.string_at(name.value).decode("latin-1")
            finally:
                x.XFree(name)
        return ""

    # ----------------------------------------------------------------- ventanas
    def windows(self) -> list[dict]:
        """Ventanas visibles de primer nivel, de la más baja a la más alta."""
        x, dpy = self._x, self._dpy
        root_ret, parent_ret = c_ulong(), c_ulong()
        children = ctypes.POINTER(c_ulong)()
        count = c_uint()
        if not x.XQueryTree(dpy, self.root, ctypes.byref(root_ret), ctypes.byref(parent_ret),
                            ctypes.byref(children), ctypes.byref(count)):
            return []
        ids = [children[i] for i in range(count.value)]
        if children:
            x.XFree(children)

        result = []
        for win in ids:  # XQueryTree devuelve el orden de apilado (abajo → arriba)
            attrs = _XWindowAttributes()
            if not x.XGetWindowAttributes(dpy, win, ctypes.byref(attrs)):
                continue
            if attrs.map_state != IS_VIEWABLE or attrs.width < 2 or attrs.height < 2:
                continue
            # Recortar a los límites de la pantalla
            x0, y0 = max(attrs.x, 0), max(attrs.y, 0)
            x1 = min(attrs.x + attrs.width, self.width)
            y1 = min(attrs.y + attrs.height, self.height)
            if x1 <= x0 or y1 <= y0:
                continue
            result.append({
                "id": int(win),
                "title": self._title(win),
                "x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0,
                # overrideredirect(True), menús emergentes y tooltips: sin marco
                "decorated": not attrs.override_redirect,
            })
        return result

    # ------------------------------------------------------------------ captura
    def grab(self, left: int, top: int, width: int, height: int):
        x = self._x
        ptr = x.XGetImage(self._dpy, self.root, left, top, width, height,
                          ALL_PLANES, ZPIXMAP)
        if not ptr:
            raise RuntimeError("XGetImage devolvió NULL")
        img = ptr.contents
        try:
            if img.bits_per_pixel != 32:
                raise RuntimeError(f"profundidad no soportada: {img.bits_per_pixel} bpp")
            raw = ctypes.string_at(img.data, img.bytes_per_line * img.height)
            stride = img.bytes_per_line
        finally:
            # XDestroyImage es una macro de C: llamamos al puntero de la estructura
            destroy = ctypes.CFUNCTYPE(c_int, c_void_p)(img.f_destroy)
            destroy(ctypes.cast(ptr, c_void_p))
        return raw, stride


# ---------------------------------------------------------------------------
# Bucle de captura
# ---------------------------------------------------------------------------
def _warn(msg: str) -> None:
    """Aviso del sandbox por stderr (visible en la consola del usuario)."""
    try:
        sys.stderr.write(f"[sandbox] {msg}\n")
        sys.stderr.flush()
    except Exception:
        pass


def _write_atomic(path: str, payload: dict) -> None:
    """Escritura atómica: el backend nunca lee un archivo a medio escribir."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    os.replace(tmp, path)


# Despierta el bucle de captura cuando llega una interacción del usuario
_wake = threading.Event()
_last_input = 0.0


def _input_loop(frames_dir: str) -> None:
    """Sigue input.jsonl (como `tail -f`) e inyecta cada orden en el display."""
    global _last_input
    from x11input import Injector

    path = os.path.join(frames_dir, "input.jsonl")
    display = os.environ.get("DISPLAY")
    injector = None
    pos = 0
    pending = b""
    while True:
        try:
            with open(path, "rb") as fh:
                fh.seek(pos)
                chunk = fh.read()
        except FileNotFoundError:
            chunk = b""
        if not chunk:
            time.sleep(0.015)
            continue
        pos += len(chunk)
        lines = (pending + chunk).split(b"\n")
        pending = lines.pop()  # última línea incompleta (si la hay)
        for line in lines:
            if not line.strip():
                continue
            try:
                if injector is None:
                    injector = Injector(display)
                injector.handle(json.loads(line))
            except Exception as exc:  # noqa: BLE001
                _warn(f"entrada ignorada ({exc})")
        _last_input = time.monotonic()
        _wake.set()


def _capture_loop(frames_dir: str, interval: float) -> None:
    """
    Cada `interval` segundos:
      1. lista las ventanas visibles;
      2. captura solo el rectángulo que las contiene;
      3. si algo cambió, escribe latest.json con la imagen (PNG base64) y la
         geometría de cada ventana relativa a ese rectángulo.
    Para ahorrar CPU, el PNG solo se codifica cuando los píxeles cambian.
    """
    display = os.environ.get("DISPLAY")
    target = os.path.join(frames_dir, "latest.json")
    screen = None
    failures = 0
    last_digest = None
    had_windows = False

    parent = os.getppid()
    while True:
        # Durante 1.5 s tras una interacción capturamos más seguido
        fast = time.monotonic() - _last_input < 1.5
        if _wake.wait(0.1 if fast else interval):
            _wake.clear()
            time.sleep(0.04)  # dar tiempo a Tk para redibujar
        if os.getppid() != parent:
            return  # el runner terminó: no seguir vivo
        try:
            if screen is None:
                screen = X11Screen(display)

            wins = screen.windows()
            if not wins:
                if had_windows:  # todas las ventanas se cerraron
                    had_windows = False
                    last_digest = None
                    _write_atomic(target, {"png": None, "width": 0, "height": 0,
                                           "left": 0, "top": 0, "windows": []})
                continue

            left = min(w["x"] for w in wins)
            top = min(w["y"] for w in wins)
            right = max(w["x"] + w["w"] for w in wins)
            bottom = max(w["y"] + w["h"] for w in wins)
            width, height = right - left, bottom - top

            raw, stride = screen.grab(left, top, width, height)
            meta = [{**w, "x": w["x"] - left, "y": w["y"] - top} for w in wins]

            digest = hashlib.blake2b(raw, digest_size=16)
            digest.update(json.dumps(meta, sort_keys=True).encode())
            digest = digest.digest()
            if digest == last_digest:
                continue  # nada cambió: no recodificar
            last_digest = digest

            # Xvfb a 24 bits guarda los píxeles como B, G, R, X (little endian)
            img = screen._Image.frombuffer("RGB", (width, height), raw, "raw",
                                           "BGRX", stride, 1)
            buf = io.BytesIO()
            img.save(buf, format="PNG", compress_level=3)

            _write_atomic(target, {
                "png": base64.b64encode(buf.getvalue()).decode("ascii"),
                "width": width,
                "height": height,
                "left": left,
                "top": top,
                "windows": meta,
            })
            had_windows = True
            failures = 0
        except Exception as exc:  # noqa: BLE001
            # Al terminar el programa el display desaparece: eso es normal.
            # Si falla varias veces seguidas, avisamos una vez y desistimos.
            failures += 1
            screen = None
            if failures == 3:
                _warn(f"vista gráfica no disponible ({exc})")
                return



def main() -> int:
    frames_dir, interval = sys.argv[1], float(sys.argv[2])
    # Dos hilos usan Xlib (captura y entrada): hay que activar su modo seguro
    lib = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
    lib.XInitThreads()
    threading.Thread(target=_input_loop, args=(frames_dir,), daemon=True).start()
    _capture_loop(frames_dir, interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
