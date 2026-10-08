"""
tools/fechas.py

Zona horaria del negocio y formato de fechas en español, compartidos por
todos los agentes y las notificaciones (Fase 13: antes vivían en el módulo
de citas por hora, que se retiró).
"""
from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta, timezone

DIAS = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
MESES = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]


def zona_horaria_configurada() -> timezone:
    """Lee `TIMEZONE_OFFSET` del `.env` (ej. "-05:00"). Si falta o viene mal
    formado, usa UTC-05:00 (Colombia)."""
    crudo = os.environ.get("TIMEZONE_OFFSET", "-05:00")
    signo = -1 if crudo.startswith("-") else 1
    crudo = crudo.lstrip("+-")
    horas_str, _, minutos_str = crudo.partition(":")
    try:
        horas, minutos = int(horas_str), int(minutos_str or "0")
    except ValueError:
        horas, minutos, signo = 5, 0, -1
    return timezone(signo * timedelta(hours=horas, minutes=minutos))


def ahora() -> datetime:
    """Hora actual en la zona del negocio (función aparte para fijarla en tests)."""
    return datetime.now(zona_horaria_configurada())


def formatear_hora(valor: time) -> str:
    """time(14, 0) -> '2:00 PM'."""
    return valor.strftime("%I:%M %p").lstrip("0")


def formatear_dia(valor: date) -> str:
    """date(2026, 10, 8) -> 'Jueves 8 de Octubre de 2026'."""
    return f"{DIAS[valor.weekday()]} {valor.day} de {MESES[valor.month - 1].capitalize()} de {valor.year}"


def formatear_fecha_legible(iso_str: str) -> str:
    """'2026-07-28T11:00:00-05:00' -> 'Martes 28 de Julio de 2026 a las 11:00 AM'.

    Siempre convierte a la zona del negocio: Supabase devuelve los
    `timestamptz` en UTC, y sin convertir una hora de las 10:00 AM de
    Colombia se mostraría como 3:00 PM."""
    dt = datetime.fromisoformat(iso_str).astimezone(zona_horaria_configurada())
    return f"{formatear_dia(dt.date())} a las {formatear_hora(dt.time())}"
