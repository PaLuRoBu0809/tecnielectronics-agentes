"""
tests/test_ventas_repositorios.py

Fase B del Agente de Ventas: repositorios de inventario, carrito y órdenes
(`tools/inventario_repository.py`, `tools/carrito_repository.py`,
`tools/ordenes_repository.py`) y las piezas nuevas de
`tools/supabase_client.py` (`rpc`, `error_de_negocio`, `rpc_con_reglas`).

Sin tocar Supabase real: las reglas que viven en SQL (stock, última orden,
atomicidad) se prueban contra Postgres en `tests/test_integracion_postgres.py`.
Aquí se prueba lo que vive en Python: la expansión de sinónimos, el plan B
de la búsqueda, la traducción de errores y los totales.

Corre con:
    python tests/test_ventas_repositorios.py
"""
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake-key")

from tools import carrito_repository, inventario_repository, ordenes_repository, supabase_client  # noqa: E402
from tools.errores_negocio import ErrorNegocio  # noqa: E402
from tools.inventario_repository import expandir_termino, formas_singulares, normalizar  # noqa: E402


def _respuesta(status_code: int, cuerpo):
    resp = MagicMock(status_code=status_code, content=b"x")
    resp.json.return_value = cuerpo
    if status_code >= 400:
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError(response=resp)
    return resp


SINONIMOS = [
    ["portatil", "laptop", "notebook"],
    ["audifono", "auricular"],
    ["parlante", "altavoz"],
    ["monitor", "pantalla"],
    ["computador", "laptop"],
]

# ---------------------------------------------------------------------------
# 1) Normalización y expansión del término (funciones puras)
# ---------------------------------------------------------------------------
assert normalizar("¡Portátil  HP-15!") == "portatil hp 15"
assert normalizar("Año Ñandú") == "ano nandu", "La ñ pierde la tilde, igual que unaccent en Postgres"
assert formas_singulares("monitores") == ["monitores", "monitore", "monitor"]
assert formas_singulares("laptops") == ["laptops", "laptop"]
assert formas_singulares("ssd") == ["ssd"] and formas_singulares("16gbs") == ["16gbs"]
assert formas_singulares("mouse") == ["mouse"]
print("✅ normalizar y formas_singulares: tildes, símbolos, plurales con -s y -es.")

assert expandir_termino("portátil HP", SINONIMOS) == [["portatil", "laptop", "notebook"], ["hp"]]
assert expandir_termino("Portátiles", SINONIMOS) == [["portatil", "laptop", "notebook"]], "Plural -es"
assert expandir_termino("parlantes", SINONIMOS)[0][1:] == ["parlante", "altavoz"], (
    "parlantes = parlante + s: el sinónimo se encuentra aunque la forma de búsqueda sea 'parlant'"
)
assert expandir_termino("monitores", SINONIMOS) == [["monitor", "pantalla"]], "monitores = monitor + es"
assert expandir_termino("auriculares", SINONIMOS) == [["auricular", "audifono"]]
assert expandir_termino("un audífono de diadema", SINONIMOS) == [["audifono", "auricular"], ["diadema"]], (
    "Las palabras vacías ('un', 'de') se ignoran"
)
assert expandir_termino("laptop laptops", SINONIMOS) == [["laptop", "portatil", "notebook", "computador"]], (
    "Palabra repetida cuenta una vez; si está en varios grupos se unen"
)
assert expandir_termino("", SINONIMOS) == [] and expandir_termino("de la", SINONIMOS) == []
assert expandir_termino("tablet", []) == [["tablet"]], "Sin sinónimos, la palabra sola"
print("✅ expandir_termino: sinónimos siempre, plurales, palabras vacías, repetidas y grupos unidos.")

# ---------------------------------------------------------------------------
# 2) Búsqueda: categoría primero, todo el catálogo como plan B
# ---------------------------------------------------------------------------
FILA_LAPTOP = {"id": "p1", "name": "Laptop HP Ultra", "description": None, "sale_price": 3500000,
               "category_id": "c-otra", "marca": "HP"}


