"""
tools/catalog_tools.py

{Servicio_tecnico}: catálogo de servicios técnicos (id, nombre, descripcion,
duracion_minutos, y cualquier otra columna que tenga la tabla real). El JSON
de este subflujo no venía entre los archivos compartidos, así que esta
función ASUME una tabla en Supabase con esas columnas — ajusta
`SUPABASE_TABLE_CATALOGO` si tu esquema real es distinto.

{Consultar_servicio_agendado}: consulta las citas de un cliente (hasta 5, de
la más reciente a la más antigua) vía `tools.citas_repository`, filtrando
por `session_id` = teléfono del cliente.

Ambas funciones muestran TODAS las columnas reales de cada fila (ver
`tools.supabase_client.formatear_fila`) en vez de una lista fija de campos,
para que el agente pueda ver cualquier detalle que Google Calendar no expone
y para que una columna nueva en Supabase no requiera tocar este archivo.
"""
from __future__ import annotations

import os

from tools import supabase_client
from tools.citas_repository import leer_citas_por_session


def _tabla_catalogo() -> str:
    return os.environ.get("SUPABASE_TABLE_CATALOGO", "servicios_tecnicos")


def servicio_tecnico(consulta: str = "") -> str:
    """Devuelve el catálogo completo de servicios técnicos como texto plano,
    una línea por servicio con todas sus columnas. El agente lo usa tanto
    para mapear el problema del cliente a un servicio como para mostrarle el
    catálogo si pregunta directamente qué servicios ofrecen.

    `consulta` no filtra en el servidor (la tabla de catálogo suele ser
    pequeña) — se acepta por si el modelo la envía como contexto, pero el
    mapeo real "cuál servicio corresponde" lo hace el propio modelo viendo
    el catálogo completo, tal como especifica el prompt original.
    """
    try:
        servicios = supabase_client.get_rows(_tabla_catalogo(), params={"select": "*"})
    except Exception as exc:
        return f"ERROR: No se pudo consultar el catálogo de servicios técnicos. Detalle técnico: {exc}"

    if not servicios:
        return "ERROR: El catálogo de servicios técnicos está vacío."

    return "\n".join(supabase_client.formatear_fila(s) for s in servicios)


def consultar_servicio_agendado(session_id: str) -> str:
    """Traducción de {Consultar_servicio_agendado}. `session_id` es el
    teléfono del cliente. En el flujo real este valor NO lo pide el agente —
    lo resuelve el sistema automáticamente a partir del número de WhatsApp
    entrante; aquí se recibe como parámetro para poder probar el agente
    suelto, fuera de WhatsApp.
    """
    try:
        citas = leer_citas_por_session(session_id, limite=5)
    except Exception as exc:
        return f"ERROR: No se pudieron consultar las citas del cliente. Detalle técnico: {exc}"

    if not citas:
        return "Sin citas registradas para este cliente."

    return "\n".join(
        supabase_client.formatear_fila(c, prefijo=f"{i}) ") for i, c in enumerate(citas, start=1)
    )
