# PyRunner · Intérprete Python online con soporte Tkinter

Aplicación web para escribir y ejecutar código Python desde el navegador, **incluidos programas Tkinter**. El servidor ejecuta cada script sobre una pantalla virtual en memoria (Xvfb), envía la consola en tiempo real y muestra una captura en vivo de la ventana.

## Arquitectura

```
┌──────────────── Navegador ────────────────┐
│  Editor (CodeMirror)   Consola   Pantalla │
│        │  fetch POST /api/run/stream  ▲   │
└────────┼──────────────────────────────┼───┘
         ▼                 NDJSON (stdout, stderr, frame, exit)
┌──────────────── FastAPI ──────────────────┐
│ main.py      → validación + streaming     │
│ executor.py  → pool de displays, límites, │
│                timeout, lectura de pipes  │
└────────┬──────────────────────────────────┘
         ▼  setrlimit + sesión propia + entorno mínimo
   xvfb-run -n :10X  python -I runner.py main.py …
         │      ├─ Xvfb (pantalla virtual 1024x768)
         │      └─ runner.py → ejecuta main.py (tk.Tk(), mainloop()…)
         │           └─ capture.py (proceso aparte): lista las ventanas X
         │              (título, posición, tamaño) + captura → frames/latest.json
         │              y lee frames/input.jsonl → x11input.py (XTEST: ratón/teclado)
         ▼
   Timeout / exceso de salida / cliente cancela → killpg (SIGTERM → SIGKILL)
```

Decisiones clave:

| Problema | Solución |
|---|---|
| Tkinter necesita una pantalla | `xvfb-run` crea un servidor X virtual por ejecución |
| Ver la ventana en el navegador | `capture.py` lee con libX11 (ctypes) las ventanas abiertas y su contenido; el navegador dibuja cada una con barra de título, icono y botones como en Windows |
| Usar la ventana desde el navegador | Clics, arrastre, rueda y teclado se envían a `POST /api/input/{run_id}`; `x11input.py` los inyecta con XTEST (como `xdotool`). Caracteres como ñ, á o € se asignan a teclas libres del teclado virtual |
| Teclado del celular | Un `textarea` invisible recibe el texto; se compara antes/después de cada cambio para soportar autocorrector y borrado |
| Apps Tkinter que esperan eventos | Al aparecer una ventana, el límite pasa de `RUN_TIMEOUT` a `GUI_TIMEOUT`; `CPU_LIMIT` sigue frenando bucles infinitos |
| Salida en tiempo real | `PYTHONUNBUFFERED=1` + respuesta **NDJSON** leída con `fetch` + `ReadableStream` |
| `xvfb-run` mezcla stderr con stdout (`2>&1`) | Pipe extra que el runner coloca como su descriptor 2 (`os.dup2`) |
| Ejecuciones simultáneas | Pool de displays (`:100`, `:101`…) en vez de `-a` (que tiene condiciones de carrera) |
| Bucles infinitos | Timeout de reloj + `RLIMIT_CPU`; se mata todo el grupo de procesos |
| `print` sin fin | Tope de bytes de salida; al superarlo se detiene el proceso |
| `input()` | stdin = `/dev/null` → `EOFError` en lugar de quedarse colgado |
| Botón **Detener** | `AbortController` cierra la conexión → el generador se cancela → `killpg` |

## Estructura

```
pyrunner/
├── backend/
│   ├── app/
│   │   ├── config.py     # límites y opciones (variables de entorno)
│   │   ├── executor.py   # motor de ejecución con Xvfb
│   │   └── main.py       # API FastAPI + servidor del frontend
│   ├── sandbox/
│   │   ├── runner.py     # envoltorio que corre dentro de xvfb-run
│   │   ├── capture.py    # lee ventanas y capturas del display virtual
│   │   └── x11input.py   # inyecta ratón y teclado (XTEST)
│   └── requirements.txt
├── frontend/
│   ├── index.html
│   ├── styles.css
│   └── app.js
├── Dockerfile
└── docker-compose.yml
```

## Puesta en marcha

### Opción A: Docker (recomendada)

```bash
docker compose up --build
# abre http://localhost:8000
```

### Opción B: local (Linux)

```bash
sudo apt install python3-tk xvfb xauth          # Debian/Ubuntu
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
# abre http://localhost:8000
```

> En Windows usa Docker o WSL2: Xvfb solo existe en Linux.

## API

### `POST /api/run/stream` (tiempo real)

Petición: `{"code": "print('hola')"}`. La respuesta es `application/x-ndjson`, con un evento JSON por línea:

