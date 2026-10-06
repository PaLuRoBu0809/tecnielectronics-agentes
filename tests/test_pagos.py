"""
tests/test_pagos.py

Fase 12, paso C: pagos en línea con MercadoPago. Sin tocar MercadoPago ni
Supabase reales:

- `tools/pagos.py`: preferencia de pago (link), firma del webhook, estados.
- `tools/servicio_pagos.py`: estado leído de la API, un solo aviso por
  estado, reintento si el aviso falló, pago de un pedido ya cancelado.
- `notificaciones/`: textos de los avisos y registro en la conversación.
- `Consultar_orden`: conciliación con MercadoPago al preguntar por el pedido.
- `POST /webhooks/mercadopago`: firma, tipo de aviso y reintentos.

Corre con:
    python tests/test_pagos.py
"""
import hashlib
import hmac
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake-key")
os.environ.setdefault("OPENROUTER_API_KEY", "fake-key-para-el-mock")
os.environ.setdefault("OPENROUTER_MODELS", "modelo-simulado:free")
os.environ["MERCADOPAGO_ACCESS_TOKEN"] = "TEST-token-de-prueba"
os.environ["MERCADOPAGO_WEBHOOK_SECRET"] = "secreto-webhook"
os.environ["URL_PUBLICA_BASE"] = "https://tecni.ejemplo.com/"
os.environ["LOG_EVENTOS_JSON"] = "0"

from fastapi.testclient import TestClient  # noqa: E402

from notificaciones import mensajes  # noqa: E402
from notificaciones.notificador import NotificadorHistorial  # noqa: E402
from tools import ordenes_repository, ordenes_tools, pagos, servicio_pagos  # noqa: E402
from tools.carrito_repository import Carrito, LineaCarrito  # noqa: E402
from web import app as modulo_app  # noqa: E402


def _respuesta(cuerpo, status=200):
    resp = MagicMock(status_code=status)
    resp.json.return_value = cuerpo
    if status >= 400:
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError(response=resp)
    return resp


def _firma(data_id, request_id, ts="1704908010", secreto="secreto-webhook"):
    manifiesto = f"id:{data_id};request-id:{request_id};ts:{ts};"
    return f"ts={ts},v1={hmac.new(secreto.encode(), manifiesto.encode(), hashlib.sha256).hexdigest()}"


ORDEN = {
    "order_number": 101, "session_id": "3001234567", "total_amount": 3600000, "payment_reference": "ref-1",
    "payment_status": "APROBADO", "shipping_status": "PENDIENTE_DESPACHO", "estado_pago_notificado": None,
    "payment_link": "https://mp/link", "reserva_expira_en": "2026-10-03T17:00:00+00:00", "Metodo_pago": "en_linea",
    "items": [{"nombre": "Laptop HP Ultra", "cantidad": 1, "subtotal": 3500000}], "created_at": "2026-10-02",
    "customer_name": "Juan", "customer_phone": "300", "customer_address": "Cra 1", "city": "Medellín",
}

# ---------------------------------------------------------------------------
# 1) pagos: link de pago, estados y firma.
# ---------------------------------------------------------------------------
carrito = Carrito((LineaCarrito("p1", "Laptop HP Ultra", 1, Decimal("3500000")),
                   LineaCarrito("p2", "Mouse", 2, Decimal("50000"))))
vence = datetime(2026, 10, 3, 16, 50, tzinfo=timezone.utc)
with patch.object(pagos.requests, "post", return_value=_respuesta({"init_point": "https://mp/pagar"})) as post:
    link = pagos.crear_link_pago("ref-1", carrito, vence)
assert link == "https://mp/pagar"
url, kwargs = post.call_args.args[0], post.call_args.kwargs
cuerpo = kwargs["json"]
assert url == "https://api.mercadopago.com/checkout/preferences"
assert kwargs["headers"]["Authorization"] == "Bearer TEST-token-de-prueba"
assert kwargs["headers"]["X-Idempotency-Key"] == "ref-1", "Reintentar tras un timeout no crea un segundo cobro"
assert cuerpo["items"][1] == {"id": "p2", "title": "Mouse", "quantity": 2, "unit_price": 50000, "currency_id": "COP"}
assert cuerpo["external_reference"] == "ref-1" and cuerpo["expires"] is True
assert cuerpo["expiration_date_to"] == "2026-10-03T16:50:00.000+00:00"
assert cuerpo["notification_url"] == "https://tecni.ejemplo.com/webhooks/mercadopago?source_news=webhooks", (
    "Solo avisos en formato Webhook (firmados), no IPN"
)

