"""
tools/citas_repository.py

Reglas de acceso a la tabla de citas (`servicios_agendados` por defecto) de
Supabase, construidas sobre el wrapper genérico `tools/supabase_client.py`.
Desde que se quitó Google Calendar (ver `tools/citas_tools.py`), este
módulo es la ÚNICA fuente de verdad de las citas — no hay ningún otro
sistema con el que sincronizar.

`leer_citas_confirmadas_en_rango` reemplaza la consulta que antes se hacía
contra la API de Calendar para calcular disponibilidad ({Consultar_eventos}
del prompt). El propio Postgres, además, garantiza con el constraint
`no_solapamiento_citas_confirmadas` (ver
`supabase/migrations/20260927000002_disponibilidad_sin_calendar.sql`) que dos citas "confirmado" nunca
puedan cruzarse en el tiempo — una garantía atómica que antes no existía ni
en Calendar ni en el código.
"""
from __future__ import annotations

import os
from typing import Optional

from tools import supabase_client

# Verificado 2026-09-27 contra el esquema real del proyecto de Supabase
# (zvxvtnxifmgubrgqegoh, tabla servicios_agendados) vía el conector MCP: la
# columna se llama "fecha_hora_fin", SIN el "2" que traía el n8n original —
# ese "2" era justo el typo que este archivo advertía que había que revisar.
CAMPO_FECHA_FIN_DB = "fecha_hora_fin"

# Valor de `estado` de una cita activa (default de la columna en Supabase).
ESTADO_CONFIRMADO = "confirmado"


def _tabla_citas() -> str:
    return os.environ.get("SUPABASE_TABLE_CITAS", "servicios_agendados")


def leer_cita_por_event_id(google_calendar_event_id: str) -> Optional[dict]:
    """Busca la fila de la tabla de citas cuyo `google_calendar_event_id`
    coincida. Devuelve `None` si no existe ninguna (nunca lanza excepción
    solo porque no haya resultados — eso es una respuesta válida)."""
    filas = supabase_client.get_rows(
        _tabla_citas(),
        params={"google_calendar_event_id": f"eq.{google_calendar_event_id}", "select": "*"},
    )
    return filas[0] if filas else None


def leer_citas_confirmadas_en_rango(
    fecha_inicio: str, fecha_fin: str, tecnico_id: Optional[int] = None
) -> list:
    """Citas con `estado='confirmado'` cuyo horario se solapa con el rango
    `[fecha_inicio, fecha_fin)`. Reemplaza la consulta que antes se hacía a
    Google Calendar ({Consultar_eventos}) para calcular disponibilidad.

    El solapamiento se expresa con dos comparaciones simples sobre columnas
    indexadas (`idx_servicios_agendados_fecha_confirmado`), sin depender del
    operador de rangos de PostgREST: una cita [a_inicio, a_fin) se solapa
    con la ventana consultada si `a_inicio < fecha_fin` Y `a_fin > fecha_inicio`.

    `tecnico_id` (opcional, ver `supabase/migrations/20260928000003_tecnicos.sql`): si se da, acota la
    disponibilidad a la agenda de ESE técnico — dos técnicos distintos
    pueden tener citas confirmadas a la misma hora sin chocar. Si se omite,
    trae la ocupación de TODOS los técnicos (útil solo para administración,
    nunca para calcular disponibilidad real de un servicio puntual, porque
    mostraría como "ocupado" un horario que el técnico correcto sí tiene libre).
    """
    params = {
        "estado": "eq.confirmado",
        "fecha_hora_inicio": f"lt.{fecha_fin}",
        CAMPO_FECHA_FIN_DB: f"gt.{fecha_inicio}",
        "select": f"fecha_hora_inicio,{CAMPO_FECHA_FIN_DB}",
        "order": "fecha_hora_inicio",
    }
    if tecnico_id is not None:
        params["tecnico_id"] = f"eq.{tecnico_id}"
    return supabase_client.get_rows(_tabla_citas(), params=params)


