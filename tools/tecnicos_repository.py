"""
tools/tecnicos_repository.py

Acceso de solo lectura a la tabla `tecnicos` (esquema en
`supabase/migrations/20260928000003_tecnicos.sql`). No la usa el agente directamente — el agente nunca
menciona técnicos al cliente (ver la nota de asignación automática en
`agents/servicio_tecnico_agent.py`); esta consulta es para la interfaz de
administración (`web/static/admin.html`), que sí necesita mostrar el nombre
del técnico asignado a cada cita.
"""
from __future__ import annotations

import os
from typing import Optional

from tools import supabase_client


def _tabla_tecnicos() -> str:
    return os.environ.get("SUPABASE_TABLE_TECNICOS", "tecnicos")


def listar_tecnicos() -> list:
    """Todos los técnicos registrados, con todas sus columnas."""
    return supabase_client.get_rows(_tabla_tecnicos(), params={"select": "*"})


def leer_tecnico_por_id(tecnico_id) -> Optional[dict]:
    """Un técnico puntual por id, o `None` si no existe."""
    filas = supabase_client.get_rows(
        _tabla_tecnicos(), params={"id": f"eq.{tecnico_id}", "select": "*"}
    )
    return filas[0] if filas else None
