"""
tests/test_validacion_tools.py

Fase 4 del plan de mejoras (`docs/PLAN_DE_MEJORAS.md`): valida sin red

1. La COHERENCIA entre `TOOLS_SCHEMA` (lo que ve el modelo), los modelos
   Pydantic de `tools/validacion_tools.py` y las funciones Python del
   agente. Un nombre o un parámetro desincronizado antes fallaba en
   silencio (el modelo recibía "la tool no existe" y nadie se enteraba).
2. La validación de argumentos (errores legibles, sin ejecutar la tool).
3. Las reglas de agenda en código (`citas_tools.validar_horario_cita`).

Corre con:
    python tests/test_validacion_tools.py
"""
import copy
import os
import sys
from datetime import datetime
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake-key")
os.environ.setdefault("OPENROUTER_API_KEY", "fake-key-para-el-mock")
os.environ.setdefault("OPENROUTER_MODELS", "modelo-simulado:free")

from agents import orquestador, servicio_tecnico_agent  # noqa: E402
from tools import citas_tools  # noqa: E402
from tools.validacion_tools import ARGUMENTOS_POR_TOOL, con_validacion, verificar_coherencia_tools  # noqa: E402

# ---------------------------------------------------------------------------
# 1) Coherencia TOOLS_SCHEMA <-> modelos Pydantic <-> tool_functions.
# ---------------------------------------------------------------------------
funciones = servicio_tecnico_agent._tool_functions_para_sesion("sesion", "turno")
problemas = verificar_coherencia_tools(servicio_tecnico_agent.TOOLS_SCHEMA, funciones, ARGUMENTOS_POR_TOOL)
assert not problemas, "\n".join(problemas)

# La verificación detecta de verdad un desfase (se prueba con un schema alterado).
schema_roto = copy.deepcopy(servicio_tecnico_agent.TOOLS_SCHEMA)
crear_roto = next(t for t in schema_roto if t["function"]["name"] == "Crear_evento")
crear_roto["function"]["parameters"]["required"].remove("descripcion")
problemas_rotos = verificar_coherencia_tools(schema_roto, funciones, ARGUMENTOS_POR_TOOL)
assert any("Crear_evento: required" in p for p in problemas_rotos), problemas_rotos

orq_funciones = {t["function"]["name"] for t in orquestador.TOOLS_SCHEMA}
assert orq_funciones == {"Agente_Servicio_Tecnico", "Agente_Ventas", "Info_empresa"}, (
    "El orquestador solo debe exponer las tools que existen"
)
print("✅ TOOLS_SCHEMA, modelos Pydantic y funciones Python están sincronizados (nombres, campos y obligatorios).")

# ---------------------------------------------------------------------------
# 2) Validación de argumentos: errores legibles y la tool NO se ejecuta.
# ---------------------------------------------------------------------------
llamada = MagicMock(return_value="ejecutada")
crear = con_validacion("Crear_evento", llamada)
validos = {
    "servicio_id": 3,  # número: se acepta y se convierte a "3"
    "cliente_nombre": "  Ana Pérez ",
    "cliente_telefono": "+57 300 123 4567",
    "descripcion": "No prende",
    "fecha_hora_inicio": "2026-10-05T09:00:00-05:00",
    "fecha_hora_fin": "2026-10-05T10:00:00-05:00",
    "campo_inventado": "x",  # extra: se ignora en vez de romper con TypeError
}
assert crear(**validos) == "ejecutada"
kwargs = llamada.call_args.kwargs
assert kwargs["servicio_id"] == "3" and kwargs["cliente_nombre"] == "Ana Pérez"
assert "campo_inventado" not in kwargs
assert kwargs["cliente_telefono"] == "+57 300 123 4567", "El teléfono se valida pero NUNCA se reformatea"
print("✅ Argumentos válidos pasan normalizados (número->texto, espacios, extras ignorados).")

