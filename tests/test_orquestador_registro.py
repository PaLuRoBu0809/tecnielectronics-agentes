"""
tests/test_orquestador_registro.py

Fases 11 y 12 del plan de mejoras (`docs/PLAN_DE_MEJORAS.md`): agregar un
sub-agente es crear su módulo y registrarlo en `orquestador.SUBAGENTES`.
Verifica el registro real (Servicio Técnico + Ventas) y, con un
"Agente_Ventas" SIMULADO, que el orquestador lo invoca, guarda su historial
bajo su propia clave y comparte el presupuesto del turno.

Corre con:
    python tests/test_orquestador_registro.py
"""
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("OPENROUTER_API_KEY", "fake-key-para-el-mock")
os.environ.setdefault("OPENROUTER_MODELS", "modelo-simulado:free")
os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake-key")
os.environ["LOG_EVENTOS_JSON"] = "0"

import llm_loop  # noqa: E402
from agents import orquestador  # noqa: E402
from agents.orquestador import SubAgente  # noqa: E402

llamadas_ventas = []


def _ventas_run(mensaje_cliente, session_id, historial, run_id, presupuesto):
    """Sub-agente de ventas simulado: responde y extiende SU historial."""
    llamadas_ventas.append({"mensaje": mensaje_cliente, "historial_previo": list(historial),
                            "presupuesto": presupuesto, "run_id": run_id})
    nuevo = historial + [{"role": "user", "content": mensaje_cliente},
                         {"role": "assistant", "content": "Tenemos el teclado X a $100."}]
    return "Tenemos el teclado X a $100.", nuevo


VENTAS = SubAgente(
    tool="Agente_Ventas",
    clave_historial="ventas",
    descripcion="Subagente especializado en venta de equipos.",
    ejecutar=_ventas_run,
)

# ---------------------------------------------------------------------------
# 1) Registro real (Fase 12): Servicio Técnico y Ventas, sin nota temporal.
# ---------------------------------------------------------------------------
assert [t["function"]["name"] for t in orquestador.TOOLS_SCHEMA] == ["Agente_Servicio_Tecnico", "Agente_Ventas"]
assert orquestador.TOOLS_SCHEMA[1]["function"]["parameters"]["required"] == ["mensaje_cliente"]
assert "NOTA TEMPORAL" not in orquestador.SYSTEM_PROMPT
assert orquestador.SYSTEM_PROMPT == orquestador.ORIGINAL_SYSTEM_PROMPT + orquestador.NOTA_ASESOR_COMERCIAL, (
    "Prompt original intacto + la nota de asesor comercial al final"
)
ventas_real = next(s for s in orquestador.SUBAGENTES if s.tool == "Agente_Ventas")
assert ventas_real.clave_historial == "ventas"
with patch("agents.ventas_agent.run", return_value=("ok", [])) as run_ventas:
    ventas_real.ejecutar(mensaje_cliente="hola", session_id="1", historial=[], run_id="r", presupuesto=None)
assert run_ventas.call_count == 1, "La referencia es perezosa: los tests pueden reemplazar ventas_agent.run"
from agents import servicio_tecnico_agent, ventas_agent  # noqa: E402

tools_por_subagente = {
    "Agente_Servicio_Tecnico": {t["function"]["name"] for t in servicio_tecnico_agent.TOOLS_SCHEMA},
    "Agente_Ventas": {t["function"]["name"] for t in ventas_agent.TOOLS_SCHEMA},
}
for sub in orquestador.SUBAGENTES:
    assert sub.tool_de_cierre in tools_por_subagente[sub.tool], (
        f"{sub.tool}: la tool de cierre '{sub.tool_de_cierre}' debe existir en el sub-agente (venta cruzada)"
    )
    assert sub.prefijo_de_exito and sub.venta_cruzada
print("✅ Registro real: Servicio Técnico y Ventas como tools; prompt original + nota de asesor comercial.")

# ---------------------------------------------------------------------------
# 2) Sin Ventas registrado, la nota temporal vuelve (la regla sigue viva).
# ---------------------------------------------------------------------------
solo_servicio = tuple(s for s in orquestador.SUBAGENTES if s.tool != "Agente_Ventas")
assert "NOTA TEMPORAL DE ESTA FASE DE DESARROLLO" in orquestador.construir_system_prompt(solo_servicio)
assert [t["function"]["name"] for t in orquestador.construir_tools_schema(solo_servicio)] == [
    "Agente_Servicio_Tecnico"]

