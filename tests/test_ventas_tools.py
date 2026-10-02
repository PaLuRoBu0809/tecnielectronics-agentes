"""
tests/test_ventas_tools.py

Fases D y E del Agente de Ventas: confirmación compartida
(`tools/confirmacion.py`), tools de inventario, carrito y órdenes, y el
agente (`agents/ventas_agent.py`). Los repositorios se simulan: sus reglas
se prueban en `tests/test_ventas_repositorios.py` y contra Postgres en
`tests/test_integracion_postgres.py`.

Las tools se llaman como las llamaría el modelo: a través de
`_tool_functions_para_sesion` (validación Pydantic + session_id/id_turno
inyectados).

Corre con:
    python tests/test_ventas_tools.py
"""
import os
import re
import sys
from decimal import Decimal
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake-key")
os.environ.setdefault("OPENROUTER_API_KEY", "fake-key-para-el-mock")
os.environ.setdefault("OPENROUTER_MODELS", "modelo-simulado:free")
os.environ["LOG_EVENTOS_JSON"] = "0"

from agents import ventas_agent  # noqa: E402
from tools import (  # noqa: E402
    carrito_repository, confirmacion, inventario_repository, ordenes_repository, ordenes_tools, pagos,
)
from tools.carrito_repository import Carrito, LineaCarrito  # noqa: E402
from tools.errores_negocio import ErrorNegocio  # noqa: E402
from tools.formato_ventas import pesos  # noqa: E402
from tools.inventario_repository import OpcionMarca, Producto, ResultadoBusqueda  # noqa: E402
from tools.validacion_tools import ARGUMENTOS_VENTAS, verificar_coherencia_tools  # noqa: E402

SESION = "3001234567"
PRODUCTO = "0009f567-00c9-49b3-bb76-1e705c6bcdf4"
CATEGORIA = "0bb25874-baea-47f0-b18f-c7e135c157f1"


def tools(turno="turno-1"):
    return ventas_agent._tool_functions_para_sesion(SESION, turno)


CARRITO = Carrito((
    LineaCarrito(PRODUCTO, "Laptop HP Ultra", 1, Decimal("3500000")),
    LineaCarrito("11111111-1111-1111-1111-111111111111", "Mouse Logitech", 2, Decimal("50000")),
))
CARRITO_VACIO = Carrito(())
DATOS = {"customer_name": "Juan Pérez", "customer_phone": "3001234567", "city": "Medellín",
         "customer_address": "Cra 1 # 2-3"}
ORDEN = {
    "order_number": 101, "created_at": "2026-10-01T10:00:00", "shipping_status": "PENDIENTE_DESPACHO",
    "payment_status": "CONTRAENTREGA", "Metodo_pago": "contra_entrega", "total_amount": 3600000,
    "items": [{"nombre": "Laptop HP Ultra", "cantidad": 1, "subtotal": 3500000}],
    "customer_name": "Juan Pérez", "customer_phone": "3001234567", "customer_address": "Cra 1 # 2-3",
    "city": "Medellín", "notes": None, "session_id": SESION, "payment_reference": "ref-secreta",
}

# ---------------------------------------------------------------------------
# 1) El agente: schema, funciones y validación sincronizados; prompt fiel.
# ---------------------------------------------------------------------------
problemas = verificar_coherencia_tools(ventas_agent.TOOLS_SCHEMA, tools(), ARGUMENTOS_VENTAS)
assert not problemas, "\n".join(problemas)
for t in ventas_agent.TOOLS_SCHEMA:
    assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", t["function"]["name"]), (
        f"Nombre de tool inválido para la API: {t['function']['name']}"
    )
