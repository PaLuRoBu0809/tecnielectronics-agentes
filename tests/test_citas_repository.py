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

from datetime import datetime as _dt  # noqa: E402

# Fase 4 del plan de mejoras: las tools de escritura ahora rechazan horarios
# en el pasado. Se fija la hora "actual" para que las fechas de ejemplo de
# estos tests (finales de septiembre / octubre de 2026) sigan siendo futuras
# sin importar cuándo se corran.
HOY_FIJO = _dt(2026, 9, 1, 8, 0, tzinfo=citas_tools.zona_horaria_configurada())
patch.object(citas_tools, "_ahora", return_value=HOY_FIJO).start()

# Servicio de catálogo típico de estos tests: técnico 1, citas de 60 minutos.
SERVICIO_TECNICO_1 = {"id": "1", "tecnico_id": 1, "duracion_minutos": 60}


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

# Fase 1.3 del plan de mejoras: columnas internas nunca llegan al modelo.
texto_con_internas = supabase_client.formatear_fila(
    {"estado": "confirmado", "session_id": "3177510761", "tecnico_id": 2, "periodo": "[...)"}
)
for oculta in ("session_id", "tecnico_id", "periodo", "3177510761"):
    assert oculta not in texto_con_internas, f"formatear_fila no debe exponer '{oculta}' al modelo"
assert "estado=confirmado" in texto_con_internas
print("✅ formatear_fila oculta al modelo session_id (teléfono), tecnico_id y periodo.")


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
#    no_solapamiento_citas_confirmadas). Se mockea leer_servicio (técnico +
#    duración del catálogo) porque crear_evento ahora resuelve el técnico ANTES de escribir la cita
#    (ver supabase/migrations/20260928000003_tecnicos.sql: la disponibilidad/el constraint son por técnico).
#    Como crear_evento ahora exige el handshake de confirmación de 2 pasos
#    (ver bloque 3c), la primera llamada NUNCA llega a insertar_cita — hay
#    que "confirmar" con un segundo mensaje antes de poder probar el error.
# ---------------------------------------------------------------------------

with patch.object(citas_tools, "leer_servicio", return_value=SERVICIO_TECNICO_1):
    citas_tools.crear_evento(
        servicio_id="1",
        cliente_nombre="Juan",
        cliente_telefono="3000000000",
        descripcion="Mouse dañado",
        fecha_hora_inicio="2026-09-30T16:00:00-05:00",
        fecha_hora_fin="2026-09-30T17:00:00-05:00",
        session_id="sesion-solapamiento",
        id_turno="quiero agendar",
    )
    with patch.object(citas_tools, "insertar_cita", side_effect=error_solapamiento):
        resultado = citas_tools.crear_evento(
            servicio_id="1",
            cliente_nombre="Juan",
            cliente_telefono="3000000000",
            descripcion="Mouse dañado",
            fecha_hora_inicio="2026-09-30T16:00:00-05:00",
            fecha_hora_fin="2026-09-30T17:00:00-05:00",
            session_id="sesion-solapamiento",
            id_turno="si, confirmo",
        )

assert "ERROR" in resultado, "crear_evento debe informar un error si el horario ya no está disponible"
assert "ya no está disponible" in resultado, (
    "crear_evento debe traducir la violación del constraint en un mensaje amable, no técnico"
)
print("✅ crear_evento traduce el choque de horarios (constraint de Postgres) en un mensaje amable.")


# ---------------------------------------------------------------------------
# 3c) LA SALVAGUARDA DE CONFIRMACIÓN EN SÍ — bug real detectado probando un
#     agendamiento real: un modelo de 120B parámetros llamó a Crear_evento
#     inmediatamente después de que el cliente eligiera la hora, sin mostrar
#     ningún resumen ni esperar un "sí". Esto verifica que el CÓDIGO (no el
#     prompt) garantiza el handshake de 2 pasos para las 3 tools de escritura.
# ---------------------------------------------------------------------------

