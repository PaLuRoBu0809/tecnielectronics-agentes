"""
tests/test_observabilidad.py

Fase 8 del plan de mejoras (`docs/PLAN_DE_MEJORAS.md`): tokens y costo por
turno, logs JSON con datos personales enmascarados, métricas y alertas.
Sin red: LLM simulado.

Corre con:
    python tests/test_observabilidad.py
"""
import io
import json
import logging
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("OPENROUTER_API_KEY", "fake-key-para-el-mock")
os.environ.setdefault("OPENROUTER_MODELS", "modelo-simulado:free")
os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake-key")
# Vacía (no ausente): load_dotenv() no pisa variables ya definidas, así el
# API_KEY del .env de quien corre los tests no se cuela en este test.
os.environ["API_KEY"] = ""

import llm_loop  # noqa: E402
from llm_loop import PresupuestoTurno, run_agent_loop  # noqa: E402
from tools import eventos_agente, metricas  # noqa: E402

# Capturar las líneas JSON que el logger de eventos escribe en stdout.
salida_json = io.StringIO()
logger_json = logging.getLogger("tecnielectronics.eventos")
logger_json.handlers[0].setStream(salida_json)


def _lineas_json():
    return [json.loads(linea) for linea in salida_json.getvalue().splitlines() if linea.strip()]


def _respuesta(content, prompt_tokens, completion_tokens, cost=None):
    usage = SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
    if cost is not None:
        usage.cost = cost
    msg = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=usage)


# ---------------------------------------------------------------------------
# 1) Tokens y costo: se acumulan en el presupuesto del turno, por agente y modelo.
# ---------------------------------------------------------------------------
llm_loop.reiniciar_disyuntor()
metricas.reiniciar()
presupuesto = PresupuestoTurno()
with patch("llm_loop.OpenAI") as mock_openai:
    mock_openai.return_value.chat.completions.create.side_effect = [
        _respuesta("uno", 1200, 80, cost=0.0003),
        _respuesta("dos", 900, 40),  # sin 'cost' (modelo :free): cuenta como 0
    ]
    for _ in range(2):
        run_agent_loop("p", [{"role": "user", "content": "x"}], [], {}, models=["m1"],
                       contexto={"presupuesto": presupuesto, "agente": "Orquestador"})
resumen = presupuesto.resumen()
assert resumen["tokens_entrada"] == 2100 and resumen["tokens_salida"] == 120
assert abs(resumen["costo_usd"] - 0.0003) < 1e-9
assert resumen["uso_por_modelo"]["Orquestador|m1"]["llamadas"] == 2
print("✅ Tokens y costo se acumulan por turno y se desglosan por agente+modelo.")

eventos_respuesta = [e for e in _lineas_json() if e["evento"] == "respuesta_modelo"]
assert eventos_respuesta[0]["tokens_entrada"] == 1200, "El evento respuesta_modelo debe llevar los tokens"
print("✅ Cada respuesta_modelo publica sus tokens y costo.")

# ---------------------------------------------------------------------------
# 2) Logs JSON: una línea por evento, parseable, con PII enmascarada.
# ---------------------------------------------------------------------------
salida_json.truncate(0)
salida_json.seek(0)
eventos_agente.publicar_evento(
    "tool_llamada",
    session_id="3177510761",
    run_id="abc123",
    nombre="Crear_evento",
    argumentos=json.dumps({"cliente_nombre": "Ana Pérez", "cliente_telefono": "300 123 4567",
                           "fecha_hora_inicio": "2026-10-05T09:00:00-05:00"}),
)
eventos_agente.publicar_evento(
    "tool_resultado", session_id="3177510761", run_id="abc123", nombre="Consultar_servicio_agendado",
    resultado="1) cliente_nombre=Ana Pérez | cliente_telefono=3001234567 | estado=confirmado",
)
lineas = _lineas_json()
assert len(lineas) == 2, "Debe escribir exactamente una línea JSON por evento"
texto = salida_json.getvalue()
for dato_personal in ("3177510761", "Ana Pérez", "300 123 4567", "3001234567"):
    assert dato_personal not in texto, f"El log JSON no debe contener '{dato_personal}'"
