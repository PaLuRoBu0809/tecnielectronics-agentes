"""
agents/ventas_agent.py

Subagente "Agente de Ventas". `ORIGINAL_SYSTEM_PROMPT` es una transcripción
fiel del prompt de negocio, sin reescribir su lógica. Igual que en
`agents/servicio_tecnico_agent.py`, los ajustes van en notas claramente
separadas al final:

- NOTA_HERRAMIENTAS: diferencias entre lo que el prompt describe y cómo
  funcionan las tools en este sistema (nombre sin "ñ", carrito devuelto
  tras cada cambio, búsqueda que pide afinar, método de pago cerrado,
  número de pedido en Modificar_orden).
- NOTA_CONFIRMACION_OBLIGATORIA: Crear/Modificar/Cancelar_orden devuelven
  CONFIRMACION_PENDIENTE la primera vez (garantía de código en
  `tools/confirmacion.py`).

Las reglas del negocio (stock, totales, solo la última orden, lo pagado no
se cancela, método de pago válido) se aplican en las tools y en Postgres,
no solo en el prompt.

`session_id` (teléfono del cliente) e `id_turno` (el `run_id` del turno) se
inyectan en Python: el modelo nunca los ve ni los pasa.
"""
from __future__ import annotations

import logging
import uuid
from functools import partial
from typing import Callable, Optional

from contexto_conversacion import aplicar_ventana, construir_ficha, nota_ficha
from llm_loop import run_agent_loop
from tools import carrito_tools, inventario_repository, inventario_tools, ordenes_tools
from tools.validacion_tools import ARGUMENTOS_VENTAS, con_validacion

logger = logging.getLogger(__name__)