# Para el turno simulado, Ventas se reemplaza por el sub-agente falso.
registro = solo_servicio + (VENTAS,)
schema = orquestador.construir_tools_schema(registro)
prompt = orquestador.construir_system_prompt(registro)
assert prompt == orquestador.SYSTEM_PROMPT
print("✅ Sin Ventas registrado la nota temporal reaparece; con él, desaparece.")

# ---------------------------------------------------------------------------
# 3) Turno completo con el LLM simulado: el orquestador delega a Ventas.
# ---------------------------------------------------------------------------


def _respuesta(content=None, tool_calls=None):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=None)


ARGS_VENTAS = json.dumps({"mensaje_cliente": "quiero un teclado"})
llamada_ventas = SimpleNamespace(
    id="v1",
    function=SimpleNamespace(name="Agente_Ventas", arguments=ARGS_VENTAS),
    model_dump=lambda: {"id": "v1", "type": "function",
                        "function": {"name": "Agente_Ventas", "arguments": ARGS_VENTAS}},
)
respuestas = iter([_respuesta(tool_calls=[llamada_ventas]), _respuesta("Tenemos el teclado X a $100.")])

historiales_previos = {
    "orquestador": [{"role": "user", "content": "hola"}, {"role": "assistant", "content": "¡Hola!"}],
    "servicio_tecnico": [{"role": "user", "content": "mouse dañado"}],
    "ventas": [{"role": "user", "content": "¿tienen audífonos?"}],
}
llm_loop.reiniciar_disyuntor()
with (
    patch.object(orquestador, "SUBAGENTES", registro),
    patch.object(orquestador, "TOOLS_SCHEMA", schema),
    patch.object(orquestador, "SYSTEM_PROMPT", prompt),
    patch("llm_loop.OpenAI") as mock_openai,
):
    mock_openai.return_value.chat.completions.create.side_effect = lambda **kw: next(respuestas)
    texto, historiales = orquestador.run("quiero un teclado", session_id="3001", historiales=historiales_previos)
    llamadas_al_modelo = mock_openai.return_value.chat.completions.create.call_count

assert texto == "Tenemos el teclado X a $100."
assert llamadas_al_modelo == 1, (
    "La respuesta del sub-agente va directo al cliente: sin una 2ª llamada al modelo solo para copiarla"
)
assert historiales["orquestador"][-1] == {"role": "assistant", "content": "Tenemos el teclado X a $100."}, (
    "El orquestador guarda en SU historial lo que respondió el sub-agente (para saber qué tema sigue)"
)
assert len(llamadas_ventas) == 1 and llamadas_ventas[0]["historial_previo"] == historiales_previos["ventas"], (
    "Ventas debe recibir SU historial previo, no el de otro agente"
)
assert llamadas_ventas[0]["presupuesto"] is not None and llamadas_ventas[0]["run_id"], (
    "Ventas debe compartir el presupuesto y el run_id del turno"
)
assert historiales["ventas"][-1]["content"] == "Tenemos el teclado X a $100.", "Su historial se guarda bajo 'ventas'"
assert historiales["servicio_tecnico"] == historiales_previos["servicio_tecnico"], "Servicio Técnico no se toca"
assert historiales["orquestador"][0]["content"] == "hola", "El historial del orquestador conserva lo anterior"
assert historiales_previos["ventas"] == [{"role": "user", "content": "¿tienen audífonos?"}], "No muta la entrada"
print("✅ Turno completo: el orquestador delega a Ventas, que recibe su historial y presupuesto, y se guarda aparte.")

# ---------------------------------------------------------------------------
# 4) Una sola delegación por mensaje (bug real de la prueba del 2026-10-01:
#    el modelo invocó a Ventas dos veces en el mismo turno). Con la respuesta
#    directa el turno termina tras la primera delegación; si el modelo pide
#    dos en la MISMA respuesta, la segunda se bloquea sin ejecutarse.
# ---------------------------------------------------------------------------
llamadas_ventas.clear()
segunda = SimpleNamespace(
    id="v2",
    function=SimpleNamespace(name="Agente_Ventas", arguments=json.dumps({"mensaje_cliente": "¿Confirmas?"})),
    model_dump=lambda: {"id": "v2", "type": "function",
                        "function": {"name": "Agente_Ventas", "arguments": "{}"}},
)
respuestas = iter([_respuesta(tool_calls=[llamada_ventas, segunda])])
llm_loop.reiniciar_disyuntor()
with (
    patch.object(orquestador, "SUBAGENTES", registro),
    patch.object(orquestador, "TOOLS_SCHEMA", schema),
    patch("llm_loop.OpenAI") as mock_openai,
):
    mock_openai.return_value.chat.completions.create.side_effect = lambda **kw: next(respuestas)
    texto, historiales = orquestador.run("quiero un teclado", session_id="3001", historiales={})

