"""
tests/test_config.py

Fase 9.1 del plan de mejoras (`docs/PLAN_DE_MEJORAS.md`): la configuración
se valida al arrancar y reporta TODOS los problemas juntos.

Corre con:
    python tests/test_config.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config import ErrorConfiguracion, problemas_de_configuracion, validar_configuracion  # noqa: E402

VALIDO = {
    "OPENROUTER_API_KEY": "sk-or-v1-real",
    "OPENROUTER_MODELS": "a:free, b:free",
    "SUPABASE_URL": "https://abc.supabase.co",
    "SUPABASE_SERVICE_KEY": "eyJreal",
    "TIMEZONE_OFFSET": "-05:00",
    "API_KEY": "clave",
}

assert problemas_de_configuracion(VALIDO) == []
validar_configuracion(VALIDO)  # no lanza
print("✅ Una configuración completa no reporta problemas.")

# Vacía: reporta las 4 obligatorias de una sola vez (no una por arranque).
problemas = problemas_de_configuracion({})
assert len(problemas) == 4, problemas
print("✅ Sin configuración, reporta las 4 variables obligatorias juntas.")

# Los valores de ejemplo de .env.example cuentan como "no configurado".
problemas = problemas_de_configuracion(
    {**VALIDO, "OPENROUTER_API_KEY": "sk-or-v1-xxxxxxxx", "SUPABASE_SERVICE_KEY": "PEGA_AQUI_TU_SERVICE_ROLE_KEY"}
)
assert any("OPENROUTER_API_KEY" in p for p in problemas) and any("SUPABASE_SERVICE_KEY" in p for p in problemas)
print("✅ Los marcadores de .env.example se detectan como variables sin configurar.")

casos = {
    "SUPABASE_URL": ("http://abc.supabase.co", "https://"),
    "OPENROUTER_MODELS": (" , ", "ningún modelo"),
    "TIMEZONE_OFFSET": ("-5", "±HH:MM"),
    "MAX_LLAMADAS_LLM_POR_TURNO": ("muchas", "número positivo"),
    "SEGUNDOS_MAX_POR_TURNO": ("0", "número positivo"),
}
for variable, (valor, esperado) in casos.items():
    problemas = problemas_de_configuracion({**VALIDO, variable: valor})
    assert any(esperado in p for p in problemas), f"{variable}={valor!r} debía reportar '{esperado}': {problemas}"
print("✅ Formatos inválidos (URL, modelos, zona horaria, números) se reportan con un mensaje claro.")

try:
    validar_configuracion({})
    raise AssertionError("validar_configuracion debía lanzar ErrorConfiguracion")
except ErrorConfiguracion as exc:
    assert "OPENROUTER_API_KEY" in str(exc) and ".env.example" in str(exc)
print("✅ validar_configuracion lanza ErrorConfiguracion con todos los problemas y una pista.")

print("\n✅ Todos los tests de configuración pasaron.")
