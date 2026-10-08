"""
tools/catalog_tools.py

{Servicio_tecnico}: catálogo de servicios técnicos (id, nombre, descripcion,
duracion_minutos, y cualquier otra columna que tenga la tabla real).

Muestra TODAS las columnas reales de cada fila (ver
`tools.supabase_client.formatear_fila`) en vez de una lista fija de campos,
para que una columna nueva en Supabase no requiera tocar este archivo.
"""
from __future__ import annotations

import os
from typing import Optional

from tools import supabase_client


def _tabla_catalogo() -> str:
    return os.environ.get("SUPABASE_TABLE_CATALOGO", "servicios_tecnicos")


def servicio_tecnico(consulta: str = "") -> str:
    """Devuelve el catálogo completo de servicios técnicos como texto plano,
    una línea por servicio con todas sus columnas. El agente lo usa tanto
    para mapear el problema del cliente a un servicio como para mostrarle el
    catálogo si pregunta directamente qué servicios ofrecen.

    `consulta` no filtra en el servidor (la tabla es pequeña): el mapeo
    "qué servicio corresponde" lo hace el modelo viendo el catálogo completo.
    """
    try:
        servicios = supabase_client.get_rows(_tabla_catalogo(), params={"select": "*"})
    except Exception as exc:
        return f"ERROR: No se pudo consultar el catálogo de servicios técnicos. Detalle técnico: {exc}"

    if not servicios:
        return "ERROR: El catálogo de servicios técnicos está vacío."

    return "\n".join(supabase_client.formatear_fila(s) for s in servicios)


def listar_catalogo() -> list:
    """Catálogo completo como lista de dicts (`select=*`), para el dashboard
    y para mostrar el nombre del servicio en las órdenes."""
    return supabase_client.get_rows(_tabla_catalogo(), params={"select": "*"})


def leer_servicio(servicio_id) -> Optional[dict]:
    """Fila completa del catálogo para UN servicio_id puntual; `None` si no existe."""
    filas = supabase_client.get_rows(
        _tabla_catalogo(), params={"id": f"eq.{servicio_id}", "select": "*"}
    )
    return filas[0] if filas else None
