"""
tools/eventos_agente.py

Bus de eventos en memoria para el panel "Flujo en Vivo" del dashboard
(web/static/dashboard.html + flujo.js): cada vez que `llm_loop.py` llama a
un modelo de OpenRouter o ejecuta una tool, publica un evento aquí.
`web/app.py` expone esos eventos como un stream de Server-Sent Events
(`GET /api/flujo/stream`) para que el navegador los pinte en tiempo real,
como una terminal de logs.

Diseño — una cola (`queue.Queue`, thread-safe de la librería estándar) por
cliente SSE conectado, en vez de un solo buffer compartido: así cada pestaña
del dashboard que tengas abierta ve el stream completo desde que se conectó,
sin robarle eventos a otra pestaña. `publicar_evento` se llama desde los
threads de trabajo donde corre el loop del agente (FastAPI ejecuta cada
request en un threadpool) — nunca bloquea al agente real: si algún
suscriptor está lleno (nadie lee su cola, ej. una pestaña de dashboard
abandonada), se descarta su evento más viejo en vez de frenar la respuesta
al cliente real de WhatsApp/chat.

Limitación conocida (documentada, no resuelta): esto es un bus EN MEMORIA de
un solo proceso — si despliegas con más de un worker, cada worker tendría su
propio bus y un dashboard conectado a un worker no vería los eventos que
procesa otro. Aceptable para un panel de debug en desarrollo; si en el
futuro corres varios workers en producción, esto necesitaría un backend de
pub/sub real (Redis, etc.).

Historial de "corridas" (estilo historial de ejecuciones de n8n): además del
stream en vivo, este módulo guarda los últimos `MAX_CORRIDAS_HISTORIAL`
turnos completos. Una "corrida" = todo lo que pasó a raíz de UN mensaje del
cliente (un solo `POST /api/chat`), identificada por `run_id` (generado en
`web/app.py`, propagado por `contexto["run_id"]` a través de
`run_agent_loop` y de `AlmacenSesiones`). El sidebar de "Corridas" del
dashboard (`flujo.js`) lo usa para listar mensajes recientes y, al hacer
click en uno, mostrar exactamente qué nodos del diagrama participaron en
ESE turno puntual — ver `GET /api/flujo/corridas` y
`GET /api/flujo/corridas/{run_id}` en `web/app.py`.

Persistencia y métricas (Fase 8 de `docs/PLAN_DE_MEJORAS.md`): además del
bus en memoria, cada evento
- se escribe como UNA línea JSON en stdout (logger `tecnielectronics.eventos`)
  con los datos personales enmascarados (`enmascarar_pii`) — así un turno de
  ayer se puede reconstruir tras reiniciar el servidor, desde los logs del
  hosting. Se desactiva con `LOG_EVENTOS_JSON=0`;
- alimenta `tools/metricas.py` (contadores + alertas).
El panel "Flujo en Vivo" (autenticado) sigue viendo los datos SIN enmascarar.
"""
from __future__ import annotations

import itertools
import json
import logging
import os
import queue
import re
import sys
import threading
import time
from collections import OrderedDict
from typing import Optional

from tools import metricas

_logger_json = logging.getLogger("tecnielectronics.eventos")
_logger_json.propagate = False  # una línea JSON pura, sin el prefijo del logging raíz
if not _logger_json.handlers:
    _manejador = logging.StreamHandler(sys.stdout)
    _manejador.setFormatter(logging.Formatter("%(message)s"))
    _logger_json.addHandler(_manejador)
    _logger_json.setLevel(logging.INFO)

_LIMITE_TEXTO_LOG = 1000

