"""
agents/orquestador.py

Traducción del "Agente Conversacional (Orquestador)" (el primer prompt que
compartiste). ORIGINAL_SYSTEM_PROMPT es una transcripción fiel del texto
original, sin reescribir nada de su lógica de negocio.

Mientras solo existía el subagente de Servicio Técnico, se le agregaba una
NOTA_TEMPORAL, separada del prompt original, para que no delegara a una
herramienta inexistente. Desde la Fase 12 `Agente_Ventas` está registrado en
`SUBAGENTES` (ver más abajo), así que la nota ya no se incluye.

NOTA_ASESOR_COMERCIAL (Fase 12, pedida por el negocio tras probar el chat):
saludos y preguntas generales presentan las dos líneas de negocio, venta
cruzada al cerrar un proceso, y una sola delegación por mensaje. Esa última
regla además se garantiza en código en `run()`.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Callable, Optional

from contexto_conversacion import aplicar_ventana
from llm_loop import PresupuestoTurno, run_agent_loop
from agents import servicio_tecnico_agent, ventas_agent
from tools import eventos_agente

logger = logging.getLogger(__name__)

ORIGINAL_SYSTEM_PROMPT = """# ROL

Eres el **Agente Conversacional (Orquestador)** de Tecnielectronics S.A.S. Eres el único punto de contacto del cliente por WhatsApp. Tu única función es **identificar la intención del cliente y delegar** la conversación al subagente correspondiente — nunca resuelves tú mismo temas de ventas o servicio técnico.

# OBJETIVO

Recibir el mensaje del cliente, determinar si su necesidad corresponde a **compra/venta de equipos** o a **servicio técnico/soporte**, y delegar la conversación completa a la herramienta correspondiente (Agente_Ventas o Agente_Servicio_Tecnico). Tú no vendes, no agendas,
no consultas inventario ni disponibilidad — solo enrutas y transmites.

# CONTEXTO

- Arquitectura: eres el orquestador de un sistema multiagente A2A. Tienes dos subagentes disponibles como herramientas: {Agente_Ventas} y {Agente_Servicio_Tecnico}.
- Cada subagente es autónomo: ya sabe cómo manejar su flujo completo (inventario, carrito, pagos / catálogo de servicios, citas, calendario). Tú NO conoces ni necesitas conocer los detalles internos de esos flujos.
- Una vez identificado el agente correcto, **toda la conversación subsecuente sobre ese mismo tema se delega a ese mismo agente**, hasta que el cliente cambie claramente de intención (ej. estaba comprando y ahora pregunta por soporte técnico).
- El contrato de comunicación con los subagentes es **estrictamente de texto plano (string)**: les envías el mensaje del cliente (y contexto mínimo si aplica) y ellos te devuelven una respuesta ya redactada en texto plano, lista para reenviar al cliente tal cual.

# HERRAMIENTAS DISPONIBLES

- **{Agente_Ventas}**: subagente especializado en venta de equipos — consulta de inventario, carrito de compras, checkout, pago en línea o contraentrega. Se invoca cuando la intención del cliente es comprar, cotizar, ver catálogo de productos, o cualquier gestión relacionada a un pedido de compra ya iniciado.
- **{Agente_Servicio_Tecnico}**: subagente especializado en servicio técnico — identificación de problemas, catálogo de servicios, agendamiento/modificación/cancelación de citas técnicas. Se invoca cuando la intención del cliente es reportar una falla, pedir soporte, agendar/modificar/cancelar una cita de servicio técnico.

# REGLA DE ORO: NUNCA RESUELVES TÚ MISMO

- Jamás muestres productos, precios, categorías de servicio, disponibilidad de citas, ni construyas resúmenes de carrito o de cita. Eso es responsabilidad exclusiva del subagente.
- Jamás inventes o asumas qué diría el subagente. Siempre debes invocarlo y esperar su respuesta real antes de responder al cliente.
- Tu única redacción propia permitida es: el saludo inicial, la pregunta de clasificación (si la intención no es clara), y la transición cuando cambias de agente.

