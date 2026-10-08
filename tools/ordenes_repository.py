"""
tools/ordenes_repository.py

Órdenes de compra (`orders`). Las escrituras pasan SIEMPRE por funciones de
Postgres que aplican las reglas del negocio en una sola transacción (ver
`supabase/migrations/20261001000006_ventas.sql`):

- `crear_desde_carrito`: verifica stock y total, descuenta stock, inserta
  la orden y vacía el carrito, todo o nada.
- `modificar_datos` / `cancelar`: solo la ÚLTIMA orden del cliente y solo
  en PENDIENTE_DESPACHO; una orden con pago APROBADO no se cancela (el
  reembolso lo gestiona la empresa directamente).
- `aplicar_estado_pago`: devuelve la orden solo si el estado cambió de
  verdad, para notificar al cliente una única vez.

El filtro por `session_id` va en todas las consultas: un cliente nunca ve
ni toca las órdenes de otro.

Los errores de negocio salen como `ErrorNegocio`; cualquier otro error se
propaga tal cual.
"""
from __future__ import annotations

import os
from datetime import datetime
from decimal import Decimal
from typing import Optional

from tools import supabase_client

METODO_CONTRA_ENTREGA = "contra_entrega"
METODO_EN_LINEA = "en_linea"
ENVIO_CANCELADO = "CANCELADO"


def _tabla_ordenes() -> str:
    return os.environ.get("SUPABASE_TABLE_ORDENES", "orders")


def crear_desde_carrito(
    session_id: str,
    nombre: str,
    telefono: str,
    direccion: str,
    ciudad: str,
    metodo_pago: str,
    total_esperado: Optional[Decimal] = None,
    referencia_pago: Optional[str] = None,
    link_pago: Optional[str] = None,
    reserva_expira_en: Optional[datetime] = None,
) -> dict:
    """Crea la orden con el contenido actual del carrito. Para pago en línea
    exige la referencia y el link de MercadoPago ya creados, el total con
    el que se crearon (si los precios cambiaron: `ErrorNegocio('TOTAL_CAMBIO')`)
    y el vencimiento de la reserva. Devuelve la fila tal como quedó guardada."""
    return supabase_client.rpc_con_reglas("crear_orden_desde_carrito", {
        "p_session_id": session_id,
        "p_customer_name": nombre,
        "p_customer_phone": telefono,
        "p_customer_address": direccion,
        "p_city": ciudad,
        "p_metodo_pago": metodo_pago,
        "p_total_esperado": None if total_esperado is None else str(total_esperado),
        "p_payment_reference": referencia_pago,
        "p_payment_link": link_pago,
        "p_reserva_expira_en": None if reserva_expira_en is None else reserva_expira_en.isoformat(),
    })


def listar(session_id: str, incluir_canceladas: bool = False) -> list:
    """Órdenes del cliente, de la más reciente a la más antigua."""
    params = {"session_id": f"eq.{session_id}", "select": "*", "order": "created_at.desc,order_number.desc"}
    if not incluir_canceladas:
        params["shipping_status"] = f"neq.{ENVIO_CANCELADO}"
    return supabase_client.get_rows(_tabla_ordenes(), params=params)


def leer(session_id: str, order_number: int) -> Optional[dict]:
    """Una orden del cliente, o `None` si no existe o es de otro cliente."""
    filas = supabase_client.get_rows(
        _tabla_ordenes(),
        params={"session_id": f"eq.{session_id}", "order_number": f"eq.{order_number}", "select": "*"},
    )
    return filas[0] if filas else None


def listar_todas(limite: int = 500) -> list:
    """Todos los pedidos de todos los clientes, del más reciente al más
    antiguo. Solo para el panel de la empresa (`GET /api/pedidos`); el
    agente nunca la usa (siempre filtra por cliente)."""
    return supabase_client.get_rows(
        _tabla_ordenes(),
        params={"select": "*", "order": "created_at.desc,order_number.desc", "limit": str(limite)},
    )