assert ventas_agent.SYSTEM_PROMPT.startswith(ventas_agent.ORIGINAL_SYSTEM_PROMPT), "Prompt original intacto al inicio"
assert "{Añadir_elemento}" in ventas_agent.ORIGINAL_SYSTEM_PROMPT
assert "Anadir_elemento" in ventas_agent.NOTA_HERRAMIENTAS
crear_schema = next(t for t in ventas_agent.TOOLS_SCHEMA if t["function"]["name"] == "Crear_orden")
assert crear_schema["function"]["parameters"]["properties"]["metodo_pago"]["enum"] == ["contra_entrega", "en_linea"]
print("✅ Agente: schema, funciones y Pydantic sincronizados; nombres válidos (sin ñ); prompt original intacto.")

# ---------------------------------------------------------------------------
# 2) Validación: lo que el modelo no puede enviar.
# ---------------------------------------------------------------------------
with patch.object(carrito_repository, "leer") as leer_mock:
    r = tools()["Crear_orden"](**DATOS, metodo_pago="contra entrega")
assert r.startswith("ERROR: argumentos inválidos para Crear_orden") and "metodo_pago" in r, r
assert leer_mock.call_count == 0, "Un método de pago mal escrito no ejecuta nada (antes caía al pago en línea)"

r = tools()["Anadir_elemento"](producto_id="A01")
assert "ERROR: argumentos inválidos" in r and "UUID" in r, r
r = tools()["Modificar_elemento"](producto_id=PRODUCTO, cantidad_nueva=0)
assert "ERROR: argumentos inválidos" in r, "La cantidad final mínima es 1; para quitar está Eliminar_elemento"
r = tools()["Modificar_orden"](order_number=101)
assert "al menos un dato personal" in r, r

with (
    patch.object(carrito_repository, "leer", return_value=CARRITO),
    patch.object(confirmacion, "_propuestas_pendientes", {}),
):
    r = tools()["Crear_orden"](**DATOS, metodo_pago="contra_entrega", notes="x", shipping_status="DESPACHADO",
                               payment_status="APROBADO")
assert "CONFIRMACION_PENDIENTE" in r, "Los campos de solo lectura se descartan sin romper la llamada"
print("✅ Validación: método de pago cerrado, ids UUID, cantidades >= 1 y campos de solo lectura descartados.")

# ---------------------------------------------------------------------------
# 3) Inventario: productos, resumen para afinar, sin resultados y errores.
# ---------------------------------------------------------------------------
assert pesos(Decimal("3500000")) == "$3.500.000" and pesos(132000.4) == "$132.000" and pesos(Decimal("0.5")) == "$1"

laptop = Producto(PRODUCTO, "Laptop HP Ultra", "16GB RAM", Decimal("3500000"), CATEGORIA, "HP")
with patch.object(inventario_repository, "buscar_productos",
                  return_value=ResultadoBusqueda(total=1, productos=(laptop,))) as buscar_mock:
    r = tools()["Inventario"](category_id=CATEGORIA, termino="portátil hp", precio_max=4000000)
assert buscar_mock.call_args.args == (CATEGORIA, "portátil hp", Decimal("4000000"), None)
assert r.startswith("PRODUCTOS (1 de 1") and "$3.500.000" in r
assert f"producto_id (interno, nunca mostrar): {PRODUCTO}" in r

muchos = ResultadoBusqueda(
    total=125, productos=(), precio_min=Decimal("132000"), precio_max=Decimal("7222000"),
    marcas=(OpcionMarca("Sony", 5, Decimal("1385000"), Decimal("6951000")),
            OpcionMarca("Acer", 11, Decimal("174000"), Decimal("5385000"))),
)
with patch.object(inventario_repository, "buscar_productos", return_value=muchos):
    r = tools()["Inventario"](category_id=CATEGORIA, termino="audífonos")
assert r.startswith("HAY_QUE_AFINAR: 125 productos") and "de $132.000 a $7.222.000" in r
assert "• Sony: 5 opciones, de $1.385.000 a $6.951.000" in r and "NO muestres productos" in r
assert "orden='mas_caros'" in r and "nunca lo deduzcas de estos rangos" in r, (
    "Para 'el más caro' debe usar orden, no adivinar por los rangos de las marcas top"
)
assert "producto_id" not in r