assert lineas[0]["session_id"] == "***761", "session_id conserva solo los 3 últimos dígitos"
assert lineas[0]["run_id"] == "abc123", "run_id no se enmascara (se necesita para reconstruir el turno)"
assert "2026-10-05T09:00:00-05:00" in texto, "Las fechas no deben confundirse con teléfonos"
assert "estado=confirmado" in texto
print("✅ Logs JSON: una línea por evento, sin teléfonos ni nombres, con run_id y fechas intactos.")

# El panel en vivo (autenticado) sigue recibiendo el dato sin enmascarar.
_, cola = eventos_agente.suscribirse()
eventos_agente.publicar_evento("tool_llamada", session_id="3177510761", nombre="x")
assert cola.get_nowait()["session_id"] == "3177510761"
print("✅ El panel 'Flujo en Vivo' sigue viendo los datos completos (solo los logs se enmascaran).")

with patch.dict(os.environ, {"LOG_EVENTOS_JSON": "0"}):
    salida_json.truncate(0)
    salida_json.seek(0)
    eventos_agente.publicar_evento("tool_llamada", nombre="x")
    assert salida_json.getvalue() == "", "LOG_EVENTOS_JSON=0 debe desactivar los logs JSON"
print("✅ LOG_EVENTOS_JSON=0 desactiva los logs JSON.")

# ---------------------------------------------------------------------------
# 3) Métricas y alertas.
# ---------------------------------------------------------------------------
metricas.reiniciar()
alertas = io.StringIO()
manejador_alertas = logging.StreamHandler(alertas)
logging.getLogger("tecnielectronics.alertas").addHandler(manejador_alertas)

for i in range(metricas.VENTANA_TURNOS):
    razon = "modelos_caidos" if i % 2 else "final"  # 50 % de respaldo
    eventos_agente.publicar_evento("resumen_turno", run_id=f"r{i}", llamadas_llm=3, tokens_entrada=100,
                                   tokens_salida=10, costo_usd=0.0, razones=[{"agente": "Orquestador", "razon": razon}])
foto = metricas.instantanea()
assert foto["turnos"] == 20 and foto["llamadas_llm_por_turno"] == 3
assert foto["razones_parada"] == {"Orquestador:final": 10, "Orquestador:modelos_caidos": 10}
assert foto["tasa_fallback_reciente"] == 0.5
assert alertas.getvalue().count("ALERTA tasa_fallback") == 1, "Debe alertar UNA vez (luego se silencia)"
print("✅ Métricas por turno y alerta de tasa de respaldo (emitida una sola vez por ventana de silencio).")

eventos_agente.publicar_evento("modelo_fallo", modelo="m1", tipo_error="credenciales")
eventos_agente.publicar_evento("modelo_fallo", modelo="m2", tipo_error="limite")
assert "ALERTA credenciales_openrouter" in alertas.getvalue()
assert metricas.instantanea()["fallos_modelo_por_tipo"] == {"credenciales": 1, "limite": 1}
print("✅ Fallos de modelo contados por tipo y alerta CRITICAL ante credenciales/saldo.")

for i in range(metricas.VENTANA_TOOLS):
    eventos_agente.publicar_evento("tool_resultado", nombre="t", resultado="ERROR: x" if i % 3 else "ok")
assert "ALERTA tasa_errores_tool" in alertas.getvalue()
print("✅ Alerta cuando más de la mitad de las tools recientes devuelven ERROR.")

# Una métrica que falla nunca tumba el turno del cliente.
with patch.object(metricas, "_registrar", side_effect=RuntimeError("roto")):
    eventos_agente.publicar_evento("resumen_turno", razones=[])
print("✅ Un error en las métricas no interrumpe la publicación de eventos.")

# ---------------------------------------------------------------------------
# 4) /api/metricas expone la instantánea.
# ---------------------------------------------------------------------------
from fastapi.testclient import TestClient  # noqa: E402

from web import app as modulo_app  # noqa: E402

respuesta = TestClient(modulo_app.app).get("/api/metricas")
assert respuesta.status_code == 200 and respuesta.json()["turnos"] == 20
print("✅ GET /api/metricas devuelve las métricas actuales.")

print("\n✅ Todos los tests de observabilidad pasaron.")
