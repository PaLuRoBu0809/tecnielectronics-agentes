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

Instrumentación para el panel "Flujo en Vivo" (`web/static/dashboard.html` +
`flujo.js`): en cada llamada a un modelo y en cada ejecución de tool, este
módulo publica un evento con `tools.eventos_agente.publicar_evento` — con
latencia, payload de entrada y de salida. `agente`/`session_id` (parámetro
`contexto`) son solo para ETIQUETAR esos eventos en el panel (para
distinguir Orquestador de Servicio Técnico, y una sesión de otra) — no
afectan la lógica del loop en absoluto.

Resiliencia (ver `docs/PLAN_DE_MEJORAS.md`, Fases 2 y 3):
- Timeouts explícitos (conexión y lectura por separado) y `max_retries=0`
  en el SDK: los reintentos viven en UNA sola capa — el fallback entre
  modelos — en vez de multiplicarse (antes: 2 reintentos del SDK x 6
  modelos x cada iteración).
- Errores clasificados (`_clasificar_error`): 401/402 cortan el turno como
  problema de credenciales/saldo en vez de disfrazarse de fallback.
- Disyuntor por modelo (`_pausar_modelo`): un modelo que acaba de fallar
  con 429/5xx/timeout se salta durante un tiempo en las siguientes
  llamadas, en vez de volver a probarse en cada iteración.
- `PresupuestoTurno`: tope de llamadas al LLM y tiempo límite COMPARTIDOS
  por orquestador y sub-agente en un mismo turno (antes eran dos loops
  anidados de 8 iteraciones cada uno, sin tope conjunto ni timeout).
- Razón de parada explícita en cada salida del loop (`RAZONES_PARADA`).
- El loop nunca propaga excepciones y siempre devuelve un historial válido
  (sin `tool_calls` huérfanos), para que el turno se pueda persistir.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Callable, Optional

from openai import APIConnectionError, APIError, OpenAI, Timeout

from tools import eventos_agente

logger = logging.getLogger(__name__)

# Tope de longitud para lo que se manda al panel de logs — un payload
# gigante (ej. el catálogo completo) no debe romper la UI ni saturar el
# stream; se corta y se avisa, nunca se descarta el evento completo.
_LIMITE_TEXTO_EVENTO = 4000


def _recortar(texto: str) -> str:
    if len(texto) <= _LIMITE_TEXTO_EVENTO:
        return texto
    return texto[:_LIMITE_TEXTO_EVENTO] + f"... (truncado, {len(texto)} caracteres en total)"


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Tope de seguridad: nunca dejar que el loop encadene tool calls infinitamente
# si el modelo entra en un ciclo raro (ej. llama la misma tool una y otra vez).
MAX_TOOL_ITERATIONS = 8


def _float_env(nombre: str, por_defecto: float) -> float:
    try:
        return float(os.environ.get(nombre, por_defecto))
    except ValueError:
        logger.warning("%s no es un número válido; se usa %s", nombre, por_defecto)
        return por_defecto


# Timeouts del cliente LLM. La lectura es larga porque un modelo puede tardar
# en generar; la conexión es corta porque si no conecta en 5 s, no va a
# conectar. Cada petición usa además min(lectura, tiempo restante del turno).
def _timeout_conexion_llm() -> float:
    return _float_env("LLM_TIMEOUT_CONEXION", 5)


def _timeout_lectura_llm() -> float:
    return _float_env("LLM_TIMEOUT_LECTURA", 45)


# Cuánto tiempo se saltea un modelo según por qué falló (disyuntor).
PAUSA_TRANSITORIA = 30.0  # 5xx, timeout, conexión, respuesta vacía
PAUSA_LIMITE = 60.0  # 429 sin cabecera Retry-After
PAUSA_NO_DISPONIBLE = 3600.0  # 404: el modelo ya no existe o dejó de ser :free
PAUSA_MAXIMA = 600.0  # tope para un Retry-After exagerado

