"""
Inyección de ratón y teclado en la pantalla virtual mediante la extensión
XTEST de X11 (la misma técnica que usa `xdotool`).

Lo usa capture.py: un hilo lee las órdenes que el backend va añadiendo a
<frames_dir>/input.jsonl (una orden JSON por línea) y las ejecuta aquí.

Órdenes soportadas (ya validadas por el backend):
    {"t": "move",  "x": 10, "y": 20}
    {"t": "down",  "b": 1, "x": 10, "y": 20}        # b: 1 izq, 2 medio, 3 der
    {"t": "up",    "b": 1, "x": 10, "y": 20}
    {"t": "wheel", "dy": -1, "x": 10, "y": 20}      # -1 arriba, +1 abajo
    {"t": "key",   "k": "Return", "mods": ["Control"]}
    {"t": "text",  "s": "Hola ñ"}
"""
from __future__ import annotations

import ctypes
import ctypes.util
import time

c_ulong, c_int, c_uint, c_void_p = ctypes.c_ulong, ctypes.c_int, ctypes.c_uint, ctypes.c_void_p
P = ctypes.POINTER

NO_SYMBOL = 0
REVERT_TO_PARENT = 2
CURRENT_TIME = 0

MODIFIERS = {"Shift": "Shift_L", "Control": "Control_L", "Alt": "Alt_L", "Meta": "Super_L"}


