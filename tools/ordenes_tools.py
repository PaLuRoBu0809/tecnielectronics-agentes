"""
tools/ordenes_tools.py

{Crear_orden}, {Consultar_orden}, {Modificar_orden} y {Cancelar_orden} del
Agente de Ventas. `session_id` e `id_turno` los inyecta
`agents/ventas_agent.py`: el modelo nunca los ve ni los pasa.

Reemplaza el subflujo "crear orden" de n8n:
1. Se lee el carrito (si está vacío, no hay nada que crear).
2. Confirmación de dos turnos (`tools/confirmacion.py`): la primera vez se
   devuelve el resumen de carrito + datos sin escribir nada. La firma
   incluye el contenido del carrito: si cambia, hay que volver a confirmar.
3. Contra entrega: una sola llamada atómica crea la orden (descuenta stock y
   vacía el carrito, todo o nada).
   En línea: PRIMERO se crea el link en la pasarela y DESPUÉS la orden
   atómica con ese link. Si la orden falla (alguien compró el último
   equipo un segundo antes), el link queda huérfano pero nadie lo vio, así
   que no hace daño y no hay nada que deshacer.

Modificar y cancelar: la regla (solo el ÚLTIMO pedido y solo en
PENDIENTE_DESPACHO; lo pagado no se cancela) la garantiza Postgres en la
misma transacción de la escritura. Aquí se verifica también ANTES de pedir
confirmación, solo para no pedirle al cliente que confirme algo imposible.

Nunca lanzan excepciones: devuelven texto para el modelo.
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from tools import carrito_repository, ordenes_repository, pagos, servicio_pagos
from tools.fechas import formatear_fecha_legible
from tools.confirmacion import requiere_confirmacion
from tools.errores_negocio import ErrorNegocio
from tools.formato_ventas import error_tecnico, mensaje_error_negocio, resumen_carrito, resumen_orden

logger = logging.getLogger(__name__)

AGENTE = "ventas"
ENVIO_PENDIENTE = "PENDIENTE_DESPACHO"
# El link de pago vence un poco antes que la reserva: así nadie paga una
# orden que la limpieza automática acaba de cancelar.
MARGEN_LINK_ANTES_DE_RESERVA = timedelta(minutes=10)


def _horas_para_pagar() -> int:
    try:
        return max(int(os.environ.get("ORDEN_EN_LINEA_EXPIRA_HORAS", 24)), 1)
    except ValueError:
        return 24


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Crear
# ---------------------------------------------------------------------------

def _datos_cliente(nombre, telefono, direccion, ciudad, metodo_pago) -> str:
    return (
        f"DATOS: Nombre: {nombre} | Teléfono: {telefono} | Dirección: {direccion} | Ciudad: {ciudad} | "
        f"Método de pago: {metodo_pago}"
    )


PAGO_EN_LINEA_NO_DISPONIBLE = (
    "ERROR: El pago en línea no está disponible en este momento. No se creó ningún pedido. Explícale al "
    "cliente con naturalidad que por ahora solo se puede pagar contra entrega y pregúntale si desea "
    "continuar así. Estado: fallido"
)


def _crear_en_linea(session_id, carrito, nombre, telefono, direccion, ciudad) -> str:
    referencia = str(uuid.uuid4())
    reserva_expira_en = _ahora() + timedelta(hours=_horas_para_pagar())
    try:
        link = pagos.crear_link_pago(referencia, carrito, reserva_expira_en - MARGEN_LINK_ANTES_DE_RESERVA)
    except pagos.PasarelaNoConfigurada:
        return PAGO_EN_LINEA_NO_DISPONIBLE
    except Exception as exc:
        return error_tecnico("generar el link de pago (no se creó ningún pedido)", exc)

    orden = ordenes_repository.crear_desde_carrito(
        session_id, nombre, telefono, direccion, ciudad, ordenes_repository.METODO_EN_LINEA,
        total_esperado=carrito.total, referencia_pago=referencia, link_pago=link,
        reserva_expira_en=reserva_expira_en,
    )
    vence = formatear_fecha_legible((reserva_expira_en - MARGEN_LINK_ANTES_DE_RESERVA).isoformat())
    return (
        "OK: PEDIDO REGISTRADO, PENDIENTE DE PAGO.\n"
        f"{resumen_orden(orden)}\n"
        f"Entrégale al cliente el número de pedido y este link de pago: {link}\n"
        f"Dile que puede pagar hasta {vence}; si no paga antes, el pedido se cancela solo y los "
        "productos se liberan.\n"
        "(interno) Pídele que te avise cuando haya pagado y entonces verifica con Consultar_orden. "
        "Nunca confirmes un pago solo porque el cliente lo diga."
    )


def crear_orden(
    customer_name: str,
    customer_phone: str,
    city: str,
    customer_address: str,
    metodo_pago: str,
    session_id: str,
    id_turno: str,
) -> str:
    try:
        carrito = carrito_repository.leer(session_id)
    except Exception as exc:
        return error_tecnico("leer el carrito", exc)
    if carrito.vacio:
        return mensaje_error_negocio(ErrorNegocio("CARRITO_VACIO"))
    if metodo_pago == ordenes_repository.METODO_EN_LINEA and not pagos.pasarela_disponible():
        return PAGO_EN_LINEA_NO_DISPONIBLE

    firma = (
        "crear_orden", customer_name, customer_phone, customer_address, city, metodo_pago,
        tuple((linea.producto_id, linea.cantidad) for linea in carrito.lineas),
    )
    if requiere_confirmacion(AGENTE, session_id, id_turno, firma):
        return (
            "CONFIRMACION_PENDIENTE: Todavía NO se ha creado el pedido. Muestra al cliente JUNTOS el "
            "resumen del carrito y sus datos, recuérdale que el método de pago no se puede cambiar sin "
            "cancelar el pedido, y espera su confirmación explícita en un mensaje NUEVO. Cuando confirme, "
            "llama Crear_orden con EXACTAMENTE estos mismos datos. Nunca digas que el pedido ya quedó "
            "registrado.\n"
            f"{resumen_carrito(carrito)}\n"
            f"{_datos_cliente(customer_name, customer_phone, customer_address, city, metodo_pago)}"
        )

    try:
        if metodo_pago == ordenes_repository.METODO_EN_LINEA:
            return _crear_en_linea(session_id, carrito, customer_name, customer_phone, customer_address, city)
        orden = ordenes_repository.crear_desde_carrito(
            session_id, customer_name, customer_phone, customer_address, city, metodo_pago,
        )
    except ErrorNegocio as exc:
        return mensaje_error_negocio(exc)
    except Exception as exc:
        return error_tecnico("registrar el pedido", exc)
    return (
        "OK: PEDIDO REGISTRADO (pago contra entrega). Muéstrale al cliente este resumen y agradécele; "
        f"el proceso termina aquí.\n{resumen_orden(orden)}"
    )


# ---------------------------------------------------------------------------
# Consultar
# ---------------------------------------------------------------------------

def _listado(ordenes: list) -> str:
    return "\n".join(resumen_orden(o) for o in ordenes)


def _conciliar_pagos(ordenes: list) -> list:
    """Antes de responder sobre pedidos en línea sin pago aprobado, consulta
    el estado REAL en MercadoPago (red de seguridad si el aviso del webhook
    se perdió). Si MercadoPago no responde, se muestra lo que hay en la base:
    nunca se bloquea la consulta por eso."""
    actualizadas = []
    for orden in ordenes:
        if (orden.get("Metodo_pago") == ordenes_repository.METODO_EN_LINEA and orden.get("payment_reference")
                and orden.get("payment_status") in ("PENDIENTE", "RECHAZADO")):
            try:
                orden = servicio_pagos.sincronizar_referencia(orden["payment_reference"]) or orden
            except Exception:
                logger.warning("No se pudo conciliar el pago del pedido #%s", orden.get("order_number"),
                               exc_info=True)
        actualizadas.append(orden)
    return actualizadas


def _con_seguimiento(ordenes: list) -> list:
    """Agrega a cada orden sus notas del dashboard (`seguimiento`). Si no se
    pueden leer, se responde igual sin ellas."""
    try:
        notas = ordenes_repository.seguimiento([o["order_number"] for o in ordenes])
    except Exception:
        logger.warning("No se pudo leer el seguimiento de los pedidos", exc_info=True)
        return ordenes
    return [{**o, "seguimiento": notas.get(o["order_number"], [])} for o in ordenes]


def consultar_orden(session_id: str, order_number: Optional[int] = None) -> str:
    try:
        if order_number is None:
            ordenes = _con_seguimiento(_conciliar_pagos(ordenes_repository.listar(session_id)))
            if not ordenes:
                return "SIN_PEDIDOS: el cliente no tiene pedidos activos (no cancelados)."
            return "PEDIDOS DEL CLIENTE (del más reciente al más antiguo):\n" + _listado(ordenes)

        orden = ordenes_repository.leer(session_id, order_number)
        if orden is not None:
            return resumen_orden(_con_seguimiento(_conciliar_pagos([orden]))[0])
        ordenes = ordenes_repository.listar(session_id, incluir_canceladas=True)
    except Exception as exc:
        return error_tecnico("consultar los pedidos", exc)
    if not ordenes:
        return f"ORDEN_NO_ENCONTRADA: el pedido #{order_number} no existe y el cliente no tiene ningún pedido."
    return (
        f"ORDEN_NO_ENCONTRADA: el pedido #{order_number} no existe para este cliente. Estos son TODOS sus "
        "pedidos (incluidos los cancelados), para ayudarlo a ubicar el correcto. Nunca asumas ni inventes "
        f"un estado:\n{_listado(ordenes)}"
    )


# ---------------------------------------------------------------------------
# Modificar y cancelar
# ---------------------------------------------------------------------------

def _orden_modificable(session_id: str, order_number: int, para_cancelar: bool) -> tuple:
    """`(orden, None)` si se puede modificar/cancelar, o `(None, mensaje)`.
    Misma regla que `orden_modificable` en Postgres (que es la garantía);
    esto solo evita pedir confirmación de algo imposible."""
    ordenes = ordenes_repository.listar(session_id, incluir_canceladas=True)
    orden = next((o for o in ordenes if o["order_number"] == order_number), None)
    if orden is None:
        return None, mensaje_error_negocio(ErrorNegocio("ORDEN_NO_ENCONTRADA"))
    if ordenes[0]["order_number"] != order_number:
        return None, mensaje_error_negocio(ErrorNegocio("NO_ES_ULTIMA_ORDEN", str(ordenes[0]["order_number"])))
    if orden["shipping_status"] != ENVIO_PENDIENTE:
        return None, mensaje_error_negocio(ErrorNegocio("ORDEN_NO_PENDIENTE", orden["shipping_status"]))
    if para_cancelar and orden.get("payment_status") == "APROBADO":
        return None, mensaje_error_negocio(ErrorNegocio("PAGO_APROBADO"))
    return orden, None


_CAMPOS_PERSONALES = (
    ("customer_name", "Nombre"),
    ("customer_phone", "Teléfono"),
    ("customer_address", "Dirección"),
    ("city", "Ciudad"),
)


def modificar_orden(
    order_number: int,
    session_id: str,
    id_turno: str,
    customer_name: Optional[str] = None,
    customer_phone: Optional[str] = None,
    customer_address: Optional[str] = None,
    city: Optional[str] = None,
) -> str:
    nuevos = {"customer_name": customer_name, "customer_phone": customer_phone,
              "customer_address": customer_address, "city": city}
    try:
        orden, error = _orden_modificable(session_id, order_number, para_cancelar=False)
    except Exception as exc:
        return error_tecnico("consultar el pedido", exc)
    if error:
        return error

    firma = ("modificar_orden", order_number, *nuevos.values())
    if requiere_confirmacion(AGENTE, session_id, id_turno, firma):
        cambios = "\n".join(
            f"   {etiqueta}: {orden.get(campo)} -> {nuevos[campo]}"
            for campo, etiqueta in _CAMPOS_PERSONALES if nuevos[campo] is not None
        )
        return (
            f"CONFIRMACION_PENDIENTE: Todavía NO se ha modificado el pedido #{order_number}. Muéstrale al "
            "cliente estos cambios y espera su confirmación explícita en un mensaje NUEVO; luego llama "
            f"Modificar_orden con EXACTAMENTE estos mismos datos.\nCAMBIOS:\n{cambios}"
        )
    try:
        actualizada = ordenes_repository.modificar_datos(
            session_id, order_number, customer_name, customer_phone, customer_address, city,
        )
    except ErrorNegocio as exc:
        return mensaje_error_negocio(exc)
    except Exception as exc:
        return error_tecnico("modificar el pedido", exc)
    return f"OK: PEDIDO ACTUALIZADO. Muéstrale al cliente el resumen:\n{resumen_orden(actualizada)}"


def cancelar_orden(order_number: int, session_id: str, id_turno: str) -> str:
    try:
        orden, error = _orden_modificable(session_id, order_number, para_cancelar=True)
    except Exception as exc:
        return error_tecnico("consultar el pedido", exc)
    if error:
        return error

    if requiere_confirmacion(AGENTE, session_id, id_turno, ("cancelar_orden", order_number)):
        return (
            f"CONFIRMACION_PENDIENTE: Todavía NO se ha cancelado el pedido #{order_number}. Pregúntale al "
            "cliente explícitamente si está seguro de cancelarlo (muéstrale el resumen) y espera su "
            "respuesta en un mensaje NUEVO; si confirma, llama Cancelar_orden con este mismo número.\n"
            f"{resumen_orden(orden)}"
        )
    try:
        ordenes_repository.cancelar(session_id, order_number)
    except ErrorNegocio as exc:
        return mensaje_error_negocio(exc)
    except Exception as exc:
        return error_tecnico("cancelar el pedido", exc)
    return (
        f"OK: PEDIDO #{order_number} CANCELADO y sus productos liberados. Si el cliente lo canceló para "
        "cambiar el método de pago o los productos, el carrito está vacío: vuelve a agregar con él lo "
        "que realmente quiere y crea un pedido nuevo."
    )