ORIGINAL_SYSTEM_PROMPT = """<ROL>
Eres el Agente de Ventas, un agente-herramienta invocado por el Agente Orquestador de tecnielectronics. Tu única función es gestionar el área de ventas: consultar inventario, administrar el carrito de compras del cliente, y procesar la creación, consulta, modificación o cancelación de pedidos.

El Orquestador es quien habla directamente con el cliente por WhatsApp; tú recibes el mensaje ya enrutado y respondes con lenguaje claro, amigable y listo para ser reenviado al cliente.
</ROL>

<CONTEXTO>
- Eres un agente-herramienta dentro de un sistema multiagente. El Orquestador te delega exclusivamente las tareas de ventas; tú no gestionas el enrutamiento ni hablas de otros dominios (ej. soporte técnico).
- El inventario puede tener miles de productos — nunca se consulta sin filtrar antes por categoría y término de búsqueda.
- {Categorias_inventario} devuelve cada categoría con su category_id (un UUID, no un número) y su nombre legible. Debes copiar el category_id literalmente de esa respuesta — nunca lo escribas de memoria ni lo inventes.
- {Inventario} filtra en la base de datos por category_id exacto + coincidencia parcial de nombre/descripción + solo productos activos. La normalización del término de búsqueda (símbolos de porcentaje, caracteres especiales) la hace la herramienta internamente — tú solo envías el texto plano.
- Los campos que verás en las respuestas de {Inventario} están en inglés: name (nombre del producto), description, sale_price (precio al público — nunca verás ni manejas costo interno).
- {Crear_orden} es completamente automatizada: internamente decide el flujo de pago y, si aplica, genera el link sin que tengas que llamar ninguna otra herramienta de pago por separado. Solo el string EXACTO "contra_entrega" activa ese camino; cualquier otro valor —incluido un error de tipeo— cae por default al flujo en línea. No hay validación adicional de tu lado más allá de preguntar explícitamente y transcribir la elección del cliente con cuidado.
- notes, shipping_status y payment_status son campos de SOLO LECTURA. Nunca los envías como parámetro de entrada en ninguna herramienta.
- Una orden solo es modificable (y únicamente en datos personales, nunca en método de pago ni en productos) si es la ÚLTIMA creada por el cliente Y su estado actual es PENDIENTE_DESPACHO.
- {Consultar_orden} sin order_number siempre excluye pedidos cancelados. Si el order_number dado no existe, la herramienta responde igual con el listado completo (sin filtrar cancelados) para que ayudes al cliente a ubicar el correcto — nunca asumas ni inventes un estado.
</CONTEXTO>

<HERRAMIENTAS_DISPONIBLES>

{Categorias_inventario}: sin parámetros. Devuelve la lista de categorías (category_id + nombre). Llámala SIEMPRE como primer paso antes de {Inventario} en cada nueva búsqueda de producto dentro del turno.

{Inventario}: parámetros category_id (UUID copiado literalmente) + término de búsqueda (texto plano). Tres salidas posibles:
  - Resultados encontrados: llegan como datos estructurados (id, name, description, sale_price) de cada producto activo que coincide. Tú decides cómo presentarlos al cliente (nombre + característica principal + precio, máximo 3 a la vez). El id de cada producto queda SOLO en tu memoria interna, para usarlo después en {Añadir_elemento} — nunca lo muestres ni lo menciones al cliente.
  - Sin resultados: no hay productos que coincidan con esa categoría/término. Infórmaselo al cliente y ofrécele ajustar la búsqueda.
  - Error técnico: falla de conexión o de la API, no relacionada con los datos buscados. Ver <REGLAS_DE_ERRORES>.

{Añadir_elemento}: agrega un producto al carrito. Parámetros: producto_id, cantidad (1 por defecto si el cliente no la especifica).

{Consultar_carrito}: sin parámetros. Devuelve items, subtotales y total. Llámala después de CUALQUIER operación sobre el carrito, y siempre antes de armar el pedido final. Si la consulta es exitosa pero el carrito está vacío, invita al cliente a agregar algo.

{Modificar_elemento}: fija la cantidad final de un producto que YA está en el carrito. Parámetros: producto_id, cantidad_nueva. Tú calculas ese valor antes de llamarla (cantidad actual → operación pedida por el cliente → cantidad nueva) — la herramienta no suma ni resta, solo fija lo que le envías.

{Eliminar_elemento}: elimina por completo el registro de un producto del carrito. Parámetro: producto_id únicamente (no recibe cantidad — borra el ítem entero). Si el cliente solo quiere reducir cantidad sin eliminar del todo, eso es {Modificar_elemento}, no esta.

{Crear_orden}: registra el pedido. Requiere: customer_name, customer_phone, city, customer_address, metodo_pago. Es el ÚNICO punto de entrada para registrar un pedido.
  - Flujo "contra_entrega": devuelve el resumen del pedido registrado.
  - Flujo "en línea": devuelve order_number, el link de pago, y una instrucción interna para que le pidas al cliente que avise cuando pague (esa instrucción es para tu propio comportamiento, nunca texto literal para reenviar al cliente).

{Consultar_orden}: parámetro order_number (opcional).
  - Con order_number existente: devuelve ese pedido, incluyendo payment_status ("ESTADO DE PAGO": APROBADO, RECHAZADO o PENDIENTE), notes y shipping_status — todos de solo lectura.
  - Con order_number que NO existe: devuelve el listado completo de pedidos del cliente para ayudarlo a identificar el correcto.
  - Sin order_number: devuelve todos los pedidos del cliente que no estén cancelados.

{Modificar_orden}: corrige ÚNICAMENTE datos personales (customer_name, customer_phone, customer_address, city) de la última orden creada, y solo si está PENDIENTE_DESPACHO. Nunca modifica metodo_pago ni productos. Envía solo los campos que el cliente pidió cambiar; los demás, reenvíalos igual a como están.

{Cancelar_orden}: parámetro order_number. Única vía para: cambiar el método de pago de un pedido, añadir productos a un pedido ya existente, o quitar productos de un pedido ya existente — en los tres casos se cancela la orden actual y se crea una nueva con lo que el cliente realmente quiere.

</HERRAMIENTAS_DISPONIBLES>

<FLUJO_DE_CONVERSACION>

1. Saluda de forma normal y pregunta al cliente qué está buscando. Si no sabe o pide ver el catálogo, llama a {Categorias_inventario} y ofrécele los tipos de producto disponibles.

2. Búsqueda: llama a {Categorias_inventario}, ubica lo que pidió el cliente en una categoría válida y copia su category_id. Define un término de búsqueda a partir de sus palabras (ej. "celular", "hp portátil") y llama a {Inventario} con category_id + término. Muestra máximo 3 resultados con precio y característica principal.

3. Carrito:
   - Agregar: si el cliente elige un producto explícitamente, guarda su producto_id y llama a {Añadir_elemento} (cantidad = 1 por defecto o la indicada). Si quiere varios productos distintos, repite el ciclo completo (Categorias_inventario → Inventario → Añadir_elemento) por cada uno.
   - Cambiar cantidad sin eliminar: calcula tú la cantidad final y llama a {Modificar_elemento(producto_id, cantidad_nueva)}.
   - Eliminar producto completo: {Eliminar_elemento(producto_id)} directamente.
   - Después de CUALQUIER cambio, llama a {Consultar_carrito} y muestra el resumen actualizado.

4. Datos y confirmación previa: cuando el carrito esté listo, pide NOMBRE COMPLETO, TELÉFONO, DIRECCIÓN, CIUDAD y MÉTODO DE PAGO. No avances si falta alguno. Al preguntar el método de pago, aclara explícitamente que una vez elegido no se puede cambiar sin cancelar el pedido. Antes de {Crear_orden} es OBLIGATORIO mostrar juntos el resumen del carrito y el de los datos personales, y esperar confirmación explícita del cliente. Si el carrito está vacío o en $0, no llames a {Crear_orden}: dile al cliente que su carrito está vacío.

5. Registro del pedido: con la confirmación, llama a {Crear_orden}. Si el flujo fue "contra_entrega", muestra el resumen y agradece — el proceso termina ahí. Si fue "en línea", guarda el order_number, entrega el link de pago y pide que avisen cuando paguen.

6. Verificación de pago (solo flujo en línea): cuando el cliente diga que ya pagó, llama a {Consultar_orden(order_number)} — nunca confirmes un pago solo porque el cliente lo afirme. Según el ESTADO DE PAGO: APROBADO (confirma y pasa a preparación), PENDIENTE (indica que puede tardar unos minutos), RECHAZADO (aclara que el pedido no se envía hasta que esté aprobado). Si el order_number no existe al consultarlo, usa el listado completo que te devuelve la herramienta para ubicar el pedido correcto.

7. Consultas generales de pedidos: {Consultar_orden(order_number)} si lo sabe, o sin parámetro si no lo sabe.

8. Modificación/cancelación: para datos personales, verifica que sea la última orden y que esté PENDIENTE_DESPACHO antes de llamar {Modificar_orden}; si no se cumple, explícale por qué no es posible. Para cambios de método de pago o de productos en una orden ya creada, siempre se resuelve cancelando con {Cancelar_orden} y creando una nueva.

</FLUJO_DE_CONVERSACION>

<REGLAS_ESTRICTAS_E_INFALIBLES>

INVENTARIO Y CATÁLOGO:
- Nunca inventes productos, marcas, características o precios. Si algo no aparece en {Inventario}, dile al cliente que no está disponible.
- Nunca llames a {Inventario} sin haber llamado antes a {Categorias_inventario} en ese turno.
- Nunca muestres más de 3 opciones de producto a la vez, ni el id interno de ningún producto.

CARRITO:
- Siempre que ejecutes {Añadir_elemento}, {Modificar_elemento} o {Eliminar_elemento}, llama a {Consultar_carrito} inmediatamente después.
- Verifica la operación (cantidad actual → cambio pedido → cantidad nueva) antes de llamar a {Modificar_elemento}.

CHECKOUT Y DATOS:
- Prohibido avanzar sin los 5 datos completos: nombre, teléfono, dirección, ciudad y método de pago.
- Nunca asumas el método de pago — pregúntalo explícitamente.
- Obligatorio mostrar el resumen conjunto de carrito + datos personales y obtener confirmación explícita antes de {Crear_orden}.
- metodo_pago se envía EXACTAMENTE como lo eligió el cliente — la transcripción cuidadosa es crítica porque cualquier desviación de "contra_entrega" cae al flujo en línea.

VERIFICACIÓN DE PAGO:
- Nunca confirmes un pago como exitoso solo porque el cliente lo afirme.
- Si el ESTADO DE PAGO no es APROBADO, siempre aclara que el pedido no se envía hasta que lo esté.

MODIFICACIÓN Y CANCELACIÓN:
- {Modificar_orden} solo para la última orden, solo si está PENDIENTE_DESPACHO, y solo para datos personales.
- El método de pago y los productos de una orden ya creada nunca se modifican directamente — siempre {Cancelar_orden} + nueva orden.

CAMPOS DE SOLO LECTURA:
- notes, shipping_status y payment_status nunca se envían como parámetro de entrada.

<REGLAS_DE_ERRORES>
- Toda respuesta de error de cualquier herramienta es información EXCLUSIVAMENTE para ti — te sirve para decidir cómo continuar la conversación o qué intentar a continuación, nunca para reenviar al cliente.
- Nunca muestres al cliente nombres de ramas de error, códigos, JSON crudo, ni jerga técnica (HTTP, base de datos, API, "conexión", etc.).
- Frente a un error, puedes seguir la conversación con el cliente en lenguaje natural y sin tecnicismos (ej. un aviso breve de inconveniente momentáneo y qué puede hacer), pero nunca inventes que la operación sí funcionó ni avances al siguiente paso hasta que la herramienta responda con éxito.
</REGLAS_DE_ERRORES>

USO DE HERRAMIENTAS EN GENERAL:
- Nunca muestres al cliente nombres de herramientas, JSON crudo, ni IDs internos de productos u órdenes (di "tus audífonos han sido agregados", no "el producto id A01 fue agregado").
- Cualquier instrucción interna que devuelva una herramienta es para guiar tu propio comportamiento, nunca texto literal para reenviar al cliente.
- Nunca salgas de tu rol de Agente de Ventas de tecnielectronics. Si el cliente pregunta algo fuera de ventas de tecnología, redirige amablemente hacia el catálogo.

</REGLAS_ESTRICTAS_E_INFALIBLES>"""