caro = Producto(PRODUCTO, "Auriculares Razer V2", "Cancelación de ruido", Decimal("6291000"), CATEGORIA, "Razer")
with patch.object(inventario_repository, "buscar_productos",
                  return_value=ResultadoBusqueda(total=125, productos=(caro,))) as buscar_mock:
    r = tools()["Inventario"](category_id=CATEGORIA, termino="audífonos", orden="mas_caros")
assert buscar_mock.call_args.args[3] == "mas_caros"
assert r.startswith("PRODUCTOS (1 de 125 que coinciden, del más caro al más barato):"), r
r = tools()["Inventario"](category_id=CATEGORIA, termino="audífonos", orden="caros")
assert "ERROR: argumentos inválidos" in r, "orden solo acepta mas_baratos o mas_caros"

otras = ResultadoBusqueda(total=1, productos=(laptop,), en_otras_categorias=True)
with patch.object(inventario_repository, "buscar_productos", return_value=otras):
    r = tools()["Inventario"](category_id=CATEGORIA, termino="laptop")
assert r.startswith("(interno) En la categoría elegida no había coincidencias")

with patch.object(inventario_repository, "buscar_productos", return_value=ResultadoBusqueda(total=0, productos=())):
    r = tools()["Inventario"](category_id=CATEGORIA, termino="nevera")
assert r.startswith("SIN_RESULTADOS") and "Nunca inventes" in r

with patch.object(inventario_repository, "buscar_productos", side_effect=ConnectionError("caído")):
    r = tools()["Inventario"](category_id=CATEGORIA, termino="mouse")
assert r.startswith("ERROR:") and "inconveniente técnico" in r

with patch.object(inventario_repository, "listar_categorias",
                  return_value=(inventario_repository.Categoria(CATEGORIA, "Computadores y Laptops"),)):
    r = tools()["Categorias_inventario"]()
assert f"category_id={CATEGORIA} | nombre=Computadores y Laptops" in r
print("✅ Inventario: productos con precio en pesos, HAY_QUE_AFINAR con marcas y rangos, otras categorías, "
      "sin resultados y error técnico.")

# ---------------------------------------------------------------------------
# 4) Carrito: cada cambio devuelve el carrito con el total del código.
# ---------------------------------------------------------------------------
with (
    patch.object(carrito_repository, "agregar", return_value=3) as agregar_mock,
    patch.object(carrito_repository, "leer", return_value=CARRITO),
):
    r = tools()["Anadir_elemento"](producto_id=PRODUCTO)
assert agregar_mock.call_args.args == (SESION, PRODUCTO, 1), "Cantidad 1 por defecto y session_id inyectado"
assert r.startswith("OK: se agregaron 1 unidad(es); ahora hay 3") and "TOTAL: $3.600.000" in r

stock = ErrorNegocio("STOCK_INSUFICIENTE", '{"stock": 5, "en_carrito": 3}')
with patch.object(carrito_repository, "agregar", side_effect=stock):
    r = tools()["Anadir_elemento"](producto_id=PRODUCTO, cantidad=4)
assert r.startswith("ERROR:") and "como máximo 2" in r, r

with patch.object(carrito_repository, "eliminar", side_effect=ErrorNegocio("NO_ESTA_EN_CARRITO")):
    r = tools()["Eliminar_elemento"](producto_id=PRODUCTO)
assert "no está en el carrito" in r

with patch.object(carrito_repository, "leer", return_value=CARRITO_VACIO):
    assert tools()["Consultar_carrito"]().startswith("CARRITO_VACIO")
print("✅ Carrito: suma con session_id inyectado, total calculado por el código y errores de stock accionables.")