with patch.dict(os.environ, {"URL_PUBLICA_BASE": ""}):
    with patch.object(pagos.requests, "post", return_value=_respuesta({"init_point": "x"})) as post:
        pagos.crear_link_pago("ref-2", carrito, vence)
    assert "notification_url" not in post.call_args.kwargs["json"], "Sin URL pública no se envía webhook"
with patch.dict(os.environ, {"MERCADOPAGO_ACCESS_TOKEN": ""}):
    assert not pagos.pasarela_disponible()
    try:
        pagos.crear_link_pago("ref-3", carrito, vence)
        raise AssertionError("Sin token debe lanzar PasarelaNoConfigurada")
    except pagos.PasarelaNoConfigurada:
        pass
assert pagos.pasarela_disponible()

assert [pagos.estado_interno(s) for s in ("approved", "rejected", "cancelled", "pending", "in_process", None)] == [
    "APROBADO", "RECHAZADO", "RECHAZADO", "PENDIENTE", "PENDIENTE", "PENDIENTE"]

assert pagos.firma_valida(_firma("123", "req-1"), "req-1", "123", "secreto-webhook")
assert not pagos.firma_valida(_firma("123", "req-1"), "req-1", "999", "secreto-webhook"), "Otro pago: firma inválida"
assert not pagos.firma_valida(_firma("123", "req-1", secreto="otro"), "req-1", "123", "secreto-webhook")
assert not pagos.firma_valida(None, "req-1", "123", "secreto-webhook")
assert pagos.firma_valida(_firma("abc1", "r"), "r", "ABC1", "secreto-webhook"), "data.id alfanumérico en minúsculas"
print("✅ pagos: preferencia en COP con idempotencia y vencimiento, webhook solo con URL pública, estados y firma.")

# ---------------------------------------------------------------------------
# 2) Mensajes al cliente.
# ---------------------------------------------------------------------------
texto = mensajes.mensaje_de_pago(ORDEN)
assert texto.startswith("✅ ¡Recibimos tu pago! Tu pedido #101 por $3.600.000") and "Laptop HP Ultra x1" in texto
rechazado = mensajes.mensaje_de_pago({**ORDEN, "payment_status": "RECHAZADO"})
assert "no fue aprobado" in rechazado and "https://mp/link" in rechazado
assert "Sábado 3 de Octubre de 2026 a las 12:00 PM" in rechazado, "El plazo en hora de Colombia, legible"
with patch.dict(os.environ, {"CONTACTO_EMPRESA": "WhatsApp 300"}):
    cancelado = mensajes.mensaje_de_pago({**ORDEN, "shipping_status": "CANCELADO"})
assert "ya estaba cancelado" in cancelado and "WhatsApp 300" in cancelado
print("✅ Mensajes: pago aprobado, rechazado con link y plazo, y pago de un pedido ya cancelado.")

# ---------------------------------------------------------------------------
# 3) servicio_pagos: estado desde la API, un solo aviso, reintento.
# ---------------------------------------------------------------------------
notificador = MagicMock()
with (
    patch.object(pagos, "consultar_pago", return_value={"status": "approved", "external_reference": "ref-1"}),
    patch.object(ordenes_repository, "aplicar_estado_pago") as aplicar,
    patch.object(ordenes_repository, "leer_por_referencia", return_value=ORDEN),
    patch.object(ordenes_repository, "marcar_estado_notificado") as marcar,
):
    servicio_pagos.sincronizar_pago("555", notificador)
assert aplicar.call_args.args == ("ref-1", "APROBADO")
assert notificador.enviar.call_args.args == ("3001234567", mensajes.pago_aprobado(ORDEN))
assert marcar.call_args.args == (101, "APROBADO")

