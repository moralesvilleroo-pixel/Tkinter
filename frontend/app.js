/* ============================================================
   PyRunner · lógica del cliente
   - Editor (CodeMirror o textarea de respaldo)
   - Ejecución vía fetch() con respuesta NDJSON en streaming
   - Consola en tiempo real + vista de la pantalla virtual Tkinter
   ============================================================ */
(() => {
  "use strict";

  const API = (window.API_BASE || "").replace(/\/$/, "");

  // ---------- Ejemplos ----------
  const EXAMPLES = {
    hello: `# ¡Bienvenido a PyRunner!
nombre = "mundo"
print(f"Hola, {nombre} 👋")

import math, datetime
print("π ≈", round(math.pi, 5))
print("Hoy es", datetime.date.today())
`,
    tk_counter: `import tkinter as tk

root = tk.Tk()
root.title("Contador")
root.geometry("320x180")
root.configure(bg="#1e1e2e")

contador = tk.IntVar(value=0)

tk.Label(root, text="Contador automático", font=("Arial", 14, "bold"),
         bg="#1e1e2e", fg="#cdd6f4").pack(pady=(20, 5))
tk.Label(root, textvariable=contador, font=("Arial", 40, "bold"),
         bg="#1e1e2e", fg="#89b4fa").pack()

def tick():
    contador.set(contador.get() + 1)
    print("valor:", contador.get())
    if contador.get() < 8:
        root.after(600, tick)
    else:
        print("Fin: cerrando la ventana")
        root.after(800, root.destroy)

root.after(600, tick)
root.mainloop()
print("mainloop terminado correctamente")
`,
    tk_form: `import tkinter as tk
from tkinter import ttk

root = tk.Tk()
root.title("Registro de estudiantes")
root.geometry("360x230")

frm = ttk.Frame(root, padding=14)
frm.pack(fill="both", expand=True)

ttk.Label(frm, text="Nombre:").grid(row=0, column=0, sticky="w", pady=4)
nombre = ttk.Entry(frm, width=28)
nombre.insert(0, "Ana Gómez")
nombre.grid(row=0, column=1, pady=4)

ttk.Label(frm, text="Programa:").grid(row=1, column=0, sticky="w", pady=4)
combo = ttk.Combobox(frm, values=["Sistemas", "Telemática", "Electrónica"], width=25)
combo.current(1)
combo.grid(row=1, column=1, pady=4)

activo = tk.BooleanVar(value=True)
ttk.Checkbutton(frm, text="Estudiante activo", variable=activo).grid(
    row=2, column=1, sticky="w", pady=4)

barra = ttk.Progressbar(frm, length=220, mode="determinate")
barra.grid(row=3, column=0, columnspan=2, pady=10)

def avanzar(v=0):
    barra["value"] = v
    if v < 100:
        root.after(150, avanzar, v + 5)
    else:
        print("Registro guardado para", nombre.get())

ttk.Button(frm, text="Guardar").grid(row=4, column=1, sticky="e")
avanzar()
root.mainloop()
`,
    tk_canvas: `import tkinter as tk
import math

root = tk.Tk()
root.title("Canvas")
c = tk.Canvas(root, width=400, height=300, bg="#0f172a", highlightthickness=0)
c.pack()

colores = ["#f87171", "#fbbf24", "#34d399", "#60a5fa", "#a78bfa", "#f472b6"]
for i in range(36):
    ang = math.radians(i * 10)
    x, y = 200 + 110 * math.cos(ang), 150 + 110 * math.sin(ang)
    c.create_line(200, 150, x, y, fill=colores[i % len(colores)], width=2)
c.create_oval(170, 120, 230, 180, fill="#e2e8f0", outline="")
c.create_text(200, 280, text="Hecho con tkinter.Canvas", fill="#94a3b8",
              font=("Arial", 12))

root.after(4000, root.destroy)   # se cierra sola a los 4 s
root.mainloop()
`,
    stream: `import time

for i in range(1, 11):
    print(f"Paso {i}/10", "█" * i)
    time.sleep(0.4)
print("¡Listo!")
`,
    error: `def dividir(a, b):
    return a / b

print("Intentando dividir por cero…")
print(dividir(10, 0))
`,
    infinite: `# El servidor detendrá este bucle al alcanzar el tiempo límite
i = 0
while True:
    i += 1
`,
  };

  // ---------- Referencias al DOM ----------
  const $ = (id) => document.getElementById(id);
  const els = {
    run: $("btn-run"),
    stop: $("btn-stop"),
    clear: $("btn-clear"),
    examples: $("examples"),
    console: $("console"),
    status: $("status"),
    screenPanel: $("screen-panel"),
    desktopWrap: $("desktop-wrap"),
    desktop: $("desktop"),
    desktopEmpty: $("desktop-empty"),
    guiTimer: $("gui-timer"),
    kbd: $("kbd"),
    kbdBtn: $("btn-kbd"),
    fullscreen: $("btn-fullscreen"),
    metaExit: $("meta-exit"),
    metaTime: $("meta-time"),
    metaServer: $("meta-server"),
    textarea: $("code"),
  };

  // ---------- Editor ----------
  const STORAGE_KEY = "pyrunner:code";
  let editor = null;

  function loadSavedCode() {
    try { return localStorage.getItem(STORAGE_KEY); } catch { return null; }
  }
  function saveCode(code) {
    try { localStorage.setItem(STORAGE_KEY, code); } catch { /* sin almacenamiento */ }
  }

  function initEditor() {
    const initial = loadSavedCode() || EXAMPLES.tk_counter;
    els.textarea.value = initial;

    if (window.CodeMirror) {
      editor = CodeMirror.fromTextArea(els.textarea, {
        mode: "python",
        lineNumbers: true,
        indentUnit: 4,
        tabSize: 4,
        indentWithTabs: false,
        matchBrackets: true,
        autoCloseBrackets: true,
        lineWrapping: false,
        extraKeys: {
          "Ctrl-Enter": run,
          "Cmd-Enter": run,
          // Tab inserta 4 espacios (estilo PEP 8)
          Tab: (cm) => cm.somethingSelected()
            ? cm.indentSelection("add")
            : cm.replaceSelection("    ", "end"),
        },
      });
      editor.on("change", () => saveCode(editor.getValue()));
    } else {
      // Respaldo: textarea simple
      els.textarea.addEventListener("input", () => saveCode(els.textarea.value));
      els.textarea.addEventListener("keydown", (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); run(); }
        if (e.key === "Tab") {
          e.preventDefault();
          const { selectionStart: s, selectionEnd: t, value } = els.textarea;
          els.textarea.value = value.slice(0, s) + "    " + value.slice(t);
          els.textarea.selectionStart = els.textarea.selectionEnd = s + 4;
        }
      });
    }
  }

  const getCode = () => (editor ? editor.getValue() : els.textarea.value);
  const setCode = (code) => (editor ? editor.setValue(code) : (els.textarea.value = code), saveCode(code));

  // ---------- Consola ----------
  function write(text, cls = "stdout") {
    const atBottom =
      els.console.scrollHeight - els.console.scrollTop - els.console.clientHeight < 40;
    // Fusiona con el último fragmento si es del mismo tipo (menos nodos en el DOM)
    const last = els.console.lastElementChild;
    if (last && last.className === cls) {
      last.textContent += text;
    } else {
      const span = document.createElement("span");
      span.className = cls;
      span.textContent = text; // textContent => sin riesgo de inyectar HTML
      els.console.appendChild(span);
    }
    if (atBottom) els.console.scrollTop = els.console.scrollHeight;
  }

  function setStatus(kind, label) {
    els.status.className = `status ${kind}`;
    els.status.textContent = label;
  }

  function clearOutput() {
    els.console.textContent = "";
    WindowManager.reset();
    els.metaExit.textContent = "—";
    els.metaTime.textContent = "—";
  }

  // ---------- Envío de ratón y teclado al servidor ----------
  /**
   * Cola ordenada de eventos hacia POST /api/input/{run_id}. Los movimientos
   * consecutivos se fusionan (solo importa la última posición) y se envía un
   * lote a la vez para conservar el orden.
   */
  const RemoteInput = {
    runId: null,
    queue: [],
    busy: false,

    push(ev) {
      if (!this.runId) return;
      const last = this.queue[this.queue.length - 1];
      if (ev.t === "move" && last && last.t === "move") this.queue[this.queue.length - 1] = ev;
      else this.queue.push(ev);
      this.flush();
    },

    async flush() {
      if (this.busy) return;
      this.busy = true;
      while (this.queue.length && this.runId) {
        const batch = this.queue.splice(0, 100);
        try {
          const res = await fetch(`${API}/api/input/${encodeURIComponent(this.runId)}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ events: batch }),
          });
          if (res.status === 404) { this.runId = null; this.queue = []; }
        } catch { /* red caída: se descarta el lote */ }
      }
      this.busy = false;
    },

    stop() { this.runId = null; this.queue = []; },
  };

  // Teclas especiales del navegador -> nombres de tecla de X11
  const X_KEYS = {
    Enter: "Return", Backspace: "BackSpace", Tab: "Tab", Escape: "Escape",
    Delete: "Delete", Insert: "Insert", Home: "Home", End: "End",
    PageUp: "Prior", PageDown: "Next",
    ArrowLeft: "Left", ArrowRight: "Right", ArrowUp: "Up", ArrowDown: "Down",
    F1: "F1", F2: "F2", F3: "F3", F4: "F4", F5: "F5", F6: "F6",
    F7: "F7", F8: "F8", F9: "F9", F10: "F10", F11: "F11", F12: "F12",
  };

  /**
   * Teclado: un textarea invisible recibe el foco.
   *  - Teclado físico: se usa keydown (teclas especiales, atajos, caracteres).
   *  - Teclado del celular (keydown "Unidentified"/229, autocorrector): se
   *    compara el texto antes/después de cada "input" y se envían las letras
   *    nuevas y los borrados como BackSpace.
   */
  const Keyboard = (() => {
    const SENTINEL = "\u200b"; // carácter invisible: permite detectar "borrar" con el texto vacío
    let prev = SENTINEL;
    let composing = false;

    const reset = () => { els.kbd.value = SENTINEL; prev = SENTINEL; };
    reset();

    function sendText(text) {
      text.split(/(\n)/).forEach((part) => {
        if (part === "\n") RemoteInput.push({ t: "key", k: "Return" });
        else if (part) RemoteInput.push({ t: "text", s: part });
      });
    }

    els.kbd.addEventListener("keydown", (e) => {
      if (e.isComposing || e.keyCode === 229 || e.key === "Unidentified" || e.key === "Process") {
        return; // lo resolverá el evento "input"
      }
      const mods = [];
      if (e.ctrlKey) mods.push("Control");
      if (e.altKey) mods.push("Alt");
      if (e.metaKey) mods.push("Meta");

      if (X_KEYS[e.key]) {
        if (e.shiftKey) mods.push("Shift");
        RemoteInput.push({ t: "key", k: X_KEYS[e.key], mods });
        e.preventDefault();
      } else if (e.key.length === 1) {
        if (mods.length) RemoteInput.push({ t: "key", k: e.key, mods });
        else RemoteInput.push({ t: "text", s: e.key });
        e.preventDefault();
      }
    });

    els.kbd.addEventListener("compositionstart", () => { composing = true; });
    els.kbd.addEventListener("compositionend", () => {
      composing = false;
      if (els.kbd.value.length > 64) reset();
    });

    els.kbd.addEventListener("input", () => {
      const now = els.kbd.value;
      let i = 0;
      while (i < prev.length && i < now.length && prev[i] === now[i]) i++;
      for (let d = 0; d < prev.length - i; d++) RemoteInput.push({ t: "key", k: "BackSpace" });
      const inserted = now.slice(i).replaceAll(SENTINEL, "");
      if (inserted) sendText(inserted);
      prev = now;
      if (!now.startsWith(SENTINEL) || (!composing && now.length > 64)) reset();
    });

    els.kbd.addEventListener("focus", () => {
      els.desktopWrap.classList.add("typing");
      els.kbdBtn.classList.add("on");
    });
    els.kbd.addEventListener("blur", () => {
      els.desktopWrap.classList.remove("typing");
      els.kbdBtn.classList.remove("on");
      reset();
    });

    return {
      focus() { els.kbd.focus({ preventScroll: true }); },
      toggle() {
        if (document.activeElement === els.kbd) els.kbd.blur();
        else els.kbd.focus({ preventScroll: true });
      },
    };
  })();

  // ---------- Ventanas Tkinter ----------
  /**
   * Dibuja cada ventana del programa con un marco al estilo de Tkinter en
   * Windows: icono, título real (root.title()), botones y sombra. El contenido
   * de todas las ventanas sale de una sola captura PNG: cada ventana muestra
   * su trozo usando background-position.
   */
  const WindowManager = (() => {
    const TITLEBAR = 30;
    const nodes = new Map(); // id de ventana X -> elemento DOM
    let natural = { w: 0, h: 0 };

    const ICON = `<svg class="tk-icon" viewBox="0 0 16 16" aria-hidden="true">
      <path d="M13.5 1.5C8 2 4.5 6 3.2 12.3l1.3.3C5 10.4 6 8.6 7.2 7.2 7.6 9 9 10 11 9.6 9.4 9.3 8.6 8.3 8.6 6.6 10 5.4 11.8 4.6 13.5 4.2 12.3 4 11.2 4.1 10.2 4.5 11.3 3.3 12.4 2.3 13.5 1.5Z" fill="#d6372e"/>
      <path d="M3.2 12.3 2 14.6" stroke="#8a2b25" stroke-width="1.2" stroke-linecap="round"/></svg>`;
    const BTN_MIN = `<svg viewBox="0 0 10 10"><path d="M0 5h10" stroke="currentColor"/></svg>`;
    const BTN_MAX = `<svg viewBox="0 0 10 10"><rect x=".5" y=".5" width="9" height="9" fill="none" stroke="currentColor"/></svg>`;
    const BTN_CLOSE = `<svg viewBox="0 0 10 10"><path d="M0 0l10 10M10 0 0 10" stroke="currentColor"/></svg>`;

    function create(id) {
      const win = document.createElement("div");
      win.className = "tkwin";
      win.innerHTML = `
        <div class="tk-titlebar">
          ${ICON}<span class="tk-title"></span>
          <div class="tk-buttons">
            <button class="tk-min" title="Minimizar" aria-label="Minimizar">${BTN_MIN}</button>
            <button class="tk-max" title="Pantalla completa" aria-label="Maximizar">${BTN_MAX}</button>
            <button class="tk-close" title="Cerrar (detiene el programa)" aria-label="Cerrar">${BTN_CLOSE}</button>
          </div>
        </div>
        <div class="tk-client"></div>`;
      win.querySelector(".tk-min").onclick = () => { win.classList.toggle("minimized"); fit(); };
      win.querySelector(".tk-max").onclick = toggleFullscreen;
      win.querySelector(".tk-close").onclick = () => stop();
      attachPointer(win, win.querySelector(".tk-client"));
      nodes.set(id, win);
      els.desktop.appendChild(win);
      return win;
    }

    /**
     * Ratón / dedo sobre el contenido de una ventana. Convierte la posición
     * en pantalla (con el escalado aplicado) a coordenadas del display virtual.
     */
    function attachPointer(win, client) {
      let pressed = 0;
      let lastHover = 0;

      const toScreen = (e) => {
        const r = client.getBoundingClientRect();
        const g = win._geo;
        const k = g.w / r.width; // deshace el escalado del escritorio
        return {
          x: Math.round(g.sx + (e.clientX - r.left) * k),
          y: Math.round(g.sy + (e.clientY - r.top) * k),
        };
      };

      client.addEventListener("pointerdown", (e) => {
        if (!win._geo) return;
        e.preventDefault();
        client.setPointerCapture(e.pointerId);
        pressed = e.button === 1 ? 2 : e.button === 2 ? 3 : 1; // 1 izq, 2 medio, 3 der
        const p = toScreen(e);
        RemoteInput.push({ t: "move", ...p });
        RemoteInput.push({ t: "down", b: pressed, ...p });
        // Con ratón damos foco al teclado; en táctil se abre con el botón ⌨
        if (e.pointerType === "mouse") Keyboard.focus();
      });

      client.addEventListener("pointermove", (e) => {
        if (!win._geo) return;
        const now = performance.now();
        if (!pressed) {
          if (e.pointerType !== "mouse" || now - lastHover < 80) return; // hover: con moderación
          lastHover = now;
        }
        RemoteInput.push({ t: "move", ...toScreen(e) });
      });

      const release = (e) => {
        if (!pressed || !win._geo) return;
        RemoteInput.push({ t: "up", b: pressed, ...toScreen(e) });
        pressed = 0;
      };
      client.addEventListener("pointerup", release);
      client.addEventListener("pointercancel", release);
      client.addEventListener("contextmenu", (e) => e.preventDefault());

      let wheelAcc = 0;
      client.addEventListener("wheel", (e) => {
        if (!win._geo) return;
        e.preventDefault();
        wheelAcc += e.deltaY;
        if (Math.abs(wheelAcc) >= 40) {
          RemoteInput.push({ t: "wheel", dy: Math.sign(wheelAcc), ...toScreen(e) });
          wheelAcc = 0;
        }
      }, { passive: false });
    }

    /** Escala el escritorio para que quepa en el panel (sin agrandar en modo normal). */
    function fit() {
      if (!natural.w) return;
      const full = els.screenPanel.classList.contains("fullscreen");
      const pad = 36;
      const availW = els.desktopWrap.clientWidth - pad;
      const availH = full ? els.desktopWrap.clientHeight - pad : Infinity;
      const scale = Math.min(full ? 2 : 1, availW / natural.w, availH / natural.h);
      els.desktop.style.width = `${natural.w}px`;
      els.desktop.style.height = `${natural.h}px`;
      els.desktop.style.transform = `scale(${scale})`;
      // transform no cambia el tamaño en el flujo: reservamos el alto escalado
      els.desktop.style.marginBottom = `${natural.h * (scale - 1)}px`;
      els.desktop.style.marginLeft = full ? `${Math.max(0, (availW - natural.w * scale) / 2)}px` : "0";
    }

    function render(frame) {
      const wins = frame.windows || [];
      if (!wins.length) {
        // El programa cerró todas sus ventanas
        nodes.forEach((n) => n.remove());
        nodes.clear();
        natural = { w: 0, h: 0 };
        els.desktop.removeAttribute("style");
        els.desktopEmpty.hidden = false;
        return;
      }
      els.desktopEmpty.hidden = true;
      if (frame.data) {
        els.desktop.style.setProperty("--frame", `url(data:image/png;base64,${frame.data})`);
      }

      // La última ventana con marco es la que está encima: la "activa"
      const activeId = [...wins].reverse().find((w) => w.decorated)?.id;
      const seen = new Set();
      let maxW = 0, maxH = 0;

      wins.forEach((w, z) => {
        seen.add(w.id);
        const win = nodes.get(w.id) || create(w.id);
        const bar = w.decorated ? TITLEBAR : 0;
        win.classList.toggle("plain", !w.decorated);
        win.classList.toggle("active", w.id === activeId);
        win.querySelector(".tk-titlebar").hidden = !w.decorated;
        win.querySelector(".tk-title").textContent = w.title || "tk";
        // Origen de la ventana en el display virtual (para mapear clics)
        win._geo = { sx: (frame.left || 0) + w.x, sy: (frame.top || 0) + w.y, w: w.w };
        win.style.left = `${w.x}px`;
        win.style.top = `${w.y}px`;
        win.style.zIndex = String(z + 1);
        const client = win.querySelector(".tk-client");
        client.style.width = `${w.w}px`;
        client.style.height = `${w.h}px`;
        client.style.backgroundPosition = `-${w.x}px -${w.y}px`;
        maxW = Math.max(maxW, w.x + w.w + 2);
        maxH = Math.max(maxH, w.y + w.h + bar + 2);
      });

      for (const [id, n] of nodes) {
        if (!seen.has(id)) { n.remove(); nodes.delete(id); }
      }
      natural = { w: maxW, h: maxH };
      fit();
    }

    function reset() {
      nodes.forEach((n) => n.remove());
      nodes.clear();
      natural = { w: 0, h: 0 };
      els.desktop.removeAttribute("style");
      els.desktop.classList.remove("ended");
      els.desktopEmpty.hidden = true;
      els.screenPanel.hidden = true;
      els.screenPanel.classList.remove("fullscreen");
      els.guiTimer.hidden = true;
    }

    function toggleFullscreen() {
      els.screenPanel.classList.toggle("fullscreen");
      els.fullscreen.textContent = els.screenPanel.classList.contains("fullscreen") ? "⤡" : "⤢";
      fit();
    }

    window.addEventListener("resize", fit);
    return { render, reset, fit, toggleFullscreen };
  })();

  // ---------- Ejecución ----------
  let controller = null;
  let timerId = null;

  function setRunning(running) {
    els.run.disabled = running;
    els.stop.disabled = !running;
    els.examples.disabled = running;
  }

  let runStart = 0;
  let guiDeadline = 0; // instante (performance.now) en que se cerrarán las ventanas

  function startClock() {
    runStart = performance.now();
    guiDeadline = 0;
    timerId = setInterval(() => {
      const now = performance.now();
      els.metaTime.textContent = `⏱ ${((now - runStart) / 1000).toFixed(1)} s`;
      if (guiDeadline) {
        const left = Math.max(0, Math.ceil((guiDeadline - now) / 1000));
        els.guiTimer.textContent =
          `Se cierra en ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
      }
    }, 100);
  }
  function stopClock() { clearInterval(timerId); timerId = null; }

  /** Procesa un evento NDJSON enviado por el backend. */
  function handleEvent(ev, ctx) {
    switch (ev.type) {
      case "start":
        RemoteInput.runId = ev.run_id;
        write(`$ python main.py   (límite: ${ev.timeout}s)\n`, "system");
        break;
      case "stdout":
        write(ev.data, "stdout");
        break;
      case "stderr":
        write(ev.data, "stderr");
        break;
      case "gui":
        // El servidor detectó una ventana y amplió el tiempo límite
        guiDeadline = runStart + ev.timeout * 1000;
        els.guiTimer.hidden = false;
        break;
      case "frame":
        if (!ctx.hadGui && ev.windows && ev.windows.length) {
          // Primera ventana: mostrar el panel y llevarlo a la vista (útil en móvil)
          ctx.hadGui = true;
          els.screenPanel.hidden = false;
          requestAnimationFrame(() =>
            els.screenPanel.scrollIntoView({ behavior: "smooth", block: "nearest" }));
        }
        WindowManager.render(ev);
        break;
      case "exit": {
        ctx.finished = true;
        RemoteInput.stop();
        els.kbd.blur();
        guiDeadline = 0;
        els.guiTimer.hidden = true;
        els.desktop.classList.add("ended");
        els.metaExit.textContent = `exit code: ${ev.exit_code}`;
        els.metaTime.textContent = `⏱ ${ev.duration.toFixed(2)} s`;
        if (ev.timed_out) {
          setStatus("timeout", "Tiempo agotado");
          if (ctx.hadGui) {
            write("ℹ Las ventanas se cierran al llegar al tiempo límite. " +
                  "También puedes cerrarlas con ✕ o con el botón Detener.\n", "system");
          }
        } else if (ev.cpu_exceeded) {
          setStatus("timeout", "CPU excedida");
        } else if (ev.exit_code === 0) {
          setStatus("ok", "Completado");
          write(`\n[Proceso terminado en ${ev.duration.toFixed(2)} s]\n`, "ok");
        } else {
          setStatus("error", `Error (${ev.exit_code})`);
          write(`\n[Proceso terminado con código ${ev.exit_code}]\n`, "system");
        }
        break;
      }
    }
  }

  async function run() {
    if (controller) return; // ya hay una ejecución en curso
    const code = getCode();
    if (!code.trim()) { write("No hay código para ejecutar.\n", "system"); return; }

    clearOutput();
    setRunning(true);
    setStatus("running", "Ejecutando…");
    startClock();

    controller = new AbortController();
    const ctx = { finished: false, hadGui: false };

    try {
      const res = await fetch(`${API}/api/run/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code }),
        signal: controller.signal,
      });

      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        const detail = Array.isArray(body.detail)
          ? body.detail.map((d) => d.msg).join("; ")
          : body.detail || res.statusText;
        throw new Error(`HTTP ${res.status}: ${detail}`);
      }

      // Lectura incremental del stream NDJSON (una línea = un evento JSON)
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let nl;
        while ((nl = buffer.indexOf("\n")) >= 0) {
          const line = buffer.slice(0, nl).trim();
          buffer = buffer.slice(nl + 1);
          if (line) handleEvent(JSON.parse(line), ctx);
        }
      }
      if (!ctx.finished) throw new Error("La conexión se cerró antes de terminar.");
    } catch (err) {
      if (err.name === "AbortError") {
        els.guiTimer.hidden = true;
        els.desktop.classList.add("ended");
        write("\n■ Ejecución detenida por el usuario.\n", "system");
        setStatus("timeout", "Detenido");
      } else {
        write(`\n✖ ${err.message}\n`, "stderr");
        setStatus("error", "Error de conexión");
      }
    } finally {
      RemoteInput.stop();
      stopClock();
      setRunning(false);
      controller = null;
    }
  }

  function stop() {
    // Abortar el fetch cierra la conexión; el backend mata el proceso.
    if (controller) controller.abort();
  }

  // ---------- Estado del servidor ----------
  async function checkHealth() {
    try {
      const res = await fetch(`${API}/api/health`);
      const h = await res.json();
      els.metaServer.textContent = h.xvfb
        ? `Servidor: en línea · Xvfb ✓ · límite ${h.timeout_seconds}s (ventanas ${h.gui_timeout_seconds}s)`
        : "Servidor: en línea · Xvfb no instalado ✗";
    } catch {
      els.metaServer.textContent = "Servidor: sin conexión ✗";
    }
  }

  // ---------- Eventos ----------
  els.run.addEventListener("click", run);
  els.stop.addEventListener("click", stop);
  els.clear.addEventListener("click", () => { if (!controller) clearOutput(); else els.console.textContent = ""; });
  els.fullscreen.addEventListener("click", () => WindowManager.toggleFullscreen());
  // pointerdown sin foco: así el botón no le quita el foco al teclado
  els.kbdBtn.addEventListener("pointerdown", (e) => e.preventDefault());
  els.kbdBtn.addEventListener("click", () => Keyboard.toggle());
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && els.screenPanel.classList.contains("fullscreen")) {
      WindowManager.toggleFullscreen();
    }
  });
  els.examples.addEventListener("change", (e) => {
    const key = e.target.value;
    if (key && EXAMPLES[key]) setCode(EXAMPLES[key]);
    e.target.value = "";
  });

  initEditor();
  checkHealth();
})();