# ---------------------------------------------------------------------------
# 5) Crear orden: confirmación de dos turnos y flujo por método de pago.
# ---------------------------------------------------------------------------
with (
    patch.object(carrito_repository, "leer", return_value=CARRITO_VACIO),
    patch.object(confirmacion, "_propuestas_pendientes", {}) as pendientes,
):
    r = tools()["Crear_orden"](**DATOS, metodo_pago="contra_entrega")
assert "carrito del cliente está vacío" in r and not pendientes, "Carrito vacío: ni siquiera se pide confirmar"

with (
    patch.object(carrito_repository, "leer", return_value=CARRITO),
    patch.object(pagos, "pasarela_disponible", return_value=False),
    patch.object(confirmacion, "_propuestas_pendientes", {}) as pendientes,
):
    r = tools()["Crear_orden"](**DATOS, metodo_pago="en_linea")
assert "pago en línea no está disponible" in r and not pendientes, (
    "Sin pasarela, se avisa ANTES de pedir confirmación"
)

with (
    patch.object(carrito_repository, "leer", return_value=CARRITO) as leer_mock,
    patch.object(ordenes_repository, "crear_desde_carrito", return_value=ORDEN) as crear_mock,
    patch.object(confirmacion, "_propuestas_pendientes", {}),
):
    primera = tools("t1")["Crear_orden"](**DATOS, metodo_pago="contra_entrega")
    reintento = tools("t1")["Crear_orden"](**DATOS, metodo_pago="contra_entrega")
    assert crear_mock.call_count == 0, "Ni la primera llamada ni el reintento del mismo turno escriben"
    confirmada = tools("t2")["Crear_orden"](**DATOS, metodo_pago="contra_entrega")
assert primera.startswith("CONFIRMACION_PENDIENTE") and "TOTAL: $3.600.000" in primera
assert "Método de pago: contra_entrega" in primera and "no se puede cambiar" in primera
assert reintento.startswith("CONFIRMACION_PENDIENTE")
assert crear_mock.call_count == 1 and crear_mock.call_args.args == (
    SESION, "Juan Pérez", "3001234567", "Cra 1 # 2-3", "Medellín", "contra_entrega")
assert confirmada.startswith("OK: PEDIDO REGISTRADO (pago contra entrega)") and "Pedido #101" in confirmada
assert "ref-secreta" not in confirmada, "La referencia de pago es interna"

otro_carrito = Carrito((LineaCarrito(PRODUCTO, "Laptop HP Ultra", 2, Decimal("3500000")),))
with (
    patch.object(carrito_repository, "leer", side_effect=[CARRITO, otro_carrito]),
    patch.object(ordenes_repository, "crear_desde_carrito") as crear_mock,
    patch.object(confirmacion, "_propuestas_pendientes", {}),
):
    tools("t1")["Crear_orden"](**DATOS, metodo_pago="contra_entrega")
    r = tools("t2")["Crear_orden"](**DATOS, metodo_pago="contra_entrega")
assert r.startswith("CONFIRMACION_PENDIENTE") and crear_mock.call_count == 0, (
    "Si el carrito cambió entre la propuesta y el 'sí', hay que volver a confirmar"
)

with (
    patch.object(carrito_repository, "leer", return_value=CARRITO),
    patch.object(ordenes_repository, "crear_desde_carrito",
                 side_effect=ErrorNegocio("STOCK_INSUFICIENTE", "Laptop HP Ultra")),
    patch.object(confirmacion, "_propuestas_pendientes", {}),
):
    tools("t1")["Crear_orden"](**DATOS, metodo_pago="contra_entrega")
    r = tools("t2")["Crear_orden"](**DATOS, metodo_pago="contra_entrega")
assert r.startswith("ERROR:") and "Laptop HP Ultra" in r and "No se creó nada" in r
print("✅ Crear orden: carrito vacío y pago en línea no disponible se avisan antes; confirmación en dos turnos; "
      "carrito cambiado reconfirma; stock agotado al final se explica.")

