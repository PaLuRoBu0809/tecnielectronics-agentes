"""
tests/test_citas_repository.py

Valida, sin tocar Supabase real, `tools/supabase_client.py` +
`tools/citas_repository.py` + `tools/citas_tools.py` (el módulo que
reemplazó a `calendar_tools.py` al quitar Google Calendar — ver su
docstring), y que la nota de optimización de flujo (Parte C) y la fecha
actual dinámica sigan agregadas al prompt del Agente de Servicio Técnico.
Mismo espíritu que `tests/test_llm_loop.py`: un script con asserts que
corre gratis y rápido, sin gastar cuota real ni tocar la red.

Corre con:
    python tests/test_citas_repository.py
"""
import os
import sys
from unittest.mock import MagicMock, patch

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake-key")
os.environ.setdefault("SUPABASE_TABLE_CITAS", "servicios_agendados")
os.environ.setdefault("SUPABASE_TABLE_CATALOGO", "servicios_tecnicos")
os.environ.setdefault("OPENROUTER_API_KEY", "fake-key-para-el-mock")
os.environ.setdefault("OPENROUTER_MODELS", "modelo-simulado:free")

from tools import supabase_client  # noqa: E402
from tools import citas_tools  # noqa: E402


def _http_error(status_code: int, body: dict) -> requests.exceptions.HTTPError:
    """Construye un HTTPError con una respuesta falsa, para simular lo que
    devuelve `requests` cuando `raise_for_status()` falla — sin llamar a
    Supabase real."""
    respuesta = MagicMock()
    respuesta.status_code = status_code
    respuesta.json.return_value = body
    error = requests.exceptions.HTTPError(response=respuesta)
    return error


# ---------------------------------------------------------------------------
# 1) formatear_fila expone columnas dinámicas (la razón de ser del refactor:
#    una columna nueva en Supabase no debe requerir tocar código Python).
# ---------------------------------------------------------------------------

fila_con_columna_nueva = {
    "servicio_id": "1",
    "estado": "confirmado",
    "tecnico_asignado": "Carlos",  # columna que NO existía cuando se escribió este código
}

texto = supabase_client.formatear_fila(fila_con_columna_nueva)
assert "tecnico_asignado=Carlos" in texto, (
    "formatear_fila debe incluir columnas nuevas automáticamente, sin listarlas a mano"
)
print("✅ formatear_fila expone columnas dinámicas (incluida una que el código no conocía).")


# ---------------------------------------------------------------------------
# 2) es_violacion_de_solapamiento reconoce el error real que devuelve
#    PostgREST (HTTP 400 + code "23P01") verificado empíricamente contra la
#    API real de Supabase, y lo distingue de cualquier otro error.
# ---------------------------------------------------------------------------

error_solapamiento = _http_error(400, {"code": "23P01", "message": "conflicting key value..."})
error_generico = _http_error(400, {"code": "23502", "message": "not null violation"})

assert supabase_client.es_violacion_de_solapamiento(error_solapamiento) is True, (
    "Debe reconocer el error real de PostgREST (400 + code 23P01) como violación de solapamiento"
)
assert supabase_client.es_violacion_de_solapamiento(error_generico) is False, (
    "No debe confundir otros errores 400 (ej. una columna NOT NULL) con el de solapamiento"
)
print("✅ es_violacion_de_solapamiento distingue el error real de PostgREST de cualquier otro.")


# ---------------------------------------------------------------------------
# 3) crear_evento traduce la violación de solapamiento en un mensaje amable
#    ("ese horario ya no está disponible"), sin ningún Calendar de por medio
#    — el propio Postgres es quien rechaza la escritura (constraint
#    no_solapamiento_citas_confirmadas).
# ---------------------------------------------------------------------------

with patch.object(citas_tools, "insertar_cita", side_effect=error_solapamiento):
    resultado = citas_tools.crear_evento(
        servicio_id="1",
        cliente_nombre="Juan",
        cliente_telefono="3000000000",
        descripcion="Mouse dañado",
        fecha_hora_inicio="2026-09-30T16:00:00-05:00",
        fecha_hora_fin="2026-09-30T17:00:00-05:00",
    )