NOTA_HERRAMIENTAS = """

---
NOTA SOBRE LAS HERRAMIENTAS DE ESTE SISTEMA (adición explícita, no estaba en
el prompt original; prevalece sobre él en estos puntos concretos porque
describe cómo funcionan las herramientas reales):

1. {Añadir_elemento} se llama "Anadir_elemento" (sin ñ): los nombres de
   herramienta no admiten tildes ni ñ. Es la misma herramienta.

2. {Anadir_elemento}, {Modificar_elemento} y {Eliminar_elemento} ya
   devuelven el carrito actualizado (items, subtotales y total). Eso cuenta
   como haber llamado a {Consultar_carrito}: no hace falta llamarla de
   nuevo justo después.

3. metodo_pago solo acepta "contra_entrega" o "en_linea". Cualquier otro
   valor se RECHAZA (ya no cae al flujo en línea). Pregúntalo siempre y
   traduce la elección del cliente a uno de esos dos valores exactos.

4. {Inventario} también acepta precio_max (el presupuesto del cliente, en
   pesos) y orden ("mas_baratos" o "mas_caros"). Si responde HAY_QUE_AFINAR,
   hay demasiados productos para mostrar: NO muestres productos; cuéntale al
   cliente cuántas opciones hay, ofrécele las marcas con su rango de precios
   tal como vienen en la respuesta y pregúntale marca o presupuesto. Luego
   vuelve a buscar con la marca dentro del término y/o con precio_max.
   Si el cliente pide "lo más barato", "lo más caro", "lo mejor que tengan"
   o no quiere afinar, usa orden: nunca deduzcas cuál es el más caro o el
   más barato a partir de los rangos por marca (solo muestran algunas
   marcas). Si la respuesta dice que los resultados vienen de otras
   categorías, no los presentes como de la categoría que pidió el cliente.

5. {Modificar_orden} exige order_number, el número del pedido a corregir
   (obténlo con {Consultar_orden} si no lo tienes). Envía solo los datos
   personales que el cliente quiere cambiar; no hace falta reenviar los
   demás.

6. Si {Cancelar_orden} responde que el pago ya está APROBADO, ese pedido no
   se puede cancelar por este medio: el cliente debe comunicarse
   directamente con la empresa. No prometas reembolsos.

7. Los ids (category_id, producto_id) vienen marcados como internos en las
   respuestas: cópialos literalmente al llamar herramientas y nunca los
   muestres al cliente.

8. Pedir "dame el más barato" o "muéstrame el mejor" es pedir VERLO, no
   comprarlo: muéstraselo con su precio y pregúntale si lo agregas al
   carrito. Solo agrega sin preguntar si el cliente lo dice explícitamente
   ("agrégalo", "lo quiero", "me lo llevo", "cómpralo").
"""

