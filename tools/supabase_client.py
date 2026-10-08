"""
tools/supabase_client.py

Wrapper genérico y sin lógica de negocio sobre el REST de Supabase
(PostgREST). Ningún otro módulo debería hablar con `requests` directamente
para tocar Supabase — deben pasar por aquí.

Cada función de consulta/escritura devuelve datos de Python (dict/list),
nunca texto ya formateado para el cliente — ese formateo final es
responsabilidad de `formatear_fila`, la única función de este módulo
pensada para producir texto legible por el agente.

Red (Fase 2 de `docs/PLAN_DE_MEJORAS.md`):
- Timeout separado de conexión y de lectura (`_timeout()`): ninguna llamada
  a Supabase puede quedarse colgada indefinidamente.
- Reintentos con backoff exponencial y jitter SOLO en `get_rows` (lecturas,
  idempotentes). Las escrituras (insert/upsert/patch/delete) NUNCA se
  reintentan aquí: tras un timeout no se sabe si la escritura llegó a
  aplicarse, y reintentar a ciegas podría duplicar una cita.
"""
from __future__ import annotations

import logging
import os
import random
import time
from typing import Optional

import requests

from tools.errores_negocio import ErrorNegocio

logger = logging.getLogger(__name__)

REINTENTOS_LECTURA = 2  # intentos extra, además del primero
_ESTADOS_TRANSITORIOS = {502, 503, 504}


def _timeout() -> tuple:
    """(conexión, lectura) en segundos, configurables por entorno."""
    try:
        conexion = float(os.environ.get("SUPABASE_TIMEOUT_CONEXION", 5))
        lectura = float(os.environ.get("SUPABASE_TIMEOUT_LECTURA", 15))
    except ValueError:
        conexion, lectura = 5.0, 15.0
    return conexion, lectura