def _resumen(productos=()):
    """Lo que devuelve buscar_productos_venta cuando son pocos (se muestran todos)."""
    precios = [f["sale_price"] for f in productos]
    return {"total": len(productos), "precio_min": min(precios, default=None),
            "precio_max": max(precios, default=None), "marcas": [], "productos": list(productos)}


VACIO = _resumen()
inventario_repository._sinonimos.invalidar()

with (
    patch.object(inventario_repository, "sinonimos", return_value=SINONIMOS),
    patch.object(supabase_client, "rpc", side_effect=[_resumen([FILA_LAPTOP])]) as rpc_mock,
):
    resultado = inventario_repository.buscar_productos("c-laptops", "portátil hp")
assert rpc_mock.call_count == 1, "Con resultados en la categoría no se busca en todo el catálogo"
nombre, params = rpc_mock.call_args.args
assert nombre == "buscar_productos_venta" and rpc_mock.call_args.kwargs == {"solo_lectura": True}
assert params == {"p_grupos": [["portatil", "laptop", "notebook"], ["hp"]], "p_category_id": "c-laptops",
                  "p_precio_max": None, "p_umbral": 5, "p_orden": None}, params
assert not resultado.en_otras_categorias and not resultado.hay_que_afinar
producto = resultado.productos[0]
assert (producto.nombre, producto.precio, producto.descripcion, producto.marca) == (
    "Laptop HP Ultra", Decimal("3500000"), "", "HP")

with (
    patch.object(inventario_repository, "sinonimos", return_value=SINONIMOS),
    patch.object(supabase_client, "rpc", side_effect=[VACIO, _resumen([FILA_LAPTOP])]) as rpc_mock,
):
    resultado = inventario_repository.buscar_productos("c-celulares", "portátil hp")
assert [c.args[1]["p_category_id"] for c in rpc_mock.call_args_list] == ["c-celulares", None]
assert resultado.en_otras_categorias and len(resultado.productos) == 1

with (
    patch.object(inventario_repository, "sinonimos", return_value=SINONIMOS),
    patch.object(supabase_client, "rpc", side_effect=[VACIO, VACIO]),
):
    resultado = inventario_repository.buscar_productos("c-celulares", "nevera")
assert resultado.productos == () and resultado.total == 0 and not resultado.en_otras_categorias
assert not resultado.hay_que_afinar
print("✅ buscar_productos: categoría primero; si no hay, todo el catálogo marcando en_otras_categorias.")

# Muchos resultados: resumen con opciones por marca y hay_que_afinar.
marcas = [{"marca": "Sony", "cantidad": 70, "precio_min": 132000.0, "precio_max": 7222000},
          {"marca": "Acer", "cantidad": 55, "precio_min": 174000, "precio_max": 5385000}]
muchos = {"total": 125, "precio_min": 132000.0, "precio_max": 7222000, "marcas": marcas, "productos": []}
with (
    patch.object(inventario_repository, "sinonimos", return_value=SINONIMOS),
    patch.object(supabase_client, "rpc", side_effect=[muchos]),
):
    resultado = inventario_repository.buscar_productos("c-audio", "audífonos")
assert resultado.hay_que_afinar and resultado.total == 125
assert resultado.marcas[0] == inventario_repository.OpcionMarca("Sony", 70, Decimal("132000.0"), Decimal("7222000"))
assert (resultado.precio_min, resultado.precio_max) == (Decimal("132000.0"), Decimal("7222000"))

with (
    patch.object(inventario_repository, "sinonimos", return_value=SINONIMOS),
    patch.object(supabase_client, "rpc", side_effect=[_resumen([FILA_LAPTOP])]) as rpc_mock,
):
    inventario_repository.buscar_productos("c-audio", "audífonos", precio_max=Decimal("500000.50"),
                                           orden="mas_caros")