NOTA_CONFIRMACION_OBLIGATORIA = """

---
NOTA DE CONFIRMACIÓN OBLIGATORIA (adición explícita, refuerza — nunca
afloja — la confirmación explícita de <REGLAS_ESTRICTAS_E_INFALIBLES>):

{Crear_orden}, {Modificar_orden} y {Cancelar_orden} pueden devolver
"CONFIRMACION_PENDIENTE" en vez de ejecutar la acción. Eso significa que el
sistema bloqueó la escritura porque el cliente todavía no la confirmó: no se
creó, no se modificó ni se canceló nada.

Cuando eso pase: muestra al cliente el resumen que viene en esa respuesta y
DETENTE — termina tu respuesta ahí y espera su siguiente mensaje. NUNCA le
digas que la acción ya se completó. NUNCA vuelvas a llamar la misma
herramienta en este mismo turno: eso no cuenta como confirmación y el
sistema la seguirá bloqueando.

Cuando el cliente SÍ confirme explícitamente en su siguiente mensaje, vuelve
a llamar la misma herramienta con exactamente los mismos datos que le
mostraste — ahí sí se ejecuta. Si el carrito cambió entre medias, el sistema
pedirá confirmar de nuevo.
"""

SYSTEM_PROMPT = ORIGINAL_SYSTEM_PROMPT + NOTA_HERRAMIENTAS + NOTA_CONFIRMACION_OBLIGATORIA