def _esperar_backoff(intento: int) -> None:
    """0.3 s, 0.6 s, ... más un jitter aleatorio, para que varios clientes
    que fallaron a la vez no reintenten todos en el mismo instante."""
    time.sleep(0.3 * (2 ** intento) + random.uniform(0, 0.3))


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

    Reintenta hasta `REINTENTOS_LECTURA` veces ante errores de red o
    502/503/504, con backoff y jitter. Un 4xx no se reintenta (repetir la
    misma petición inválida daría el mismo error).
    """
    return _con_reintentos(
        lambda: requests.get(url(tabla), headers=headers(), params=params or {}, timeout=_timeout()),
        f"GET {tabla}",
    )


def _con_reintentos(peticion, descripcion: str):
    """Ejecuta `peticion()` (una lectura idempotente) con los reintentos y
    el backoff descritos en `get_rows`, y devuelve el JSON de la respuesta."""
    for intento in range(REINTENTOS_LECTURA + 1):
        ultimo = intento == REINTENTOS_LECTURA
        try:
            resp = peticion()
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            if ultimo:
                raise
            logger.warning("%s falló (%s); reintento %s", descripcion, type(exc).__name__, intento + 1)
            _esperar_backoff(intento)
            continue
        if resp.status_code in _ESTADOS_TRANSITORIOS and not ultimo:
            logger.warning("%s respondió %s; reintento %s", descripcion, resp.status_code, intento + 1)
            _esperar_backoff(intento)
            continue
        resp.raise_for_status()
        return resp.json()
    raise AssertionError("inalcanzable")  # el último intento siempre retorna o lanza


def rpc(funcion: str, parametros: dict, solo_lectura: bool = False):
    """Llama a una función de Postgres expuesta por PostgREST
    (`POST /rest/v1/rpc/<funcion>`) y devuelve su resultado ya decodificado.

    `solo_lectura=True` solo para funciones que no escriben (ej. la búsqueda
    de productos): se reintentan como `get_rows`. Las que escriben (crear
    orden, añadir al carrito) NUNCA se reintentan, por la misma razón que
    `insert_row`: tras un timeout no se sabe si se aplicaron.

    Si la función lanza un error de negocio (`raise exception 'CODIGO'`),
    PostgREST responde 400 y aquí sale como `HTTPError`; ver
    `error_de_negocio` para leer su código.
    """
    def peticion():
        return requests.post(url(f"rpc/{funcion}"), headers=headers(), json=parametros, timeout=_timeout())

    if solo_lectura:
        return _con_reintentos(peticion, f"RPC {funcion}")
    resp = peticion()
    resp.raise_for_status()
    return resp.json()


# SQLSTATE de `raise exception` sin código explícito en PL/pgSQL.
_CODIGO_RAISE_EXCEPTION = "P0001"


def error_de_negocio(exc: Exception) -> Optional[tuple]:
    """Si `exc` es un error lanzado A PROPÓSITO por una función de Postgres
    (`raise exception 'STOCK_INSUFICIENTE' using detail = ...`), devuelve
    `(codigo, detalle)`. Para cualquier otro error (red, credenciales, un
    bug) devuelve `None`: esos no son reglas de negocio y no deben
    traducirse como tales."""
    response = getattr(exc, "response", None)
    if response is None or response.status_code != 400:
        return None
    try:
        cuerpo = response.json()
    except ValueError:
        return None
    if cuerpo.get("code") != _CODIGO_RAISE_EXCEPTION:
        return None
    return cuerpo.get("message"), cuerpo.get("details")


def rpc_con_reglas(funcion: str, parametros: dict):
    """`rpc` para funciones que aplican reglas del negocio: su error de
    negocio sale como `ErrorNegocio`; cualquier otro error, tal cual."""
    try:
        return rpc(funcion, parametros)
    except requests.exceptions.HTTPError as exc:
        negocio = error_de_negocio(exc)
        if negocio is None:
            raise
        raise ErrorNegocio(*negocio) from exc


def insert_row(tabla: str, payload: dict) -> dict:
    """INSERT de una sola fila en `tabla`.

    Devuelve la fila REAL tal como quedó guardada en Supabase (gracias a
    `Prefer: return=representation`) — esa es la fuente de verdad que debe
    usarse para confirmarle algo al cliente, nunca los valores que se
    enviaron de entrada.
    """
    resp = requests.post(url(tabla), headers=headers(), json=payload, timeout=_timeout())
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
        timeout=_timeout(),
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
    resp = requests.patch(url(tabla), headers=headers(), params=params, json=payload, timeout=_timeout())
    resp.raise_for_status()
    filas = resp.json()
    if not filas:
        raise RuntimeError(f"PATCH a '{tabla}' con params={params} no afectó ninguna fila.")
    return filas[0] if isinstance(filas, list) else filas


def delete_rows(tabla: str, params: dict) -> list:
    """DELETE de la(s) fila(s) de `tabla` que cumplan `params`. Devuelve las
    filas borradas (lista vacía si no coincidió ninguna), gracias a
    `Prefer: return=representation`."""
    resp = requests.delete(url(tabla), headers=headers(), params=params, timeout=_timeout())
    resp.raise_for_status()
    return resp.json() if resp.content else []


# Columnas que NUNCA se le muestran al modelo, aunque vengan en la fila:
# - session_id: es el teléfono desde el que escribe el cliente (dato
#   personal que el agente no necesita — el sistema ya lo inyecta solo).
# - tecnico_id: el prompt prohíbe mencionar técnicos al cliente; si el
#   modelo no lo ve, no lo puede filtrar.
# - periodo: columna generada (tstzrange) para el constraint anti-choque;
#   solo es ruido y tokens para el modelo.
# Es una lista de OCULTAS (no de permitidas) a propósito: se conserva el
# diseño original de este módulo, donde una columna nueva de negocio aparece
# sola sin tocar código. Si agregas una columna sensible, agrégala aquí.
COLUMNAS_OCULTAS_AL_AGENTE = frozenset({"session_id", "tecnico_id", "periodo"})


def formatear_fila(fila: dict, prefijo: str = "") -> str:
    """Convierte una fila (dict) de Supabase en texto plano `clave=valor |
    clave=valor | ...`, incluyendo todas las columnas que vengan en `fila`
    (salvo las de `COLUMNAS_OCULTAS_AL_AGENTE`), sin necesidad de listarlas
    a mano en el código.

    Este es el cambio central que le permite al agente ver cualquier detalle
    de una cita que Google Calendar no expone (estado, servicio_id, o
    cualquier columna nueva que se agregue a la tabla en el futuro): en vez
    de que cada función elija a mano qué campos mostrar, se vuelca la fila
    completa tal como está en la base de datos.

    `prefijo` es opcional y se antepone al string completo (útil para
    numerar varias citas en una lista, ej. `prefijo="1) "`).
    """
    partes = [f"{clave}={valor}" for clave, valor in fila.items() if clave not in COLUMNAS_OCULTAS_AL_AGENTE]
    return f"{prefijo}{' | '.join(partes)}"
