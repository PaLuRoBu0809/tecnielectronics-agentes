"""
tests/test_llm_loop.py

Valida que llm_loop.py ejecuta correctamente el ciclo
tool_use -> ejecutar función Python -> reinyectar resultado -> respuesta
final, simulando al modelo con respuestas "canned" (sin llamar a OpenRouter
de verdad, así corre gratis y rápido en cualquier máquina). Correr con:

    python tests/test_llm_loop.py

Es la primera línea de defensa antes de probar contra un modelo real: si
esto falla, el problema está en el loop genérico, no en un prompt ni en un
modelo específico.
"""
import os
import sys
import json
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("OPENROUTER_API_KEY", "fake-key-para-el-mock")
os.environ.setdefault("OPENROUTER_MODELS", "modelo-simulado:free")

from llm_loop import run_agent_loop  # noqa: E402


def _fake_tool_call(call_id, nombre, args_dict):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=nombre, arguments=json.dumps(args_dict)),
        model_dump=lambda: {
            "id": call_id,
            "type": "function",
            "function": {"name": nombre, "arguments": json.dumps(args_dict)},
        },
    )


def _fake_response(content=None, tool_calls=None):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


llamadas = {"n": 0}


def fake_create(*args, **kwargs):
    llamadas["n"] += 1
    if llamadas["n"] == 1:
        # Primer turno: el modelo decide llamar a la tool "saludar"
        return _fake_response(
            content=None,
            tool_calls=[_fake_tool_call("call_1", "saludar", {"nombre": "Isaac"})],
        )
    # Segundo turno: ya con el resultado de la tool, responde en texto plano
    return _fake_response(content="¡Hola Isaac! Este es el mensaje final tras usar la tool.")


def tool_saludar(nombre: str) -> str:
    return f"Resultado de la tool: hola {nombre}, tool ejecutada con éxito."


with patch("llm_loop.OpenAI") as MockOpenAI:
    instancia = MockOpenAI.return_value
    instancia.chat.completions.create.side_effect = fake_create

    texto, historial = run_agent_loop(
        system_prompt="Eres un agente de prueba.",
        messages=[{"role": "user", "content": "hola"}],
        tools_schema=[
            {
                "type": "function",
                "function": {
                    "name": "saludar",
                    "description": "saluda a alguien",
                    "parameters": {
                        "type": "object",
                        "properties": {"nombre": {"type": "string"}},
                        "required": ["nombre"],
                    },
                },
            }
        ],
        tool_functions={"saludar": tool_saludar},
    )

print("Respuesta final del loop:", texto)
print("Número de llamadas al modelo simulado:", llamadas["n"])
print("Mensajes en el historial devuelto:", len(historial))

assert "Hola Isaac" in texto, "El loop no devolvió el texto final esperado"
assert llamadas["n"] == 2, "El loop no hizo exactamente 2 llamadas (tool_use + respuesta final)"
assert any(m.get("role") == "tool" for m in historial), "El resultado de la tool no quedó en el historial"
print(
    "\n✅ El loop de tool use funciona correctamente: detecta tool_calls, ejecuta la función Python, "
    "reinyecta el resultado y termina en una respuesta final en texto plano."
)