with patch.object(citas_tools, "leer_servicio", return_value=SERVICIO_TECNICO_1):
    with patch.object(citas_tools, "insertar_cita") as insertar_mock:
        # Primera llamada: nunca debe escribir, sin importar los datos.
        primera = citas_tools.crear_evento(
            servicio_id="1", cliente_nombre="Ana", cliente_telefono="3000000001",
            descripcion="Prueba salvaguarda", fecha_hora_inicio="2026-10-01T09:00:00-05:00",
            fecha_hora_fin="2026-10-01T10:00:00-05:00",
            session_id="sesion-salvaguarda", id_turno="a las 9am",
        )
        assert "CONFIRMACION_PENDIENTE" in primera, "La primera llamada nunca debe escribir directamente"
        insertar_mock.assert_not_called()

        # Reintento con el MISMO mensaje del cliente (simula al modelo
        # "autoconfirmándose" dentro del mismo turno): debe seguir bloqueado.
        reintento_mismo_turno = citas_tools.crear_evento(
            servicio_id="1", cliente_nombre="Ana", cliente_telefono="3000000001",
            descripcion="Prueba salvaguarda", fecha_hora_inicio="2026-10-01T09:00:00-05:00",
            fecha_hora_fin="2026-10-01T10:00:00-05:00",
            session_id="sesion-salvaguarda", id_turno="a las 9am",
        )
        assert "CONFIRMACION_PENDIENTE" in reintento_mismo_turno, (
            "Reintentar con el mismo mensaje del cliente (mismo turno) NO debe contar como confirmación"
        )
        insertar_mock.assert_not_called()

        # Un mensaje NUEVO y distinto del cliente, con los MISMOS datos de la
        # propuesta -> ahí sí se interpreta como confirmación real y escribe.
        insertar_mock.return_value = {
            "fecha_hora_inicio": "2026-10-01T09:00:00-05:00",
            citas_tools.CAMPO_FECHA_FIN_DB: "2026-10-01T10:00:00-05:00",
        }
        confirmada = citas_tools.crear_evento(
            servicio_id="1", cliente_nombre="Ana", cliente_telefono="3000000001",
            descripcion="Prueba salvaguarda", fecha_hora_inicio="2026-10-01T09:00:00-05:00",
            fecha_hora_fin="2026-10-01T10:00:00-05:00",
            session_id="sesion-salvaguarda", id_turno="si, confirmo",
        )
        assert "CONFIRMACION_PENDIENTE" not in confirmada, "Con un mensaje nuevo confirmando, sí debe escribir"
        insertar_mock.assert_called_once()
print("✅ La salvaguarda de confirmación bloquea la escritura hasta un mensaje nuevo del cliente.")


# ---------------------------------------------------------------------------
# 3b) Si el servicio no tiene técnico configurado, crear_evento/consultar_eventos
#     deben fallar con un mensaje claro ANTES de tocar la base de citas —
#     nunca asignar "cualquier técnico" ni calcular disponibilidad a ciegas.
# ---------------------------------------------------------------------------

with (
    patch.object(citas_tools, "leer_servicio", return_value={"id": "99", "tecnico_id": None}),
    patch.object(citas_tools, "resolver_tecnico_para_servicio", return_value=None),
):
    # Un solo intento basta: la validación de técnico ocurre ANTES de la
    # salvaguarda de confirmación (no tiene sentido pedir confirmar algo
    # que nunca podría completarse).
    resultado_sin_tecnico = citas_tools.crear_evento(
        servicio_id="99",
        cliente_nombre="Juan",
        cliente_telefono="3000000000",
        descripcion="Servicio sin técnico asignado",
        fecha_hora_inicio="2026-09-30T16:00:00-05:00",
        fecha_hora_fin="2026-09-30T17:00:00-05:00",
        session_id="sesion-sin-tecnico",
        id_turno="quiero agendar",
    )
    resultado_consulta_sin_tecnico = citas_tools.consultar_eventos(
        "2026-09-27T00:00:00-05:00", "2026-10-15T00:00:00-05:00", servicio_id="99"
    )

assert "ERROR" in resultado_sin_tecnico, "crear_evento debe rechazar el agendamiento sin técnico configurado"
assert "técnico" in resultado_sin_tecnico, "el mensaje debe explicar que falta el técnico del servicio"
assert "ERROR" in resultado_consulta_sin_tecnico, "consultar_eventos debe rechazar la consulta sin técnico configurado"
assert "técnico" in resultado_consulta_sin_tecnico, "el mensaje debe explicar que falta el técnico del servicio"
print("✅ Sin técnico configurado para el servicio, ninguna operación asume disponibilidad ni agenda.")


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

