"""
tools/metricas.py

Métricas y alertas mínimas del agente (Fase 8.3 de `docs/PLAN_DE_MEJORAS.md`).

No instrumenta nada por su cuenta: `tools/eventos_agente.publicar_evento`
le pasa CADA evento que ya se publica para el panel "Flujo en Vivo", y aquí
se derivan los contadores. Así agregar una métrica nunca obliga a tocar el
loop ni los agentes.

Qué cuenta (desde que arrancó el proceso):
- turnos y su razón de parada (un evento `resumen_turno` por mensaje);
- llamadas al LLM, tokens y costo;
- fallos de modelo por tipo (`modelo_fallo.tipo_error`);
- errores devueltos por tools (`tool_resultado` que empieza con "ERROR").

Alertas: son líneas de log con el prefijo `ALERTA` (nivel ERROR o CRITICAL),
pensadas para engancharse a las alertas por log del hosting (Render, etc.).
Cada tipo de alerta se emite como mucho una vez cada `SILENCIO_ALERTA_S`
para no inundar el log:
- credenciales/saldo de OpenRouter (401/402) -> CRITICAL de inmediato;
- tasa de turnos que NO terminan en "final" > 30 % en los últimos 20;
- tasa de errores de tools > 50 % en las últimas 30 ejecuciones.

Limitación: contadores en RAM de un proceso; se reinician con el servidor.
Para históricos, los logs JSON (ver `eventos_agente`) son la fuente.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import Counter, deque

logger = logging.getLogger("tecnielectronics.alertas")

VENTANA_TURNOS = 20
UMBRAL_FALLBACK = 0.30
VENTANA_TOOLS = 30
UMBRAL_ERRORES_TOOL = 0.50
SILENCIO_ALERTA_S = 600.0

_lock = threading.Lock()
_contadores: Counter = Counter()
_razones: Counter = Counter()
_fallos_modelo: Counter = Counter()
_ultimos_turnos: deque = deque(maxlen=VENTANA_TURNOS)  # True si terminó en "final"
_ultimas_tools: deque = deque(maxlen=VENTANA_TOOLS)  # True si la tool devolvió ERROR
_ultima_alerta: dict = {}
_inicio = time.time()


def _alertar(clave: str, nivel: int, mensaje: str, *args) -> None:
    ahora = time.monotonic()
    if ahora - _ultima_alerta.get(clave, -SILENCIO_ALERTA_S) < SILENCIO_ALERTA_S:
        return
    _ultima_alerta[clave] = ahora
    logger.log(nivel, "ALERTA %s: " + mensaje, clave, *args)


def registrar(evento: dict) -> None:
    """Actualiza los contadores con un evento de `eventos_agente`. Nunca lanza
    (una métrica rota no debe tumbar un turno de un cliente)."""
    try:
        _registrar(evento)
    except Exception:
        logger.exception("Error registrando métrica para el evento %s", evento.get("tipo"))


def _registrar(evento: dict) -> None:
    tipo = evento.get("tipo")
    tasa = tasa_tools = 0.0
    lleno = lleno_tools = False
    with _lock:
        if tipo == "resumen_turno":
            _contadores["turnos"] += 1
            _contadores["llamadas_llm"] += evento.get("llamadas_llm", 0)
            _contadores["tokens_entrada"] += evento.get("tokens_entrada", 0)
            _contadores["tokens_salida"] += evento.get("tokens_salida", 0)
            _contadores["costo_micro_usd"] += round(evento.get("costo_usd", 0) * 1_000_000)
            razones = evento.get("razones") or []
            # La razón que ve el cliente es la del último loop que terminó
            # (el del orquestador); las del sub-agente se cuentan aparte.
            for r in razones:
                _razones[f"{r.get('agente')}:{r.get('razon')}"] += 1
            final = bool(razones) and razones[-1].get("razon") == "final"
            _ultimos_turnos.append(final)
            tasa = _ultimos_turnos.count(False) / len(_ultimos_turnos)
            lleno = len(_ultimos_turnos) == VENTANA_TURNOS
        elif tipo == "modelo_fallo":
            tipo_error = evento.get("tipo_error") or "desconocido"
            _fallos_modelo[tipo_error] += 1
        elif tipo == "tool_resultado":
            es_error = str(evento.get("resultado", "")).startswith("ERROR")
            _contadores["tools_ejecutadas"] += 1
            _contadores["tools_con_error"] += es_error
            _ultimas_tools.append(es_error)
            tasa_tools = _ultimas_tools.count(True) / len(_ultimas_tools)
            lleno_tools = len(_ultimas_tools) == VENTANA_TOOLS

    if tipo == "modelo_fallo" and evento.get("tipo_error") == "credenciales":
        _alertar("credenciales_openrouter", logging.CRITICAL,
                 "OpenRouter rechazó la clave o no hay saldo (401/402). Revisa OPENROUTER_API_KEY y los créditos.")
    elif tipo == "resumen_turno" and lleno and tasa > UMBRAL_FALLBACK:
        _alertar("tasa_fallback", logging.ERROR,
                 "%.0f%% de los últimos %s turnos no terminaron en una respuesta normal del modelo.",
                 tasa * 100, VENTANA_TURNOS)
    elif tipo == "tool_resultado" and lleno_tools and tasa_tools > UMBRAL_ERRORES_TOOL:
        _alertar("tasa_errores_tool", logging.ERROR,
                 "%.0f%% de las últimas %s ejecuciones de tools devolvieron ERROR.", tasa_tools * 100, VENTANA_TOOLS)


def instantanea() -> dict:
    """Estado actual de las métricas, para `GET /api/metricas`."""
    with _lock:
        turnos = _contadores["turnos"]
        return {
            "desde": _inicio,
            "turnos": turnos,
            "llamadas_llm": _contadores["llamadas_llm"],
            "llamadas_llm_por_turno": round(_contadores["llamadas_llm"] / turnos, 2) if turnos else 0,
            "tokens_entrada": _contadores["tokens_entrada"],
            "tokens_salida": _contadores["tokens_salida"],
            "costo_usd": _contadores["costo_micro_usd"] / 1_000_000,
            "razones_parada": dict(_razones),
            "fallos_modelo_por_tipo": dict(_fallos_modelo),
            "tools_ejecutadas": _contadores["tools_ejecutadas"],
            "tools_con_error": _contadores["tools_con_error"],
            "tasa_fallback_reciente": (
                round(_ultimos_turnos.count(False) / len(_ultimos_turnos), 3) if _ultimos_turnos else 0
            ),
        }


def reiniciar() -> None:
    """Borra contadores y alertas (usado por los tests)."""
    global _inicio
    with _lock:
        for c in (_contadores, _razones, _fallos_modelo, _ultima_alerta):
            c.clear()
        _ultimos_turnos.clear()
        _ultimas_tools.clear()
        _inicio = time.time()
