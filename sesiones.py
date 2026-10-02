"""
sesiones.py

Memoria conversacional persistente de los agentes, compartida por los dos
"front doors" de este proyecto: el arnés de consola (`main.py`) y la
interfaz de chat web (`web/app.py`).

Cada sesión se identifica por `session_id` (el teléfono del cliente, tal
como llegaría desde WhatsApp) y guarda el historial de CADA agente por
separado, en un diccionario `{clave_agente: [mensajes]}` (columna
`historiales`, Fase 11): "orquestador", "servicio_tecnico" y, cuando exista,
"ventas". Agregar un agente no requiere cambiar este archivo ni la tabla.

Antes esto vivía en un diccionario en la RAM del proceso: se perdía en cada
reinicio y no se compartía entre instancias, lo que lo hacía inservible
para desplegar en un hosting como Render. Ahora se guarda en la tabla
`conversaciones` de Supabase (esquema en `supabase/migrations/20260927000001_conversaciones.sql`): se
lee al inicio de cada turno con `obtener()` y se escribe al final con
`guardar()`.

Concurrencia (Fase 7 de `docs/PLAN_DE_MEJORAS.md`): antes, si llegaban dos
mensajes de la MISMA sesión casi a la vez (normal en WhatsApp: "hola" y
enseguida "quiero agendar"), ambos turnos leían el mismo historial y el
último en guardar borraba lo que hizo el otro. Ahora
`AlmacenSesiones.turno_exclusivo(session_id)` serializa los turnos de una
misma sesión: el segundo mensaje ESPERA a que termine el primero y lee el
historial ya actualizado. Sesiones distintas siguen en paralelo.

Limitación conocida: el lock vive en la RAM de UN proceso. Con varios
workers o instancias haría falta un guardado condicionado (columna
`version` en `conversaciones` + `PATCH ...?version=eq.N`); un advisory lock
de Postgres NO sirve a través de PostgREST, porque cada petición HTTP es su
propia transacción. Por eso el Dockerfile arranca un solo worker.

Instrumentación para el panel "Flujo en Vivo" (`web/static/flujo.js`):
`obtener()`/`guardar()` publican eventos `memoria_lectura`/`memoria_escritura`
(ver `tools/eventos_agente.py`) — es el único punto de este proyecto que
toca la tabla `conversaciones`, así que es el lugar correcto para instrumentar
el nodo "Memoria" del diagrama, sin tocar `llm_loop.py` para nada que no sea
modelos/tools.
"""
from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

from contexto_conversacion import limitar_historial_guardado
from llm_loop import sanear_historial
from tools import eventos_agente, supabase_client


def _tabla_conversaciones() -> str:
    return os.environ.get("SUPABASE_TABLE_CONVERSACIONES", "conversaciones")


def _espera_maxima_turno() -> float:
    """Cuánto espera un mensaje a que termine el turno anterior de su misma
    sesión: el tope de duración de un turno (`SEGUNDOS_MAX_POR_TURNO`) más un
    margen para la última petición en curso y el guardado."""
    try:
        return float(os.environ.get("SEGUNDOS_MAX_POR_TURNO", 90)) + 30
    except ValueError:
        return 120.0


def _preparar_para_guardar(historial: list) -> list:
    """Sanea (sin tool calls huérfanas) y compacta (Fase 6: recorta
    resultados de tools antiguos y aplica el tope de mensajes). Se sanea otra
    vez al final porque el tope podría cortar justo antes de un resultado."""
    return sanear_historial(limitar_historial_guardado(sanear_historial(historial)))


# Columnas de la estructura anterior a la Fase 11 (una por agente). Solo se
# LEEN, como respaldo para filas que la migración 005 todavía no copió.
_COLUMNAS_VIEJAS = {"orquestador": "historial_orquestador", "servicio_tecnico": "historial_servicio_tecnico"}


class TurnoEnCurso(Exception):
    """El turno anterior de la misma sesión no terminó a tiempo."""