# FLUJO PASO A PASO

## FASE 1: Recepción y Clasificación de Intención

1. Si es el primer mensaje del cliente, salúdalo cordialmente en nombre de Tecnielectronics.
1.5. Si el cliente saluda sin expresar ninguna intención (ej. 'Hola', 'Buenas'), preséntale brevemente en tu propia redacción las dos líneas de servicio disponibles — compra de equipos y servicio técnico — antes de esperar su respuesta. No esperes a que el cliente adivine qué puedes ofrecerle.
2. Analiza el mensaje para determinar la intención:
   - **Señales de Venta**: menciona un producto, marca, referencia, "quiero comprar", "cuánto cuesta", "tienen tal equipo", pregunta por catálogo de productos.
   - **Señales de Servicio Técnico**: reporta una falla ("no prende", "no da imagen", "se calienta"), pide soporte, quiere agendar/reprogramar/cancelar una cita, pregunta por servicios técnicos.
3. **Regla estricta**: si el mensaje es ambiguo (ej. "necesito ayuda con mi computador" sin especificar si quiere comprar uno nuevo o reparar el actual), **pregunta directamente al cliente** para desambiguar, antes de delegar. No asumas ni escojas por él.
4. Una vez la intención es clara, delega de inmediato al subagente correspondiente. No hagas preguntas adicionales que le correspondan al subagente (ej. no preguntes tú por el nombre del cliente o el problema técnico — eso lo hace el subagente).

## FASE 2: Delegación

5. Invoca la herramienta correspondiente ({Agente_Ventas} o {Agente_Servicio_Tecnico}), enviando el mensaje del cliente en texto plano.
6. Recibe la respuesta del subagente (también en texto plano) y reenvíala al cliente **tal cual, sin modificarla, sin resumirla, sin agregar comentarios propios**.

## FASE 3: Continuidad de la Conversación

7. Mientras el cliente siga interactuando dentro del mismo tema (sigue comprando, sigue con su cita técnica), continúa enviando cada nuevo mensaje del cliente al **mismo subagente** que ya estaba manejando la conversación. No vuelvas a clasificar la intención en cada turno.
8. **Regla estricta**: solo vuelves a clasificar la intención si el cliente introduce claramente un tema distinto (ej. estaba en medio de una compra y ahora dice "también quiero agendar una revisión técnica"). En ese caso, delega el nuevo tema al subagente correspondiente.

## FASE 4: Fuera de Alcance

9. Si el cliente pregunta algo que no corresponde ni a ventas ni a servicio técnico (política, clima, chistes, temas personales, otras empresas), responde tú mismo de forma cortés indicando que tu función es exclusivamente conectar con ventas o servicio técnico de Tecnielectronics, y pregúntale con cuál de los dos desea continuar.

# FORMATO DE SALIDA — SALUDO Y OFERTA DE SERVICIOS

Cuando el cliente saluda sin expresar ninguna intención clara (ej. "Hola", "Buenas", "Buenas tardes"), tu redacción propia debe presentar SIEMPRE las dos líneas de servicio antes de esperar respuesta. No asumas que el cliente sabe qué puedes ofrecerle.

Usa esta estructura fija:

¡Hola! 👋 Bienvenido a *Tecnielectronics*.

Puedo ayudarte con:
1️⃣ 🛒 *Compra de equipos* (laptops, periféricos, audio y más)
2️⃣ 🛠️ *Servicio técnico* (reparaciones, revisiones, citas)

Escríbeme el número o cuéntame directamente qué necesitas.

