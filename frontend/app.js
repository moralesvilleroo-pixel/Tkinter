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
    screen: $("screen"),
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
    els.screenPanel.hidden = true;
    els.screen.removeAttribute("src");
    els.metaExit.textContent = "—";
    els.metaTime.textContent = "—";
  }

  // ---------- Ejecución ----------
  let controller = null;
  let timerId = null;

  function setRunning(running) {
    els.run.disabled = running;
    els.stop.disabled = !running;
    els.examples.disabled = running;
  }

  function startClock() {
    const t0 = performance.now();
    timerId = setInterval(() => {
      els.metaTime.textContent = `⏱ ${((performance.now() - t0) / 1000).toFixed(1)} s`;
    }, 100);
  }
  function stopClock() { clearInterval(timerId); timerId = null; }

  /** Procesa un evento NDJSON enviado por el backend. */
  function handleEvent(ev, ctx) {
    switch (ev.type) {
      case "start":
        write(`$ python main.py   (límite: ${ev.timeout}s)\n`, "system");
        break;
      case "stdout":
        write(ev.data, "stdout");
        break;
      case "stderr":
        write(ev.data, "stderr");
        break;
      case "frame":
        ctx.hadGui = true;
        els.screenPanel.hidden = false;
        els.screen.src = `data:image/png;base64,${ev.data}`;
        break;
      case "exit": {
        ctx.finished = true;
        els.metaExit.textContent = `exit code: ${ev.exit_code}`;
        els.metaTime.textContent = `⏱ ${ev.duration.toFixed(2)} s`;
        if (ev.timed_out) {
          setStatus("timeout", "Tiempo agotado");
          if (ctx.hadGui) {
            write("ℹ Las ventanas Tkinter se cierran al alcanzar el límite. " +
                  "Usa root.after(ms, root.destroy) para cerrarlas antes.\n", "system");
          }
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
        write("\n■ Ejecución detenida por el usuario.\n", "system");
        setStatus("timeout", "Detenido");
      } else {
        write(`\n✖ ${err.message}\n`, "stderr");
        setStatus("error", "Error de conexión");
      }
    } finally {
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
        ? `Servidor: en línea · Xvfb ✓ · límite ${h.timeout_seconds}s`
        : "Servidor: en línea · Xvfb no instalado ✗";
    } catch {
      els.metaServer.textContent = "Servidor: sin conexión ✗";
    }
  }

  // ---------- Eventos ----------
  els.run.addEventListener("click", run);
  els.stop.addEventListener("click", stop);
  els.clear.addEventListener("click", clearOutput);
  els.examples.addEventListener("change", (e) => {
    const key = e.target.value;
    if (key && EXAMPLES[key]) setCode(EXAMPLES[key]);
    e.target.value = "";
  });

  initEditor();
  checkHealth();
})();
