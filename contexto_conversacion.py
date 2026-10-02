"""
contexto_conversacion.py

Compactación del contexto de conversación (Fase 6 de
`docs/PLAN_DE_MEJORAS.md`). Antes, el historial de cada agente crecía sin
límite: se guardaba completo en Supabase y se mandaba COMPLETO al modelo en
cada llamada (costo de tokens creciente, y con el tiempo, errores por
superar la ventana de contexto del modelo).

Tres piezas, todas en código:

1. `limitar_historial_guardado` (al GUARDAR, `sesiones.py`):
   - recorta el contenido de resultados de tools ANTIGUOS y largos (el
     catálogo, la disponibilidad...), pero conserva SIEMPRE el resultado
     más reciente de cada tool — así no se pierde, por ejemplo, el catálogo
     con el servicio_id y la duración en medio de un agendamiento;
   - pone un tope de mensajes almacenados.

2. `aplicar_ventana` (al LLAMAR al modelo, en cada agente): solo se envían
   los últimos N turnos del cliente. La ventana siempre empieza en un
   mensaje `user`, así nunca separa una llamada a tools de sus resultados.
   Lo que queda fuera de la ventana se conserva en el historial guardado.

3. `construir_ficha` + `nota_ficha`: si la ventana dejó mensajes fuera, se
   arma una ficha (nombre, teléfono, servicio_id, descripción) a partir de
   los ARGUMENTOS de las tools que ya se ejecutaron con esos datos — nunca
   con un LLM resumiendo, para no reabrir la regla de "cero alucinación".
   Es la única parte que cambia lo que lee el modelo [PROMPT], y solo en
   conversaciones largas: en una normal no se inyecta nada.
"""
from __future__ import annotations

import json
import os
from typing import Optional


def _entero_env(nombre: str, por_defecto: int) -> int:
    try:
        valor = int(os.environ.get(nombre, por_defecto))
        return valor if valor > 0 else por_defecto
    except ValueError:
        return por_defecto


def max_turnos_ventana() -> int:
    """Turnos del cliente (mensajes `user`) que se envían al modelo."""
    return _entero_env("CONTEXTO_MAX_TURNOS", 12)


def _indices_user(mensajes: list) -> list:
    return [i for i, m in enumerate(mensajes) if m.get("role") == "user"]


# ---------------------------------------------------------------------------
# 1. Al guardar
# ---------------------------------------------------------------------------

def _nombres_de_tool_calls(mensajes: list) -> dict:
    """{tool_call_id: nombre_de_la_tool}, a partir de los mensajes assistant."""
    nombres = {}
    for m in mensajes:
        for tc in m.get("tool_calls") or []:
            nombres[tc.get("id")] = (tc.get("function") or {}).get("name")
    return nombres


def limitar_historial_guardado(
    mensajes: list,
    turnos_intactos: Optional[int] = None,
    max_caracteres_tool: Optional[int] = None,
    max_mensajes: Optional[int] = None,
) -> list:
    """Copia de `mensajes` lista para guardar. Espera un historial ya saneado
    (`llm_loop.sanear_historial`)."""
    turnos_intactos = turnos_intactos or _entero_env("CONTEXTO_TURNOS_INTACTOS", 4)
    max_caracteres_tool = max_caracteres_tool or _entero_env("CONTEXTO_MAX_CARACTERES_TOOL", 1500)
    max_mensajes = max_mensajes or _entero_env("CONTEXTO_MAX_MENSAJES_GUARDADOS", 300)

    resultado = [dict(m) for m in mensajes]

    # Tope de mensajes: se descartan los más antiguos, cortando en un `user`.
    if len(resultado) > max_mensajes:
        corte = next((i for i in _indices_user(resultado) if len(resultado) - i <= max_mensajes), None)
        resultado = resultado[corte:] if corte is not None else resultado[-max_mensajes:]

    # Recorte de resultados de tools antiguos.
    users = _indices_user(resultado)
    inicio_intacto = users[-turnos_intactos] if len(users) >= turnos_intactos else 0
    nombres = _nombres_de_tool_calls(resultado)
    ultimo_por_tool = {}
    for i, m in enumerate(resultado):
        if m.get("role") == "tool":
            ultimo_por_tool[nombres.get(m.get("tool_call_id"))] = i
    conservar = set(ultimo_por_tool.values())

    for i, m in enumerate(resultado[:inicio_intacto]):
        contenido = str(m.get("content") or "")
        if m.get("role") == "tool" and i not in conservar and len(contenido) > max_caracteres_tool:
            m["content"] = (
                contenido[:300]
                + f"... [resultado antiguo recortado: {len(contenido)} caracteres en total; "
                "si lo necesitas, vuelve a llamar la herramienta]"
            )
    return resultado