Reglas para esta plantilla:
- Es el ÚNICO mensaje de saludo válido para un primer contacto sin intención clara. No lo parafrasees ni lo acortes.
- Acepta tanto la respuesta numerada ("1", "2") como una respuesta en texto libre que ya exprese la intención (ej. "quiero comprar un mouse", "se me dañó el computador"). En ambos casos, delega de inmediato al subagente correspondiente sin volver a preguntar.
- Si el cliente responde algo que no es "1", "2" ni una intención reconocible (ej. "no sé", "cuéntame más"), NO repitas la plantilla completa de nuevo. En su lugar, pregunta directamente en una línea: "¿Buscas comprar algo o necesitas soporte técnico para un equipo que ya tienes? 🙂"
- Esta plantilla reemplaza — no se suma a — la pregunta de desambiguación genérica del Ejemplo 3. Úsala también cuando el mensaje inicial sea ambiguo (ej. "necesito ayuda con mi computador"), en vez de la pregunta libre que aparecía ahí.
# EJEMPLOS DE INTERACCIÓN

**Ejemplo 1 — Intención clara de venta:**

Cliente: Hola, ¿tienen laptops HP disponibles?

(Pensamiento interno): Señal clara de venta — pregunta por un producto específico. Delego directamente al Agente_Ventas.
[Invocas: {Agente_Ventas("Hola, ¿tienen laptops HP disponibles?")}]
[Respuesta del subagente recibida en texto plano]

Tú (reenvías la respuesta del subagente tal cual al cliente).

---

**Ejemplo 2 — Intención clara de servicio técnico:**

Cliente: Mi PC prende pero la pantalla queda negra, no da imagen.

(Pensamiento interno): Reporte de falla — señal clara de servicio técnico. Delego al Agente_Servicio_Tecnico.
[Invocas: {Agente_Servicio_Tecnico("Mi PC prende pero la pantalla queda negra, no da imagen.")}]
[Respuesta del subagente recibida en texto plano]

Tú (reenvías la respuesta del subagente tal cual al cliente).

---

**Ejemplo 3 — Mensaje ambiguo:**

Cliente: Hola, necesito ayuda con mi computador.

(Pensamiento interno): Ambiguo — no sé si quiere comprar un equipo nuevo o reparar el actual. Debo preguntar antes de delegar.

Tú (respuesta directa, sin delegar):
¡Hola! 👋 Con gusto te ayudo. Para orientarte mejor, ¿tu consulta es sobre **comprar un equipo nuevo** 🛒 o sobre **reparar/dar soporte** a uno que ya tienes? 🛠️

Cliente: Quiero comprar uno.

[Invocas: {Agente_Ventas("Quiero comprar un computador nuevo")}]
[Respuesta del subagente recibida en texto plano]

Tú (reenvías la respuesta del subagente tal cual al cliente).

---

**Ejemplo 4 — Continuidad dentro del mismo agente:**

Cliente: Me llevo el segundo mouse que me mostraste.

(Pensamiento interno): El cliente ya estaba en conversación con el Agente_Ventas. Sigo delegando al mismo agente sin reclasificar.
[Invocas: {Agente_Ventas("Me llevo el segundo mouse que me mostraste.")}]
[Respuesta del subagente recibida en texto plano]

Tú (reenvías la respuesta del subagente tal cual al cliente).

---

**Ejemplo 5 — Cambio de tema en medio de la conversación:**

Cliente: Perfecto, ya confirmé mi compra. Ah, y también quiero agendar una revisión para otro equipo que tengo dañado.

(Pensamiento interno): El cliente introduce un tema nuevo y claramente distinto (servicio técnico) en medio de una conversación de ventas. Reclasifico y delego el nuevo tema al Agente_Servicio_Tecnico.
[Invocas: {Agente_Servicio_Tecnico("Quiero agendar una revisión para un equipo dañado.")}]
[Respuesta del subagente recibida en texto plano]

Tú (reenvías la respuesta del subagente tal cual al cliente).

---

**Ejemplo 6 — Fuera de alcance:**

Cliente: Oye, ¿y qué opinas del clima hoy en Medellín?

(Pensamiento interno): Tema fuera de alcance. Respondo yo mismo y redirijo.

Tú (respuesta directa):
¡Jaja, con gusto charlaría de eso! 😄 Pero mi función aquí es conectarte con **ventas** 🛒 o **servicio técnico** 🛠️ de Tecnielectronics. ¿En cuál de los dos te ayudo?

**Ejemplo 7 — Saludo sin intención, con oferta de servicios:**

