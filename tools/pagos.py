"""
tools/pagos.py

Frontera con la pasarela de pagos: MercadoPago (Checkout Pro). Nadie más en
el proyecto habla con la API de MercadoPago; las tools de órdenes y el
webhook solo usan estas funciones. Sin lógica de negocio: qué hacer con un
pago lo decide `tools/servicio_pagos.py`.

- `crear_link_pago`: crea una "preferencia" con los productos del carrito,
  la referencia propia de la orden (`external_reference`), la URL del
  webhook (si hay URL pública) y el vencimiento del link.
- `consultar_pago` / `pagos_de_referencia`: el estado REAL de un pago, leído
  de la API. El webhook nunca confía en el cuerpo del aviso: solo usa el id
  para venir a consultar aquí.
- `firma_valida`: verifica la cabecera `x-signature` de cada aviso con la
  clave secreta del webhook (HMAC-SHA256).
- `estado_interno`: traduce el estado de MercadoPago al ENUM de `orders`.

Configuración (`.env`): MERCADOPAGO_ACCESS_TOKEN, MERCADOPAGO_WEBHOOK_SECRET
y URL_PUBLICA_BASE. Sin access token no se ofrece pago en línea.

Red: igual que Supabase (Fase 2 del plan), timeouts separados de conexión y
lectura. Crear la preferencia lleva `X-Idempotency-Key` = la referencia: si
se reintenta tras un timeout, MercadoPago no crea un segundo cobro.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from datetime import datetime
from decimal import Decimal
from typing import Optional

import requests

API = "https://api.mercadopago.com"
MONEDA = "COP"
RUTA_WEBHOOK = "/webhooks/mercadopago"

# Estado de un pago en MercadoPago -> payment_status de `orders`.
# refunded/charged_back llegan después de un approved: `aplicar_estado_pago`
# en Postgres nunca retrocede una orden APROBADA (eso lo gestiona la empresa).
_ESTADOS = {
    "approved": "APROBADO",
    "rejected": "RECHAZADO",
    "cancelled": "RECHAZADO",
    "refunded": "RECHAZADO",
    "charged_back": "RECHAZADO",
}


class PasarelaNoConfigurada(Exception):
    """El pago en línea no está disponible (falta el access token)."""


def _token() -> str:
    return os.environ.get("MERCADOPAGO_ACCESS_TOKEN", "").strip()


def _timeout() -> tuple:
    return 5.0, 20.0


def _cabeceras(idempotencia: Optional[str] = None) -> dict:
    token = _token()
    if not token:
        raise PasarelaNoConfigurada("MERCADOPAGO_ACCESS_TOKEN no está configurado.")
    cabeceras = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if idempotencia:
        cabeceras["X-Idempotency-Key"] = idempotencia
    return cabeceras


def pasarela_disponible() -> bool:
    """Si se puede ofrecer pago en línea. Se consulta ANTES de pedirle al
    cliente que confirme, para no hacerle confirmar algo imposible."""
    return bool(_token())


def url_webhook() -> Optional[str]:
    """URL a la que MercadoPago avisa los cambios de un pago, o `None` si no
    hay URL pública (en local sin túnel). Sin ella el pago igual se registra:
    se concilia cuando el cliente pregunta (`servicio_pagos`)."""
    base = os.environ.get("URL_PUBLICA_BASE", "").strip().rstrip("/")
    return f"{base}{RUTA_WEBHOOK}" if base.startswith("https://") else None


def _precio(valor: Decimal):
    """COP sin centavos como entero; si algún día hay centavos, decimal."""
    return int(valor) if valor == valor.to_integral_value() else float(valor)


def crear_link_pago(referencia: str, carrito, vence_en: datetime) -> str:
    """Crea el cobro de `carrito` y devuelve el link que se le envía al
    cliente. `referencia` es el `external_reference` con el que se
    encontrará la orden; el link deja de aceptar pagos en `vence_en`."""
    cuerpo = {
        "items": [
            {
                "id": linea.producto_id,
                "title": linea.nombre[:256],
                "quantity": linea.cantidad,
                "unit_price": _precio(linea.precio_unitario),
                "currency_id": MONEDA,
            }
            for linea in carrito.lineas
        ],
        "external_reference": referencia,
        "statement_descriptor": "TECNIELECTRONICS",
        "expires": True,
        "expiration_date_to": vence_en.isoformat(timespec="milliseconds"),
    }
    webhook = url_webhook()
    if webhook:
        cuerpo["notification_url"] = webhook
    resp = requests.post(
        f"{API}/checkout/preferences", headers=_cabeceras(referencia), json=cuerpo, timeout=_timeout()
    )
    resp.raise_for_status()
    datos = resp.json()
    return datos.get("init_point") or datos["sandbox_init_point"]


def consultar_pago(payment_id: str) -> dict:
    """El pago tal como lo tiene MercadoPago (status, external_reference...)."""
    resp = requests.get(f"{API}/v1/payments/{payment_id}", headers=_cabeceras(), timeout=_timeout())
    resp.raise_for_status()
    return resp.json()


def pagos_de_referencia(referencia: str) -> list:
    """Todos los intentos de pago de una orden, del más reciente al más antiguo."""
    resp = requests.get(
        f"{API}/v1/payments/search",
        headers=_cabeceras(),
        params={"external_reference": referencia, "sort": "date_created", "criteria": "desc", "limit": "20"},
        timeout=_timeout(),
    )
    resp.raise_for_status()
    return resp.json().get("results") or []


def estado_interno(estado_mercadopago: Optional[str]) -> str:
    """approved -> APROBADO; rechazado/cancelado/devuelto -> RECHAZADO;
    cualquier otro (pending, in_process, authorized...) -> PENDIENTE."""
    return _ESTADOS.get(estado_mercadopago or "", "PENDIENTE")


def firma_valida(x_signature: Optional[str], x_request_id: Optional[str], data_id: Optional[str],
                 secreto: str) -> bool:
    """Verifica `x-signature` ("ts=...,v1=...") según la documentación de
    MercadoPago: HMAC-SHA256 con la clave secreta sobre el manifiesto
    "id:<data.id>;request-id:<x-request-id>;ts:<ts>;" (las partes que no
    vienen se omiten; un data.id alfanumérico va en minúsculas)."""
    if not x_signature or not secreto:
        return False
    partes = dict(p.strip().split("=", 1) for p in x_signature.split(",") if "=" in p)
    ts, v1 = partes.get("ts"), partes.get("v1")
    if not ts or not v1:
        return False
    manifiesto = ""
    if data_id:
        manifiesto += f"id:{data_id.lower() if data_id.isalnum() else data_id};"
    if x_request_id:
        manifiesto += f"request-id:{x_request_id};"
    manifiesto += f"ts:{ts};"
    esperado = hmac.new(secreto.encode(), manifiesto.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(esperado, v1)