class _LocksPorSesion:
    """Un `threading.Lock` por `session_id`, creado bajo demanda y borrado
    cuando nadie lo usa (para no acumular un lock por cada cliente que
    escribió alguna vez)."""

    def __init__(self):
        self._guardia = threading.Lock()
        self._locks: dict = {}  # {session_id: [lock, cantidad_de_turnos_usandolo]}

    @contextmanager
    def exclusivo(self, session_id: str, timeout: float):
        with self._guardia:
            entrada = self._locks.setdefault(session_id, [threading.Lock(), 0])
            entrada[1] += 1
        adquirido = entrada[0].acquire(timeout=timeout)
        try:
            if not adquirido:
                raise TurnoEnCurso(session_id)
            yield
        finally:
            if adquirido:
                entrada[0].release()
            with self._guardia:
                entrada[1] -= 1
                if entrada[1] == 0:
                    del self._locks[session_id]

    def activos(self) -> int:
        with self._guardia:
            return len(self._locks)


class AlmacenSesiones:
    """Lee y guarda en Supabase el historial de conversación de cada sesión."""

    def __init__(self):
        self._locks = _LocksPorSesion()

    def turno_exclusivo(self, session_id: str, timeout: Optional[float] = None):
        """Context manager que envuelve UN turno completo (obtener -> agentes
        -> guardar) de `session_id`, para que dos mensajes de la misma sesión
        no se pisen el historial. Lanza `TurnoEnCurso` si el turno anterior
        no termina dentro de `timeout` segundos."""
        return self._locks.exclusivo(session_id, _espera_maxima_turno() if timeout is None else timeout)

    def obtener(self, session_id: str, run_id: Optional[str] = None) -> dict:
        """Devuelve `{clave_agente: [mensajes]}` con el historial guardado de
        esa sesión (ej. `{"orquestador": [...], "servicio_tecnico": [...]}`),
        o `{}` si es la primera vez que escribe (la fila se crea al llamar
        `guardar`). Un agente sin historial todavía simplemente no aparece.

        Fase 11: lee la columna genérica `historiales`. Si una fila todavía
        no se migró (columna vacía), arma el diccionario desde las columnas
        viejas `historial_orquestador` / `historial_servicio_tecnico`.

        `run_id` (opcional) solo etiqueta el evento `memoria_lectura` que se
        publica al panel "Flujo en Vivo" — ver `tools/eventos_agente.py`."""
        inicio = time.perf_counter()
        filas = supabase_client.get_rows(
            _tabla_conversaciones(),
            params={"session_id": f"eq.{session_id}", "select": "*"},
        )
        eventos_agente.publicar_evento(
            "memoria_lectura",
            session_id=session_id,
            run_id=run_id,
            encontrada=bool(filas),
            latencia_ms=round((time.perf_counter() - inicio) * 1000),
        )
        if not filas:
            return {}
        fila = filas[0]
        historiales = fila.get("historiales") or {}
        if not historiales:
            for clave, columna_vieja in _COLUMNAS_VIEJAS.items():
                if fila.get(columna_vieja):
                    historiales[clave] = fila[columna_vieja]
        return {clave: list(mensajes or []) for clave, mensajes in historiales.items()}

    def guardar(self, session_id: str, historiales: dict, run_id: Optional[str] = None) -> None:
        """Persiste `{clave_agente: [mensajes]}` al final de un turno, creando
        la fila de la sesión si aún no existe. `run_id` (opcional) solo
        etiqueta el evento `memoria_escritura` publicado.

        Cada historial pasa por `_preparar_para_guardar` (saneado + Fase 6):
        nunca se persiste un `assistant` con `tool_calls` sin sus resultados,
        que haría fallar todos los turnos siguientes de la sesión."""
        preparados = {clave: _preparar_para_guardar(mensajes) for clave, mensajes in historiales.items()}
        inicio = time.perf_counter()
        supabase_client.upsert_row(
            _tabla_conversaciones(),
            payload={
                "session_id": session_id,
                "historiales": preparados,
                "actualizado_en": datetime.now(timezone.utc).isoformat(),
            },
            on_conflict="session_id",
        )
        eventos_agente.publicar_evento(
            "memoria_escritura",
            session_id=session_id,
            run_id=run_id,
            mensajes_por_agente={clave: len(m) for clave, m in preparados.items()},
            latencia_ms=round((time.perf_counter() - inicio) * 1000),
        )

    def ids(self) -> list:
        """Lista los `session_id` con conversación guardada, de la más
        reciente a la más antigua."""
        filas = supabase_client.get_rows(
            _tabla_conversaciones(),
            params={"select": "session_id", "order": "actualizado_en.desc"},
        )
        return [f["session_id"] for f in filas]