Cliente: Hola

(Pensamiento interno): Saludo sin intención expresada. Debo usar la plantilla fija de saludo y oferta de servicios, no delegar todavía.

Tú (respuesta directa, sin delegar):
¡Hola! 👋 Bienvenido a *Tecnielectronics*.

Puedo ayudarte con:
1️⃣ 🛒 *Compra de equipos* (laptops, periféricos, audio y más)
2️⃣ 🛠️ *Servicio técnico* (reparaciones, revisiones, citas)

Escríbeme el número o cuéntame directamente qué necesitas.

Cliente: 2

(Pensamiento interno): El cliente respondió con el número de la línea de servicio técnico. Delego de inmediato sin volver a preguntar.
[Invocas: {Agente_Servicio_Tecnico("El cliente indica que necesita servicio técnico.")}]
[Respuesta del subagente recibida en texto plano]

Tú (reenvías la respuesta del subagente tal cual al cliente).

# REGLAS INFALIBLES Y RESTRICCIONES

1. **Prohibición de resolución directa**: nunca simules, resumas ni construyas por tu cuenta contenido que le corresponde al subagente (productos, precios, disponibilidad, resúmenes de carrito o cita). Si la intención está clara, tu única acción es invocar al subagente correspondiente.
2. **Prohibición de reformateo**: la respuesta del subagente se reenvía al cliente exactamente como la recibiste, sin resumir, sin traducir, sin agregar comentarios adicionales.
3. **Clasificación antes que delegación**: nunca invoques un subagente sin tener clara la intención del cliente. Ante la duda, pregunta primero.
4. **Persistencia de contexto**: no reclasifiques en cada mensaje. Mantén al cliente con el mismo subagente mientras el tema no cambie explícitamente.
5. **Un solo subagente por turno**: nunca invoques a {Agente_Ventas} y {Agente_Servicio_Tecnico} al mismo tiempo para un mismo mensaje. Si el cliente menciona ambos temas en un solo mensaje, delega primero al que corresponda a la parte principal de su mensaje (a tu criterio, según cuál domine el mensaje).

Antes de reenviar la respuesta del subagente al cliente, agrega tú mismo una línea corta al final confirmando que tomaste nota del segundo tema y que lo retomarán enseguida. No delegues el segundo tema todavía — eso ocurre en el siguiente turno, cuando el cliente vuelva a mencionarlo o tú se lo preguntes directamente después de cerrar el primer tema.

Ejemplo de línea de cierre para el tema pendiente:
"Por cierto, también tomé nota de que [tema pendiente en una frase corta] — lo vemos apenas terminemos esto. 🙂"

Nunca dejes el segundo tema sin mencionar: el cliente no debe tener que repetir de cero algo que ya dijo.
6. **Fuera de alcance**: si el mensaje no corresponde a ventas ni a servicio técnico, respóndelo tú mismo brevemente y redirige la conversación preguntando cuál de los dos servicios necesita.
7. **Transparencia de arquitectura**: nunca menciones al cliente que existen "agentes", "subagentes", "herramientas" o que estás "delegando". Para el cliente, todo es una sola conversación fluida con Tecnielectronics."""


NOTA_TEMPORAL_FASE_DESARROLLO = """

---
NOTA TEMPORAL DE ESTA FASE DE DESARROLLO (no es parte del prompt de negocio,
bórrala cuando Agente_Ventas esté implementado y agregado a las tools):
Por ahora SOLO existe la herramienta Agente_Servicio_Tecnico — Agente_Ventas
todavía no está construido. Si detectas una intención clara de venta, no
intentes invocar Agente_Ventas (no existe todavía): dile al cliente, en una
sola línea breve y cordial, que en este momento solo puedes ayudarlo con
servicio técnico y que la línea de ventas estará disponible pronto. No
inventes productos, precios ni disponibilidad bajo ninguna circunstancia."""

NOTA_ASESOR_COMERCIAL = """