RAZONES_PARADA = (
    "final",  # el modelo respondió en texto: el caso normal
    "max_iteraciones",  # el modelo encadenó demasiadas tool calls
    "presupuesto_llamadas",  # se agotó el tope de llamadas al LLM del turno
    "deadline",  # se agotó el tiempo límite del turno
    "modelos_caidos",  # ningún modelo del fallback respondió
    "credenciales",  # 401/402: clave inválida o sin saldo
    "error_interno",  # excepción inesperada dentro del loop
)


class ErrorCredencialesLLM(Exception):
    """401/402 de OpenRouter (o clave ausente): no tiene sentido probar otro
    modelo con la misma clave, y hay que alertar a un humano."""


class PresupuestoAgotado(Exception):
    def __init__(self, razon: str):
        super().__init__(razon)
        self.razon = razon


class PresupuestoTurno:
    """Límites de UN turno completo (un mensaje del cliente), compartidos por
    todos los loops que se ejecuten en él — el orquestador y el sub-agente
    reciben el MISMO objeto (vía `contexto["presupuesto"]`).

    Cuenta cada INTENTO de llamada a un modelo (también los que fallan y
    pasan al siguiente del fallback), porque todos consumen tiempo y cuota.
    Así el peor caso de un turno queda acotado: como mucho `max_llamadas`
    peticiones HTTP al LLM y `segundos` de duración (más lo que tarde la
    última petición en curso, que a su vez tiene timeout).
    """

    def __init__(self, max_llamadas: Optional[int] = None, segundos: Optional[float] = None, reloj=time.monotonic):
        self._reloj = reloj
        if max_llamadas is None:
            max_llamadas = int(_float_env("MAX_LLAMADAS_LLM_POR_TURNO", 15))
        self.max_llamadas = int(max_llamadas)
        self.segundos = float(segundos if segundos is not None else _float_env("SEGUNDOS_MAX_POR_TURNO", 90))
        self.inicio = reloj()
        self.llamadas = 0
        self.razones: list = []  # [(agente, razon_parada)] en orden de salida
        # Consumo del turno (Fase 8.1): totales y desglose por agente+modelo.
        self.tokens_entrada = 0
        self.tokens_salida = 0
        self.costo_usd = 0.0
        # {"agente|modelo": {"llamadas", "tokens_entrada", "tokens_salida", "costo_usd"}}
        self.uso_por_modelo: dict = {}

    def registrar_uso(self, agente: Optional[str], modelo: str, usage) -> dict:
        """Suma el `usage` de una respuesta exitosa del modelo. `cost` solo
        viene si el proveedor lo informa (OpenRouter lo hace en su campo
        `usage.cost`; con modelos `:free` es 0). Devuelve lo registrado."""
        entrada = int(getattr(usage, "prompt_tokens", 0) or 0)
        salida = int(getattr(usage, "completion_tokens", 0) or 0)
        costo = float(getattr(usage, "cost", 0) or 0)
        self.tokens_entrada += entrada
        self.tokens_salida += salida
        self.costo_usd += costo
        clave = f"{agente}|{modelo}"
        uso = self.uso_por_modelo.setdefault(
            clave, {"llamadas": 0, "tokens_entrada": 0, "tokens_salida": 0, "costo_usd": 0.0}
        )
        uso["llamadas"] += 1
        uso["tokens_entrada"] += entrada
        uso["tokens_salida"] += salida
        uso["costo_usd"] += costo
        return {"tokens_entrada": entrada, "tokens_salida": salida, "costo_usd": costo}

    def resumen(self) -> dict:
        return {
            "razones": [{"agente": a, "razon": r} for a, r in self.razones],
            "llamadas_llm": self.llamadas,
            "duracion_s": round(self.duracion(), 2),
            "tokens_entrada": self.tokens_entrada,
            "tokens_salida": self.tokens_salida,
            "costo_usd": round(self.costo_usd, 6),
            "uso_por_modelo": self.uso_por_modelo,
        }

    def segundos_restantes(self) -> float:
        return self.segundos - (self._reloj() - self.inicio)

    def verificar(self) -> None:
        """Lanza `PresupuestoAgotado` si ya no se puede hacer otra llamada."""
        if self.llamadas >= self.max_llamadas:
            raise PresupuestoAgotado("presupuesto_llamadas")
        if self.segundos_restantes() <= 0:
            raise PresupuestoAgotado("deadline")

    def registrar_llamada(self) -> None:
        self.llamadas += 1

    def duracion(self) -> float:
        return self._reloj() - self.inicio


