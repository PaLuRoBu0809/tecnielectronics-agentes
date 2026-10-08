"""
notificaciones/mensajes.py

Textos que el sistema le envía al cliente POR SU CUENTA (sin que el cliente
haya escrito), hoy por cambios en el pago. Los arma el código con los datos
reales de la orden, no el LLM: así no puede inventar nada, es instantáneo y
no gasta llamadas al modelo.

Funciones puras: reciben la fila de `orders` y devuelven texto.
"""
from __future__ import annotations

import os

from tools.fechas import formatear_fecha_legible
from tools.formato_ventas import pesos


def _productos(orden: dict) -> str:
    return "\n".join(
        f"• {item.get('nombre', 'Producto')} x{item.get('cantidad', 1)}" for item in orden.get("items") or []
    )


def _contacto() -> str:
    contacto = os.environ.get("CONTACTO_EMPRESA", "").strip()
    return f" ({contacto})" if contacto else ""


def pago_aprobado(orden: dict) -> str:
    return (
        f"✅ ¡Recibimos tu pago! Tu pedido #{orden['order_number']} por {pesos(orden['total_amount'])} "
        "quedó confirmado y pasa a preparación para el despacho.\n"
        f"{_productos(orden)}\n"
        "¡Gracias por comprar en Tecnielectronics! 🙌"
    )


def pago_rechazado(orden: dict) -> str:
    vence = orden.get("reserva_expira_en")
    plazo = f" antes del {formatear_fecha_legible(vence)}" if vence else ""
    return (
        f"⚠️ El pago de tu pedido #{orden['order_number']} por {pesos(orden['total_amount'])} no fue "
        "aprobado, así que el pedido todavía no se puede despachar.\n"
        f"Puedes intentarlo de nuevo{plazo} con este link: {orden.get('payment_link')}\n"
        "Si prefieres, también puedes pedirlo con pago contra entrega. 🙂"
    )


def pago_en_pedido_cancelado(orden: dict) -> str:
    """Pagó justo cuando el pedido ya se había cancelado (por vencimiento o
    porque lo canceló). No se despacha solo: lo resuelve la empresa."""
    return (
        f"Recibimos tu pago del pedido #{orden['order_number']} por {pesos(orden['total_amount'])}, pero "
        "ese pedido ya estaba cancelado. No te preocupes: comunícate con Tecnielectronics"
        f"{_contacto()} para reactivarlo o gestionar la devolución de tu dinero."
    )


def mensaje_de_pago(orden: dict) -> str:
    """El aviso que corresponde al estado actual de la orden."""
    if orden.get("payment_status") == "APROBADO":
        if orden.get("shipping_status") == "CANCELADO":
            return pago_en_pedido_cancelado(orden)
        return pago_aprobado(orden)
    return pago_rechazado(orden)