params = rpc_mock.call_args.args[1]
assert params["p_precio_max"] == "500000.50" and params["p_orden"] == "mas_caros", params
print("✅ Muchos resultados: resumen con total, rango y marcas; presupuesto y orden llegan a la búsqueda.")

# ---------------------------------------------------------------------------
# 3) Caché de sinónimos y categorías
# ---------------------------------------------------------------------------
reloj = MagicMock(return_value=0.0)
cache = inventario_repository._Cache(MagicMock(side_effect=["v1", "v2"]), reloj=reloj)
assert cache.obtener() == "v1" and cache.obtener() == "v1", "Dentro del plazo no se vuelve a cargar"
reloj.return_value = inventario_repository.SEGUNDOS_CACHE + 1
assert cache.obtener() == "v2", "Vencido el plazo se recarga (la empresa editó los sinónimos)"

inventario_repository._categorias.invalidar()
with patch.object(supabase_client, "get_rows", return_value=[{"id": "c1", "name": "Audio"}]) as get_mock:
    assert inventario_repository.listar_categorias() == (inventario_repository.Categoria("c1", "Audio"),)
    inventario_repository.listar_categorias()
assert get_mock.call_count == 1
print("✅ Caché: categorías y sinónimos se leen una vez y se recargan al vencer.")

# ---------------------------------------------------------------------------
# 4) supabase_client: rpc, error_de_negocio y rpc_con_reglas
# ---------------------------------------------------------------------------
error_stock = _respuesta(400, {"code": "P0001", "message": "STOCK_INSUFICIENTE", "details": '{"stock": 3}'})
error_otro_400 = _respuesta(400, {"code": "22P02", "message": "invalid input value for enum"})

with patch.object(supabase_client.requests, "post", return_value=error_stock):
    try:
        supabase_client.rpc_con_reglas("agregar_al_carrito", {})
        raise AssertionError("Debía lanzar ErrorNegocio")
    except ErrorNegocio as exc:
        assert exc.codigo == "STOCK_INSUFICIENTE" and exc.detalle_json() == {"stock": 3}

with patch.object(supabase_client.requests, "post", return_value=error_otro_400):
    try:
        supabase_client.rpc_con_reglas("crear_orden_desde_carrito", {})
        raise AssertionError("Debía propagar el HTTPError")
    except ErrorNegocio:
        raise AssertionError("Un error que no es de negocio no debe disfrazarse de regla del negocio") from None
    except requests.exceptions.HTTPError:
        pass

with (
    patch.object(supabase_client.requests, "post", side_effect=requests.exceptions.Timeout()) as post_mock,
    patch.object(supabase_client, "_esperar_backoff"),
):
    try:
        supabase_client.rpc("crear_orden_desde_carrito", {})
        raise AssertionError("Debía propagar el timeout")
    except requests.exceptions.Timeout:
        pass
assert post_mock.call_count == 1, "Una RPC que escribe nunca se reintenta"

with (
    patch.object(supabase_client.requests, "post",
                 side_effect=[requests.exceptions.ConnectionError(), _respuesta(200, [{"id": 1}])]) as post_mock,
    patch.object(supabase_client, "_esperar_backoff"),
):
    assert supabase_client.rpc("buscar_productos_venta", {}, solo_lectura=True) == [{"id": 1}]
assert post_mock.call_count == 2, "Una RPC de solo lectura sí se reintenta"
assert post_mock.call_args.args[0] == "https://fake.supabase.co/rest/v1/rpc/buscar_productos_venta"
print("✅ supabase_client: errores de negocio -> ErrorNegocio; otros errores intactos; "
      "solo las lecturas se reintentan.")