notificador.reset_mock()
with (
    patch.object(pagos, "consultar_pago", return_value={"status": "approved", "external_reference": "ref-1"}),
    patch.object(ordenes_repository, "aplicar_estado_pago"),
    patch.object(ordenes_repository, "leer_por_referencia",
                 return_value={**ORDEN, "estado_pago_notificado": "APROBADO"}),
    patch.object(ordenes_repository, "marcar_estado_notificado") as marcar,
):
    servicio_pagos.sincronizar_pago("555", notificador)
assert notificador.enviar.call_count == 0, "Aviso repetido: el cliente no recibe dos mensajes"
assert marcar.call_count == 0

notificador.enviar.side_effect = RuntimeError("canal caído")
with (
    patch.object(pagos, "consultar_pago", return_value={"status": "approved", "external_reference": "ref-1"}),
    patch.object(ordenes_repository, "aplicar_estado_pago"),
    patch.object(ordenes_repository, "leer_por_referencia", return_value=ORDEN),
    patch.object(ordenes_repository, "marcar_estado_notificado") as marcar,
):
    try:
        servicio_pagos.sincronizar_pago("555", notificador)
        raise AssertionError("El fallo del envío debe propagarse (el webhook responde 500 y MercadoPago reintenta)")
    except RuntimeError:
        pass
assert marcar.call_count == 0, "Si el aviso no salió, no se marca: el siguiente reintento lo vuelve a enviar"
notificador.enviar.side_effect = None

with (
    patch.object(pagos, "consultar_pago", return_value={"status": "pending", "external_reference": "ref-1"}),
    patch.object(ordenes_repository, "aplicar_estado_pago"),
    patch.object(ordenes_repository, "leer_por_referencia", return_value={**ORDEN, "payment_status": "PENDIENTE"}),
    patch.object(ordenes_repository, "marcar_estado_notificado") as marcar,
):
    notificador.reset_mock()
    servicio_pagos.sincronizar_pago("555", notificador)
assert notificador.enviar.call_count == 0 and marcar.call_count == 0, "PENDIENTE no se avisa"

with patch.object(pagos, "consultar_pago", return_value={"status": "approved"}), \
        patch.object(ordenes_repository, "aplicar_estado_pago") as aplicar:
    assert servicio_pagos.sincronizar_pago("777", notificador) is None and aplicar.call_count == 0, (
        "Un pago sin external_reference no es de este sistema"
    )

intentos = [{"status": "rejected"}, {"status": "approved"}]  # el más reciente primero
with (
    patch.object(pagos, "pagos_de_referencia", return_value=intentos),
    patch.object(ordenes_repository, "aplicar_estado_pago") as aplicar,
    patch.object(ordenes_repository, "leer_por_referencia", return_value=ORDEN),
    patch.object(ordenes_repository, "marcar_estado_notificado") as marcar,
):
    servicio_pagos.sincronizar_referencia("ref-1")
assert aplicar.call_args.args == ("ref-1", "APROBADO"), "Si algún intento se aprobó, la orden está pagada"
assert marcar.call_args.args == (101, "APROBADO"), "Sin notificador (lo cuenta el agente): solo se marca"
with patch.object(pagos, "pagos_de_referencia", return_value=[]):
    assert servicio_pagos.sincronizar_referencia("ref-1") is None, "Todavía no intentó pagar"
print("✅ servicio_pagos: estado leído de la API, un aviso por estado, reintento si falló, conciliación.")

# ---------------------------------------------------------------------------
# 4) Consultar_orden concilia con MercadoPago antes de responder.
# ---------------------------------------------------------------------------
pendiente = {**ORDEN, "payment_status": "PENDIENTE"}
with (
    patch.object(ordenes_repository, "leer", return_value=pendiente),
    patch.object(servicio_pagos, "sincronizar_referencia", return_value=ORDEN) as sincronizar,
):
    r = ordenes_tools.consultar_orden("3001234567", 101)
assert sincronizar.call_args.args == ("ref-1",) and "ESTADO DE PAGO: APROBADO" in r

