"""
tests/test_ordenes_servicio.py

Fase 13: tools del Agente de Servicio Técnico (`tools/ordenes_servicio_tools.py`)
y el propio agente, sin red (Supabase, catálogo e info_empresa simulados).

1. Crear: valida el día ANTES de pedir confirmación, confirmación de dos
   turnos, éxito con horario y dirección.
2. Consultar: estado legible y novedades, sin el responsable (dato interno).
3. Modificar/cancelar: solo órdenes del cliente en PENDIENTE_RECEPCION; el día
   nuevo se valida igual que al crear.
4. El agente: prompt, nota de fecha con días hábiles, id del turno inyectado,
   y el orquestador reconoce la tool de cierre.

Corre con:
    python tests/test_ordenes_servicio.py
"""
import os
import sys
from datetime import date, datetime, time
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake-key")
os.environ.setdefault("OPENROUTER_API_KEY", "fake-key-para-el-mock")
os.environ.setdefault("OPENROUTER_MODELS", "modelo-simulado:free")

from tools import agenda_entregas, catalog_tools, info_empresa  # noqa: E402
from tools import ordenes_servicio_repository as repo  # noqa: E402
from tools import ordenes_servicio_tools as t  # noqa: E402
from tools.errores_negocio import ErrorNegocio  # noqa: E402
from tools.fechas import zona_horaria_configurada  # noqa: E402
from tools.info_empresa import TemaEmpresa  # noqa: E402

# Jueves 8 de octubre de 2026, 10:00 AM (el lunes 12 es festivo).
patch.object(agenda_entregas, "ahora", return_value=datetime(2026, 10, 8, 10, 0, tzinfo=zona_horaria_configurada())
             ).start()
patch.object(catalog_tools, "leer_servicio", return_value={"id": 3, "nombre": "Mantenimiento de computador"}).start()
patch.object(catalog_tools, "listar_catalogo", return_value=[{"id": 3, "nombre": "Mantenimiento de computador"},
                                                            {"id": 4, "nombre": "Reparación de pantalla"}]).start()
patch.object(info_empresa, "listar", return_value=(
    TemaEmpresa("horario", "Horario de atención", "Lunes a viernes: 8:15 a.m. a 12:00 p.m.", "siempre"),
    TemaEmpresa("ubicacion", "Dónde estamos", "Cartagena, Urb. La Gloria, Casa 1.", "siempre"),
)).start()

ORDEN = {
    "numero": 25, "session_id": "s1", "cliente_nombre": "Ana Pérez", "cliente_telefono": "3001112222",
    "servicio_id": 3, "equipo": "Portátil Lenovo", "descripcion": "No prende", "fecha_entrega": "2026-10-09",
    "hora_aproximada": "09:30:00", "estado": "PENDIENTE_RECEPCION",
}
DATOS = dict(servicio_id=3, cliente_nombre="Ana Pérez", cliente_telefono="3001112222", equipo="Portátil Lenovo",
             descripcion="No prende", fecha_entrega=date(2026, 10, 9), hora_aproximada=time(9, 30))

# ---------------------------------------------------------------------------
# 1) Crear
# ---------------------------------------------------------------------------
with patch.object(repo, "crear", return_value=ORDEN) as crear_mock:
    domingo = t.crear_orden_servicio(**{**DATOS, "fecha_entrega": date(2026, 10, 11)}, session_id="s1", id_turno="t1")
    assert "domingo" in domingo and "CONFIRMACION_PENDIENTE" not in domingo, "Nunca pide confirmar un día cerrado"
    sabado_tarde = t.crear_orden_servicio(**{**DATOS, "fecha_entrega": date(2026, 10, 10),
                                             "hora_aproximada": time(15, 0)}, session_id="s1", id_turno="t1")
    assert "no atiende" in sabado_tarde, sabado_tarde

    pendiente = t.crear_orden_servicio(**DATOS, session_id="s1", id_turno="t1")
    assert pendiente.startswith("CONFIRMACION_PENDIENTE"), pendiente
    assert "Viernes 9 de Octubre de 2026" in pendiente and "9:30 AM" in pendiente, pendiente
    reintento = t.crear_orden_servicio(**DATOS, session_id="s1", id_turno="t1")
    assert reintento.startswith("CONFIRMACION_PENDIENTE"), "Reintentar en el mismo turno no cuenta como confirmación"
    crear_mock.assert_not_called()

    ok = t.crear_orden_servicio(**DATOS, session_id="s1", id_turno="t2")
