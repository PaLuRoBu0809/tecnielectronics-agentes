"""
llm_loop.py — loop genérico de tool use, model-agnostic, con fallback entre
varios modelos de OpenRouter si uno falla o se limita.

Este módulo es el reemplazo directo del nodo "AI Agent" de n8n: recibe un
system prompt, una lista de tools (formato OpenAI function-calling) y un
mapa de nombre_tool -> función Python que la ejecuta, y devuelve la
respuesta final del modelo ya resuelta (después de ejecutar todas las tool
calls necesarias). Tanto el orquestador como el agente de servicio técnico
usan ESTE mismo loop — lo único que cambia entre ellos es el prompt, las
tools y qué funciones Python hay detrás.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Callable, Optional

from openai import OpenAI, APIError, RateLimitError

logger = logging.getLogger(__name__)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Tope de seguridad: nunca dejar que el loop encadene tool calls infinitamente
# si el modelo entra en un ciclo raro (ej. llama la misma tool una y otra vez).
MAX_TOOL_ITERATIONS = 8


def _client() -> OpenAI:
    api_key = os.environ["OPENROUTER_API_KEY"]
    return OpenAI(base_url=OPENROUTER_BASE_URL, api_key=api_key)


def _models_from_env() -> list:
    raw = os.environ.get("OPENROUTER_MODELS", "")
    modelos = [m.strip() for m in raw.split(",") if m.strip()]
    if not modelos:
        raise RuntimeError("OPENROUTER_MODELS está vacío en el .env")
    return modelos


def run_agent_loop(
    system_prompt: str,
    messages: list,
    tools_schema: list,
    tool_functions: dict,
    models: Optional[list] = None,
):
    """
    Ejecuta el loop completo de tool use para UN agente.

    Parámetros
    ----------
    system_prompt : el prompt de sistema de ese agente, tal cual (no se
        cachea aquí porque OpenRouter/los modelos :free no soportan prompt
        caching de todas formas — ver la nota en README.md).
    messages : historial de la conversación de ESE agente, SIN el mensaje
        de system (este se antepone aquí en cada llamada). Se pasa y se
        devuelve actualizado, para que quien invoque lo guarde en la sesión.
    tools_schema : lista de tools en formato OpenAI function-calling
        (ver agents/*.py para los schemas exactos de cada agente).
    tool_functions : dict {nombre_tool: función Python}. Cada función recibe
        los argumentos que decidió el modelo como **kwargs y devuelve un
        string (el "resumen" que en n8n devolvía cada subflujo).
    models : lista de modelos a intentar en orden (fallback). Si es None,
        se lee de OPENROUTER_MODELS en el .env.

    Devuelve
    --------
    (texto_final_para_el_cliente, historial_actualizado_sin_system)
    """
    client = _client()
    modelos = models or _models_from_env()

    full_messages = [{"role": "system", "content": system_prompt}] + messages

    for _ in range(MAX_TOOL_ITERATIONS):
        try:
            response = _call_with_fallback(client, modelos, full_messages, tools_schema)
        except RuntimeError as exc:
            # Ningún modelo del fallback respondió (API key inválida, todos
            # los modelos :free saturados, etc.). Antes esto tumbaba la
            # petición completa con un error sin manejar (500 en la interfaz
            # de chat, excepción sin capturar en la consola) — se convierte
            # en una respuesta final amable, igual que el caso de abajo
            # cuando se agotan los MAX_TOOL_ITERATIONS.
            logger.exception("Todos los modelos de OpenRouter fallaron: %s", exc)
            return (
                "Estoy teniendo dificultades técnicas para responder en este momento "
                "(no se pudo contactar ningún modelo configurado). Por favor intenta de "
                "nuevo en unos minutos.",
                full_messages[1:],
            )
        choice = response.choices[0]
        msg = choice.message

        # Respuesta final en texto plano, sin más tool calls -> terminamos.
        if not msg.tool_calls:
            full_messages.append({"role": "assistant", "content": msg.content or ""})
            return msg.content or "", full_messages[1:]

        # El modelo pidió una o más tool calls -> ejecutarlas y reinyectar
        # el resultado como mensajes "tool" antes de volver a llamarlo.
        full_messages.append(
            {
                "role": "assistant",
                "content": msg.content,
                "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
            }
        )

        for tool_call in msg.tool_calls:
            nombre = tool_call.function.name
            try:
                args = json.loads(tool_call.function.arguments or "{}")
            except json.JSONDecodeError:
                resultado = f"ERROR: el modelo envió argumentos inválidos (JSON malformado) para {nombre}"
            else:
                funcion = tool_functions.get(nombre)
                if funcion is None:
                    resultado = f"ERROR: la tool '{nombre}' no existe en este agente"
                else:
                    try:
                        resultado = funcion(**args)
                    except Exception as exc:  # nunca dejar caer el loop completo por un error de tool
                        logger.exception("Fallo ejecutando tool %s con args %s", nombre, args)
                        resultado = f"ERROR interno ejecutando {nombre}: {exc}"

            full_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": str(resultado),
                }
            )

    logger.warning("Loop de tool use agotó %s iteraciones sin respuesta final", MAX_TOOL_ITERATIONS)
    return (
        "Estoy teniendo dificultades para procesar tu solicitud en este momento, "
        "¿podrías reformularla o intentarlo de nuevo en un momento?",
        full_messages[1:],
    )


def _call_with_fallback(client: OpenAI, modelos: list, messages: list, tools_schema: list):
    """Intenta cada modelo de la lista en orden; salta al siguiente si uno
    falla (rate limit, modelo caído, error de la API). Esto es lo que
    reemplaza tener que configurar el fallback manualmente en n8n.

    Bug real detectado en pruebas: bajo el pool compartido de modelos
    ":free" de OpenRouter, un modelo saturado a veces responde 200 OK con un
    cuerpo vacío/roto (`choices` en None) en vez de lanzar un error HTTP.
    Antes eso no se detectaba aquí y tumbaba el proceso más adelante con un
    `TypeError` al indexar `response.choices[0]`. Ahora se trata igual que
    cualquier otra falla del modelo: se descarta y se prueba el siguiente."""
    ultimo_error = None
    for modelo in modelos:
        try:
            response = client.chat.completions.create(
                model=modelo,
                messages=messages,
                tools=tools_schema,
                tool_choice="auto",
            )
        except (RateLimitError, APIError) as exc:
            logger.warning("Modelo %s falló (%s) — probando el siguiente del fallback", modelo, exc)
            ultimo_error = exc
            continue
        if not response.choices:
            logger.warning(
                "Modelo %s respondió 200 OK sin 'choices' (%s) — probando el siguiente del fallback",
                modelo,
                response,
            )
            ultimo_error = RuntimeError(f"Respuesta sin 'choices' del modelo {modelo}: {response}")
            continue
        return response
    raise RuntimeError(f"Todos los modelos del fallback fallaron. Último error: {ultimo_error}")