# ---------------------------------------------------------------------------
# Disyuntor por modelo (estado del proceso, compartido entre turnos)
# ---------------------------------------------------------------------------

_lock_pausas = threading.Lock()
_pausas: dict = {}  # {modelo: instante (time.monotonic) hasta el que se saltea}


def _pausar_modelo(modelo: str, segundos: float) -> None:
    if segundos <= 0:
        return
    with _lock_pausas:
        _pausas[modelo] = max(_pausas.get(modelo, 0.0), time.monotonic() + min(segundos, PAUSA_MAXIMA))


def _modelos_a_intentar(modelos: list) -> list:
    """Los modelos que no están en pausa, en el orden configurado. Si TODOS
    están en pausa, devuelve solo el que sale antes de la pausa: es mejor
    un intento con probabilidad de fallar que no responder nunca."""
    ahora = time.monotonic()
    with _lock_pausas:
        disponibles = [m for m in modelos if _pausas.get(m, 0.0) <= ahora]
        if disponibles:
            return disponibles
        return [min(modelos, key=lambda m: _pausas.get(m, 0.0))]


def reiniciar_disyuntor() -> None:
    """Olvida todas las pausas (usado por los tests)."""
    with _lock_pausas:
        _pausas.clear()


def _retry_after(exc: Exception) -> Optional[float]:
    respuesta = getattr(exc, "response", None)
    cabeceras = getattr(respuesta, "headers", None) or {}
    try:
        valor = cabeceras.get("retry-after") or cabeceras.get("Retry-After")
        return float(valor) if valor is not None else None
    except (TypeError, ValueError):
        return None


def _clasificar_error(exc: Exception) -> tuple:
    """Devuelve `(tipo, segundos_de_pausa)` para un error del SDK.

    - 401/402: credenciales o saldo -> se corta el turno (no es culpa del
      modelo, cambiar de modelo no lo arregla).
    - 400/422: la petición fue rechazada. Puede ser un bug nuestro (mensajes
      mal formados) o una incompatibilidad del modelo (ej. no soporta tools),
      así que se registra como ERROR y se prueba el siguiente modelo.
    - 403: en OpenRouter también lo devuelve la moderación de algunos
      modelos, así que NO se trata como credenciales: siguiente modelo.
    - 404: el modelo ya no existe o dejó de ser gratuito -> pausa larga.
    - 429: límite de uso -> pausa según `Retry-After`.
    - 5xx / timeout / conexión: transitorio -> pausa corta.
    """
    if isinstance(exc, APIConnectionError):  # incluye APITimeoutError
        return "transitorio", PAUSA_TRANSITORIA
    estado = getattr(exc, "status_code", None)
    if estado in (401, 402):
        return "credenciales", 0.0
    if estado == 429:
        return "limite", _retry_after(exc) or PAUSA_LIMITE
    if estado == 404:
        return "no_disponible", PAUSA_NO_DISPONIBLE
    if estado in (400, 422):
        return "peticion_rechazada", 0.0
    if estado == 403:
        return "prohibido", 0.0
    return "transitorio", PAUSA_TRANSITORIA


def _client() -> OpenAI:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise ErrorCredencialesLLM("OPENROUTER_API_KEY no está definida")
    return OpenAI(
        base_url=OPENROUTER_BASE_URL,
        api_key=api_key,
        max_retries=0,
        timeout=Timeout(_timeout_lectura_llm(), connect=_timeout_conexion_llm()),
    )


def _models_from_env() -> list:
    raw = os.environ.get("OPENROUTER_MODELS", "")
    modelos = [m.strip() for m in raw.split(",") if m.strip()]
    if not modelos:
        raise RuntimeError("OPENROUTER_MODELS está vacío en el .env")
    return modelos


# ---------------------------------------------------------------------------
# Historial válido
# ---------------------------------------------------------------------------