# Claves cuyo valor es un dato personal y se enmascara siempre.
_CLAVES_PII = {"session_id", "cliente_nombre", "cliente_telefono"}
# Claves técnicas que NUNCA se tocan (se necesitan para correlacionar).
_CLAVES_TECNICAS = {"run_id", "tipo", "agente", "modelo", "nombre", "timestamp", "tipo_error", "razon"}
# Posible teléfono: 7+ dígitos, cada uno con a lo sumo UN espacio o guion
# antes (sin ambigüedad: no hay backtracking costoso). No empieza pegado a
# una letra/dígito/punto (así no corta ids hexadecimales ni decimales) y no
# coincide con fechas ISO (2026-10-05).
_RE_TELEFONO = re.compile(r"(?<![\w.])(?!\d{4}-\d{2}-\d{2})\+?\d(?:[\s\-]?\d){6,}(?!\w|\.\d)")
_RE_EMAIL = re.compile(r"[\w.+-]+@([\w-]+\.[\w.-]+)")
# Dentro de textos tipo formatear_fila: "cliente_nombre=Ana Pérez | ...".
_RE_CAMPO_PII = re.compile(r"(cliente_nombre|cliente_telefono|session_id)=([^|\n]*)")


def _enmascarar_texto(texto: str) -> str:
    texto = _RE_CAMPO_PII.sub(lambda m: f"{m.group(1)}=***", texto)
    texto = _RE_EMAIL.sub(lambda m: f"***@{m.group(1)}", texto)
    return _RE_TELEFONO.sub(lambda m: "***" + re.sub(r"\D", "", m.group(0))[-3:], texto)


def enmascarar_pii(valor, clave: Optional[str] = None):
    """Copia de `valor` (str/dict/list/escalares) con teléfonos, correos y
    nombres de cliente enmascarados. Un teléfono conserva sus 3 últimos
    dígitos (sirve para correlacionar sin exponerlo).

    Limitación: un nombre escrito en texto libre por el cliente ("soy Ana")
    no se puede detectar de forma confiable; solo se enmascaran los campos
    estructurados (`cliente_nombre=...`, claves conocidas)."""
    if clave in _CLAVES_TECNICAS:
        return valor
    if clave in _CLAVES_PII and valor is not None:
        crudo = str(valor)
        digitos = re.sub(r"\D", "", crudo)
        return "***" + digitos[-3:] if len(digitos) >= 7 else "***"
    if isinstance(valor, dict):
        return {k: enmascarar_pii(v, k) for k, v in valor.items()}
    if isinstance(valor, list):
        return [enmascarar_pii(v) for v in valor]
    if isinstance(valor, str):
        return _enmascarar_texto(valor)
    return valor


def _escribir_log_json(evento: dict) -> None:
    if os.environ.get("LOG_EVENTOS_JSON", "1") == "0":
        return
    try:
        seguro = enmascarar_pii(evento)
        for campo, valor in seguro.items():
            if isinstance(valor, str) and len(valor) > _LIMITE_TEXTO_LOG:
                seguro[campo] = valor[:_LIMITE_TEXTO_LOG] + "...(truncado)"
        _logger_json.info(json.dumps({"evento": seguro.pop("tipo"), **seguro}, ensure_ascii=False, default=str))
    except Exception:
        logging.getLogger(__name__).exception("No se pudo escribir el log JSON del evento")


_lock = threading.Lock()
_suscriptores: dict = {}
_contador_ids = itertools.count()

TAMANO_MAXIMO_COLA = 300
MAX_CORRIDAS_HISTORIAL = 50

_lock_corridas = threading.Lock()
_corridas: "OrderedDict[str, dict]" = OrderedDict()


def suscribirse():
    """Registra un nuevo cliente SSE. Devuelve `(id_suscriptor, cola)` — la
    cola es de donde `web/app.py` debe leer los eventos para ese cliente."""
    id_suscriptor = next(_contador_ids)
    cola: queue.Queue = queue.Queue(maxsize=TAMANO_MAXIMO_COLA)
    with _lock:
        _suscriptores[id_suscriptor] = cola
    return id_suscriptor, cola


def desuscribirse(id_suscriptor: int) -> None:
    """Elimina un cliente SSE (se llama cuando el navegador cierra la
    conexión) para no acumular colas de clientes que ya se fueron."""
    with _lock:
        _suscriptores.pop(id_suscriptor, None)