# Bug real detectado probando un agendamiento real: Supabase devuelve TODO
# timestamptz normalizado a UTC (comportamiento estándar de Postgres, sin
# importar en qué offset se insertó el valor). formatear_fecha_legible debe
# convertir siempre a la zona horaria del negocio antes de formatear, o el
# resumen final que ve el cliente muestra la hora UTC cruda en vez de la
# hora de Colombia real de la cita (una cita de las 10:00 AM Colombia le
# llegaba al cliente como "3:00 PM").
assert citas_tools.formatear_fecha_legible("2026-09-29T15:00:00+00:00") == citas_tools.formatear_fecha_legible(
    "2026-09-29T10:00:00-05:00"
), "formatear_fecha_legible debe convertir a la zona horaria del negocio, no formatear el offset crudo"
assert "10:00 AM" in citas_tools.formatear_fecha_legible("2026-09-29T15:00:00+00:00"), (
    "15:00 UTC debe mostrarse como 10:00 AM (hora de Colombia, offset -05:00), no como 3:00 PM"
)
print("✅ formatear_fecha_legible convierte a la zona horaria del negocio, no muestra la hora UTC cruda de Supabase.")

with (
    patch.object(citas_tools, "leer_servicio", return_value={"tecnico_id": 7, "duracion_minutos": 60}),
    patch.object(citas_tools, "insertar_cita") as insertar_cita_mock,
):
    insertar_cita_mock.return_value = {
        "fecha_hora_inicio": "2026-09-30T16:00:00-05:00",
        citas_tools.CAMPO_FECHA_FIN_DB: "2026-09-30T17:00:00-05:00",
    }
    # Primer mensaje propone (no escribe); un segundo mensaje distinto
    # confirma — así se prueba el payload real que SÍ llega a insertar_cita.
    citas_tools.crear_evento(
        servicio_id="1", cliente_nombre="Juan", cliente_telefono="3000000000",
        descripcion="Prueba de zona horaria",
        fecha_hora_inicio="2026-09-30T16:00:00", fecha_hora_fin="2026-09-30T17:00:00",
        session_id="sesion-tz", id_turno="quiero esa hora",
    )
    citas_tools.crear_evento(
        servicio_id="1",
        cliente_nombre="Juan",
        cliente_telefono="3000000000",
        # Deliberadamente SIN offset: si normalizar_fecha_hora no se aplicara,
        # esta hora quedaría mal guardada.
        descripcion="Prueba de zona horaria",
        fecha_hora_inicio="2026-09-30T16:00:00",
        fecha_hora_fin="2026-09-30T17:00:00",
        session_id="sesion-tz",
        id_turno="si, confirmo",
    )

payload_enviado = insertar_cita_mock.call_args.args[0]
assert payload_enviado["fecha_hora_inicio"] == "2026-09-30T16:00:00-05:00", (
    "crear_evento debe normalizar la fecha (agregar el offset del negocio) antes de guardarla"
)
assert payload_enviado["google_calendar_event_id"], (
    "crear_evento debe seguir generando un identificador (ya no viene de Calendar, es un UUID propio)"
)
assert payload_enviado["tecnico_id"] == 7, (
    "crear_evento debe guardar el tecnico_id resuelto automáticamente a partir del servicio_id"
)
# Bug real detectado probando el flujo de consulta de citas: la fila se
# guardaba con session_id=cliente_telefono (el teléfono de CONTACTO dictado
# en la conversación) en vez del session_id REAL de la sesión — si un
# cliente agenda dando un teléfono de contacto distinto al de su sesión
# activa, Consultar_servicio_agendado (que filtra por el session_id real)
# nunca encontraba esa cita después. En este test cliente_telefono
# ("3000000000") y session_id ("sesion-tz") son deliberadamente distintos
# para detectar justamente esta confusión.
assert payload_enviado["session_id"] == "sesion-tz", (
    "crear_evento debe guardar el session_id REAL de la sesión, nunca el cliente_telefono dictado"
)
print("✅ crear_evento normaliza siempre la fecha con offset explícito antes de guardarla en Supabase.")


