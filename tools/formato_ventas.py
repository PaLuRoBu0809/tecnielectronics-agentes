"""
tools/formato_ventas.py

Texto que las tools del Agente de Ventas le devuelven al MODELO (no al
cliente): precios, resumen del carrito, resumen de una orden y traducción de
los errores de negocio a instrucciones claras.

Todo el formato vive aquí, en funciones puras, para que las tools solo
orquesten y los totales que ve el modelo sean siempre los calculados por el
código, nunca sumados por el modelo.

Convención (la misma de Servicio Técnico): los errores empiezan con
"ERROR:"; lo que dice "(interno)" es para guiar al modelo y nunca se reenvía
al cliente.
"""
from __future__ import annotations

import os
from decimal import ROUND_HALF_UP, Decimal

from tools.fechas import formatear_fecha_legible
from tools.errores_negocio import ErrorNegocio

ESTADOS_PAGO_LEGIBLES = {
    "PENDIENTE": "PENDIENTE",
    "APROBADO": "APROBADO",
    "RECHAZADO": "RECHAZADO",
    "CONTRAENTREGA": "PAGO CONTRA ENTREGA",
}
# Notas del seguimiento (dashboard) que se le muestran al modelo por pedido.
MAX_NOVEDADES = 3


def pesos(valor) -> str:
    """Pesos colombianos sin centavos: Decimal("3500000") -> "$3.500.000"."""
    entero = int(Decimal(str(valor)).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return f"${entero:,}".replace(",", ".")


def resumen_carrito(carrito) -> str:
    """Carrito con subtotales y total calculados por el código."""
    if carrito.vacio:
        return "CARRITO_VACIO: el carrito del cliente está vacío. Invítalo a buscar y agregar productos."
    lineas = [
        f"{i}) {linea.nombre} | cantidad: {linea.cantidad} | precio unitario: {pesos(linea.precio_unitario)} | "
        f"subtotal: {pesos(linea.subtotal)} | producto_id (interno, nunca mostrar): {linea.producto_id}"
        for i, linea in enumerate(carrito.lineas, start=1)
    ]
    return "CARRITO:\n" + "\n".join(lineas) + f"\nTOTAL: {pesos(carrito.total)}"


def resumen_orden(orden: dict) -> str:
    """Una orden en texto: lo que el modelo necesita para responder sobre
    ella. Columnas internas (session_id, referencia de pago, reserva de
    stock) nunca se incluyen. Si la orden trae `seguimiento` (las notas del
    dashboard), se muestran las últimas, sin el responsable (dato interno)."""
    items = orden.get("items") or []
    lineas = "\n".join(
        f"   - {item.get('nombre', 'Producto')} x{item.get('cantidad', 1)}"
        + (f" = {pesos(item['subtotal'])}" if item.get("subtotal") is not None else "")
        for item in items
    )
    estado_pago = orden.get("payment_status") or ""
    partes = [
        f"Pedido #{orden['order_number']} | creado: {orden.get('created_at', '')}",
        f"   ESTADO DE ENVÍO: {orden.get('shipping_status')}",
        f"   ESTADO DE PAGO: {ESTADOS_PAGO_LEGIBLES.get(estado_pago, estado_pago or 'SIN REGISTRO')}",
        f"   Método de pago: {orden.get('Metodo_pago') or 'sin registro'}",
        f"   Productos:\n{lineas}" if lineas else "   Productos: sin detalle",
        f"   TOTAL: {pesos(orden['total_amount'])}",
        f"   Datos de envío: {orden.get('customer_name')} | {orden.get('customer_phone')} | "
        f"{orden.get('customer_address')} | {orden.get('city')}",
    ]
    if orden.get("notes"):
        partes.append(f"   Notas: {orden['notes']}")
    novedades = (orden.get("seguimiento") or [])[-MAX_NOVEDADES:]
    if novedades:
        partes.append("   NOVEDADES DEL ENVÍO (de la más antigua a la más reciente):\n" + "\n".join(
            f"      - {formatear_fecha_legible(n['creado_en'])} [{n.get('estado')}]: {n['nota']}" for n in novedades
        ))
    if estado_pago in ("PENDIENTE", "RECHAZADO") and orden.get("payment_link") \
            and orden.get("shipping_status") == "PENDIENTE_DESPACHO":
        partes.append(f"   Link de pago: {orden['payment_link']}")
        if orden.get("reserva_expira_en"):
            vence = formatear_fecha_legible(orden["reserva_expira_en"])
            partes.append(f"   El link y la reserva de los productos vencen: {vence}")
    return "\n".join(partes)


def _contacto_empresa() -> str:
    contacto = os.environ.get("CONTACTO_EMPRESA", "").strip()
    return f" ({contacto})" if contacto else ""


# Código de ErrorNegocio -> instrucción para el modelo. `{detalle}` y las
# claves de `detalle_json()` se completan en `mensaje_error_negocio`.
_MENSAJES = {
    "CANTIDAD_INVALIDA": "La cantidad debe ser un número entero mayor que cero.",
    "PRODUCTO_NO_DISPONIBLE": (
        "Ese producto ya no está disponible (inactivo o inexistente). Vuelve a buscarlo con "
        "Inventario; nunca uses un producto_id que no venga de la búsqueda más reciente."
    ),
    "NO_ESTA_EN_CARRITO": "Ese producto no está en el carrito del cliente. Revisa el carrito actual.",
    "CARRITO_VACIO": "El carrito del cliente está vacío: no se puede crear el pedido.",
    "TOTAL_CAMBIO": (
        "Los precios cambiaron mientras se creaba el pedido (nuevo total: {detalle}). No se creó "
        "nada: muestra el carrito actualizado y pide confirmación de nuevo."
    ),
    "ORDEN_NO_ENCONTRADA": "Ese número de pedido no existe entre los pedidos de este cliente.",
    "NO_ES_ULTIMA_ORDEN": (
        "Solo se puede modificar o cancelar el ÚLTIMO pedido del cliente (el #{detalle}). "
        "Explícaselo al cliente."
    ),
    "ORDEN_NO_PENDIENTE": (
        "El pedido ya no está pendiente de despacho (estado: {detalle}), así que ya no se puede "
        "modificar ni cancelar. Explícaselo al cliente."
    ),
    "PAGO_APROBADO": (
        "Este pedido ya tiene el pago APROBADO: no se puede cancelar por este medio. Dile al cliente "
        "que para cancelarlo y gestionar el reembolso debe comunicarse directamente con "
        "Tecnielectronics{contacto}. No prometas reembolsos ni plazos."
    ),
}


def mensaje_error_negocio(exc: ErrorNegocio) -> str:
    if exc.codigo == "STOCK_INSUFICIENTE":
        datos = exc.detalle_json()
        if "stock" in datos:
            disponible = int(datos["stock"]) - int(datos.get("en_carrito", 0))
            texto = (
                f"No hay unidades suficientes: quedan {datos['stock']} en total y el cliente ya tiene "
                f"{datos.get('en_carrito', 0)} en el carrito, así que puede agregar como máximo "
                f"{max(disponible, 0)}. Ofrécele esa cantidad."
            )
        else:
            texto = (
                f"Ya no hay unidades suficientes de: {exc.detalle}. No se creó nada: ajusta esas "
                "cantidades con el cliente (Modificar_elemento o Eliminar_elemento) antes de reintentar."
            )
    else:
        plantilla = _MENSAJES.get(exc.codigo, f"La operación no se pudo completar ({exc.codigo}).")
        texto = plantilla.format(detalle=exc.detalle or "", contacto=_contacto_empresa())
    return f"ERROR: {texto} Estado: fallido"


def error_tecnico(accion: str, exc: Exception) -> str:
    return (
        f"ERROR: No se pudo {accion} por un inconveniente técnico momentáneo. No le digas al cliente "
        f"que la operación funcionó; avísale con naturalidad y ofrece reintentar. Estado: fallido | "
        f"Detalle técnico (interno): {type(exc).__name__}: {exc}"
    )