class Injector:
    def __init__(self, display: str | None) -> None:
        x = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
        t = ctypes.CDLL(ctypes.util.find_library("Xtst") or "libXtst.so.6")

        x.XOpenDisplay.restype = c_void_p
        x.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x.XDefaultScreen.argtypes = [c_void_p]
        x.XRootWindow.restype = c_ulong
        x.XRootWindow.argtypes = [c_void_p, c_int]
        x.XDisplayWidth.argtypes = [c_void_p, c_int]
        x.XDisplayHeight.argtypes = [c_void_p, c_int]
        x.XDisplayKeycodes.argtypes = [c_void_p, P(c_int), P(c_int)]
        x.XGetKeyboardMapping.restype = P(c_ulong)
        x.XGetKeyboardMapping.argtypes = [c_void_p, ctypes.c_ubyte, c_int, P(c_int)]
        x.XChangeKeyboardMapping.argtypes = [c_void_p, c_int, c_int, P(c_ulong), c_int]
        x.XStringToKeysym.restype = c_ulong
        x.XStringToKeysym.argtypes = [ctypes.c_char_p]
        x.XTranslateCoordinates.argtypes = [c_void_p, c_ulong, c_ulong, c_int, c_int,
                                            P(c_int), P(c_int), P(c_ulong)]
        x.XSetInputFocus.argtypes = [c_void_p, c_ulong, c_int, c_ulong]
        x.XFlush.argtypes = [c_void_p]
        x.XSync.argtypes = [c_void_p, c_int]
        x.XFree.argtypes = [c_void_p]
        t.XTestFakeMotionEvent.argtypes = [c_void_p, c_int, c_int, c_int, c_ulong]
        t.XTestFakeButtonEvent.argtypes = [c_void_p, c_uint, c_int, c_ulong]
        t.XTestFakeKeyEvent.argtypes = [c_void_p, c_uint, c_int, c_ulong]
        self._x, self._t = x, t

        self._dpy = x.XOpenDisplay(display.encode() if display else None)
        if not self._dpy:
            raise RuntimeError(f"no se pudo abrir el display {display!r}")
        screen = x.XDefaultScreen(self._dpy)
        self._root = x.XRootWindow(self._dpy, screen)
        self._w = x.XDisplayWidth(self._dpy, screen)
        self._h = x.XDisplayHeight(self._dpy, screen)
        self._load_keymap()

    # ------------------------------------------------------------- teclado
    def _load_keymap(self) -> None:
        """Construye keysym -> (keycode, necesita Shift) y busca teclas libres."""
        x = self._x
        lo, hi = c_int(), c_int()
        x.XDisplayKeycodes(self._dpy, ctypes.byref(lo), ctypes.byref(hi))
        count = hi.value - lo.value + 1
        per = c_int()
        syms = x.XGetKeyboardMapping(self._dpy, lo.value, count, ctypes.byref(per))
        self._keys: dict[int, tuple[int, bool]] = {}
        self._spare: list[int] = []
        for i in range(count):
            kc = lo.value + i
            row = [syms[i * per.value + j] for j in range(per.value)]
            if all(s == NO_SYMBOL for s in row):
                self._spare.append(kc)
                continue
            if row[0] != NO_SYMBOL:
                self._keys.setdefault(row[0], (kc, False))
            if per.value > 1 and row[1] != NO_SYMBOL:
                self._keys.setdefault(row[1], (kc, True))
        x.XFree(syms)
        self._next_spare = 0

    def _keycode(self, keysym: int) -> tuple[int, bool]:
        """
        Keycode para un keysym. Si el teclado virtual (US) no tiene esa tecla,
        por ejemplo "ñ", "á" o "€", la asignamos a un keycode libre.
        """
        if keysym in self._keys:
            return self._keys[keysym]
        if not self._spare:
            raise RuntimeError("no hay teclas libres para remapear")
        kc = self._spare[self._next_spare % len(self._spare)]
        self._next_spare += 1
        # Liberar la asociación anterior de ese keycode
        for ks, (code, _) in list(self._keys.items()):
            if code == kc:
                del self._keys[ks]
        pair = (c_ulong * 2)(keysym, keysym)
        self._x.XChangeKeyboardMapping(self._dpy, kc, 2, pair, 1)
        self._x.XSync(self._dpy, 0)
        time.sleep(0.03)  # dar tiempo a Tk para procesar MappingNotify
        self._keys[keysym] = (kc, False)
        return kc, False

    def _tap(self, keysym: int, extra_mods: list[str] = ()) -> None:
        kc, shift = self._keycode(keysym)
        mods = [self._keycode(self._sym(MODIFIERS[m]))[0] for m in extra_mods if m in MODIFIERS]
        if shift:
            mods.append(self._keycode(self._sym("Shift_L"))[0])
        fake = self._t.XTestFakeKeyEvent
        for m in mods:
            fake(self._dpy, m, 1, CURRENT_TIME)
        fake(self._dpy, kc, 1, CURRENT_TIME)
        fake(self._dpy, kc, 0, CURRENT_TIME)
        for m in reversed(mods):
            fake(self._dpy, m, 0, CURRENT_TIME)

    def _sym(self, name: str) -> int:
        ks = self._x.XStringToKeysym(name.encode())
        if ks == NO_SYMBOL:
            raise ValueError(f"tecla desconocida: {name}")
        return ks

    @staticmethod
    def _char_keysym(ch: str) -> int:
        code = ord(ch)
        if ch == "\n":
            return 0xFF0D  # Return
        if ch == "\t":
            return 0xFF09  # Tab
        if 0x20 <= code <= 0x7E or 0xA0 <= code <= 0xFF:
            return code  # Latin-1: keysym == código
        return 0x01000000 | code  # resto de Unicode

    # --------------------------------------------------------------- ratón
    def _clamp(self, cmd: dict) -> tuple[int, int]:
        return (min(max(int(cmd["x"]), 0), self._w - 1),
                min(max(int(cmd["y"]), 0), self._h - 1))

    def _move(self, x: int, y: int) -> None:
        self._t.XTestFakeMotionEvent(self._dpy, -1, x, y, CURRENT_TIME)

    def _focus_at(self, x: int, y: int) -> None:
        """
        Sin gestor de ventanas nadie da el foco al hacer clic: lo damos a la
        ventana de primer nivel bajo el puntero para que reciba el teclado.
        """
        dx, dy, child = c_int(), c_int(), c_ulong()
        if self._x.XTranslateCoordinates(self._dpy, self._root, self._root, x, y,
                                         ctypes.byref(dx), ctypes.byref(dy),
                                         ctypes.byref(child)) and child.value:
            self._x.XSetInputFocus(self._dpy, child.value, REVERT_TO_PARENT, CURRENT_TIME)

    # ------------------------------------------------------------- órdenes
    def handle(self, cmd: dict) -> None:
        kind = cmd.get("t")
        fake_button = self._t.XTestFakeButtonEvent
        if kind == "move":
            self._move(*self._clamp(cmd))
        elif kind in ("down", "up"):
            x, y = self._clamp(cmd)
            self._move(x, y)
            if kind == "down":
                self._focus_at(x, y)
            fake_button(self._dpy, int(cmd.get("b", 1)), 1 if kind == "down" else 0, CURRENT_TIME)
        elif kind == "wheel":
            x, y = self._clamp(cmd)
            self._move(x, y)
            button = 4 if cmd.get("dy", 1) < 0 else 5
            fake_button(self._dpy, button, 1, CURRENT_TIME)
            fake_button(self._dpy, button, 0, CURRENT_TIME)
        elif kind == "key":
            k = cmd["k"]
            keysym = self._char_keysym(k) if len(k) == 1 else self._sym(k)
            self._tap(keysym, cmd.get("mods", []))
        elif kind == "text":
            for ch in cmd["s"]:
                self._tap(self._char_keysym(ch))
        self._x.XFlush(self._dpy)