# ---------------------------------------------------------------------------
# 5) consultar_eventos consulta Supabase (ya no Calendar) filtrando por
#    solapamiento Y por el técnico del servicio_id dado, y agrupa el
#    resultado por día en el mismo formato de siempre — el prompt no tuvo
#    que cambiar en su lógica, solo ganó el parámetro servicio_id.
# ---------------------------------------------------------------------------

citas_ocupadas = [
    {"fecha_hora_inicio": "2026-09-30T09:00:00-05:00", citas_tools.CAMPO_FECHA_FIN_DB: "2026-09-30T10:00:00-05:00"},
    {"fecha_hora_inicio": "2026-09-30T14:00:00-05:00", citas_tools.CAMPO_FECHA_FIN_DB: "2026-09-30T15:00:00-05:00"},
]

with (
    patch.object(citas_tools, "resolver_tecnico_para_servicio", return_value=7) as resolver_mock,
    patch.object(citas_tools, "leer_citas_confirmadas_en_rango", return_value=citas_ocupadas) as leer_mock,
):
    resultado = citas_tools.consultar_eventos(
        "2026-09-27T00:00:00-05:00", "2026-10-15T00:00:00-05:00", servicio_id="1"
    )

resolver_mock.assert_called_once_with("1")
leer_mock.assert_called_once_with("2026-09-27T00:00:00-05:00", "2026-10-15T00:00:00-05:00", tecnico_id=7)
assert "9:00 AM a 10:00 AM" in resultado, "consultar_eventos debe mostrar la primera franja ocupada"
assert "2:00 PM a 3:00 PM" in resultado, "consultar_eventos debe agrupar la segunda franja del mismo día"
print("✅ consultar_eventos consulta Supabase (no Calendar), filtra por técnico y agrupa por día.")

with (
    patch.object(citas_tools, "resolver_tecnico_para_servicio", return_value=7),
    patch.object(citas_tools, "leer_citas_confirmadas_en_rango", return_value=[]),
):
    resultado_vacio = citas_tools.consultar_eventos(
        "2026-09-27T00:00:00-05:00", "2026-10-15T00:00:00-05:00", servicio_id="1"
    )

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

cita_activa_de_sesion_cancelar = {
    "google_calendar_event_id": "abc123", "session_id": "sesion-cancelar", "estado": "confirmado",
}
with (
    patch.object(citas_tools, "leer_cita_por_event_id", return_value=cita_activa_de_sesion_cancelar),
    patch.object(citas_tools, "cancelar_cita", return_value=fila_cancelada) as cancelar_cita_mock,
):
    pendiente = citas_tools.eliminar_evento(
        "abc123", session_id="sesion-cancelar", id_turno="quiero cancelar"
    )
    assert "CONFIRMACION_PENDIENTE" in pendiente, "eliminar_evento no debe cancelar sin confirmación previa"
    cancelar_cita_mock.assert_not_called()

    resultado = citas_tools.eliminar_evento(
        "abc123", session_id="sesion-cancelar", id_turno="si, seguro"
    )

cancelar_cita_mock.assert_called_once_with("abc123", session_id="sesion-cancelar")
assert "Cita cancelada exitosamente" in resultado, "eliminar_evento debe confirmar la cancelación"
assert "estado=cancelado por cliente" in resultado, (
    "eliminar_evento debe cancelar marcando 'estado' (UPDATE), nunca borrando la fila"
)
print("✅ eliminar_evento exige confirmación previa y cancela con un UPDATE de 'estado' (soft-delete).")


# ---------------------------------------------------------------------------
# 6b) actualizar_evento también exige la confirmación de 2 pasos antes de
#     escribir — misma salvaguarda que crear_evento/eliminar_evento.
# ---------------------------------------------------------------------------