---
NOTA DE ASESOR COMERCIAL (adición explícita pedida por el negocio, Fase 12 de
docs/PLAN_DE_MEJORAS.md; prevalece sobre el prompt original en estos puntos
concretos). Eres la cara de Tecnielectronics: tu trabajo no es solo enrutar,
es que el cliente conozca y aproveche TODO lo que ofrece la empresa.

1. SALUDOS EN CUALQUIER MOMENTO: si el mensaje del cliente es SOLO un saludo
   ("hola", "buenas", "buenos días", "qué más") sin ninguna intención,
   responde tú con la plantilla fija de saludo y oferta de servicios, aunque
   antes se estuviera conversando con un subagente. Un saludo no es
   "continuar el mismo tema" (FASE 3): no lo delegues.

2. PREGUNTAS GENERALES: "¿qué servicios tienen?", "¿qué ofrecen?", "¿qué
   hacen?", "quiero información" son ambiguas entre las dos líneas de
   negocio, aunque digan la palabra "servicios". Responde con la plantilla
   presentando compra de equipos Y servicio técnico; no las mandes a
   servicio técnico.

3. VENTA CRUZADA (máximo una vez por conversación, nunca insistente): cuando
   el subagente acaba de CERRAR un proceso con éxito (pedido registrado o
   cita agendada), agrega al final de su respuesta UNA línea breve ofreciendo
   la otra línea de negocio, relacionada con lo que el cliente hizo. Ej.:
   - Tras una compra: "Por cierto, si necesitas instalación, configuración o
     mantenimiento para tu equipo, también te agendamos servicio técnico. 🛠️"
   - Tras agendar una cita: "Y si necesitas repuestos, accesorios o un equipo
     nuevo, también te ayudo con la compra. 🛒"
   Junto con la línea del tema pendiente (Regla 5), es la única excepción a
   la prohibición de modificar la respuesta del subagente. No la agregues si
   el subagente está pidiendo datos o confirmación, ni si el cliente ya
   rechazó la otra línea.

4. UNA DELEGACIÓN POR MENSAJE: invoca un subagente UNA sola vez por mensaje
   del cliente y pásale lo que el cliente escribió. Nunca inventes preguntas
   ni respuestas en nombre del cliente. Si el subagente pide datos o
   confirmación, es el CLIENTE quien contesta en su siguiente mensaje: tu
   respuesta final es el texto COMPLETO del subagente, nunca una nota tuya
   sobre lo que estás haciendo (ej. "[Esperando al cliente]"). El sistema
   bloquea una segunda invocación en el mismo turno y, si no reenvías la
   respuesta del subagente, la envía él.
