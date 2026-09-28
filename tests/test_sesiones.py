"""
tests/test_sesiones.py

Valida, sin tocar Supabase real, que `AlmacenSesiones` (sesiones.py) lea y
guarde la memoria conversacional en la tabla `conversaciones` en vez de en
la RAM del proceso. Mismo estilo que los demás tests del repo: un script con
asserts que corre gratis y rápido.

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
# 1) Una sesión que nunca ha escrito arranca con ambos historiales vacíos.
# ---------------------------------------------------------------------------
with patch.object(supabase_client, "get_rows", return_value=[]):
    sesion_nueva = almacen.obtener("3000000000")

assert sesion_nueva == {"orq": [], "st": []}, "Una sesión nueva debe arrancar con historiales vacíos"
print("✅ Una sesión nueva arranca con ambos historiales vacíos.")

# ---------------------------------------------------------------------------
# 2) Una sesión existente recupera lo que se guardó antes — la memoria ya no
#    depende de que el proceso siga vivo.
# ---------------------------------------------------------------------------
fila_guardada = {
    "historial_orquestador": [{"role": "user", "content": "hola"}],
    "historial_servicio_tecnico": [{"role": "user", "content": "se dañó el mouse"}],
}
with patch.object(supabase_client, "get_rows", return_value=[fila_guardada]) as get_rows_mock:
    sesion_existente = almacen.obtener("3177510761")

assert sesion_existente["orq"] == fila_guardada["historial_orquestador"], "Debe recuperar el historial del orquestador"
assert sesion_existente["st"] == fila_guardada["historial_servicio_tecnico"], "Debe recuperar el historial del subagente"
assert get_rows_mock.call_args.kwargs["params"]["session_id"] == "eq.3177510761", "Debe filtrar por session_id"
print("✅ Una sesión existente recupera su historial guardado, filtrando por session_id.")

# ---------------------------------------------------------------------------
# 3) guardar() hace upsert por session_id con ambos historiales.
# ---------------------------------------------------------------------------
with patch.object(supabase_client, "upsert_row", return_value={}) as upsert_mock:
    almacen.guardar("3177510761", [{"role": "user", "content": "a"}], [{"role": "user", "content": "b"}])

payload = upsert_mock.call_args.kwargs["payload"]
assert upsert_mock.call_args.kwargs["on_conflict"] == "session_id", "El upsert debe resolverse por session_id"
assert payload["session_id"] == "3177510761", "Debe guardar bajo el session_id correcto"
assert payload["historial_orquestador"] == [{"role": "user", "content": "a"}], "Debe guardar el historial del orquestador"
assert payload["historial_servicio_tecnico"] == [{"role": "user", "content": "b"}], "Debe guardar el historial del subagente"
print("✅ guardar() hace upsert por session_id con ambos historiales.")

print("\n✅ Todos los tests de sesiones pasaron.")
