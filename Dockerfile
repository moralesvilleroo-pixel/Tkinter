# ------------------------------------------------------------------
# PyRunner: FastAPI + Xvfb + Tkinter
# Se usa el Python de Debian porque trae tkinter (python3-tk); la imagen
# python:*-slim compila Python sin las librerías Tk en tiempo de ejecución.
# fonts-liberation: equivalentes métricos de Arial/Times/Courier, para que
# font=("Arial", 14) se vea parecido a Windows.
# ------------------------------------------------------------------
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-venv python3-tk \
        xvfb xauth \
        fonts-dejavu-core fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Entorno virtual (hereda tkinter de la stdlib del sistema)
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend/ backend/
COPY frontend/ frontend/

# Usuario sin privilegios: el código del usuario nunca corre como root
RUN useradd --create-home --uid 10001 runner
USER runner

WORKDIR /app/backend
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s CMD python3 -c \
  "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
