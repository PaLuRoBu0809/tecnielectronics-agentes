"""
tests/test_info_empresa.py

Conocimiento de la empresa para el orquestador (`tools/info_empresa.py`):
la nota del prompt con los temas de uso 'siempre', la tool {Info_empresa}
para los de uso 'bajo_demanda', y que el agente siga funcionando si la tabla
no se puede leer. La tabla y sus reglas se prueban contra Postgres en
`tests/test_integracion_postgres.py`.

Corre con:
    python tests/test_info_empresa.py
"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake-key")
os.environ.setdefault("OPENROUTER_API_KEY", "fake-key-para-el-mock")
os.environ.setdefault("OPENROUTER_MODELS", "modelo-simulado:free")
os.environ["LOG_EVENTOS_JSON"] = "0"

from agents import orquestador  # noqa: E402
from tools import info_empresa, supabase_client  # noqa: E402

FILAS = [
    {"tema": "contacto", "titulo": "Contacto y atención humana", "contenido": "Línea: 301 208 6262",
     "uso": "siempre"},
    {"tema": "redes", "titulo": "Redes sociales", "contenido": "Instagram: https://instagram.com/x", "uso": "siempre"},
    {"tema": "quienes_somos", "titulo": "Quiénes somos", "contenido": "Muebles y equipos de oficina...",
     "uso": "bajo_demanda"},
    {"tema": "privacidad", "titulo": "Política de privacidad", "contenido": "No compartimos datos...",
     "uso": "bajo_demanda"},
]


def _con_tabla(filas):
    info_empresa._temas.invalidar()
    return patch.object(supabase_client, "get_rows", return_value=filas)


# ---------------------------------------------------------------------------
# 1) Nota del prompt: solo los temas 'siempre', más la lista de consultables.
# ---------------------------------------------------------------------------
with _con_tabla(FILAS) as get_mock:
    nota = info_empresa.nota_para_prompt()
    info_empresa.nota_para_prompt()
assert get_mock.call_count == 1, "La tabla se lee una vez y se guarda en caché"
assert "- Contacto y atención humana: Línea: 301 208 6262" in nota
assert "Instagram: https://instagram.com/x" in nota
assert "Muebles y equipos" not in nota, "Los textos 'bajo_demanda' no van en cada mensaje"
assert "quienes_somos (Quiénes somos), privacidad (Política de privacidad)" in nota, (
    "La nota dice qué temas se pueden consultar con Info_empresa"
)

with _con_tabla([]):
    assert info_empresa.nota_para_prompt() == ""
info_empresa._temas.invalidar()
with patch.object(supabase_client, "get_rows", side_effect=ConnectionError("Supabase caído")):
    assert info_empresa.nota_para_prompt() == "", "Si la tabla no se puede leer, el agente sigue sin la nota"
print("✅ Nota del prompt: datos 'siempre' + lista de temas consultables; vacía si la tabla no responde.")

# ---------------------------------------------------------------------------
# 2) Tool Info_empresa.
# ---------------------------------------------------------------------------
with _con_tabla(FILAS):
    assert info_empresa.info_empresa("quienes_somos") == "QUIÉNES SOMOS:\nMuebles y equipos de oficina..."
    assert info_empresa.info_empresa("  Privacidad ") == "POLÍTICA DE PRIVACIDAD:\nNo compartimos datos..."
    r = info_empresa.info_empresa("historia")
assert r.startswith("TEMA_NO_ENCONTRADO") and "contacto, redes, quienes_somos, privacidad" in r
assert "Nunca lo inventes" in r
info_empresa._temas.invalidar()
with patch.object(supabase_client, "get_rows", side_effect=ConnectionError("Supabase caído")):
    r = info_empresa.info_empresa("quienes_somos")
assert r.startswith("ERROR:") and "línea de contacto" in r
print("✅ Info_empresa: devuelve el tema, avisa si no existe (sin inventar) y no lanza si la tabla falla.")

# ---------------------------------------------------------------------------
# 3) El orquestador: datos en su prompt y la tool disponible (no terminal).
# ---------------------------------------------------------------------------
with (
    _con_tabla(FILAS),
    patch.object(orquestador, "run_agent_loop", return_value=("Estamos en Cartagena.", [])) as loop_mock,
):
    orquestador.run("¿dónde están ubicados?", session_id="3001", historiales={})
kwargs = loop_mock.call_args.kwargs
assert kwargs["system_prompt"].startswith(orquestador.SYSTEM_PROMPT)
assert "Línea: 301 208 6262" in kwargs["system_prompt"], "Los datos 'siempre' llegan al prompt del orquestador"
assert kwargs["tool_functions"]["Info_empresa"] is info_empresa.info_empresa
assert "Info_empresa" not in kwargs["tools_terminales"], (
    "Info_empresa no es terminal: el orquestador redacta la respuesta con lo que devuelve"
)
assert {"Agente_Servicio_Tecnico", "Agente_Ventas"} <= kwargs["tools_terminales"]
assert "Info_empresa" in {t["function"]["name"] for t in kwargs["tools_schema"]}
print("✅ Orquestador: recibe los datos de la empresa en su prompt y tiene Info_empresa (no terminal).")

print("\n✅ Todos los tests de información de la empresa pasaron.")
