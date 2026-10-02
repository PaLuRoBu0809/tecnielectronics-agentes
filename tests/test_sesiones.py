"""
tests/test_sesiones.py

Valida, sin tocar Supabase real, que `AlmacenSesiones` (sesiones.py) lea y
guarde la memoria conversacional en la tabla `conversaciones` en vez de en
la RAM del proceso. Mismo estilo que los demás tests del repo: un script con
asserts que corre gratis y rápido.

Desde la Fase 11 (`docs/PLAN_DE_MEJORAS.md`) la memoria es un diccionario
`{clave_agente: [mensajes]}` en la columna `historiales`, en vez de una
columna por agente.

Corre con:
    python tests/test_sesiones.py
"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake-key")

from sesiones import AlmacenSesiones  # noqa: E402
from tools import supabase_client  # noqa: E402

almacen = AlmacenSesiones()

# ---------------------------------------------------------------------------
# 1) Una sesión que nunca ha escrito arranca sin historiales.
# ---------------------------------------------------------------------------
with patch.object(supabase_client, "get_rows", return_value=[]):
    sesion_nueva = almacen.obtener("3000000000")

assert sesion_nueva == {}, "Una sesión nueva debe arrancar sin historiales"
print("✅ Una sesión nueva arranca sin historiales.")

# ---------------------------------------------------------------------------
# 2) Una sesión existente recupera lo que se guardó antes, para cualquier
#    cantidad de agentes (la clave "ventas" no requiere código nuevo).
# ---------------------------------------------------------------------------
fila_guardada = {
    "session_id": "3177510761",
    "historiales": {
        "orquestador": [{"role": "user", "content": "hola"}],
        "servicio_tecnico": [{"role": "user", "content": "se dañó el mouse"}],
        "ventas": [{"role": "user", "content": "quiero un teclado"}],
    },
}
with patch.object(supabase_client, "get_rows", return_value=[fila_guardada]) as get_rows_mock:
    sesion_existente = almacen.obtener("3177510761")

assert sesion_existente == fila_guardada["historiales"], "Debe recuperar el historial de cada agente"
assert get_rows_mock.call_args.kwargs["params"]["session_id"] == "eq.3177510761", "Debe filtrar por session_id"
print("✅ Una sesión existente recupera el historial de cada agente, filtrando por session_id.")

# ---------------------------------------------------------------------------
# 3) Fila todavía no migrada (columna `historiales` vacía): se lee desde las
#    columnas viejas, sin perder la conversación.
# ---------------------------------------------------------------------------
fila_vieja = {
    "session_id": "3001",
    "historiales": {},
    "historial_orquestador": [{"role": "user", "content": "a"}],
    "historial_servicio_tecnico": [{"role": "user", "content": "b"}],
}
with patch.object(supabase_client, "get_rows", return_value=[fila_vieja]):
    sesion_vieja = almacen.obtener("3001")
assert sesion_vieja == {
    "orquestador": [{"role": "user", "content": "a"}],
    "servicio_tecnico": [{"role": "user", "content": "b"}],
}, sesion_vieja
print("✅ Filas con el formato anterior se leen desde las columnas viejas (compatibilidad).")

# ---------------------------------------------------------------------------
# 4) guardar() hace upsert por session_id con el diccionario de historiales.
# ---------------------------------------------------------------------------
historiales = {
    "orquestador": [{"role": "user", "content": "a"}],
    "servicio_tecnico": [{"role": "user", "content": "b"}],
}
with patch.object(supabase_client, "upsert_row", return_value={}) as upsert_mock:
    almacen.guardar("3177510761", historiales)

payload = upsert_mock.call_args.kwargs["payload"]
assert upsert_mock.call_args.kwargs["on_conflict"] == "session_id", "El upsert debe resolverse por session_id"
assert payload["session_id"] == "3177510761", "Debe guardar bajo el session_id correcto"
assert payload["historiales"] == historiales, "Debe guardar el historial de cada agente"
assert "historial_orquestador" not in payload, "Las columnas viejas ya no se escriben"
print("✅ guardar() hace upsert por session_id con el historial de cada agente.")

print("\n✅ Todos los tests de sesiones pasaron.")