def leer_todas_las_citas(
    fecha_desde: Optional[str] = None, fecha_hasta: Optional[str] = None, limite: int = 500
) -> list:
    """Todas las citas registradas, de cualquier estado y de cualquier cliente,
    con todas sus columnas (`select=*`). Es la fuente de datos de la interfaz
    de administración (calendario + listado de registros) — a diferencia de
    `leer_citas_confirmadas_en_rango`, aquí NO se filtra por
    `estado='confirmado'` (la vista de administración necesita ver también
    las canceladas) ni se recortan las columnas devueltas.

    `fecha_desde`/`fecha_hasta` son opcionales (ISO 8601): si se dan ambos,
    se combinan en un único filtro `and=(...)` de PostgREST sobre
    `fecha_hora_inicio`, porque un `dict` de Python no puede tener la misma
    clave dos veces para expresar "gte X Y lte Y" por separado.
    """
    params = {"select": "*", "order": "fecha_hora_inicio.desc", "limit": str(limite)}
    condiciones = []
    if fecha_desde:
        condiciones.append(f"fecha_hora_inicio.gte.{fecha_desde}")
    if fecha_hasta:
        condiciones.append(f"fecha_hora_inicio.lte.{fecha_hasta}")
    if condiciones:
        params["and"] = f"({','.join(condiciones)})"
    return supabase_client.get_rows(_tabla_citas(), params=params)


def leer_citas_por_session(session_id: str, limite: int = 5) -> list:
    """Últimas `limite` citas de un cliente (identificado por `session_id`,
    su teléfono), de la más reciente a la más antigua."""
    return supabase_client.get_rows(
        _tabla_citas(),
        params={
            "session_id": f"eq.{session_id}",
            "select": "*",
            "order": "fecha_hora_inicio.desc",
            "limit": str(limite),
        },
    )


def insertar_cita(payload: dict) -> dict:
    """Crea el registro de una cita nueva. `payload` debe incluir ya el
    campo de fecha de fin con el nombre real de columna (`CAMPO_FECHA_FIN_DB`).
    Devuelve la fila tal como quedó guardada en Supabase."""
    return supabase_client.insert_row(_tabla_citas(), payload)


def actualizar_cita(google_calendar_event_id: str, payload: dict, session_id: str) -> dict:
    """Actualiza los campos de `payload` en la cita identificada por
    `google_calendar_event_id` Y perteneciente a `session_id`. Devuelve la
    fila ya actualizada, tal como quedó guardada en Supabase.

    El filtro por `session_id` va en el propio PATCH (no solo en una
    lectura previa): aunque quien llama olvide verificar el dueño, la base
    de datos nunca modifica la cita de otro cliente — `patch_rows` lanza
    `RuntimeError` si no coincide ninguna fila."""
    return supabase_client.patch_rows(
        _tabla_citas(),
        params={
            "google_calendar_event_id": f"eq.{google_calendar_event_id}",
            "session_id": f"eq.{session_id}",
        },
        payload=payload,
    )


def cancelar_cita(google_calendar_event_id: str, session_id: str, estado: str = "cancelado por cliente") -> dict:
    """Marca una cita como cancelada (soft-delete): actualiza su `estado` en
    vez de borrar la fila.

    Verificado 2026-09-27 contra el esquema real: `servicios_agendados`
    tiene una columna `estado` con default `'confirmado'`, y el prompt del
    Agente de Servicio Técnico (Flujo D, "historial de citas") espera poder
    mostrarle al cliente citas con un estado distinto de "Confirmado" — eso
    solo es posible si la fila se conserva en vez de borrarse. Devuelve la
    fila ya actualizada.

    Igual que `actualizar_cita`, filtra también por `session_id`: solo el
    cliente dueño de la cita puede cancelarla.
    """
    return supabase_client.patch_rows(
        _tabla_citas(),
        params={
            "google_calendar_event_id": f"eq.{google_calendar_event_id}",
            "session_id": f"eq.{session_id}",
        },
        payload={"estado": estado},
    )
