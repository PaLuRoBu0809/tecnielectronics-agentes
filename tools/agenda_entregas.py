"""
tools/agenda_entregas.py

Reglas de calendario para el día en que el cliente lleva su equipo a la sede
(Servicio Técnico, Fase 13). Funciones puras: no tocan la base de datos.

- Se recibe equipo en el horario de atención de la sede (`JORNADAS`).
  IMPORTANTE: debe coincidir con el tema 'horario' de la tabla `info_empresa`
  (lo que el agente le dice al cliente). Si la empresa cambia el horario,
  hay que cambiarlo en los dos lugares.
- Domingos y festivos de Colombia (paquete `holidays`) no hay atención.
- Se agenda desde hoy y hasta `DIAS_MAXIMOS` días adelante.
- La hora aproximada es solo informativa (no hay cupos), pero debe caer
  dentro de una jornada de ese día.

Cada validación devuelve `None` si está bien, o un texto "ERROR: ..." que el
modelo puede leer y corregir.
"""
from __future__ import annotations

from datetime import date, time, timedelta
from functools import lru_cache
from typing import Optional

import holidays

from tools.fechas import ahora, formatear_dia, formatear_hora

# weekday() -> jornadas (inicio, fin). Lunes = 0, domingo = 6 (sin jornadas).
JORNADAS: dict[int, tuple] = {
    **{dia: ((time(8, 15), time(12, 0)), (time(14, 0), time(17, 45))) for dia in range(5)},
    5: ((time(8, 0), time(12, 30)),),
    6: (),
}
DIAS_MAXIMOS = 30

_SUFIJO = " No se guardó nada. Estado: fallido"


@lru_cache(maxsize=8)
def _festivos(anio: int) -> dict:
    return dict(holidays.country_holidays("CO", years=anio))


def festivo(fecha: date) -> Optional[str]:
    """Nombre del festivo de Colombia en esa fecha, o `None`."""
    return _festivos(fecha.year).get(fecha)


def jornadas_del_dia(fecha: date) -> tuple:
    """Jornadas de atención de esa fecha; vacía si es domingo o festivo."""
    if festivo(fecha):
        return ()
    return JORNADAS[fecha.weekday()]


def describir_jornadas(fecha: date) -> str:
    """'8:15 AM a 12:00 PM y 2:00 PM a 5:45 PM' (o 'cerrado')."""
    jornadas = jornadas_del_dia(fecha)
    if not jornadas:
        return "cerrado"
    return " y ".join(f"{formatear_hora(inicio)} a {formatear_hora(fin)}" for inicio, fin in jornadas)


def _hoy() -> date:
    return ahora().date()


def _ya_cerro_hoy(fecha: date) -> bool:
    jornadas = jornadas_del_dia(fecha)
    return fecha == _hoy() and bool(jornadas) and ahora().time() >= jornadas[-1][1]


def validar_dia_entrega(fecha: date) -> Optional[str]:
    hoy = _hoy()
    if fecha < hoy:
        return f"ERROR: El día {formatear_dia(fecha)} ya pasó; elige un día desde hoy en adelante." + _SUFIJO
    if fecha > hoy + timedelta(days=DIAS_MAXIMOS):
        return (
            f"ERROR: Solo se puede agendar hasta {DIAS_MAXIMOS} días adelante "
            f"(máximo {formatear_dia(hoy + timedelta(days=DIAS_MAXIMOS))})." + _SUFIJO
        )
    nombre_festivo = festivo(fecha)
    if nombre_festivo:
        return f"ERROR: El {formatear_dia(fecha)} es festivo ({nombre_festivo}) y la sede está cerrada." + _SUFIJO
    if not jornadas_del_dia(fecha):
        return f"ERROR: El {formatear_dia(fecha)} es domingo y la sede está cerrada." + _SUFIJO
    if _ya_cerro_hoy(fecha):
        return "ERROR: Hoy la sede ya cerró; elige otro día." + _SUFIJO
    return None


def validar_hora_aproximada(fecha: date, hora: Optional[time]) -> Optional[str]:
    """La hora (si viene) debe caer dentro de una jornada de ese día, y si es
    hoy, no puede haber pasado."""
    if hora is None:
        return None
    jornadas = jornadas_del_dia(fecha)
    if not any(inicio <= hora <= fin for inicio, fin in jornadas):
        return (
            f"ERROR: A las {formatear_hora(hora)} la sede no atiende ese día. Horario del "
            f"{formatear_dia(fecha)}: {describir_jornadas(fecha)}. Pídele al cliente una hora dentro de ese "
            "horario." + _SUFIJO
        )
    if fecha == _hoy() and hora < ahora().time():
        return f"ERROR: Las {formatear_hora(hora)} de hoy ya pasaron; pide otra hora." + _SUFIJO
    return None


def proximos_dias_habiles(cantidad: int = 6) -> list:
    """Los próximos `cantidad` días con atención, desde hoy (si aún no cerró)."""
    dias: list = []
    fecha = _hoy()
    while len(dias) < cantidad and fecha <= _hoy() + timedelta(days=DIAS_MAXIMOS):
        if validar_dia_entrega(fecha) is None:
            dias.append(fecha)
        fecha += timedelta(days=1)
    return dias