def sanear_historial(mensajes: list) -> list:
    """Devuelve una copia de `mensajes` donde cada mensaje `assistant` con
    `tool_calls` va seguido de TODOS sus resultados `tool` — si falta alguno,
    se descartan ese mensaje y sus resultados parciales. También descarta
    mensajes `tool` sueltos que no respondan a ninguna llamada previa.

    Por qué: la API rechaza (400) un historial con tool calls sin respuesta.
    Si un turno se aborta a mitad (error, presupuesto agotado) y se
    persistiera así, TODOS los turnos siguientes de ese cliente fallarían.
    """
    limpio: list = []
    i = 0
    while i < len(mensajes):
        msg = mensajes[i]
        if msg.get("role") == "tool":
            i += 1  # huérfano: su assistant(tool_calls) no quedó en `limpio`
            continue
        llamadas = msg.get("tool_calls") if msg.get("role") == "assistant" else None
        if not llamadas:
            limpio.append(msg)
            i += 1
            continue
        ids_esperados = {tc.get("id") for tc in llamadas}
        j = i + 1
        resultados = []
        while j < len(mensajes) and mensajes[j].get("role") == "tool":
            resultados.append(mensajes[j])
            j += 1
        if ids_esperados <= {r.get("tool_call_id") for r in resultados}:
            limpio.append(msg)
            limpio.extend(r for r in resultados if r.get("tool_call_id") in ids_esperados)
        else:
            logger.warning("Se descartó una llamada a tools incompleta del historial (ids=%s)", ids_esperados)
        i = j
    return limpio


# ---------------------------------------------------------------------------
# Loop principal
# ---------------------------------------------------------------------------

_MENSAJES_RESPALDO = {
    "max_iteraciones": (
        "Estoy teniendo dificultades para procesar tu solicitud en este momento, "
        "¿podrías reformularla o intentarlo de nuevo en un momento?"
    ),
    "presupuesto_llamadas": (
        "Estoy tardando más de lo normal en procesar tu solicitud. "
        "¿Podrías intentarlo de nuevo en un momento?"
    ),
    "deadline": (
        "Estoy tardando más de lo normal en procesar tu solicitud. "
        "¿Podrías intentarlo de nuevo en un momento?"
    ),
    "modelos_caidos": (
        "Estoy teniendo dificultades técnicas para responder en este momento "
        "(no se pudo contactar ningún modelo configurado). Por favor intenta de "
        "nuevo en unos minutos."
    ),
    "credenciales": (
        "Estoy teniendo dificultades técnicas para responder en este momento. "
        "Por favor intenta de nuevo más tarde."
    ),
    "error_interno": (
        "Ocurrió un problema técnico al procesar tu mensaje. "
        "Por favor intenta de nuevo en un momento."
    ),
}