registro_original = {
    "cliente_nombre": "Ana", "cliente_telefono": "3000000001", "servicio_id": "1",
    "fecha_hora_inicio": "2026-10-01T09:00:00-05:00", citas_tools.CAMPO_FECHA_FIN_DB: "2026-10-01T10:00:00-05:00",
    "Descripcion": "Original", "tecnico_id": 1,
    "session_id": "sesion-modificar", "estado": "confirmado",
}
with (
    patch.object(citas_tools, "leer_cita_por_event_id", return_value=registro_original),
    patch.object(citas_tools, "leer_servicio", return_value=SERVICIO_TECNICO_1),
    patch.object(citas_tools, "actualizar_cita") as actualizar_cita_mock,
):
    pendiente_act = citas_tools.actualizar_evento(
        "abc123", session_id="sesion-modificar", id_turno="quiero cambiar la hora",
        fecha_hora_inicio="2026-10-01T11:00:00-05:00", fecha_hora_fin="2026-10-01T12:00:00-05:00",
    )
    assert "CONFIRMACION_PENDIENTE" in pendiente_act, "actualizar_evento no debe modificar sin confirmación previa"
    actualizar_cita_mock.assert_not_called()

    actualizar_cita_mock.return_value = {
        "fecha_hora_inicio": "2026-10-01T11:00:00-05:00", citas_tools.CAMPO_FECHA_FIN_DB: "2026-10-01T12:00:00-05:00",
    }
    citas_tools.actualizar_evento(
        "abc123", session_id="sesion-modificar", id_turno="si, ese cambio",
        fecha_hora_inicio="2026-10-01T11:00:00-05:00", fecha_hora_fin="2026-10-01T12:00:00-05:00",
    )
actualizar_cita_mock.assert_called_once()
assert actualizar_cita_mock.call_args.kwargs["session_id"] == "sesion-modificar", (
    "El PATCH también debe filtrar por session_id (segunda barrera en la base de datos)"
)
print("✅ actualizar_evento también exige confirmación de un mensaje nuevo antes de aplicar el cambio.")


# ---------------------------------------------------------------------------
# 6c) Fase 1.1 del plan de mejoras: un cliente NO puede modificar ni cancelar
#     la cita de otro, aunque conozca su Event ID, ni una cita ya cancelada.
#     El rechazo ocurre ANTES de la salvaguarda de confirmación y nunca
#     llega a escribir.
# ---------------------------------------------------------------------------

cita_de_otro_cliente = {**registro_original, "session_id": "sesion-de-otro"}
cita_ya_cancelada = {**registro_original, "estado": "cancelado por cliente"}

for fila_encontrada, sesion_que_pide in (
    (cita_de_otro_cliente, "sesion-atacante"),
    (None, "sesion-atacante"),
):
    with (
        patch.object(citas_tools, "leer_cita_por_event_id", return_value=fila_encontrada),
        patch.object(citas_tools, "actualizar_cita") as actualizar_mock,
        patch.object(citas_tools, "cancelar_cita") as cancelar_mock,
    ):
        resultados = [
            citas_tools.actualizar_evento(
                "abc123", session_id=sesion_que_pide, id_turno=f"msg-{i}",
                descripcion="cambio malicioso",
            )
            for i in range(2)
        ] + [
            citas_tools.eliminar_evento("abc123", session_id=sesion_que_pide, id_turno=f"msg-{i}")
            for i in range(2)
        ]
    for r in resultados:
        assert r.startswith("ERROR") and "No se encontró" in r, f"Debe rechazar la cita ajena/inexistente: {r}"
    actualizar_mock.assert_not_called()
    cancelar_mock.assert_not_called()

# El mensaje para una cita ajena es idéntico al de una inexistente: no se
# revela que ese Event ID existe.
with patch.object(citas_tools, "leer_cita_por_event_id", return_value=cita_de_otro_cliente):
    msg_ajena = citas_tools.eliminar_evento("abc123", session_id="sesion-atacante", id_turno="x")
with patch.object(citas_tools, "leer_cita_por_event_id", return_value=None):
    msg_inexistente = citas_tools.eliminar_evento("abc123", session_id="sesion-atacante", id_turno="x")
assert msg_ajena == msg_inexistente, "Una cita ajena debe verse exactamente igual que una inexistente"

with (
    patch.object(citas_tools, "leer_cita_por_event_id", return_value=cita_ya_cancelada),
    patch.object(citas_tools, "cancelar_cita") as cancelar_mock,
):
    r = citas_tools.eliminar_evento("abc123", session_id="sesion-modificar", id_turno="x")