assert "ERROR" in resultado, "crear_evento debe informar un error si el horario ya no está disponible"
assert "ya no está disponible" in resultado, (
    "crear_evento debe traducir la violación del constraint en un mensaje amable, no técnico"
)
print("✅ crear_evento traduce el choque de horarios (constraint de Postgres) en un mensaje amable.")


# ---------------------------------------------------------------------------
# 4) normalizar_fecha_hora + crear_evento: la fecha que se guarda en
#    Supabase es SIEMPRE la que nosotros normalizamos, con offset explícito
#    — nunca un valor "naive". Bug real detectado en pruebas manuales: al
#    reprogramar una cita de las 4:00 PM de Bogotá, quedó guardada como
#    "16:00:00+00" (5 horas corrida) por no garantizar el offset.
# ---------------------------------------------------------------------------

naive = citas_tools.normalizar_fecha_hora("2026-09-30T16:00:00")
assert naive.endswith("-05:00"), (
    "Un datetime sin offset debe normalizarse asumiendo la zona horaria del negocio (TIMEZONE_OFFSET)"
)

con_offset = citas_tools.normalizar_fecha_hora("2026-09-30T16:00:00-05:00")
assert con_offset == "2026-09-30T16:00:00-05:00", "Un datetime que YA trae offset explícito no debe alterarse"

with patch.object(citas_tools, "insertar_cita") as insertar_cita_mock:
    insertar_cita_mock.return_value = {
        "fecha_hora_inicio": "2026-09-30T16:00:00-05:00",
        citas_tools.CAMPO_FECHA_FIN_DB: "2026-09-30T17:00:00-05:00",
    }
    citas_tools.crear_evento(
        servicio_id="1",
        cliente_nombre="Juan",
        cliente_telefono="3000000000",
        # Deliberadamente SIN offset: si normalizar_fecha_hora no se aplicara,
        # esta hora quedaría mal guardada.
        descripcion="Prueba de zona horaria",
        fecha_hora_inicio="2026-09-30T16:00:00",
        fecha_hora_fin="2026-09-30T17:00:00",
    )

payload_enviado = insertar_cita_mock.call_args.args[0]
assert payload_enviado["fecha_hora_inicio"] == "2026-09-30T16:00:00-05:00", (
    "crear_evento debe normalizar la fecha (agregar el offset del negocio) antes de guardarla"
)
assert payload_enviado["google_calendar_event_id"], (
    "crear_evento debe seguir generando un identificador (ya no viene de Calendar, es un UUID propio)"
)
print("✅ crear_evento normaliza siempre la fecha con offset explícito antes de guardarla en Supabase.")


# ---------------------------------------------------------------------------
# 5) consultar_eventos consulta Supabase (ya no Calendar) filtrando por
#    solapamiento, y agrupa el resultado por día en el mismo formato de
#    siempre — el prompt no tuvo que cambiar.
# ---------------------------------------------------------------------------

citas_ocupadas = [
    {"fecha_hora_inicio": "2026-09-30T09:00:00-05:00", citas_tools.CAMPO_FECHA_FIN_DB: "2026-09-30T10:00:00-05:00"},
    {"fecha_hora_inicio": "2026-09-30T14:00:00-05:00", citas_tools.CAMPO_FECHA_FIN_DB: "2026-09-30T15:00:00-05:00"},
]

with patch.object(citas_tools, "leer_citas_confirmadas_en_rango", return_value=citas_ocupadas) as leer_mock:
    resultado = citas_tools.consultar_eventos("2026-09-27T00:00:00-05:00", "2026-10-15T00:00:00-05:00")

leer_mock.assert_called_once_with("2026-09-27T00:00:00-05:00", "2026-10-15T00:00:00-05:00")
assert "9:00 AM a 10:00 AM" in resultado, "consultar_eventos debe mostrar la primera franja ocupada"
assert "2:00 PM a 3:00 PM" in resultado, "consultar_eventos debe agrupar la segunda franja del mismo día"
print("✅ consultar_eventos consulta Supabase (no Calendar) y agrupa la disponibilidad por día.")