def run_agent_loop(
    system_prompt: str,
    messages: list,
    tools_schema: list,
    tool_functions: dict,
    models: Optional[list] = None,
    contexto: Optional[dict] = None,
    reenviar_ultima_tool_si_se_agota: bool = False,
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
    contexto : dict opcional `{"agente", "session_id", "run_id",
        "presupuesto"}`. Los tres primeros solo ETIQUETAN los eventos del
        panel "Flujo en Vivo". `presupuesto` (un `PresupuestoTurno`) se
        comparte entre todos los loops de un mismo turno; si no viene, se
        crea uno nuevo solo para este loop.
    reenviar_ultima_tool_si_se_agota : si el presupuesto del turno se agota
        justo después de que una tool devolvió un resultado válido, devolver
        ese resultado como respuesta final en vez de un mensaje de respaldo.
        Lo usa el orquestador: su trabajo es reenviar TAL CUAL la respuesta
        del sub-agente, así que si el sub-agente ya respondió (quizá
        confirmando una cita), no se pierde esa respuesta por falta de una
        última llamada al LLM.

    Devuelve
    --------
    (texto_final_para_el_cliente, historial_actualizado_sin_system)

    Nunca lanza excepciones: cualquier fallo termina en un texto de respaldo
    y un historial saneado. La razón de parada queda en
    `presupuesto.razones` y en el evento `respuesta_final`.
    """
    contexto = contexto or {}
    presupuesto = contexto.get("presupuesto") or PresupuestoTurno()
    full_messages = [{"role": "system", "content": system_prompt}] + sanear_historial(messages)

    def _terminar(texto: str, razon: str):
        historial = sanear_historial(full_messages[1:])
        historial.append({"role": "assistant", "content": texto})
        presupuesto.razones.append((contexto.get("agente"), razon))
        if razon != "final":
            logger.warning(
                "Loop de %s terminó por '%s' (llamadas LLM del turno: %s, %.1fs)",
                contexto.get("agente"), razon, presupuesto.llamadas, presupuesto.duracion(),
            )
        eventos_agente.publicar_evento(
            "respuesta_final",
            agente=contexto.get("agente"),
            session_id=contexto.get("session_id"),
            run_id=contexto.get("run_id"),
            texto=_recortar(texto),
            fallback=razon != "final",
            razon_parada=razon,
            llamadas_llm_turno=presupuesto.llamadas,
        )
        return texto, historial

    def _respaldo(razon: str):
        if reenviar_ultima_tool_si_se_agota and razon in ("presupuesto_llamadas", "deadline"):
            ultimo = full_messages[-1]
            if ultimo.get("role") == "tool" and not str(ultimo.get("content", "")).startswith("ERROR"):
                return _terminar(str(ultimo["content"]), razon)
        return _terminar(_MENSAJES_RESPALDO[razon], razon)

    try:
        client = _client()
        modelos = models or _models_from_env()

        for _ in range(MAX_TOOL_ITERATIONS):
            try:
                response = _call_with_fallback(client, modelos, full_messages, tools_schema, contexto, presupuesto)
            except PresupuestoAgotado as exc:
                return _respaldo(exc.razon)
            except ErrorCredencialesLLM:
                logger.critical("OpenRouter rechazó las credenciales o no hay saldo — revisa OPENROUTER_API_KEY")
                return _respaldo("credenciales")
            except RuntimeError:
                logger.exception("Todos los modelos de OpenRouter fallaron")
                return _respaldo("modelos_caidos")

            msg = response.choices[0].message

            # Respuesta final en texto plano, sin más tool calls -> terminamos.
            if not msg.tool_calls:
                return _terminar(msg.content or "", "final")

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
                full_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": _ejecutar_tool(tool_call, tool_functions, contexto),
                    }
                )

        return _respaldo("max_iteraciones")
    except ErrorCredencialesLLM:
        logger.critical("OPENROUTER_API_KEY no está definida")
        return _respaldo("credenciales")
    except Exception:  # nunca dejar caer el turno completo
        logger.exception("Error inesperado dentro del loop de %s", contexto.get("agente"))
        return _respaldo("error_interno")


def _ejecutar_tool(tool_call, tool_functions: dict, contexto: dict) -> str:
    """Ejecuta UNA tool call y devuelve su resultado como texto. Nunca lanza:
    cualquier error se convierte en un texto "ERROR ..." que el modelo lee."""
    nombre = tool_call.function.name
    etiquetas = {
        "agente": contexto.get("agente"),
        "session_id": contexto.get("session_id"),
        "run_id": contexto.get("run_id"),
    }
    eventos_agente.publicar_evento(
        "tool_llamada", nombre=nombre, argumentos=_recortar(tool_call.function.arguments or "{}"), **etiquetas
    )
    inicio_tool = time.perf_counter()
    try:
        args = json.loads(tool_call.function.arguments or "{}")
    except json.JSONDecodeError:
        resultado = f"ERROR: el modelo envió argumentos inválidos (JSON malformado) para {nombre}"
    else:
        funcion: Optional[Callable] = tool_functions.get(nombre)
        if funcion is None:
            resultado = f"ERROR: la tool '{nombre}' no existe en este agente"
        elif not isinstance(args, dict):
            resultado = f"ERROR: los argumentos de {nombre} deben ser un objeto JSON"
        else:
            try:
                resultado = funcion(**args)
            except Exception as exc:  # nunca dejar caer el loop completo por un error de tool
                logger.exception("Fallo ejecutando tool %s con args %s", nombre, args)
                resultado = f"ERROR interno ejecutando {nombre}: {exc}"
    eventos_agente.publicar_evento(
        "tool_resultado",
        nombre=nombre,
        resultado=_recortar(str(resultado)),
        latencia_ms=round((time.perf_counter() - inicio_tool) * 1000),
        **etiquetas,
    )
    return str(resultado)


def _call_with_fallback(
    client: OpenAI,
    modelos: list,
    messages: list,
    tools_schema: list,
    contexto: dict,
    presupuesto: Optional[PresupuestoTurno] = None,
):
    """Intenta cada modelo disponible en orden; salta al siguiente si uno
    falla. Esto es lo que reemplaza tener que configurar el fallback
    manualmente en n8n.

    - Los modelos en pausa (disyuntor) se saltean.
    - Cada intento consume presupuesto del turno y usa como timeout de
      lectura el mínimo entre `LLM_TIMEOUT_LECTURA` y el tiempo restante.
    - 401/402 lanzan `ErrorCredencialesLLM` sin probar más modelos.

    Bug real detectado en pruebas: bajo el pool compartido de modelos
    ":free" de OpenRouter, un modelo saturado a veces responde 200 OK con un
    cuerpo vacío/roto (`choices` en None) en vez de lanzar un error HTTP.
    Se trata igual que cualquier otra falla transitoria del modelo."""
    presupuesto = presupuesto or PresupuestoTurno()
    etiquetas = {
        "agente": contexto.get("agente"),
        "session_id": contexto.get("session_id"),
        "run_id": contexto.get("run_id"),
    }
    ultimo_error: Optional[Exception] = None
    for modelo in _modelos_a_intentar(modelos):
        presupuesto.verificar()
        presupuesto.registrar_llamada()
        timeout = max(1.0, min(_timeout_lectura_llm(), presupuesto.segundos_restantes()))
        eventos_agente.publicar_evento("llamada_modelo", modelo=modelo, **etiquetas)
        inicio = time.perf_counter()
        try:
            response = client.chat.completions.create(
                model=modelo,
                messages=messages,
                tools=tools_schema,
                tool_choice="auto",
                timeout=timeout,
            )
        except APIError as exc:
            tipo, pausa = _clasificar_error(exc)
            latencia_ms = round((time.perf_counter() - inicio) * 1000)
            eventos_agente.publicar_evento(
                "modelo_fallo", modelo=modelo, error=_recortar(str(exc)), tipo_error=tipo,
                latencia_ms=latencia_ms, **etiquetas,
            )
            if tipo == "credenciales":
                raise ErrorCredencialesLLM(str(exc)) from exc
            if tipo in ("peticion_rechazada", "prohibido"):
                logger.error("Modelo %s rechazó la petición (%s): %s", modelo, tipo, exc)
            else:
                logger.warning("Modelo %s falló (%s) — pausa de %.0fs, se prueba el siguiente", modelo, tipo, pausa)
            _pausar_modelo(modelo, pausa)
            ultimo_error = exc
            continue
        latencia_ms = round((time.perf_counter() - inicio) * 1000)
        if not response.choices:
            logger.warning("Modelo %s respondió 200 OK sin 'choices' — se prueba el siguiente", modelo)
            eventos_agente.publicar_evento(
                "modelo_fallo", modelo=modelo, error="Respuesta 200 OK sin 'choices'", tipo_error="respuesta_vacia",
                latencia_ms=latencia_ms, **etiquetas,
            )
            _pausar_modelo(modelo, PAUSA_TRANSITORIA)
            ultimo_error = RuntimeError(f"Respuesta sin 'choices' del modelo {modelo}")
            continue
        tiene_tool_calls = bool(response.choices[0].message.tool_calls)
        uso = presupuesto.registrar_uso(contexto.get("agente"), modelo, getattr(response, "usage", None))
        eventos_agente.publicar_evento(
            "respuesta_modelo", modelo=modelo, latencia_ms=latencia_ms,
            tiene_tool_calls=tiene_tool_calls, **uso, **etiquetas,
        )
        return response
    raise RuntimeError(f"Todos los modelos del fallback fallaron. Último error: {ultimo_error}")