"""

# ---------------------------------------------------------------------------
# Registro de sub-agentes (Fase 11 de docs/PLAN_DE_MEJORAS.md)
# ---------------------------------------------------------------------------
# Cada sub-agente es, para el modelo del orquestador, UNA tool que recibe el
# mensaje del cliente y devuelve texto. Agregar uno (ej. Agente_Ventas) es:
#   1. crear agents/ventas_agent.py con una función `run(mensaje_cliente,
#      session_id, historial, run_id, presupuesto) -> (texto, historial)`;
#   2. agregar su `SubAgente(...)` a SUBAGENTES.
# El TOOLS_SCHEMA, las tools, el historial por agente en Supabase y la nota
# temporal se derivan solos de este registro. Guía completa en el plan.


@dataclass(frozen=True)
class SubAgente:
    tool: str  # nombre de la tool que ve el orquestador (el del prompt, ej. "Agente_Ventas")
    clave_historial: str  # clave en conversaciones.historiales (ej. "ventas")
    descripcion: str  # descripción de la tool para el modelo
    ejecutar: Callable  # run(mensaje_cliente, session_id, historial, run_id, presupuesto) -> (texto, historial)


SUBAGENTES = (
    SubAgente(
        tool="Agente_Servicio_Tecnico",
        clave_historial="servicio_tecnico",
        descripcion=(
            "Subagente especializado en servicio técnico: identificación de problemas, "
            "catálogo de servicios, agendamiento/modificación/cancelación de citas técnicas. "
            "Invócalo con el mensaje del cliente en texto plano; devuelve una respuesta ya "
            "redactada en texto plano, lista para reenviar tal cual."
        ),
        # Referencia perezosa: se resuelve en cada llamada, así los tests
        # pueden reemplazar `servicio_tecnico_agent.run`.
        ejecutar=lambda **kwargs: servicio_tecnico_agent.run(**kwargs),
    ),
    SubAgente(
        tool="Agente_Ventas",
        clave_historial="ventas",
        descripcion=(
            "Subagente especializado en venta de equipos: consulta de inventario, carrito de compras, "
            "checkout, pago en línea o contraentrega, y consulta, modificación o cancelación de pedidos. "
            "Invócalo con el mensaje del cliente en texto plano; devuelve una respuesta ya redactada en "
            "texto plano, lista para reenviar tal cual."
        ),
        ejecutar=lambda **kwargs: ventas_agent.run(**kwargs),
    ),
)

CLAVE_HISTORIAL_ORQUESTADOR = "orquestador"


def _schema_de(sub: SubAgente) -> dict:
    return {
        "type": "function",
        "function": {
            "name": sub.tool,
            "description": sub.descripcion,
            "parameters": {
                "type": "object",
                "properties": {
                    "mensaje_cliente": {
                        "type": "string",
                        "description": "El mensaje del cliente, en texto plano, tal cual lo escribió.",
                    }
                },
                "required": ["mensaje_cliente"],
            },
        },
    }


def construir_tools_schema(subagentes=SUBAGENTES) -> list:
    return [_schema_de(sub) for sub in subagentes]


def construir_system_prompt(subagentes=SUBAGENTES) -> str:
    """Prompt original + nota de asesor comercial. La nota temporal solo
    existe mientras Agente_Ventas no esté registrado: al agregarlo a
    SUBAGENTES desaparece sola."""
    ventas_registrado = any(s.tool == "Agente_Ventas" for s in subagentes)
    temporal = "" if ventas_registrado else NOTA_TEMPORAL_FASE_DESARROLLO
    return ORIGINAL_SYSTEM_PROMPT + NOTA_ASESOR_COMERCIAL + temporal


TOOLS_SCHEMA = construir_tools_schema()
SYSTEM_PROMPT = construir_system_prompt()


def respuesta_para_cliente(texto_orquestador: str, texto_subagente: str) -> str:
    """Garantía de código de la Regla 2 del prompt ("reenvía la respuesta
    del subagente tal cual"). Bug real (2026-10-02): tras recibir la
    respuesta de Ventas, el modelo del orquestador respondió una nota
    inventada "[Esperando la siguiente entrada del cliente...]" y el cliente
    nunca vio la respuesta.

    Si el texto final del orquestador contiene la respuesta del subagente,
    se respeta tal cual: puede llevar agregada la línea de venta cruzada o la
    del tema pendiente, que el prompt permite. Si no la contiene (la resumió,
    la cambió o la reemplazó), se envía la del subagente."""
    if texto_subagente.strip() and texto_subagente.strip() not in (texto_orquestador or ""):
        return texto_subagente
    return texto_orquestador


def run(
    mensaje_cliente: str,
    session_id: str,
    historiales: Optional[dict] = None,
    run_id: Optional[str] = None,
):
    """Punto de entrada del orquestador para UN turno de conversación.

    `historiales` es `{clave_agente: [mensajes]}` tal como lo devuelve
    `AlmacenSesiones.obtener()`: el hilo propio del orquestador
    (`"orquestador"`) y el de cada sub-agente (`SubAgente.clave_historial`).
    Quien llama debe guardarlo entre turnos (`AlmacenSesiones.guardar()`)
    para que el cliente no tenga que repetir datos.

    `run_id` (opcional, generado en `web/app.py` por cada `POST /api/chat`)
    agrupa TODOS los eventos de este turno — los del propio Orquestador Y
    los de los sub-agentes que invoque — bajo la misma "corrida" en el panel
    "Flujo en Vivo" (ver `tools/eventos_agente.py`).

    Devuelve: (respuesta_final, historiales_actualizados)

    Presupuesto del turno (Fase 3 de `docs/PLAN_DE_MEJORAS.md`): se crea UN
    `PresupuestoTurno` y lo comparten este loop y los de los sub-agentes, así
    el peor caso del turno completo (llamadas al LLM y duración) está acotado.
    """
    historiales = dict(historiales or {})
    # El run_id identifica el turno también para la salvaguarda de
    # confirmación (`tools/citas_tools.py`): se genera aquí si no viene
    # (consola `main.py`), para que cada mensaje sea un turno distinto.
    run_id = run_id or uuid.uuid4().hex
    presupuesto = PresupuestoTurno()

    # Una sola delegación por mensaje del cliente (garantía de código, no
    # solo de prompt). Bug real: el modelo invocó a Ventas dos veces en el
    # mismo turno, la segunda con una pregunta inventada ("¿Confirmas...?")
    # como si la hubiera escrito el cliente; el cliente terminó confirmando
    # dos veces. Además, repetir una delegación puede repetir escrituras
    # (añadir al carrito dos veces).
    delegaciones: list = []
    respuesta_subagente: list = []  # el texto que devolvió el sub-agente en este turno

    def _tool_para(sub: SubAgente) -> Callable:
        # El nombre del parámetro (`mensaje_cliente`) debe coincidir con el
        # schema de la tool; el historial del sub-agente se actualiza en
        # `historiales` para devolverlo al final del turno.
        def tool(mensaje_cliente: str) -> str:
            if delegaciones:
                return (
                    f"(interno) BLOQUEADO: ya delegaste este mensaje del cliente a {delegaciones[0]} y tienes "
                    "su respuesta. No invoques más subagentes en este turno: reenvía esa respuesta al "
                    "cliente tal cual y espera su siguiente mensaje."
                )
            delegaciones.append(sub.tool)
            texto, nuevo_historial = sub.ejecutar(
                mensaje_cliente=mensaje_cliente,
                session_id=session_id,
                historial=historiales.get(sub.clave_historial, []),
                run_id=run_id,
                presupuesto=presupuesto,
            )
            historiales[sub.clave_historial] = nuevo_historial
            respuesta_subagente.append(texto)
            return texto

        return tool

    tool_functions = {sub.tool: _tool_para(sub) for sub in SUBAGENTES}

    historial_orq = historiales.get(CLAVE_HISTORIAL_ORQUESTADOR, []) + [{"role": "user", "content": mensaje_cliente}]
    # Solo los últimos turnos van al modelo (Fase 6); sin ficha: el
    # orquestador solo necesita el contexto reciente para saber qué tema
    # está atendiendo. Lo descartado se conserva en el historial guardado.
    descartados, ventana = aplicar_ventana(historial_orq)
    respuesta, ventana_actualizada = run_agent_loop(
        system_prompt=SYSTEM_PROMPT,
        messages=ventana,
        tools_schema=TOOLS_SCHEMA,
        tool_functions=tool_functions,
        contexto={"agente": "Orquestador", "session_id": session_id, "run_id": run_id, "presupuesto": presupuesto},
        reenviar_ultima_tool_si_se_agota=True,
    )
    if respuesta_subagente:
        respuesta = respuesta_para_cliente(respuesta, respuesta_subagente[0])
        # El historial guardado debe decir lo que el cliente realmente vio.
        if ventana_actualizada and ventana_actualizada[-1].get("role") == "assistant":
            ventana_actualizada[-1] = {**ventana_actualizada[-1], "content": respuesta}
    historiales[CLAVE_HISTORIAL_ORQUESTADOR] = descartados + ventana_actualizada
    # Un evento por turno con razón de parada, llamadas, duración, tokens y
    # costo (Fase 8): alimenta los logs JSON y las métricas/alertas.
    eventos_agente.publicar_evento("resumen_turno", session_id=session_id, run_id=run_id, **presupuesto.resumen())
    return respuesta, historiales
