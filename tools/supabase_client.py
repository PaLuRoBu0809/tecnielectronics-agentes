"""
tools/supabase_client.py

Wrapper genérico y sin lógica de negocio sobre el REST de Supabase
(PostgREST). Ni `citas_tools.py` ni `catalog_tools.py` deberían hablar con
`requests` directamente para tocar Supabase — deben pasar por aquí.

Esto reemplaza los helpers "privados" (`_supabase_headers`, `_supabase_url`)
que antes vivían dentro de `calendar_tools.py` (renombrado a `citas_tools.py`
al quitar Google Calendar, ver docstring de ese módulo) y que
`catalog_tools.py` importaba por atrás — un módulo de catálogo dependiendo
de internals de otro módulo, una violación de capas.

Cada función de consulta/escritura devuelve datos de Python (dict/list),
nunca texto ya formateado para el cliente — ese formateo final es
responsabilidad de `formatear_fila`, la única función de este módulo
pensada para producir texto legible por el agente.
"""
from __future__ import annotations

import os
from typing import Optional

import requests


def headers() -> dict:
    """Cabeceras HTTP para autenticar contra Supabase.

    Usa la Service Role key (nunca la `anon`): así las escrituras no pasan
    por Row Level Security, igual que hacía el nodo HTTP Request de n8n.
    `Prefer: return=representation` es lo que le pide a PostgREST que
    devuelva la fila real tal como quedó guardada en cada INSERT/PATCH, en
    vez de solo un código de éxito — esa fila real es la que se usa después
    como fuente de verdad para armar el resumen que ve el cliente.
    """
    key = os.environ["SUPABASE_SERVICE_KEY"]
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def url(tabla: str) -> str:
    """URL REST completa de una tabla de Supabase, ej. `url('servicios_agendados')`."""
    base = os.environ["SUPABASE_URL"].rstrip("/")
    return f"{base}/rest/v1/{tabla}"


def get_rows(tabla: str, params: Optional[dict] = None) -> list:
    """SELECT genérico sobre `tabla`.

    `params` son query params de PostgREST tal cual (ej.
    `{"session_id": "eq.123", "select": "*", "order": "...", "limit": "5"}`).
    Devuelve la lista de filas (vacía si no hay resultados).
    """
    resp = requests.get(url(tabla), headers=headers(), params=params or {}, timeout=10)
    resp.raise_for_status()
    return resp.json()


def insert_row(tabla: str, payload: dict) -> dict:
    """INSERT de una sola fila en `tabla`.

    Devuelve la fila REAL tal como quedó guardada en Supabase (gracias a
    `Prefer: return=representation`) — esa es la fuente de verdad que debe
    usarse para confirmarle algo al cliente, nunca los valores que se
    enviaron de entrada.
    """
    resp = requests.post(url(tabla), headers=headers(), json=payload, timeout=10)
    resp.raise_for_status()
    filas = resp.json()
    return filas[0] if isinstance(filas, list) else filas


def upsert_row(tabla: str, payload: dict, on_conflict: str) -> dict:
    """INSERT de una fila, o UPDATE si ya existe otra con el mismo valor en
    la columna única `on_conflict` (ej. la llave primaria). Devuelve la fila
    tal como quedó guardada."""
    cabeceras = headers()
    cabeceras["Prefer"] = "resolution=merge-duplicates,return=representation"
    resp = requests.post(
        url(tabla),
        headers=cabeceras,
        params={"on_conflict": on_conflict},
        json=payload,
        timeout=10,
    )
    resp.raise_for_status()
    filas = resp.json()
    return filas[0] if isinstance(filas, list) else filas


def patch_rows(tabla: str, params: dict, payload: dict) -> dict:
    """UPDATE (PATCH) de la(s) fila(s) de `tabla` que cumplan `params`.

    Devuelve la primera fila actualizada, ya con los valores reales
    guardados. Lanza `RuntimeError` si `params` no coincidió con ninguna
    fila — evita construir un resumen a partir de una fila inexistente, que
    antes de este refactor pasaba silenciosamente como si fuera un éxito.
    """
    resp = requests.patch(url(tabla), headers=headers(), params=params, json=payload, timeout=10)
    resp.raise_for_status()
    filas = resp.json()
    if not filas:
        raise RuntimeError(f"PATCH a '{tabla}' con params={params} no afectó ninguna fila.")
    return filas[0] if isinstance(filas, list) else filas


def delete_rows(tabla: str, params: dict) -> None:
    """DELETE de la(s) fila(s) de `tabla` que cumplan `params`."""
    resp = requests.delete(url(tabla), headers=headers(), params=params, timeout=10)
    resp.raise_for_status()


def es_violacion_de_solapamiento(exc: Exception) -> bool:
    """True si `exc` es el error HTTP que devuelve PostgREST cuando una
    escritura viola el constraint `no_solapamiento_citas_confirmadas` (ver
    `sql/002_disponibilidad_sin_calendar.sql`) — es decir, cuando se intenta
    crear o mover una cita a un horario que ya choca con otra cita
    confirmada. Verificado empíricamente contra la API REST real: PostgREST
    devuelve **HTTP 400** (no 409) con `{"code": "23P01", ...}` en el cuerpo.

    Se usa para traducir ese error técnico en un mensaje amable para el
    cliente ("ese horario ya no está disponible") en vez de un error
    genérico de "no se pudo guardar"."""
    response = getattr(exc, "response", None)
    if response is None or response.status_code != 400:
        return False
    try:
        return response.json().get("code") == "23P01"
    except ValueError:
        return False


def formatear_fila(fila: dict, prefijo: str = "") -> str:
    """Convierte una fila (dict) de Supabase en texto plano `clave=valor |
    clave=valor | ...`, incluyendo TODAS las columnas que vengan en `fila`,
    sin necesidad de listarlas a mano en el código.

    Este es el cambio central que le permite al agente ver cualquier detalle
    de una cita que Google Calendar no expone (estado, servicio_id, o
    cualquier columna nueva que se agregue a la tabla en el futuro): en vez
    de que cada función elija a mano qué campos mostrar, se vuelca la fila
    completa tal como está en la base de datos.

    `prefijo` es opcional y se antepone al string completo (útil para
    numerar varias citas en una lista, ej. `prefijo="1) "`).
    """
    partes = [f"{clave}={valor}" for clave, valor in fila.items()]
    return f"{prefijo}{' | '.join(partes)}"