def leer_por_referencia(referencia_pago: str) -> Optional[dict]:
    """La orden de un pago en línea (por su `external_reference` en
    MercadoPago), o `None`. Solo la usa el servicio de pagos: no filtra por
    cliente porque la referencia ya identifica una sola orden."""
    filas = supabase_client.get_rows(
        _tabla_ordenes(), params={"payment_reference": f"eq.{referencia_pago}", "select": "*"}
    )
    return filas[0] if filas else None


def modificar_datos(
    session_id: str,
    order_number: int,
    nombre: Optional[str] = None,
    telefono: Optional[str] = None,
    direccion: Optional[str] = None,
    ciudad: Optional[str] = None,
) -> dict:
    """Cambia solo los datos personales enviados (`None` = dejar igual)."""
    return supabase_client.rpc_con_reglas("modificar_datos_orden", {
        "p_order_number": order_number,
        "p_session_id": session_id,
        "p_customer_name": nombre,
        "p_customer_phone": telefono,
        "p_customer_address": direccion,
        "p_city": ciudad,
    })


def cancelar(session_id: str, order_number: int) -> dict:
    """Cancela la orden y devuelve su stock al inventario."""
    return supabase_client.rpc_con_reglas(
        "cancelar_orden", {"p_order_number": order_number, "p_session_id": session_id}
    )


def aplicar_estado_pago(referencia_pago: str, estado: str) -> Optional[dict]:
    """Registra el estado que reporta MercadoPago. Devuelve la orden si
    cambió (hay que avisarle al cliente) o `None` si no cambió nada."""
    filas = supabase_client.rpc(
        "aplicar_estado_pago", {"p_payment_reference": referencia_pago, "p_estado": estado}
    )
    return filas[0] if filas else None


def marcar_estado_notificado(order_number: int, estado: str) -> None:
    """Recuerda que ya se le avisó al cliente de `estado`."""
    supabase_client.patch_rows(
        _tabla_ordenes(),
        params={"order_number": f"eq.{order_number}"},
        payload={"estado_pago_notificado": estado},
    )


# ---------------------------------------------------------------------------
# Seguimiento (migración 20261008000014): estado de envío + notas
# ---------------------------------------------------------------------------

TABLA_SEGUIMIENTO = "seguimiento_pedido"


def seguimiento(order_numbers: list) -> dict:
    """{order_number: [notas de la más antigua a la más reciente]} de varios
    pedidos en una sola consulta."""
    if not order_numbers:
        return {}
    filas = supabase_client.get_rows(TABLA_SEGUIMIENTO, params={
        "order_number": f"in.({','.join(str(int(n)) for n in order_numbers)})",
        "select": "*", "order": "creado_en.asc,id.asc",
    })
    agrupadas: dict = {int(n): [] for n in order_numbers}
    for fila in filas:
        agrupadas.setdefault(fila["order_number"], []).append(fila)
    return agrupadas


def leer_para_panel(order_number: int) -> Optional[dict]:
    """Un pedido de cualquier cliente (solo para el dashboard)."""
    filas = supabase_client.get_rows(
        _tabla_ordenes(), params={"order_number": f"eq.{order_number}", "select": "*"}
    )
    return filas[0] if filas else None


def cambiar_estado_envio(order_number: int, estado: str, nota: str, responsable: str) -> dict:
    """Dashboard: nuevo estado de envío con nota y responsable obligatorios.
    CANCELADO devuelve el stock reservado."""
    return supabase_client.rpc_con_reglas("cambiar_estado_pedido", {
        "p_order_number": order_number, "p_estado": estado, "p_nota": nota, "p_responsable": responsable,
    })


def agregar_nota(order_number: int, nota: str, responsable: str) -> dict:
    return supabase_client.rpc_con_reglas("agregar_nota_pedido", {
        "p_order_number": order_number, "p_nota": nota, "p_responsable": responsable,
    })


def editar_nota(id_nota: int, nota: str, responsable: str) -> dict:
    return supabase_client.rpc_con_reglas("editar_nota_pedido", {
        "p_id": id_nota, "p_nota": nota, "p_responsable": responsable,
    })
