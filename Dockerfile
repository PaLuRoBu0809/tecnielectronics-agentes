# syntax=docker/dockerfile:1
#
# Imagen de producción del agente TecniElectronics (docs/PLAN_DE_MEJORAS.md, Fase 9.3).
#
# Construir y correr (un solo comando cada uno):
#   docker build -t tecnielectronics .
#   docker run --env-file .env -p 8000:8000 tecnielectronics
#
# La configuración llega por variables de entorno (en el hosting, en su panel
# de variables). El archivo .env NUNCA entra en la imagen (ver .dockerignore).

# --- Etapa 1: dependencias -------------------------------------------------
# Herramientas de compilación solo aquí, por si alguna dependencia no trae
# wheel precompilado; no llegan a la imagen final.
FROM python:3.13-slim AS dependencias

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install -r requirements.txt

# --- Etapa 2: ejecución ----------------------------------------------------
FROM python:3.13-slim AS ejecucion

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    PORT=8000

# Usuario sin privilegios: el proceso no corre como root.
RUN useradd --create-home --uid 10001 app

WORKDIR /app
COPY --from=dependencias /opt/venv /opt/venv
COPY --chown=app:app . .

USER app
EXPOSE 8000

# Liveness: /health no toca Supabase (ver web/app.py).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request as u; u.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT', '8000'), timeout=4)"

# UN solo worker a propósito: el lock por sesión, la confirmación pendiente,
# el limitador, el disyuntor y el bus de eventos viven en la memoria del
# proceso (ver docs/PLAN_DE_MEJORAS.md, Fase 7). Para escalar, más de un
# worker exige mover ese estado a Supabase/Redis primero.
CMD ["sh", "-c", "exec uvicorn web.app:app --host 0.0.0.0 --port ${PORT} --workers 1 --proxy-headers"]
