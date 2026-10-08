"""
tools/ordenes_servicio_repository.py

Órdenes de servicio técnico (`ordenes_servicio`) y su historial de notas
(`seguimiento_orden_servicio`), ver
`supabase/migrations/20261008000013_ordenes_servicio.sql`.

Las escrituras pasan SIEMPRE por funciones de Postgres, que aplican las
reglas en una sola transacción (el cliente solo cambia o cancela sus órdenes
y antes de llevar el equipo; todo seguimiento lleva nota y responsable).
Sus errores salen como `ErrorNegocio`.

Las lecturas del agente filtran por `session_id`: un cliente nunca ve las
órdenes de otro. Las de `*_todas`/`leer` son solo para el dashboard.
"""
from __future__ import annotations

from datetime import date, time
from typing import Optional

from tools import supabase_client

ESTADO_PENDIENTE = "PENDIENTE_RECEPCION"
ESTADO_CANCELADO = "CANCELADO"

TABLA_ORDENES = "ordenes_servicio"
TABLA_SEGUIMIENTO = "seguimiento_orden_servicio"


def _hora(valor: Optional[time]) -> Optional[str]:
    return None if valor is None else valor.strftime("%H:%M")


def _fecha(valor: Optional[date]) -> Optional[str]:
    return None if valor is None else valor.isoformat()


# ---------------------------------------------------------------------------
# Agente (siempre filtrado por cliente)
# ---------------------------------------------------------------------------

def crear(
    session_id: str,
    cliente_nombre: str,
    cliente_telefono: str,
    servicio_id: int,
    equipo: str,
    descripcion: str,
    fecha_entrega: date,
    hora_aproximada: Optional[time] = None,
) -> dict:
    return supabase_client.rpc_con_reglas("crear_orden_servicio", {
        "p_session_id": session_id,
        "p_cliente_nombre": cliente_nombre,
        "p_cliente_telefono": cliente_telefono,
        "p_servicio_id": servicio_id,
        "p_equipo": equipo,
        "p_descripcion": descripcion,
        "p_fecha_entrega": _fecha(fecha_entrega),
        "p_hora_aproximada": _hora(hora_aproximada),
    })


def listar_del_cliente(session_id: str, limite: int = 5) -> list:
    """Órdenes del cliente, de la más reciente a la más antigua."""
    return supabase_client.get_rows(TABLA_ORDENES, params={
        "session_id": f"eq.{session_id}", "select": "*", "order": "creado_en.desc,numero.desc", "limit": str(limite),
    })


def leer_del_cliente(session_id: str, numero: int) -> Optional[dict]:
    """La orden si es de este cliente; `None` si no existe o es de otro."""
    filas = supabase_client.get_rows(
        TABLA_ORDENES, params={"session_id": f"eq.{session_id}", "numero": f"eq.{numero}", "select": "*"}
    )
    return filas[0] if filas else None


def modificar(
    session_id: str,
    numero: int,
    fecha_entrega: Optional[date] = None,
    hora_aproximada: Optional[time] = None,
    cliente_nombre: Optional[str] = None,
    cliente_telefono: Optional[str] = None,
    equipo: Optional[str] = None,
    descripcion: Optional[str] = None,
    servicio_id: Optional[int] = None,
) -> dict:
    """Cambia solo lo enviado (`None` = dejar igual)."""
    return supabase_client.rpc_con_reglas("modificar_orden_servicio", {
        "p_numero": numero,
        "p_session_id": session_id,
        "p_fecha_entrega": _fecha(fecha_entrega),
        "p_hora_aproximada": _hora(hora_aproximada),
        "p_cliente_nombre": cliente_nombre,
        "p_cliente_telefono": cliente_telefono,
        "p_equipo": equipo,
        "p_descripcion": descripcion,
        "p_servicio_id": servicio_id,
    })


def cancelar(session_id: str, numero: int) -> dict:
    return supabase_client.rpc_con_reglas("cancelar_orden_servicio", {"p_numero": numero, "p_session_id": session_id})


def seguimiento(numeros: list) -> dict:
    """{numero: [notas de la más antigua a la más reciente]} de varias órdenes
    en una sola consulta."""
    if not numeros:
        return {}
    filas = supabase_client.get_rows(TABLA_SEGUIMIENTO, params={
        "numero": f"in.({','.join(str(int(n)) for n in numeros)})", "select": "*", "order": "creado_en.asc,id.asc",
    })
    agrupadas: dict = {int(n): [] for n in numeros}
    for fila in filas:
        agrupadas.setdefault(fila["numero"], []).append(fila)
    return agrupadas


# ---------------------------------------------------------------------------
# Dashboard de la empresa
# ---------------------------------------------------------------------------

def listar_todas(desde: Optional[str] = None, hasta: Optional[str] = None, limite: int = 1000) -> list:
    """Órdenes de todos los clientes, opcionalmente entre dos fechas de
    entrega (YYYY-MM-DD, inclusive)."""
    params: dict = {"select": "*", "order": "fecha_entrega.desc,hora_aproximada.asc,numero.desc",
                    "limit": str(limite)}
    filtros = [f"fecha_entrega.gte.{desde}"] if desde else []
    filtros += [f"fecha_entrega.lte.{hasta}"] if hasta else []
    if filtros:
        params["and"] = f"({','.join(filtros)})"
    return supabase_client.get_rows(TABLA_ORDENES, params=params)


def leer(numero: int) -> Optional[dict]:
    filas = supabase_client.get_rows(TABLA_ORDENES, params={"numero": f"eq.{numero}", "select": "*"})
    return filas[0] if filas else None


def cambiar_estado(numero: int, estado: str, nota: str, responsable: str) -> dict:
    return supabase_client.rpc_con_reglas("cambiar_estado_orden_servicio", {
        "p_numero": numero, "p_estado": estado, "p_nota": nota, "p_responsable": responsable,
    })


def agregar_nota(numero: int, nota: str, responsable: str) -> dict:
    return supabase_client.rpc_con_reglas("agregar_nota_orden_servicio", {
        "p_numero": numero, "p_nota": nota, "p_responsable": responsable,
    })


def editar_nota(id_nota: int, nota: str, responsable: str) -> dict:
    return supabase_client.rpc_con_reglas("editar_nota_orden_servicio", {
        "p_id": id_nota, "p_nota": nota, "p_responsable": responsable,
    })