# ---------------------------------------------------------------------------
# 2. Al llamar al modelo
# ---------------------------------------------------------------------------

def aplicar_ventana(mensajes: list, max_turnos: Optional[int] = None) -> tuple:
    """Divide `mensajes` en `(descartados, ventana)`: la ventana son los
    últimos `max_turnos` turnos del cliente (empieza en un `user`) y
    `descartados + ventana == mensajes`."""
    max_turnos = max_turnos or max_turnos_ventana()
    users = _indices_user(mensajes)
    if len(users) <= max_turnos:
        return [], list(mensajes)
    corte = users[-max_turnos]
    return list(mensajes[:corte]), list(mensajes[corte:])


# ---------------------------------------------------------------------------
# 3. Ficha para lo que quedó fuera de la ventana
# ---------------------------------------------------------------------------

def construir_ficha(mensajes: list, campos_por_tool: dict) -> dict:
    """Datos que el cliente ya dio, extraídos de los argumentos de las tools
    listadas en `campos_por_tool` ({nombre_tool: (campo, ...)}). Si un campo
    aparece varias veces, gana el más reciente (ej. un teléfono corregido)."""
    ficha: dict = {}
    for m in mensajes:
        for tc in m.get("tool_calls") or []:
            funcion = tc.get("function") or {}
            campos = campos_por_tool.get(funcion.get("name"))
            if not campos:
                continue
            try:
                args = json.loads(funcion.get("arguments") or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(args, dict):
                continue
            for campo in campos:
                valor = args.get(campo)
                if valor not in (None, ""):
                    ficha[campo] = str(valor)
    return ficha


_ETIQUETAS = {
    "cliente_nombre": "Nombre del cliente",
    "cliente_telefono": "Teléfono de contacto",
    "servicio_id": "servicio_id del servicio identificado",
    "descripcion": "Descripción del problema",
    "customer_name": "Nombre completo del cliente",
    "customer_phone": "Teléfono del cliente",
    "customer_address": "Dirección de entrega",
    "city": "Ciudad de entrega",
    "metodo_pago": "Método de pago elegido",
}

# Lo que la ficha NO reemplaza: el estado real siempre se vuelve a consultar.
RECORDATORIO_CITAS = (
    "Para cualquier cita existente, consulta siempre {Consultar_servicio_agendado}: esta nota no reemplaza "
    "esa consulta."
)


def nota_ficha(ficha: dict, recordatorio: str = RECORDATORIO_CITAS) -> str:
    """Nota para agregar al system prompt. Vacía si no hay ficha.
    `recordatorio`: qué debe volver a consultar SIEMPRE ese agente (la ficha
    son datos que dio el cliente, nunca el estado actual de nada)."""
    if not ficha:
        return ""
    lineas = "\n".join(f"- {_ETIQUETAS.get(campo, campo)}: {valor}" for campo, valor in ficha.items())
    return (
        "\n\n---\n"
        "NOTA DE CONTEXTO ANTERIOR (generada automáticamente por el sistema): esta "
        "conversación es larga y sus mensajes más antiguos ya no se muestran. En esa "
        "parte el cliente ya proporcionó estos datos (tomados de las herramientas que "
        "se ejecutaron con ellos):\n"
        f"{lineas}\n"
        "No se los vuelvas a pedir si siguen vigentes; si el cliente los cambia, usa "
        f"los nuevos. {recordatorio}"
    )