with patch.object(citas_tools, "leer_citas_confirmadas_en_rango", return_value=[]):
    resultado_vacio = citas_tools.consultar_eventos("2026-09-27T00:00:00-05:00", "2026-10-15T00:00:00-05:00")

assert resultado_vacio == "Sin eventos ocupados en el rango consultado", (
    "Una lista vacía es una respuesta válida (todo el rango está libre), no un error"
)
print("✅ consultar_eventos informa correctamente cuando todo el rango está libre.")


# ---------------------------------------------------------------------------
# 6) eliminar_evento cancela con un UPDATE de estado (soft-delete) — sin
#    ningún Calendar de por medio.
# ---------------------------------------------------------------------------

fila_cancelada = {
    "google_calendar_event_id": "abc123",
    "fecha_hora_inicio": "2026-07-28T09:00:00-05:00",
    "estado": "cancelado por cliente",
}

with patch.object(citas_tools, "cancelar_cita", return_value=fila_cancelada) as cancelar_cita_mock:
    resultado = citas_tools.eliminar_evento("abc123")

cancelar_cita_mock.assert_called_once_with("abc123")
assert "Cita cancelada exitosamente" in resultado, "eliminar_evento debe confirmar la cancelación"
assert "estado=cancelado por cliente" in resultado, (
    "eliminar_evento debe cancelar marcando 'estado' (UPDATE), nunca borrando la fila"
)
print("✅ eliminar_evento cancela con un UPDATE de 'estado' (soft-delete).")


# ---------------------------------------------------------------------------
# 7) La nota de optimización de flujo (Parte C) sigue agregada al prompt del
#    Agente de Servicio Técnico, sin haber tocado el prompt original.
# ---------------------------------------------------------------------------

from agents import servicio_tecnico_agent  # noqa: E402

assert "<ROL>" in servicio_tecnico_agent.ORIGINAL_SYSTEM_PROMPT, (
    "El prompt original de negocio no debe reescribirse"
)
assert "NOTA DE OPTIMIZACIÓN DE FLUJO" in servicio_tecnico_agent.SYSTEM_PROMPT, (
    "La nota de optimización de flujo (Parte C) debe seguir agregada al final del prompt"
)
assert servicio_tecnico_agent.SYSTEM_PROMPT.startswith(servicio_tecnico_agent.ORIGINAL_SYSTEM_PROMPT), (
    "SYSTEM_PROMPT debe ser el prompt original intacto + la nota, en ese orden"
)
print("✅ SYSTEM_PROMPT combina el prompt original intacto + la nota de optimización de flujo.")


# ---------------------------------------------------------------------------
# 8) La fecha "actual" que se le anuncia al modelo es la fecha real de HOY
#    (calculada en el momento), no un valor fijo copiado del prompt.
# ---------------------------------------------------------------------------
from datetime import datetime  # noqa: E402

nota = servicio_tecnico_agent._nota_fecha_actual()
ahora_esperado = datetime.now(citas_tools.zona_horaria_configurada())

assert "NOTA DE FECHA ACTUAL" in nota, "Debe existir una nota explícita anunciando la fecha actual"
assert ahora_esperado.strftime("%Y-%m-%d") in nota, (
    "La nota debe contener la fecha de HOY calculada en el momento, no un valor fijo del prompt"
)

with patch.object(
    servicio_tecnico_agent, "run_agent_loop", return_value=("ok", [])
) as run_agent_loop_mock:
    servicio_tecnico_agent.run(mensaje_cliente="hola", session_id="3000000000")

system_prompt_usado = run_agent_loop_mock.call_args.kwargs["system_prompt"]
assert ahora_esperado.strftime("%Y-%m-%d") in system_prompt_usado, (
    "run() debe pasarle a run_agent_loop un system_prompt que incluya la fecha de HOY"
)
print("✅ El agente recibe la fecha/hora real de HOY en cada turno, calculada dinámicamente.")

print("\n✅ Todos los tests de citas_repository/citas_tools/prompt pasaron.")
