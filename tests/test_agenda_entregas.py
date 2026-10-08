"""
tests/test_agenda_entregas.py

Fase 13: reglas de calendario para el día en que el cliente trae su equipo
(`tools/agenda_entregas.py`) — días con atención, festivos de Colombia,
máximo de días, hora aproximada dentro del horario de ese día. Sin red.

Corre con:
    python tests/test_agenda_entregas.py
"""
import os
import sys
from datetime import date, datetime, time
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools import agenda_entregas as agenda  # noqa: E402
from tools.fechas import formatear_dia, formatear_fecha_legible, zona_horaria_configurada  # noqa: E402

TZ = zona_horaria_configurada()


def _a_las(anio, mes, dia, hora, minuto=0):
    return patch.object(agenda, "ahora", return_value=datetime(anio, mes, dia, hora, minuto, tzinfo=TZ))


# Jueves 8 de octubre de 2026, 10:00 AM. El lunes 12 es festivo (Día de la Raza).
with _a_las(2026, 10, 8, 10):
    assert agenda.validar_dia_entrega(date(2026, 10, 8)) is None, "Hoy, con la sede abierta, es válido"
    assert agenda.validar_dia_entrega(date(2026, 10, 10)) is None, "El sábado se atiende"
    assert "ya pasó" in agenda.validar_dia_entrega(date(2026, 10, 7))
    assert "domingo" in agenda.validar_dia_entrega(date(2026, 10, 11))
    festivo = agenda.validar_dia_entrega(date(2026, 10, 12))
    assert "festivo" in festivo and "Raza" in festivo, festivo
    assert "30 días" in agenda.validar_dia_entrega(date(2026, 11, 20))
    assert agenda.validar_dia_entrega(date(2026, 11, 6)) is None, "29 días adelante es válido"
    print("✅ Día de entrega: desde hoy, lunes a sábado, sin festivos de Colombia, hasta 30 días.")

    v = agenda.validar_hora_aproximada
    assert v(date(2026, 10, 9), None) is None, "Sin hora es válido (es opcional)"
    assert v(date(2026, 10, 9), time(8, 15)) is None and v(date(2026, 10, 9), time(17, 45)) is None
    assert "no atiende" in v(date(2026, 10, 9), time(13, 0)), "Al mediodía está cerrado"
    assert "no atiende" in v(date(2026, 10, 9), time(18, 0))
    sabado = v(date(2026, 10, 10), time(15, 0))
    assert "no atiende" in sabado and "8:00 AM a 12:30 PM" in sabado, "El sábado solo en la mañana, y lo dice"
    assert "ya pasaron" in v(date(2026, 10, 8), time(9, 0)), "Hoy, una hora que ya pasó no sirve"
    assert v(date(2026, 10, 8), time(11, 0)) is None
    print("✅ Hora aproximada: dentro de una jornada de ese día (sábado solo mañana) y no en el pasado.")

    dias = agenda.proximos_dias_habiles(4)
    assert dias == [date(2026, 10, 8), date(2026, 10, 9), date(2026, 10, 10), date(2026, 10, 13)], dias
    print("✅ Próximos días con atención: salta el domingo y el festivo.")

with _a_las(2026, 10, 8, 18):
    assert "ya cerró" in agenda.validar_dia_entrega(date(2026, 10, 8))
    assert agenda.proximos_dias_habiles(1) == [date(2026, 10, 9)], "Después del cierre, hoy ya no se ofrece"
print("✅ Después del cierre, hoy ya no es un día válido.")

assert agenda.describir_jornadas(date(2026, 10, 9)) == "8:15 AM a 12:00 PM y 2:00 PM a 5:45 PM"
assert agenda.describir_jornadas(date(2026, 10, 11)) == "cerrado"
assert formatear_dia(date(2026, 10, 8)) == "Jueves 8 de Octubre de 2026"
assert formatear_fecha_legible("2026-09-29T15:00:00+00:00") == "Martes 29 de Septiembre de 2026 a las 10:00 AM", (
    "Las horas que llegan en UTC se muestran en hora de Colombia"
)
print("✅ Formato en español y en hora de Colombia.")

print("\n✅ Todos los tests de la agenda de entregas pasaron.")