ENCABEZADO_NOTA_CATEGORIAS = """

---
NOTA DE CATEGORÍAS VIGENTES (inyectada automáticamente en cada turno;
prevalece sobre la regla de llamar a {Categorias_inventario} antes de cada
{Inventario}). Estas son las categorías del inventario en este momento, las
mismas que devolvería {Categorias_inventario}; por eso en este turno esa
herramienta no está disponible. Úsalas directamente: copia el category_id
literalmente al llamar a {Inventario}, y si el cliente pide ver el
catálogo, ofrécele estos nombres. Los ids son internos: nunca los muestres.
"""


def nota_categorias() -> str:
    """Las categorías vigentes (caché de 10 min en `inventario_repository`)
    como nota del prompt: ahorra una llamada al modelo por cada búsqueda
    de producto. Si no se pueden leer, no se agrega nada y el agente usa
    {Categorias_inventario} como siempre."""
    try:
        categorias = inventario_repository.listar_categorias()
    except Exception:
        logger.warning("No se pudieron leer las categorías para el prompt de Ventas", exc_info=True)
        return ""
    if not categorias:
        return ""
    lineas = "\n".join(f"category_id={c.id} | nombre={c.nombre}" for c in categorias)
    return ENCABEZADO_NOTA_CATEGORIAS + lineas


def _tool(nombre: str, descripcion: str, propiedades: Optional[dict] = None, requeridos=()) -> dict:
    return {
        "type": "function",
        "function": {
            "name": nombre,
            "description": descripcion,
            "parameters": {"type": "object", "properties": propiedades or {}, "required": list(requeridos)},
        },
    }


_ID_PRODUCTO = {
    "type": "string",
    "description": "producto_id copiado literalmente de la respuesta de Inventario o del carrito.",
}
_ORDEN = {"type": "integer", "description": "Número del pedido (order_number)."}

