"""
tools/servicio_pagos.py

Qué hacer cuando cambia un pago. Lo usan dos caminos, con la MISMA lógica:

1. El webhook de MercadoPago (`web/app.py`): MercadoPago avisa "el pago X
   cambió" -> `sincronizar_pago(X, notificador)`.
2. La red de seguridad: cuando el cliente pregunta por su pedido,
   `Consultar_orden` concilia con MercadoPago (`sincronizar_referencia`) por
   si el aviso del webhook se perdió (servidor caído, sin URL pública en
   local...). Así "ya pagué" nunca se responde con un estado viejo.

Reglas:
- El estado SIEMPRE se lee de la API de MercadoPago, nunca del cuerpo del
  aviso: un aviso falso no puede marcar nada como pagado.
- `aplicar_estado_pago` (Postgres) solo cambia la orden si el estado es
  distinto y nunca retrocede una orden APROBADA.
- El cliente recibe UN aviso por cada estado final (APROBADO / RECHAZADO):
  `orders.estado_pago_notificado` recuerda el último avisado. Si el envío
  falla, no se marca, y el siguiente aviso de MercadoPago lo reintenta.
- Sin notificador (camino 2, dentro de un turno del agente): el propio
  agente le cuenta el estado al cliente, así que solo se marca como avisado.
"""
from __future__ import annotations

import logging
from typing import Optional

from notificaciones.mensajes import mensaje_de_pago
from tools import ordenes_repository, pagos

logger = logging.getLogger(__name__)

ESTADOS_QUE_SE_AVISAN = ("APROBADO", "RECHAZADO")


def sincronizar_pago(payment_id: str, notificador=None) -> Optional[dict]:
    """Camino del webhook. Devuelve la orden (ya actualizada) o `None` si el
    pago no pertenece a ninguna orden de este sistema."""
    pago = pagos.consultar_pago(payment_id)
    referencia = pago.get("external_reference")
    if not referencia:
        logger.info("Pago %s sin external_reference: no es de un pedido de este sistema", payment_id)
        return None
    return _aplicar(referencia, pagos.estado_interno(pago.get("status")), notificador)


def sincronizar_referencia(referencia: str, notificador=None) -> Optional[dict]:
    """Camino de conciliación: revisa todos los intentos de pago de la orden.
    Si alguno se aprobó, la orden está pagada; si no, vale el más reciente.
    `None` si el cliente todavía no intentó pagar."""
    estados = [pagos.estado_interno(p.get("status")) for p in pagos.pagos_de_referencia(referencia)]
    if not estados:
        return None
    return _aplicar(referencia, "APROBADO" if "APROBADO" in estados else estados[0], notificador)


def _aplicar(referencia: str, estado: str, notificador) -> Optional[dict]:
    ordenes_repository.aplicar_estado_pago(referencia, estado)
    # Se relee siempre (no solo si cambió): si un aviso anterior cambió el
    # estado pero falló al notificar, este reintento completa la notificación.
    orden = ordenes_repository.leer_por_referencia(referencia)
    if orden is None:
        logger.warning("Pago con referencia %s sin orden asociada", referencia)
        return None
    _avisar_si_falta(orden, notificador)
    return orden


def _avisar_si_falta(orden: dict, notificador) -> None:
    estado = orden.get("payment_status")
    if estado not in ESTADOS_QUE_SE_AVISAN or orden.get("estado_pago_notificado") == estado:
        return
    if estado == "APROBADO" and orden.get("shipping_status") == "CANCELADO":
        logger.warning("Pedido #%s pagado DESPUÉS de cancelado: requiere gestión manual", orden["order_number"])
    if notificador is not None:
        notificador.enviar(orden["session_id"], mensaje_de_pago(orden))
    ordenes_repository.marcar_estado_notificado(orden["order_number"], estado)