assert len(llamadas_ventas) == 1, "La segunda delegación del mismo turno NO debe ejecutar al sub-agente"
bloqueo = next(m for m in historiales["orquestador"] if m.get("tool_call_id") == "v2")
assert bloqueo["content"].startswith("(interno) BLOQUEADO") and "Agente_Ventas" in bloqueo["content"]
assert texto == "Tenemos el teclado X a $100.", "Al cliente le llega la respuesta de la primera delegación"
print("✅ Una delegación por mensaje: un segundo intento en el mismo turno se bloquea sin ejecutar al sub-agente.")

# ---------------------------------------------------------------------------
# 5) Venta cruzada desde el código: al registrar un pedido se ofrece la otra
#    línea de negocio, una sola vez por conversación.
# ---------------------------------------------------------------------------
LINEA = "Por cierto, también te agendamos servicio técnico. 🛠️"
resultado_crear_orden = {"valor": "OK: PEDIDO REGISTRADO (pago contra entrega). Pedido #50"}


def _ventas_que_crea_orden(mensaje_cliente, session_id, historial, run_id, presupuesto):
    """Ventas simulado que en este turno llamó a Crear_orden."""
    nuevo = historial + [
        {"role": "user", "content": mensaje_cliente},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "Crear_orden", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": resultado_crear_orden["valor"]},
        {"role": "assistant", "content": "¡Tu pedido #50 quedó registrado!"},
    ]
    return "¡Tu pedido #50 quedó registrado!", nuevo


VENTAS_CIERRE = SubAgente(
    tool="Agente_Ventas", clave_historial="ventas", descripcion="Ventas.", ejecutar=_ventas_que_crea_orden,
    tool_de_cierre="Crear_orden", prefijo_de_exito="OK: PEDIDO REGISTRADO", venta_cruzada=LINEA,
)
registro_cierre = solo_servicio + (VENTAS_CIERRE,)


def _turno(historiales_previos):
    respuestas_turno = iter([_respuesta(tool_calls=[llamada_ventas])])
    llm_loop.reiniciar_disyuntor()
    with (
        patch.object(orquestador, "SUBAGENTES", registro_cierre),
        patch.object(orquestador, "TOOLS_SCHEMA", orquestador.construir_tools_schema(registro_cierre)),
        patch("llm_loop.OpenAI") as mock_openai,
    ):
        mock_openai.return_value.chat.completions.create.side_effect = lambda **kw: next(respuestas_turno)
        return orquestador.run("sí, confirmo", session_id="3001", historiales=historiales_previos)


texto, historiales = _turno({})
assert texto == "¡Tu pedido #50 quedó registrado!" + "\n\n" + LINEA, texto
assert historiales["orquestador"][-1]["content"] == texto, "El historial guarda lo que el cliente vio"
texto, _ = _turno(historiales)
assert texto == "¡Tu pedido #50 quedó registrado!", "La venta cruzada se ofrece una sola vez por conversación"
resultado_crear_orden["valor"] = "CONFIRMACION_PENDIENTE: Todavía NO se ha creado el pedido."
texto, _ = _turno({})
assert LINEA not in texto, "Si el pedido no se registró (pendiente de confirmar), no hay venta cruzada"
print("✅ Venta cruzada desde el código: tras registrar un pedido, una sola vez por conversación.")

# Respuesta final vacía y sin delegación: nunca una burbuja en blanco.
respuestas = iter([_respuesta("")])
llm_loop.reiniciar_disyuntor()
with (
    patch.object(orquestador, "SUBAGENTES", registro),
    patch.object(orquestador, "TOOLS_SCHEMA", schema),
    patch("llm_loop.OpenAI") as mock_openai,
):
    mock_openai.return_value.chat.completions.create.side_effect = lambda **kw: next(respuestas)
    texto, historiales = orquestador.run("hola", session_id="3001", historiales={})
assert texto == orquestador.RESPUESTA_VACIA, texto
assert historiales["orquestador"][-1]["content"] == orquestador.RESPUESTA_VACIA
print("✅ Si el modelo termina sin texto, el cliente recibe un aviso para repetir el mensaje.")

print("\n✅ Todos los tests del registro de sub-agentes pasaron.")