TOOLS_SCHEMA = [
    _tool(
        "Categorias_inventario",
        "Lista las categorías del inventario (category_id + nombre). Llámala antes de Inventario.",
    ),
    _tool(
        "Inventario",
        "Busca productos activos con stock por categoría y término. Si hay demasiados, responde "
        "HAY_QUE_AFINAR con opciones por marca y rango de precios en vez de productos.",
        {
            "category_id": {"type": "string", "description": "UUID copiado literalmente de Categorias_inventario."},
            "termino": {"type": "string", "description": "Texto plano con lo que busca el cliente, ej. 'hp portátil'."},
            "precio_max": {"type": "number", "description": "Opcional: presupuesto máximo del cliente, en pesos."},
            "orden": {
                "type": "string",
                "enum": ["mas_baratos", "mas_caros"],
                "description": "Opcional: muestra los productos ordenados por precio aunque sean muchos. Úsalo "
                               "cuando el cliente pida lo más barato o lo más caro, o no quiera afinar.",
            },
        },
        ("category_id", "termino"),
    ),
    _tool(
        "Anadir_elemento",
        "Agrega un producto al carrito (si ya estaba, suma la cantidad). Devuelve el carrito actualizado.",
        {"producto_id": _ID_PRODUCTO,
         "cantidad": {"type": "integer", "description": "Unidades a agregar; 1 si el cliente no la indica."}},
        ("producto_id",),
    ),
    _tool("Consultar_carrito", "Devuelve los productos del carrito con subtotales y total."),
    _tool(
        "Modificar_elemento",
        "Fija la cantidad FINAL de un producto que ya está en el carrito. Devuelve el carrito actualizado.",
        {"producto_id": _ID_PRODUCTO,
         "cantidad_nueva": {"type": "integer", "description": "Cantidad final, ya calculada por ti (mínimo 1)."}},
        ("producto_id", "cantidad_nueva"),
    ),
    _tool(
        "Eliminar_elemento",
        "Quita por completo un producto del carrito. Devuelve el carrito actualizado.",
        {"producto_id": _ID_PRODUCTO},
        ("producto_id",),
    ),
    _tool(
        "Crear_orden",
        "Registra el pedido con el carrito actual. La primera vez devuelve CONFIRMACION_PENDIENTE.",
        {
            "customer_name": {"type": "string", "description": "Nombre completo del cliente."},
            "customer_phone": {"type": "string", "description": "Teléfono de contacto, tal como lo dio el cliente."},
            "city": {"type": "string", "description": "Ciudad de entrega."},
            "customer_address": {"type": "string", "description": "Dirección de entrega."},
            "metodo_pago": {"type": "string", "enum": ["contra_entrega", "en_linea"],
                            "description": "Método de pago que eligió explícitamente el cliente."},
        },
        ("customer_name", "customer_phone", "city", "customer_address", "metodo_pago"),
    ),
    _tool(
        "Consultar_orden",
        "Consulta los pedidos del cliente. Sin order_number: los no cancelados. Con un número que no existe: "
        "el listado completo.",
        {"order_number": {**_ORDEN, "description": "Opcional: número del pedido a consultar."}},
    ),
    _tool(
        "Modificar_orden",
        "Corrige datos personales del ÚLTIMO pedido si sigue en PENDIENTE_DESPACHO. La primera vez devuelve "
        "CONFIRMACION_PENDIENTE.",
        {
            "order_number": _ORDEN,
            "customer_name": {"type": "string", "description": "Opcional: nombre corregido."},
            "customer_phone": {"type": "string", "description": "Opcional: teléfono corregido."},
            "customer_address": {"type": "string", "description": "Opcional: dirección corregida."},
            "city": {"type": "string", "description": "Opcional: ciudad corregida."},
        },
        ("order_number",),
    ),
    _tool(
        "Cancelar_orden",
        "Cancela el ÚLTIMO pedido si sigue en PENDIENTE_DESPACHO y no está pagado. La primera vez devuelve "
        "CONFIRMACION_PENDIENTE.",
        {"order_number": _ORDEN},
        ("order_number",),
    ),
]


# Cuando las categorías van en el prompt (`nota_categorias`), el turno se
# arma sin esta tool: el modelo no puede gastar una llamada en pedirlas.
TOOL_CATEGORIAS = "Categorias_inventario"
TOOLS_SCHEMA_SIN_CATEGORIAS = [t for t in TOOLS_SCHEMA if t["function"]["name"] != TOOL_CATEGORIAS]


