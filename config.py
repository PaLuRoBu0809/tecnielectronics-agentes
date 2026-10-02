"""
config.py

Validación de la configuración (variables de entorno / `.env`) AL ARRANCAR
(Fase 9.1 de `docs/PLAN_DE_MEJORAS.md`). Antes, una variable faltante solo
se descubría cuando llegaba el primer mensaje de un cliente (un KeyError en
medio de un turno). Ahora el servidor no levanta y dice exactamente qué
falta.

Lo llaman `web/app.py` y `main.py` justo después de `load_dotenv()`.
"""
from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

# Variables sin las cuales nada funciona.
OBLIGATORIAS = {
    "OPENROUTER_API_KEY": "clave de OpenRouter (https://openrouter.ai/keys)",
    "OPENROUTER_MODELS": "lista de modelos separados por coma, en orden de fallback",
    "SUPABASE_URL": "URL del proyecto de Supabase (https://<ref>.supabase.co)",
    "SUPABASE_SERVICE_KEY": "service_role key de Supabase (JWT que empieza con eyJ)",
}

# Valores de ejemplo de .env.example que significan "no configurado".
_MARCADORES = ("PEGA_AQUI", "xxxxxxxx")

# Variables numéricas opcionales: si están, deben ser números positivos.
NUMERICAS_OPCIONALES = (
    "LIMITE_MENSAJES_POR_MINUTO",
    "LIMITE_MENSAJES_POR_DIA",
    "LLM_TIMEOUT_CONEXION",
    "LLM_TIMEOUT_LECTURA",
    "SUPABASE_TIMEOUT_CONEXION",
    "SUPABASE_TIMEOUT_LECTURA",
    "MAX_LLAMADAS_LLM_POR_TURNO",
    "SEGUNDOS_MAX_POR_TURNO",
    "CONTEXTO_MAX_TURNOS",
    "CONTEXTO_TURNOS_INTACTOS",
    "CONTEXTO_MAX_CARACTERES_TOOL",
    "CONTEXTO_MAX_MENSAJES_GUARDADOS",
    "ORDEN_EN_LINEA_EXPIRA_HORAS",
)


class ErrorConfiguracion(RuntimeError):
    pass


def problemas_de_configuracion(entorno=None) -> list:
    """Lista de problemas encontrados (vacía si todo está bien)."""
    entorno = os.environ if entorno is None else entorno
    problemas = []
    for nombre, descripcion in OBLIGATORIAS.items():
        valor = (entorno.get(nombre) or "").strip()
        if not valor or any(m in valor for m in _MARCADORES):
            problemas.append(f"{nombre} no está configurada ({descripcion})")

    url = (entorno.get("SUPABASE_URL") or "").strip()
    if url and not url.startswith("https://"):
        problemas.append("SUPABASE_URL debe empezar con https://")

    modelos = [m.strip() for m in (entorno.get("OPENROUTER_MODELS") or "").split(",") if m.strip()]
    if (entorno.get("OPENROUTER_MODELS") or "").strip() and not modelos:
        problemas.append("OPENROUTER_MODELS no contiene ningún modelo")

    url_publica = (entorno.get("URL_PUBLICA_BASE") or "").strip()
    if url_publica and not url_publica.startswith("https://"):
        problemas.append("URL_PUBLICA_BASE debe empezar con https:// (MercadoPago solo avisa a URLs HTTPS)")

    zona = (entorno.get("TIMEZONE_OFFSET") or "").strip()
    if zona and not re.fullmatch(r"[+-]\d{2}:\d{2}", zona):
        problemas.append("TIMEZONE_OFFSET debe tener el formato ±HH:MM (ej. -05:00)")

    for nombre in NUMERICAS_OPCIONALES:
        crudo = (entorno.get(nombre) or "").strip()
        if not crudo:
            continue
        try:
            if float(crudo) <= 0:
                raise ValueError
        except ValueError:
            problemas.append(f"{nombre} debe ser un número positivo (valor actual: {crudo!r})")
    return problemas


def validar_configuracion(entorno=None) -> None:
    """Lanza `ErrorConfiguracion` con TODOS los problemas juntos (no solo el
    primero), y avisa en el log de lo que es opcional pero riesgoso."""
    entorno = os.environ if entorno is None else entorno
    problemas = problemas_de_configuracion(entorno)
    if problemas:
        raise ErrorConfiguracion(
            "Configuración inválida — el servidor no puede arrancar:\n  - "
            + "\n  - ".join(problemas)
            + "\nRevisa tu .env (plantilla en .env.example)."
        )
    if not (entorno.get("API_KEY") or "").strip():
        logger.warning("API_KEY no está definida: el API queda ABIERTO (solo aceptable en desarrollo local).")
    if not (entorno.get("MERCADOPAGO_ACCESS_TOKEN") or "").strip():
        logger.warning("MERCADOPAGO_ACCESS_TOKEN no está definido: el pago en línea queda deshabilitado.")
    else:
        if not (entorno.get("MERCADOPAGO_WEBHOOK_SECRET") or "").strip():
            logger.warning("MERCADOPAGO_WEBHOOK_SECRET no está definido: no se verifica la firma de los avisos.")
        if not (entorno.get("URL_PUBLICA_BASE") or "").strip():
            logger.warning("URL_PUBLICA_BASE no está definida: MercadoPago no avisará los pagos; se concilian "
                           "cuando el cliente pregunta por su pedido.")