# ---------------------------------------------------------------------------
# 5) Carrito
# ---------------------------------------------------------------------------
filas_carrito = [
    {"producto_id": "p1", "cantidad": 2, "products": {"name": "Mouse", "sale_price": 50000.5}},
    {"producto_id": "p2", "cantidad": 1, "products": {"name": "Laptop", "sale_price": 3000000}},
]
with patch.object(supabase_client, "get_rows", return_value=filas_carrito) as get_mock:
    carrito = carrito_repository.leer("3001")
assert get_mock.call_args.kwargs["params"]["session_id"] == "eq.3001"
assert carrito.lineas[0].subtotal == Decimal("100001.0")
assert carrito.total == Decimal("3100001.0"), "Total exacto con Decimal, calculado por el código"
assert not carrito.vacio

with patch.object(supabase_client, "get_rows", return_value=[]):
    assert carrito_repository.leer("3001").vacio
    assert carrito_repository.leer("3001").total == Decimal(0)

with patch.object(supabase_client, "rpc_con_reglas", return_value=3) as rpc_mock:
    assert carrito_repository.agregar("3001", "p1", 1) == 3
assert rpc_mock.call_args.args == (
    "agregar_al_carrito", {"p_session_id": "3001", "p_producto_id": "p1", "p_cantidad": 1})

with patch.object(supabase_client, "delete_rows", return_value=[]):
    try:
        carrito_repository.eliminar("3001", "p9")
        raise AssertionError("Eliminar algo que no estaba debe avisarse")
    except ErrorNegocio as exc:
        assert exc.codigo == "NO_ESTA_EN_CARRITO"
with patch.object(supabase_client, "delete_rows", return_value=[{"producto_id": "p1"}]) as delete_mock:
    carrito_repository.eliminar("3001", "p1")
assert delete_mock.call_args.kwargs["params"] == {"session_id": "eq.3001", "producto_id": "eq.p1"}
print("✅ Carrito: totales con Decimal, suma vía RPC, eliminar avisa si el producto no estaba.")

# ---------------------------------------------------------------------------
# 6) Órdenes
# ---------------------------------------------------------------------------
vence = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
with patch.object(supabase_client, "rpc_con_reglas", return_value={"order_number": 101}) as rpc_mock:
    orden = ordenes_repository.crear_desde_carrito(
        "3001", "Juan", "3001234567", "Cra 1", "Medellín", "en_linea",
        total_esperado=Decimal("3100001.0"), referencia_pago="ref-1", link_pago="https://mp/1",
        reserva_expira_en=vence,
    )
assert orden == {"order_number": 101}
nombre, params = rpc_mock.call_args.args
assert nombre == "crear_orden_desde_carrito"
assert params["p_total_esperado"] == "3100001.0", "El total viaja como texto: sin pérdida de precisión"
assert params["p_reserva_expira_en"] == "2026-10-02T12:00:00+00:00"
assert params["p_session_id"] == "3001" and params["p_metodo_pago"] == "en_linea"

with patch.object(supabase_client, "get_rows", return_value=[]) as get_mock:
    ordenes_repository.listar("3001")
    assert get_mock.call_args.kwargs["params"]["shipping_status"] == "neq.CANCELADO"
    ordenes_repository.listar("3001", incluir_canceladas=True)
    assert "shipping_status" not in get_mock.call_args.kwargs["params"]
    assert ordenes_repository.leer("3001", 7) is None
    assert get_mock.call_args.kwargs["params"]["session_id"] == "eq.3001", "Nunca se lee la orden de otro cliente"

with patch.object(supabase_client, "rpc", return_value=[]):
    assert ordenes_repository.aplicar_estado_pago("ref-1", "APROBADO") is None, "Sin cambio real: no se notifica"
with patch.object(supabase_client, "rpc", return_value=[{"order_number": 101}]):
    assert ordenes_repository.aplicar_estado_pago("ref-1", "APROBADO") == {"order_number": 101}
print("✅ Órdenes: parámetros exactos, total sin pérdida de precisión, filtro por cliente "
      "y avisos de pago una sola vez.")

print("\n✅ Todos los tests de los repositorios de ventas pasaron.")