crear_mock.assert_called_once()
assert crear_mock.call_args.args[0] == "s1", "La orden se guarda con el session_id real de la conversación"
assert ok.startswith(t.PREFIJO_EXITO_CREAR), ok
assert "#25" in ok and "Cartagena, Urb. La Gloria" in ok and "8:15 a.m." in ok, "El éxito trae dirección y horario"
print("✅ Crear: valida el día antes de confirmar, confirmación de dos turnos y éxito con horario y dirección.")

with patch.object(repo, "crear", side_effect=ErrorNegocio("FECHA_PASADA")):
    t.crear_orden_servicio(**DATOS, session_id="s2", id_turno="a")
    error = t.crear_orden_servicio(**DATOS, session_id="s2", id_turno="b")
assert error.startswith("ERROR:") and "ya pasó" in error, error
with patch.object(repo, "crear", side_effect=ConnectionError("caído")):
    t.crear_orden_servicio(**DATOS, session_id="s3", id_turno="a")
    tecnico = t.crear_orden_servicio(**DATOS, session_id="s3", id_turno="b")
assert tecnico.startswith("ERROR:") and "inconveniente técnico" in tecnico, tecnico
print("✅ Crear: los errores de negocio y técnicos vuelven como texto, nunca como excepción.")

# ---------------------------------------------------------------------------
# 2) Consultar
# ---------------------------------------------------------------------------
NOTAS = [
    {"creado_en": "2026-10-09T15:00:00+00:00", "estado": "RECIBIDO", "nota": "Llegó con cargador",
     "responsable": "Laura"},
    {"creado_en": "2026-10-10T15:00:00+00:00", "estado": "EN_DIAGNOSTICO", "nota": "Falla en la fuente",
     "responsable": "Pedro"},
]
with (
    patch.object(repo, "listar_del_cliente", return_value=[{**ORDEN, "estado": "EN_DIAGNOSTICO"}]) as listar_mock,
    patch.object(repo, "seguimiento", return_value={25: NOTAS}),
):
    consulta = t.consultar_ordenes_servicio(session_id="s1")
listar_mock.assert_called_once_with("s1")
assert "En diagnóstico" in consulta and "Falla en la fuente" in consulta, consulta
assert "Mantenimiento de computador" in consulta, "Muestra el nombre del servicio, no solo el id"
assert "Pedro" not in consulta and "Laura" not in consulta, "El responsable de cada nota es interno"
with patch.object(repo, "listar_del_cliente", return_value=[]), patch.object(repo, "seguimiento", return_value={}):
    assert t.consultar_ordenes_servicio(session_id="s1").startswith("SIN_ORDENES")
with patch.object(repo, "leer_del_cliente", return_value=None), patch.object(repo, "seguimiento", return_value={}):
    assert "no existe" in t.consultar_ordenes_servicio(session_id="s1", numero=99)
print("✅ Consultar: estado legible, novedades sin el responsable y respuestas claras si no hay órdenes.")

# ---------------------------------------------------------------------------
# 3) Modificar y cancelar
# ---------------------------------------------------------------------------
with patch.object(repo, "leer_del_cliente", return_value={**ORDEN, "estado": "RECIBIDO"}), \
        patch.object(repo, "modificar") as modificar_mock, patch.object(repo, "cancelar") as cancelar_mock:
    recibido = t.modificar_orden_servicio(25, session_id="s1", id_turno="x", fecha_entrega=date(2026, 10, 13))
    assert "ya está en manos de la empresa" in recibido and "Recibido en la sede" in recibido, recibido
    assert "ya está en manos" in t.cancelar_orden_servicio(25, session_id="s1", id_turno="x")
modificar_mock.assert_not_called()
cancelar_mock.assert_not_called()

with patch.object(repo, "leer_del_cliente", return_value=ORDEN), \
        patch.object(repo, "modificar", return_value={**ORDEN, "fecha_entrega": "2026-10-13"}) as modificar_mock:
    festivo = t.modificar_orden_servicio(25, session_id="s1", id_turno="m1", fecha_entrega=date(2026, 10, 12))
    assert "festivo" in festivo, "El día nuevo se valida igual que al crear"
    pendiente = t.modificar_orden_servicio(25, session_id="s1", id_turno="m1", fecha_entrega=date(2026, 10, 13))
    assert "CONFIRMACION_PENDIENTE" in pendiente and "Viernes 9 de Octubre de 2026 -> Martes 13 de Octubre" in \
        pendiente, pendiente
    ok = t.modificar_orden_servicio(25, session_id="s1", id_turno="m2", fecha_entrega=date(2026, 10, 13))