```json
{"type": "start",  "display": 100, "timeout": 10}
{"type": "stdout", "data": "hola\n"}
{"type": "stderr", "data": "Traceback …"}
{"type": "gui",    "timeout": 120}
{"type": "frame",  "data": "<PNG base64>", "width": 360, "height": 230,
                   "windows": [{"id": 2097153, "title": "Contador", "x": 0, "y": 0,
                                "w": 320, "h": 180, "decorated": true}]}
{"type": "exit",   "exit_code": 0, "timed_out": false, "cpu_exceeded": false,
                   "truncated": false, "duration": 0.31}
```

### `POST /api/input/{run_id}` (interactuar con las ventanas)

El `run_id` llega en el evento `start` del stream. Coordenadas en píxeles del display virtual (`left`/`top` del frame + `x`/`y` de la ventana).

```json
{"events": [
  {"t": "down", "b": 1, "x": 80, "y": 60},
  {"t": "up",   "b": 1, "x": 80, "y": 60},
  {"t": "text", "s": "Hola ñ"},
  {"t": "key",  "k": "Return", "mods": []},
  {"t": "wheel", "dy": 1, "x": 80, "y": 60}
]}
```

### `POST /api/run` (resultado completo)

```bash
curl -X POST localhost:8000/api/run -H 'Content-Type: application/json' \
     -d '{"code": "print(2**10)"}'
```

```json
{"stdout": "1024\n", "stderr": "", "exit_code": 0, "timed_out": false,
 "truncated": false, "duration": 0.09, "screenshot": null}
```

### `GET /api/health`

Indica si `xvfb-run` está disponible y los límites configurados. La documentación interactiva está en `/docs` (Swagger).

## Configuración (variables de entorno)

| Variable | Defecto | Descripción |
|---|---|---|
| `RUN_TIMEOUT` | `10` | Segundos máximos por ejecución sin ventanas |
| `GUI_TIMEOUT` | `120` | Segundos máximos cuando el programa abre una ventana |
| `CPU_LIMIT` | `20` | Segundos de CPU por proceso (detiene bucles infinitos) |
| `MAX_CONCURRENT_RUNS` | `4` | Ejecuciones simultáneas |
| `QUEUE_WAIT_SECONDS` | `5` | Espera por un hueco antes de responder 503 |
| `MAX_CODE_BYTES` | `100000` | Tamaño máximo del código |
| `MAX_OUTPUT_BYTES` | `200000` | Tope de salida (stdout + stderr) |
| `MEMORY_LIMIT_MB` | `1024` | Memoria virtual por proceso |
| `FILE_SIZE_LIMIT_MB` | `10` | Tamaño máximo de archivo escrito |
| `NPROC_LIMIT` | `0` | `RLIMIT_NPROC` (0 = desactivado) |
| `XVFB_SCREEN` | `1024x768x24` | Resolución de la pantalla virtual |
| `FRAME_INTERVAL` | `0.4` | Segundos entre capturas |
| `CORS_ORIGINS` | `*` | Orígenes permitidos, separados por comas |

## Seguridad: lo que hay y lo que falta

Ya incluido: timeout, límites de CPU, memoria, tamaño de archivos y salida, entorno sin variables del servidor, directorio temporal por ejecución, usuario sin privilegios y contenedor endurecido (`read_only`, `cap_drop: ALL`, `pids_limit`, `no-new-privileges`).

**Importante:** ejecutar código arbitrario nunca es 100 % seguro con solo límites de procesos. El código del usuario corre con el mismo usuario que el backend, así que podría leer los archivos de la app o abrir conexiones de red. Antes de abrirlo al público:

1. **Aísla cada ejecución**: un contenedor efímero por ejecución con `--network none`, o herramientas como [nsjail](https://github.com/google/nsjail), [bubblewrap](https://github.com/containers/bubblewrap) o [gVisor](https://gvisor.dev/). El `executor.py` está preparado para ello: basta con cambiar `_build_command`.
2. Añade **limitación de peticiones** por IP (por ejemplo `slowapi` o el proxy inverso).
3. Pon un **proxy inverso** (nginx/Caddy) con HTTPS; ya se envía `X-Accel-Buffering: no` para que nginx no retenga el stream.

## Limitaciones y siguientes pasos

- La imagen se actualiza por capturas (más rápido justo después de cada interacción), no es video continuo: animaciones muy rápidas se verán a saltos. Para video real, el siguiente paso sería `x11vnc` + **noVNC** por WebSocket.
- Las apps Tkinter se cierran al llegar a `GUI_TIMEOUT` (120 s por defecto), con ✕ o con **Detener**.
- `input()` no recibe datos. Se puede añadir un campo "stdin" en el frontend y pasarlo al proceso.
- Solo librería estándar + Pillow. Para más paquetes, añádelos en `requirements.txt`.