def iniciar_corrida(run_id: str, session_id: str, titulo: str) -> None:
    """Registra el inicio de una corrida (un turno completo disparado por UN
    mensaje del cliente) — se llama una sola vez por `POST /api/chat`, antes
    de invocar al orquestador. `titulo` es lo que verá el sidebar (un
    resumen corto del mensaje del cliente)."""
    with _lock_corridas:
        _corridas[run_id] = {
            "run_id": run_id,
            "session_id": session_id,
            "titulo": titulo,
            "iniciado_en": time.time(),
            "eventos": [],
        }
        _corridas.move_to_end(run_id)
        while len(_corridas) > MAX_CORRIDAS_HISTORIAL:
            _corridas.popitem(last=False)


def listar_corridas() -> list:
    """Resumen de las corridas guardadas (sin la lista completa de eventos,
    para que el sidebar sea liviano), de la más reciente a la más antigua."""
    with _lock_corridas:
        corridas = list(_corridas.values())
    corridas.sort(key=lambda c: c["iniciado_en"], reverse=True)
    resumenes = []
    for corrida in corridas:
        hubo_error = any(
            e["tipo"] == "modelo_fallo"
            or (e["tipo"] == "tool_resultado" and str(e.get("resultado", "")).startswith("ERROR"))
            for e in corrida["eventos"]
        )
        resumenes.append(
            {
                "run_id": corrida["run_id"],
                "session_id": corrida["session_id"],
                "titulo": corrida["titulo"],
                "iniciado_en": corrida["iniciado_en"],
                "total_eventos": len(corrida["eventos"]),
                "hubo_error": hubo_error,
                "terminada": any(e["tipo"] == "respuesta_final" for e in corrida["eventos"]),
            }
        )
    return resumenes


def obtener_corrida(run_id: str) -> Optional[dict]:
    """La corrida completa (con todos sus eventos) o `None` si no existe —
    ya sea porque nunca existió o porque salió del historial
    (`MAX_CORRIDAS_HISTORIAL`)."""
    with _lock_corridas:
        corrida = _corridas.get(run_id)
        return {**corrida, "eventos": list(corrida["eventos"])} if corrida else None


def publicar_evento(tipo: str, run_id: Optional[str] = None, **datos) -> None:
    """Publica un evento a TODOS los suscriptores conectados en este momento
    y, si `run_id` corresponde a una corrida activa, lo archiva ahí también.

    `tipo` identifica la naturaleza del evento (ej. "llamada_modelo",
    "tool_llamada") — ver los tipos exactos que emite `llm_loop.py`. `datos`
    son los campos propios de ese tipo (modelo, nombre de tool, argumentos,
    resultado, latencia, etc.) — todos deben ser serializables a JSON.
    """
    evento = {"tipo": tipo, "timestamp": time.time(), "run_id": run_id, **datos}
    _escribir_log_json(evento)
    metricas.registrar(evento)
    if run_id:
        with _lock_corridas:
            corrida = _corridas.get(run_id)
            if corrida is not None:
                corrida["eventos"].append(evento)
    with _lock:
        colas = list(_suscriptores.values())
    for cola in colas:
        try:
            cola.put_nowait(evento)
        except queue.Full:
            # Suscriptor lento (pestaña de dashboard abandonada): se
            # descarta el evento más viejo en vez de acumular memoria
            # indefinidamente o bloquear al agente real esperando espacio.
            try:
                cola.get_nowait()
            except queue.Empty:
                pass
            try:
                cola.put_nowait(evento)
            except queue.Full:
                pass


def recibir_siguiente(cola, timeout: Optional[float] = None):
    """Bloquea (de forma segura para llamarse desde un thread de FastAPI)
    hasta que llegue un evento nuevo para esta cola, o hasta `timeout`
    segundos. Devuelve `None` si se agotó el timeout sin eventos nuevos —
    `web/app.py` lo usa para mandar un "keep-alive" y no dejar morir la
    conexión SSE en proxies que cierran conexiones inactivas."""
    try:
        return cola.get(timeout=timeout)
    except queue.Empty:
        return None