with (
    patch.object(ordenes_repository, "leer", return_value=pendiente),
    patch.object(servicio_pagos, "sincronizar_referencia", side_effect=ConnectionError("MP caído")),
):
    r = ordenes_tools.consultar_orden("3001234567", 101)
assert "ESTADO DE PAGO: PENDIENTE" in r, "Si MercadoPago no responde, se muestra lo que hay en la base"

contra_entrega = {**ORDEN, "Metodo_pago": "contra_entrega", "payment_status": "CONTRAENTREGA"}
with (
    patch.object(ordenes_repository, "listar", return_value=[contra_entrega]),
    patch.object(servicio_pagos, "sincronizar_referencia") as sincronizar,
):
    ordenes_tools.consultar_orden("3001234567")
assert sincronizar.call_count == 0, "Contra entrega no se concilia con MercadoPago"
print("✅ Consultar_orden: concilia pagos en línea pendientes; si MercadoPago falla, responde igual.")

# ---------------------------------------------------------------------------
# 5) NotificadorHistorial: el aviso queda en la conversación, con el candado.
# ---------------------------------------------------------------------------
eventos = []


class AlmacenFalso:
    def __init__(self):
        self.guardado = None

    @contextmanager
    def turno_exclusivo(self, session_id):
        eventos.append(("lock", session_id))
        yield

    def obtener(self, session_id):
        return {"orquestador": [{"role": "user", "content": "ya pagué"}]}

    def guardar(self, session_id, historiales):
        self.guardado = historiales


almacen = AlmacenFalso()
NotificadorHistorial(almacen).enviar("3001234567", "✅ ¡Recibimos tu pago!")
assert eventos == [("lock", "3001234567")], "Usa el mismo candado que los turnos"
assert almacen.guardado["orquestador"][-1] == {"role": "assistant", "content": "✅ ¡Recibimos tu pago!"}
assert almacen.guardado["ventas"] == [{"role": "assistant", "content": "✅ ¡Recibimos tu pago!"}]
print("✅ NotificadorHistorial: el aviso queda en los hilos del orquestador y de Ventas.")

# ---------------------------------------------------------------------------
# 6) POST /webhooks/mercadopago
# ---------------------------------------------------------------------------
cliente = TestClient(modulo_app.app)
ruta = "/webhooks/mercadopago?data.id=123&type=payment"
cuerpo_aviso = {"type": "payment", "action": "payment.updated", "data": {"id": "123"}}

with patch.object(servicio_pagos, "sincronizar_pago") as sincronizar:
    r = cliente.post(ruta, json=cuerpo_aviso, headers={"x-signature": "ts=1,v1=falsa", "x-request-id": "req-1"})
    assert r.status_code == 401 and sincronizar.call_count == 0, "Firma inválida: no se procesa"
    r = cliente.post(ruta, json=cuerpo_aviso)
    assert r.status_code == 401, "Sin firma (con secreto configurado): no se procesa"

    r = cliente.post(ruta, json=cuerpo_aviso, headers={"x-signature": _firma("123", "req-1"), "x-request-id": "req-1"})
    assert r.status_code == 200 and r.json() == {"ok": True, "procesado": True}
    assert sincronizar.call_args.args[0] == "123"
    assert isinstance(sincronizar.call_args.args[1], NotificadorHistorial)

    otro = "/webhooks/mercadopago?data.id=9&type=merchant_order"
    r = cliente.post(otro, json={}, headers={"x-signature": _firma("9", "r"), "x-request-id": "r"})
    assert r.status_code == 200 and r.json()["procesado"] is False, "Avisos que no son de pagos se ignoran"

with patch.object(servicio_pagos, "sincronizar_pago", side_effect=RuntimeError("MP caído")):
    r = cliente.post(ruta, json=cuerpo_aviso, headers={"x-signature": _firma("123", "req-2"), "x-request-id": "req-2"})
assert r.status_code == 500, "Si falla, 500: MercadoPago reenvía el aviso más tarde"
print("✅ Webhook: firma obligatoria si hay secreto, solo avisos de pagos, 500 para que MercadoPago reintente.")

print("\n✅ Todos los tests de pagos pasaron.")