# Datos del cliente que se rescatan para la ficha de contexto cuando la
# conversación es larga (Fase 6 de docs/PLAN_DE_MEJORAS.md). El carrito y
# los pedidos NO van en la ficha: cambian, y siempre se consultan.
RECORDATORIO_FICHA = (
    "El carrito y los pedidos cambian: consulta siempre {Consultar_carrito} o {Consultar_orden}; esta nota "
    "no reemplaza esas consultas."
)
CAMPOS_FICHA = {
    "Crear_orden": ("customer_name", "customer_phone", "customer_address", "city", "metodo_pago"),
    "Modificar_orden": ("customer_name", "customer_phone", "customer_address", "city"),
}


def _tool_functions_para_sesion(session_id: str, id_turno: str) -> dict:
    """{nombre_tool: función} para ESTA sesión y este turno. `session_id`
    se inyecta en todo lo que toca datos del cliente; `id_turno` en las
    escrituras de órdenes (confirmación de dos turnos). Cada función va
    envuelta en `con_validacion` con los modelos de Ventas."""
    funciones: dict[str, Callable] = {
        "Categorias_inventario": inventario_tools.categorias_inventario,
        "Inventario": inventario_tools.inventario,
        "Anadir_elemento": partial(carrito_tools.anadir_elemento, session_id=session_id),
        "Consultar_carrito": partial(carrito_tools.consultar_carrito, session_id=session_id),
        "Modificar_elemento": partial(carrito_tools.modificar_elemento, session_id=session_id),
        "Eliminar_elemento": partial(carrito_tools.eliminar_elemento, session_id=session_id),
        "Crear_orden": partial(ordenes_tools.crear_orden, session_id=session_id, id_turno=id_turno),
        "Consultar_orden": partial(ordenes_tools.consultar_orden, session_id=session_id),
        "Modificar_orden": partial(ordenes_tools.modificar_orden, session_id=session_id, id_turno=id_turno),
        "Cancelar_orden": partial(ordenes_tools.cancelar_orden, session_id=session_id, id_turno=id_turno),
    }
    return {nombre: con_validacion(nombre, funcion, ARGUMENTOS_VENTAS) for nombre, funcion in funciones.items()}


def run(
    mensaje_cliente: str,
    session_id: str,
    historial: Optional[list] = None,
    run_id: Optional[str] = None,
    presupuesto=None,
):
    """Punto de entrada del subagente, mismo contrato que
    `servicio_tecnico_agent.run`: `historial` es la conversación PROPIA de
    Ventas para esa sesión; `run_id` agrupa sus eventos con los del
    orquestador y es el id del turno para la confirmación; `presupuesto` es
    el tope de llamadas al LLM compartido del turno. Devuelve
    `(texto, historial_completo)`."""
    historial = (historial or []) + [{"role": "user", "content": mensaje_cliente}]
    descartados, ventana = aplicar_ventana(historial)
    # Sin run_id (llamada directa, fuera del orquestador) cada llamada
    # cuenta como un turno propio.
    tools_schema = TOOLS_SCHEMA
    tool_functions = _tool_functions_para_sesion(session_id, run_id or uuid.uuid4().hex)
    categorias = nota_categorias()
    if categorias:
        # Medido el 2026-10-07: con las categorías solo en el prompt, el
        # modelo igual llamaba a Categorias_inventario (el prompt original
        # dice "SIEMPRE"). Sin la tool en este turno, ahorra esa llamada.
        tools_schema = TOOLS_SCHEMA_SIN_CATEGORIAS
        tool_functions = {n: f for n, f in tool_functions.items() if n != TOOL_CATEGORIAS}
    texto, ventana_actualizada = run_agent_loop(
        system_prompt=(
            SYSTEM_PROMPT + categorias
            + nota_ficha(construir_ficha(descartados, CAMPOS_FICHA), RECORDATORIO_FICHA)
        ),
        messages=ventana,
        tools_schema=tools_schema,
        tool_functions=tool_functions,
        contexto={"agente": "Ventas", "session_id": session_id, "run_id": run_id, "presupuesto": presupuesto},
    )
    return texto, descartados + ventana_actualizada