# Pago en línea con pasarela disponible: primero el link, después la orden.
orden_en_linea = {**ORDEN, "payment_status": "PENDIENTE", "Metodo_pago": "en_linea", "payment_link": "https://mp/1"}
eventos = []
with (
    patch.object(carrito_repository, "leer", return_value=CARRITO),
    patch.object(pagos, "pasarela_disponible", return_value=True),
    patch.object(pagos, "crear_link_pago",
                 side_effect=lambda ref, carrito, vence: eventos.append(("link", ref, vence)) or "https://mp/1"),
    patch.object(ordenes_repository, "crear_desde_carrito",
                 side_effect=lambda *a, **kw: eventos.append(("orden", kw)) or orden_en_linea),
    patch.object(confirmacion, "_propuestas_pendientes", {}),
):
    tools("t1")["Crear_orden"](**DATOS, metodo_pago="en_linea")
    r = tools("t2")["Crear_orden"](**DATOS, metodo_pago="en_linea")
assert [e[0] for e in eventos] == ["link", "orden"], "Primero el link, después la orden atómica"
_, referencia, vence_link = eventos[0]
kwargs_orden = eventos[1][1]
assert kwargs_orden["referencia_pago"] == referencia and kwargs_orden["link_pago"] == "https://mp/1"
assert kwargs_orden["total_esperado"] == Decimal("3600000"), "La orden se crea con el MISMO total del link"
assert kwargs_orden["reserva_expira_en"] - vence_link == ordenes_tools.MARGEN_LINK_ANTES_DE_RESERVA
assert "https://mp/1" in r and "Nunca confirmes un pago" in r
print("✅ Pago en línea: link primero, orden después con la misma referencia y total; "
      "el link vence antes que la reserva.")

# ---------------------------------------------------------------------------
# 6) Consultar orden.
# ---------------------------------------------------------------------------
with patch.object(ordenes_repository, "listar", return_value=[]) as listar_mock:
    assert tools()["Consultar_orden"]().startswith("SIN_PEDIDOS")
assert listar_mock.call_args.args == (SESION,), "Sin número: solo los no cancelados"

with (
    patch.object(ordenes_repository, "leer", return_value=None),
    patch.object(ordenes_repository, "listar", return_value=[ORDEN]) as listar_mock,
):
    r = tools()["Consultar_orden"](order_number=999)
assert listar_mock.call_args.kwargs == {"incluir_canceladas": True}, "Número inexistente: listado completo"
assert r.startswith("ORDEN_NO_ENCONTRADA") and "Pedido #101" in r

with patch.object(ordenes_repository, "leer", return_value=orden_en_linea):
    r = tools()["Consultar_orden"](order_number=101)
assert "ESTADO DE PAGO: PENDIENTE" in r and "Link de pago: https://mp/1" in r
assert "ref-secreta" not in r and "payment_reference" not in r
print("✅ Consultar orden: activos, listado completo si el número no existe, estado de pago y sin datos internos.")

# ---------------------------------------------------------------------------
# 7) Modificar y cancelar: la regla se verifica antes de pedir confirmación.
# ---------------------------------------------------------------------------
nueva = {**ORDEN, "order_number": 102}
with (
    patch.object(ordenes_repository, "listar", return_value=[nueva, ORDEN]),
    patch.object(confirmacion, "_propuestas_pendientes", {}) as pendientes,
):
    r = tools()["Modificar_orden"](order_number=101, city="Bogotá")
assert "ÚLTIMO pedido" in r and "#102" in r and not pendientes, "No se pide confirmar algo imposible"

despachada = {**ORDEN, "shipping_status": "DESPACHADO"}
with patch.object(ordenes_repository, "listar", return_value=[despachada]):
    r = tools()["Cancelar_orden"](order_number=101)
assert "ya no está pendiente de despacho" in r and "DESPACHADO" in r

