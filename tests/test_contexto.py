"""
tests/test_contexto.py

Fase 6 del plan de mejoras (`docs/PLAN_DE_MEJORAS.md`): compactación del
contexto — ventana deslizante, ficha construida por código y recorte al
guardar. Sin red.

Corre con:
    python tests/test_contexto.py
"""
import json
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake-key")
os.environ.setdefault("OPENROUTER_API_KEY", "fake-key-para-el-mock")
os.environ.setdefault("OPENROUTER_MODELS", "modelo-simulado:free")

from agents import servicio_tecnico_agent  # noqa: E402
from contexto_conversacion import (  # noqa: E402
    aplicar_ventana,
    construir_ficha,
    limitar_historial_guardado,
    nota_ficha,
)
from llm_loop import sanear_historial  # noqa: E402


def _turno(i, tool=None, args=None, resultado="ok"):
    """Un turno: user -> (assistant con tool_call -> tool) -> assistant."""
    mensajes = [{"role": "user", "content": f"mensaje {i}"}]
    if tool:
        mensajes += [
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": f"c{i}", "type": "function", "function": {"name": tool, "arguments": json.dumps(args or {})}}
            ]},
            {"role": "tool", "tool_call_id": f"c{i}", "content": resultado},
        ]
    mensajes.append({"role": "assistant", "content": f"respuesta {i}"})
    return mensajes


# ---------------------------------------------------------------------------
# 1) Ventana: últimos N turnos, empieza en `user`, no corta tool calls.
# ---------------------------------------------------------------------------
historial = []
for i in range(20):
    historial += _turno(i, tool="Servicio_tecnico" if i % 2 else None)

descartados, ventana = aplicar_ventana(historial, max_turnos=12)
assert descartados + ventana == historial, "Ventana + descartados deben reconstruir el historial exacto"
assert ventana[0]["role"] == "user" and ventana[0]["content"] == "mensaje 8"
assert sum(m["role"] == "user" for m in ventana) == 12
assert sanear_historial(ventana) == ventana, "La ventana no debe cortar una llamada a tools de su resultado"
assert aplicar_ventana(historial[:10], max_turnos=12) == ([], historial[:10]), "Historial corto: sin cambios"
print("✅ La ventana envía los últimos N turnos, empieza en un mensaje del cliente y no corta tool calls.")

# ---------------------------------------------------------------------------
# 2) Ficha: construida SOLO desde argumentos de tools; el valor más reciente gana.
# ---------------------------------------------------------------------------
viejos = (
    _turno(1, "Consultar_eventos", {"servicio_id": 3, "fecha_inicio": "x", "fecha_fin": "y"})
    + _turno(2, "Crear_evento", {"cliente_nombre": "Ana Pérez", "cliente_telefono": "3001112222",
                                 "servicio_id": "3", "descripcion": "No prende",
                                 "fecha_hora_inicio": "x", "fecha_hora_fin": "y"})
    + _turno(3, "Actualizar_evento", {"google_calendar_event_id": "abc", "cliente_telefono": "3009998888"})
)
ficha = construir_ficha(viejos, servicio_tecnico_agent.CAMPOS_FICHA)
assert ficha == {"servicio_id": "3", "cliente_nombre": "Ana Pérez", "cliente_telefono": "3009998888",
                 "descripcion": "No prende"}, ficha
assert "abc" not in nota_ficha(ficha), "La ficha nunca incluye el Event ID (el prompt prohíbe reutilizarlo)"
assert nota_ficha({}) == "", "Sin datos no se agrega ninguna nota al prompt"
print("✅ La ficha sale de los argumentos de tools, gana el dato más reciente y nunca incluye el Event ID.")

