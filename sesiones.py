"""
sesiones.py

Memoria conversacional persistente de los agentes, compartida por los dos
"front doors" de este proyecto: el arnés de consola (`main.py`) y la
interfaz de chat web (`web/app.py`).

Cada sesión se identifica por `session_id` (el teléfono del cliente, tal
como llegaría desde WhatsApp) y guarda el historial de mensajes de AMBOS
agentes por separado ("orq" para el Orquestador, "st" para el Agente de
Servicio Técnico) — cada uno en su propio hilo de conversación, tal como
exige `agents.orquestador.run()`.

Antes esto vivía en un diccionario en la RAM del proceso: se perdía en cada
reinicio y no se compartía entre instancias, lo que lo hacía inservible
para desplegar en un hosting como Render. Ahora se guarda en la tabla
`conversaciones` de Supabase (esquema en `sql/001_conversaciones.sql`): se
lee al inicio de cada turno con `obtener()` y se escribe al final con
`guardar()`.

Limitación conocida: si llegan dos mensajes de la MISMA sesión al mismo
tiempo, el último en guardar gana (no hay bloqueo). Para un chat de
WhatsApp, donde un cliente escribe de a un mensaje, es aceptable.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from tools import supabase_client


def _tabla_conversaciones() -> str:
    return os.environ.get("SUPABASE_TABLE_CONVERSACIONES", "conversaciones")


class AlmacenSesiones:
    """Lee y guarda en Supabase el historial de conversación de cada sesión."""

    def obtener(self, session_id: str) -> dict:
        """Devuelve `{"orq": [...], "st": [...]}` con el historial guardado
        de esa sesión, o ambas listas vacías si es la primera vez que
        escribe (la fila recién se crea al llamar `guardar`)."""
        filas = supabase_client.get_rows(
            _tabla_conversaciones(),
            params={
                "session_id": f"eq.{session_id}",
                "select": "historial_orquestador,historial_servicio_tecnico",
            },
        )
        if not filas:
            return {"orq": [], "st": []}
        return {
            "orq": filas[0]["historial_orquestador"],
            "st": filas[0]["historial_servicio_tecnico"],
        }

    def guardar(self, session_id: str, historial_orq: list, historial_st: list) -> None:
        """Persiste el historial de ambos agentes al final de un turno,
        creando la fila de la sesión si aún no existe."""
        supabase_client.upsert_row(
            _tabla_conversaciones(),
            payload={
                "session_id": session_id,
                "historial_orquestador": historial_orq,
                "historial_servicio_tecnico": historial_st,
                "actualizado_en": datetime.now(timezone.utc).isoformat(),
            },
            on_conflict="session_id",
        )

    def ids(self) -> list:
        """Lista los `session_id` con conversación guardada, de la más
        reciente a la más antigua."""
        filas = supabase_client.get_rows(
            _tabla_conversaciones(),
            params={"select": "session_id", "order": "actualizado_en.desc"},
        )
        return [f["session_id"] for f in filas]