assert ok.startswith("OK: ORDEN DE SERVICIO ACTUALIZADA"), ok
assert modificar_mock.call_args.args[:3] == ("s1", 25, date(2026, 10, 13)), modificar_mock.call_args
print("✅ Modificar: solo antes de traer el equipo, el día nuevo se valida y se confirma Anterior -> Nuevo.")

tarde = {**ORDEN, "hora_aproximada": "15:00:00"}
with patch.object(repo, "leer_del_cliente", return_value=tarde):
    r = t.modificar_orden_servicio(25, session_id="s1", id_turno="h", fecha_entrega=date(2026, 10, 10))
assert "no atiende" in r, "Mover al sábado con una hora de la tarde: se valida la combinación día + hora"
print("✅ Modificar: al cambiar solo el día, se valida con la hora que ya tenía la orden.")

with patch.object(repo, "leer_del_cliente", return_value=ORDEN), patch.object(repo, "cancelar") as cancelar_mock:
    assert t.cancelar_orden_servicio(25, session_id="s1", id_turno="c1").startswith("CONFIRMACION_PENDIENTE")
    cancelar_mock.assert_not_called()
    assert "CANCELADA" in t.cancelar_orden_servicio(25, session_id="s1", id_turno="c2")
cancelar_mock.assert_called_once_with("s1", 25)
with patch.object(repo, "leer_del_cliente", return_value=None):
    assert "no existe" in t.cancelar_orden_servicio(99, session_id="otro", id_turno="c")
print("✅ Cancelar: confirmación de dos turnos y solo órdenes del propio cliente.")

# ---------------------------------------------------------------------------
# 4) El agente y el orquestador
# ---------------------------------------------------------------------------
from agents import orquestador, servicio_tecnico_agent as agente  # noqa: E402

for nota in (agente.NOTA_CONFIRMACION_OBLIGATORIA, agente.NOTA_ESTADO_DEL_EQUIPO):
    assert nota in agente.SYSTEM_PROMPT
assert agente.SYSTEM_PROMPT.startswith(agente.ORIGINAL_SYSTEM_PROMPT)
assert agente.SYSTEM_PROMPT.endswith(agente.NOTA_NATURALIDAD_CONVERSACION), "La nota de naturalidad va al final"
for viejo in ("Consultar_eventos", "Crear_evento", "google_calendar_event_id", "7:00 AM"):
    assert viejo not in agente.SYSTEM_PROMPT, f"El prompt no debe mencionar el modelo viejo: {viejo}"

nota_fecha = agente._nota_fecha_actual()
assert "2026-10-08" in nota_fecha and "Martes 13 de Octubre de 2026" in nota_fecha, nota_fecha
assert "2026-10-11" not in nota_fecha and "2026-10-12" not in nota_fecha, "Sin domingos ni festivos"
print("✅ Prompt del agente sin rastros del modelo de citas, y nota de fecha con los próximos días hábiles.")

ids = []
with patch.object(info_empresa, "nota_temas", return_value="\nDATOS DE LA SEDE: Cartagena"):
    for run_id in ("turno-1", "turno-2"):
        with patch.object(agente, "run_agent_loop", return_value=("ok", [])) as loop_mock:
            agente.run(mensaje_cliente="Sí, confirmo.", session_id="s", run_id=run_id)
        ids.append(loop_mock.call_args.kwargs["tool_functions"]["Crear_orden_servicio"]
                   .funcion_original.keywords["id_turno"])
        assert "DATOS DE LA SEDE: Cartagena" in loop_mock.call_args.kwargs["system_prompt"]
assert ids == ["turno-1", "turno-2"], "El id_turno inyectado es el run_id de cada turno"
print("✅ El agente inyecta el run_id del turno y los datos de la sede en cada llamada.")

st = next(s for s in orquestador.SUBAGENTES if s.clave_historial == "servicio_tecnico")
nombres = {tool["function"]["name"] for tool in agente.TOOLS_SCHEMA}
assert st.tool_de_cierre in nombres and st.prefijo_de_exito == t.PREFIJO_EXITO_CREAR
print("✅ El orquestador reconoce la orden de servicio registrada (venta cruzada).")

print("\n✅ Todos los tests de órdenes de servicio pasaron.")