# ---------------------------------------------------------------------------
# 3) El sub-agente: ventana al modelo, ficha solo si hubo descarte, y
#    devuelve el historial COMPLETO.
# ---------------------------------------------------------------------------
largo = viejos + sum((_turno(i) for i in range(10, 25)), [])
with patch.object(servicio_tecnico_agent, "run_agent_loop", side_effect=lambda **kw: ("ok", kw["messages"] + [
        {"role": "assistant", "content": "ok"}])) as loop_mock:
    _, devuelto = servicio_tecnico_agent.run(mensaje_cliente="nuevo", session_id="s", historial=largo, run_id="r")
kwargs = loop_mock.call_args.kwargs
assert sum(m["role"] == "user" for m in kwargs["messages"]) == 12, "Al modelo solo le llega la ventana"
assert "NOTA DE CONTEXTO ANTERIOR" in kwargs["system_prompt"] and "3009998888" in kwargs["system_prompt"]
assert devuelto[: len(largo)] == largo and devuelto[-2]["content"] == "nuevo", (
    "El historial devuelto debe ser el completo: lo descartado + la ventana actualizada"
)

corto = sum((_turno(i) for i in range(3)), [])
with patch.object(servicio_tecnico_agent, "run_agent_loop", return_value=("ok", [])) as loop_mock:
    servicio_tecnico_agent.run(mensaje_cliente="hola", session_id="s", historial=corto, run_id="r")
assert "NOTA DE CONTEXTO ANTERIOR" not in loop_mock.call_args.kwargs["system_prompt"], (
    "En una conversación normal (sin descarte) el prompt no debe cambiar"
)
print("✅ El sub-agente envía la ventana, agrega la ficha solo en conversaciones largas y conserva todo el historial.")

# ---------------------------------------------------------------------------
# 4) Al guardar: recorta resultados de tools antiguos y largos, salvo el más
#    reciente de cada tool; los turnos recientes quedan intactos.
# ---------------------------------------------------------------------------
catalogo_largo = "servicio " * 400  # ~3600 caracteres
guardado = (
    _turno(1, "Consultar_eventos", {}, resultado="ocupado " * 400)  # antiguo y largo -> se recorta
    + _turno(2, "Servicio_tecnico", {}, resultado=catalogo_largo)  # el ÚNICO catálogo -> se conserva
    + _turno(3, "Consultar_eventos", {}, resultado="ocupado " * 400)  # último de su tool -> se conserva
    + sum((_turno(i) for i in range(4, 10)), [])
)
limitado = limitar_historial_guardado(guardado, turnos_intactos=4, max_caracteres_tool=1500, max_mensajes=300)
herramientas = [m for m in limitado if m["role"] == "tool"]
assert "recortado" in herramientas[0]["content"] and len(herramientas[0]["content"]) < 500
assert herramientas[1]["content"] == catalogo_largo, "El resultado más reciente de cada tool nunca se recorta"
assert herramientas[2]["content"] == "ocupado " * 400
assert guardado[0]["role"] == "user" and "recortado" not in guardado[3]["content"], "No debe mutar el original"
print("✅ Al guardar se recortan resultados antiguos, salvo el más reciente de cada tool (ej. el catálogo).")

tope = limitar_historial_guardado(sum((_turno(i, "Servicio_tecnico") for i in range(50)), []), max_mensajes=40)
assert len(tope) <= 40 and tope[0]["role"] == "user" and sanear_historial(tope) == tope
print("✅ El tope de mensajes guardados corta en un mensaje del cliente y deja un historial válido.")

# sesiones.guardar aplica la compactación.
from sesiones import AlmacenSesiones  # noqa: E402
from tools import supabase_client  # noqa: E402

with patch.object(supabase_client, "upsert_row", return_value={}) as upsert_mock:
    AlmacenSesiones().guardar("s", {"orquestador": guardado})
payload = upsert_mock.call_args.kwargs["payload"]
assert "recortado" in json.dumps(payload["historiales"]["orquestador"], ensure_ascii=False)
print("✅ AlmacenSesiones.guardar aplica la compactación antes de escribir en Supabase.")

print("\n✅ Todos los tests de contexto pasaron.")