llamada.reset_mock()
casos_invalidos = [
    ({k: v for k, v in validos.items() if k != "descripcion"}, "descripcion: falta este campo obligatorio"),
    ({**validos, "fecha_hora_inicio": "lunes a las 9"}, "ISO 8601"),
    ({**validos, "fecha_hora_fin": "2026-10-05T08:00:00-05:00"}, "posterior"),
    ({**validos, "cliente_telefono": "300-12"}, "entre 7 y 15 dígitos"),
    ({**validos, "cliente_telefono": "tres cero cero"}, "solo puede contener"),
    ({**validos, "cliente_nombre": "   "}, "no puede estar vacío"),
    # Una fecha con offset y otra sin él: antes de este arreglo, Python
    # lanzaba TypeError al compararlas.
    ({**validos, "fecha_hora_fin": "2026-10-05T08:00:00"}, "posterior"),
]
for args, esperado in casos_invalidos:
    resultado = crear(**args)
    assert resultado.startswith("ERROR: argumentos inválidos para Crear_evento"), resultado
    assert esperado in resultado, f"Se esperaba '{esperado}' en: {resultado}"
llamada.assert_not_called()
print("✅ Argumentos inválidos devuelven un ERROR legible y la tool no se ejecuta.")

actualizar = con_validacion("Actualizar_evento", llamada)
r = actualizar(google_calendar_event_id="abc", fecha_hora_inicio="2026-10-05T09:00:00-05:00")
assert "juntos" in r, "Cambiar la fecha exige enviar inicio y fin juntos"
assert actualizar(google_calendar_event_id="abc", descripcion="nuevo detalle") == "ejecutada"
assert llamada.call_args.kwargs == {"google_calendar_event_id": "abc", "descripcion": "nuevo detalle"}, (
    "Solo se pasan los campos que el modelo envió (los demás significan 'no cambiar')"
)
print("✅ Actualizar_evento: fechas juntas y solo los campos enviados llegan a la función.")

# ---------------------------------------------------------------------------
# 3) Reglas de agenda en código.
# ---------------------------------------------------------------------------
tz = citas_tools.zona_horaria_configurada()
with patch.object(citas_tools, "_ahora", return_value=datetime(2026, 10, 1, 8, 0, tzinfo=tz)):
    v = citas_tools.validar_horario_cita
    # 2026-10-05 es lunes; 2026-10-10 es sábado.
    assert v("2026-10-05T09:00:00-05:00", "2026-10-05T10:00:00-05:00", 60) is None
    assert "ya pasaron" in v("2026-09-30T09:00:00-05:00", "2026-09-30T10:00:00-05:00", 60)
    assert "lunes a viernes" in v("2026-10-10T09:00:00-05:00", "2026-10-10T10:00:00-05:00", 60)
    assert "7:00 AM y 6:00 PM" in v("2026-10-05T06:00:00-05:00", "2026-10-05T07:00:00-05:00", 60)
    assert "7:00 AM y 6:00 PM" in v("2026-10-05T17:30:00-05:00", "2026-10-05T18:30:00-05:00", 60)
    assert v("2026-10-05T17:00:00-05:00", "2026-10-05T18:00:00-05:00", 60) is None, "Terminar a las 6:00 PM es válido"
    duracion = v("2026-10-05T09:00:00-05:00", "2026-10-05T10:00:00-05:00", 90)
    assert "dura 90 minutos" in duracion and "2026-10-05T10:30:00-05:00" in duracion, (
        "Debe decirle al modelo la hora de fin correcta según el catálogo"
    )
    # 14:00 UTC = 9:00 AM Colombia: la regla se evalúa en la zona del negocio.
    assert v("2026-10-05T14:00:00+00:00", "2026-10-05T15:00:00+00:00", 60) is None
print("✅ Reglas de agenda: futuro, lunes-viernes, 7 AM-6 PM, duración del catálogo, en hora de Colombia.")

# Crear_evento aplica las reglas ANTES de la salvaguarda: nunca pide
# confirmar un horario inválido.
with (
    patch.object(citas_tools, "_ahora", return_value=datetime(2026, 10, 1, 8, 0, tzinfo=tz)),
    patch.object(citas_tools, "leer_servicio", return_value={"tecnico_id": 1, "duracion_minutos": 60}),
    patch.object(citas_tools, "insertar_cita") as insertar_mock,
):
    r = citas_tools.crear_evento(
        servicio_id="1", cliente_nombre="Ana", cliente_telefono="3001234567", descripcion="x",
        fecha_hora_inicio="2026-10-11T09:00:00-05:00", fecha_hora_fin="2026-10-11T10:00:00-05:00",  # domingo
        session_id="s-reglas", id_turno="t1",
    )
assert "lunes a viernes" in r and "CONFIRMACION_PENDIENTE" not in r
insertar_mock.assert_not_called()
print("✅ Crear_evento rechaza un horario inválido antes de pedir confirmación al cliente.")

print("\n✅ Todos los tests de validación pasaron.")