assert "no está activa" in r, "No debe permitir cancelar una cita que ya no está confirmada"
cancelar_mock.assert_not_called()
print("✅ Actualizar/Eliminar_evento rechazan citas ajenas o inactivas sin revelar que existen.")


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
assert "NOTA DE ASIGNACIÓN AUTOMÁTICA DE TÉCNICOS" in servicio_tecnico_agent.SYSTEM_PROMPT, (
    "La nota de agendamiento por técnico debe estar agregada al prompt"
)
assert "NOTA DE CONFIRMACIÓN OBLIGATORIA" in servicio_tecnico_agent.SYSTEM_PROMPT, (
    "La nota que explica CONFIRMACION_PENDIENTE debe estar agregada al prompt"
)
assert "NOTA DE NATURALIDAD DE LA CONVERSACIÓN" in servicio_tecnico_agent.SYSTEM_PROMPT, (
    "La nota que evita repetir la bienvenida y mensajes textuales debe estar agregada al prompt"
)
assert servicio_tecnico_agent.SYSTEM_PROMPT.endswith(servicio_tecnico_agent.NOTA_NATURALIDAD_CONVERSACION), (
    "La nota de naturalidad va al final, después de las reglas de negocio"
)
assert servicio_tecnico_agent.SYSTEM_PROMPT.startswith(servicio_tecnico_agent.ORIGINAL_SYSTEM_PROMPT), (
    "SYSTEM_PROMPT debe ser el prompt original intacto + las notas, en ese orden"
)
assert servicio_tecnico_agent.NOTA_ESTADO_DEL_EQUIPO in servicio_tecnico_agent.SYSTEM_PROMPT, (
    "La nota del estado del equipo (Notas_servicio) debe estar en el prompt"
)
assert "Notas_servicio" not in servicio_tecnico_agent.ORIGINAL_SYSTEM_PROMPT, (
    "El prompt original no se toca: Notas_servicio solo aparece en la nota agregada"
)

consultar_eventos_schema = next(
    t["function"] for t in servicio_tecnico_agent.TOOLS_SCHEMA if t["function"]["name"] == "Consultar_eventos"
)
assert "servicio_id" in consultar_eventos_schema["parameters"]["required"], (
    "Consultar_eventos debe exigir servicio_id en su schema (para resolver de qué técnico consulta)"
)
print("✅ SYSTEM_PROMPT combina el prompt original intacto + las notas, y Consultar_eventos exige servicio_id.")


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


# ---------------------------------------------------------------------------
# 9) Fase 5 del plan de mejoras: la salvaguarda compara el TURNO (run_id),
#    no el texto que el orquestador le pasa al sub-agente. Bug que corrige:
#    si el orquestador parafraseaba igual dos mensajes distintos del cliente
#    ("El cliente confirma la cita."), la confirmación nunca pasaba.
# ---------------------------------------------------------------------------
ids_inyectados = []
for run_id in ("turno-1", "turno-2"):
    with patch.object(servicio_tecnico_agent, "run_agent_loop", return_value=("ok", [])) as loop_mock:
        servicio_tecnico_agent.run(mensaje_cliente="El cliente confirma la cita.", session_id="s", run_id=run_id)
    # funcion_original: el partial, sin el envoltorio de validación.
    crear = loop_mock.call_args.kwargs["tool_functions"]["Crear_evento"].funcion_original
    ids_inyectados.append(crear.keywords["id_turno"])
assert ids_inyectados == ["turno-1", "turno-2"], (
    "Con el mismo texto en dos turnos, el id_turno inyectado debe ser distinto (el run_id de cada turno)"
)

from agents import orquestador  # noqa: E402

with patch.object(orquestador, "run_agent_loop", return_value=("ok", [])) as loop_orq_mock:
    orquestador.run(mensaje_cliente="hola", session_id="s")  # sin run_id, como main.py
assert loop_orq_mock.call_args.kwargs["contexto"]["run_id"], "orquestador.run debe generar un run_id si no viene"
print("✅ La salvaguarda de confirmación usa el run_id del turno, no el texto parafraseado.")

print("\n✅ Todos los tests de citas_repository/citas_tools/prompt pasaron.")
