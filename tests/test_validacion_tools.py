"""
tests/test_validacion_tools.py

Fase 4 del plan de mejoras (`docs/PLAN_DE_MEJORAS.md`): valida sin red

1. La COHERENCIA entre `TOOLS_SCHEMA` (lo que ve el modelo), los modelos
   Pydantic de `tools/validacion_tools.py` y las funciones Python del
   agente de Servicio Técnico. Un nombre o un parámetro desincronizado antes
   fallaba en silencio.
2. La validación de argumentos (errores legibles, sin ejecutar la tool).

Las reglas de calendario se prueban en `tests/test_agenda_entregas.py`.

Corre con:
    python tests/test_validacion_tools.py
"""
import copy
import os
import sys
from datetime import date, time
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake-key")
os.environ.setdefault("OPENROUTER_API_KEY", "fake-key-para-el-mock")
os.environ.setdefault("OPENROUTER_MODELS", "modelo-simulado:free")

from agents import orquestador, servicio_tecnico_agent  # noqa: E402
from tools.validacion_tools import ARGUMENTOS_POR_TOOL, con_validacion, verificar_coherencia_tools  # noqa: E402

# ---------------------------------------------------------------------------
# 1) Coherencia TOOLS_SCHEMA <-> modelos Pydantic <-> tool_functions.
# ---------------------------------------------------------------------------
funciones = servicio_tecnico_agent._tool_functions_para_sesion("sesion", "turno")
problemas = verificar_coherencia_tools(servicio_tecnico_agent.TOOLS_SCHEMA, funciones, ARGUMENTOS_POR_TOOL)
assert not problemas, "\n".join(problemas)

# La verificación detecta de verdad un desfase (se prueba con un schema alterado).
schema_roto = copy.deepcopy(servicio_tecnico_agent.TOOLS_SCHEMA)
crear_roto = next(t for t in schema_roto if t["function"]["name"] == "Crear_orden_servicio")
crear_roto["function"]["parameters"]["required"].remove("descripcion")
problemas_rotos = verificar_coherencia_tools(schema_roto, funciones, ARGUMENTOS_POR_TOOL)
assert any("Crear_orden_servicio: required" in p for p in problemas_rotos), problemas_rotos

orq_funciones = {t["function"]["name"] for t in orquestador.TOOLS_SCHEMA}
assert orq_funciones == {"Agente_Servicio_Tecnico", "Agente_Ventas", "Info_empresa"}, (
    "El orquestador solo debe exponer las tools que existen"
)
print("✅ TOOLS_SCHEMA, modelos Pydantic y funciones Python están sincronizados (nombres, campos y obligatorios).")

# ---------------------------------------------------------------------------
# 2) Validación de argumentos: errores legibles y la tool NO se ejecuta.
# ---------------------------------------------------------------------------
llamada = MagicMock(return_value="ejecutada")
crear = con_validacion("Crear_orden_servicio", llamada)
validos = {
    "servicio_id": "3",  # texto: se acepta y se convierte a 3
    "cliente_nombre": "  Ana Pérez ",
    "cliente_telefono": "+57 300 123 4567",
    "equipo": "Portátil Lenovo",
    "descripcion": "No prende",
    "fecha_entrega": "2026-10-09",
    "hora_aproximada": "9:30",
    "campo_inventado": "x",  # extra: se ignora en vez de romper con TypeError
}
assert crear(**validos) == "ejecutada"
kwargs = llamada.call_args.kwargs
assert kwargs["servicio_id"] == 3 and kwargs["cliente_nombre"] == "Ana Pérez"
assert kwargs["fecha_entrega"] == date(2026, 10, 9) and kwargs["hora_aproximada"] == time(9, 30)
assert "campo_inventado" not in kwargs
assert kwargs["cliente_telefono"] == "+57 300 123 4567", "El teléfono se valida pero NUNCA se reformatea"
sin_hora = {k: v for k, v in validos.items() if k != "hora_aproximada"}
assert crear(**sin_hora) == "ejecutada" and "hora_aproximada" not in llamada.call_args.kwargs, (
    "La hora aproximada es opcional"
)
print("✅ Argumentos válidos pasan normalizados (texto->número, fecha y hora como objetos, extras ignorados).")

llamada.reset_mock()
casos_invalidos = [
    ({k: v for k, v in validos.items() if k != "equipo"}, "equipo: falta este campo obligatorio"),
    ({**validos, "fecha_entrega": "el lunes"}, "YYYY-MM-DD"),
    ({**validos, "hora_aproximada": "9 am"}, "HH:MM"),
    ({**validos, "hora_aproximada": "25:00"}, "HH:MM"),
    ({**validos, "cliente_telefono": "300-12"}, "entre 7 y 15 dígitos"),
    ({**validos, "cliente_telefono": "tres cero cero"}, "solo puede contener"),
    ({**validos, "cliente_nombre": "   "}, "no puede estar vacío"),
    ({**validos, "servicio_id": "abc"}, "servicio_id"),
]
for args, esperado in casos_invalidos:
    resultado = crear(**args)
    assert resultado.startswith("ERROR: argumentos inválidos para Crear_orden_servicio"), resultado
    assert esperado in resultado, f"Se esperaba '{esperado}' en: {resultado}"
llamada.assert_not_called()
print("✅ Argumentos inválidos devuelven un ERROR legible y la tool no se ejecuta.")

modificar = con_validacion("Modificar_orden_servicio", llamada)
assert "al menos un dato" in modificar(numero=25), "Modificar sin ningún cambio es un error del modelo"
assert modificar(numero="25", fecha_entrega="2026-10-10") == "ejecutada"
assert llamada.call_args.kwargs == {"numero": 25, "fecha_entrega": date(2026, 10, 10)}, (
    "Solo se pasan los campos que el modelo envió (los demás significan 'no cambiar')"
)
print("✅ Modificar_orden_servicio: exige algún cambio y solo los campos enviados llegan a la función.")

print("\n✅ Todos los tests de validación pasaron.")