pagada = {**ORDEN, "payment_status": "APROBADO"}
with (
    patch.object(ordenes_repository, "listar", return_value=[pagada]),
    patch.dict(os.environ, {"CONTACTO_EMPRESA": "WhatsApp 300 000 0000"}),
):
    r = tools()["Cancelar_orden"](order_number=101)
assert "pago APROBADO" in r and "WhatsApp 300 000 0000" in r and "No prometas reembolsos" in r

with (
    patch.object(ordenes_repository, "listar", return_value=[ORDEN]),
    patch.object(ordenes_repository, "modificar_datos", return_value={**ORDEN, "city": "Bogotá"}) as mod_mock,
    patch.object(confirmacion, "_propuestas_pendientes", {}),
):
    pendiente = tools("t1")["Modificar_orden"](order_number=101, city="Bogotá")
    r = tools("t2")["Modificar_orden"](order_number=101, city="Bogotá")
assert pendiente.startswith("CONFIRMACION_PENDIENTE") and "Ciudad: Medellín -> Bogotá" in pendiente
assert mod_mock.call_args.args == (SESION, 101, None, None, None, "Bogotá"), "Solo cambia lo enviado"
assert r.startswith("OK: PEDIDO ACTUALIZADO")

with (
    patch.object(ordenes_repository, "listar", return_value=[ORDEN]),
    patch.object(ordenes_repository, "cancelar", return_value={}) as cancelar_mock,
    patch.object(confirmacion, "_propuestas_pendientes", {}),
):
    assert tools("t1")["Cancelar_orden"](order_number=101).startswith("CONFIRMACION_PENDIENTE")
    r = tools("t2")["Cancelar_orden"](order_number=101)
assert cancelar_mock.call_args.args == (SESION, 101) and r.startswith("OK: PEDIDO #101 CANCELADO")
print("✅ Modificar/cancelar: solo la última y pendiente (antes de confirmar), lo pagado remite a la empresa, "
      "confirmación en dos turnos.")

# ---------------------------------------------------------------------------
# 8) Confirmación compartida: cada agente tiene su propia propuesta.
# ---------------------------------------------------------------------------
with patch.object(confirmacion, "_propuestas_pendientes", {}):
    assert confirmacion.requiere_confirmacion("servicio_tecnico", SESION, "t1", ("cita",))
    assert confirmacion.requiere_confirmacion("ventas", SESION, "t1", ("orden",))
    assert not confirmacion.requiere_confirmacion("servicio_tecnico", SESION, "t2", ("cita",)), (
        "La orden pendiente de Ventas no pisa la cita pendiente de Servicio Técnico"
    )
    assert not confirmacion.requiere_confirmacion("ventas", SESION, "t2", ("orden",))
print("✅ Confirmación compartida: una cita y una orden pendientes del mismo cliente conviven.")

# ---------------------------------------------------------------------------
# 9) run(): contexto del agente y session_id/turno inyectados.
# ---------------------------------------------------------------------------
with patch.object(ventas_agent, "run_agent_loop", return_value=("¡Claro!", [{"role": "assistant"}])) as loop_mock:
    texto, historial = ventas_agent.run("quiero un mouse", SESION, historial=[], run_id="run-9", presupuesto="P")
kwargs = loop_mock.call_args.kwargs
assert kwargs["contexto"] == {"agente": "Ventas", "session_id": SESION, "run_id": "run-9", "presupuesto": "P"}
assert kwargs["tools_schema"] is ventas_agent.TOOLS_SCHEMA and kwargs["system_prompt"].startswith(
    ventas_agent.SYSTEM_PROMPT)
assert set(kwargs["tool_functions"]) == {t["function"]["name"] for t in ventas_agent.TOOLS_SCHEMA}
assert texto == "¡Claro!" and historial == [{"role": "assistant"}]
print("✅ run(): agente 'Ventas', run_id como turno de confirmación y todas las tools disponibles.")

print("\n✅ Todos los tests de las tools de Ventas pasaron.")
