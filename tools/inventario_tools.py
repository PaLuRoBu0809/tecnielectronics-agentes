"""
tools/inventario_tools.py

{Categorias_inventario} y {Inventario} del Agente de Ventas. Solo
orquestan: la búsqueda (sinónimos, plan B en todo el catálogo, resumen
cuando hay demasiados resultados, orden por precio) vive en
`tools/inventario_repository.py` y el formato de precios en
`tools/formato_ventas.py`.

Nunca lanzan excepciones: devuelven texto para el modelo, con "ERROR:" si
algo falla.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from tools import inventario_repository
from tools.formato_ventas import error_tecnico, pesos

_SENTIDO = {"mas_baratos": "del más barato al más caro", "mas_caros": "del más caro al más barato"}


def categorias_inventario() -> str:
    try:
        categorias = inventario_repository.listar_categorias()
    except Exception as exc:
        return error_tecnico("consultar las categorías", exc)
    if not categorias:
        return "ERROR: No hay categorías registradas en el inventario. Estado: fallido"
    lineas = "\n".join(f"category_id={c.id} | nombre={c.nombre}" for c in categorias)
    return (
        "CATEGORIAS (copia el category_id literalmente; los ids son internos, nunca los muestres):\n"
        + lineas
    )


def _texto_afinar(resultado, termino: str) -> str:
    opciones = "\n".join(
        f"• {m.marca}: {m.cantidad} opciones, de {pesos(m.precio_min)} a {pesos(m.precio_max)}"
        for m in resultado.marcas
    )
    return (
        f"HAY_QUE_AFINAR: {resultado.total} productos coinciden con '{termino}', con precios de "
        f"{pesos(resultado.precio_min)} a {pesos(resultado.precio_max)}. Son demasiados para mostrar: "
        "NO muestres productos todavía. Cuéntale al cliente cuántas opciones hay y ofrécele estas "
        "marcas con su rango de precios (son las que más opciones tienen; hay más marcas):\n"
        f"{opciones}\n"
        "Pregúntale qué marca prefiere o cuál es su presupuesto. Después vuelve a llamar Inventario "
        "con la marca elegida dentro del termino y/o con precio_max = su presupuesto. Si el cliente "
        "pide lo más barato o lo más caro (o no quiere afinar), llama con orden='mas_baratos' u "
        "orden='mas_caros'; nunca lo deduzcas de estos rangos."
    )


def _texto_productos(resultado, orden: Optional[str]) -> str:
    lineas = "\n".join(
        f"{i}) {p.nombre} | marca: {p.marca} | precio: {pesos(p.precio)} | {p.descripcion} | "
        f"producto_id (interno, nunca mostrar): {p.id}"
        for i, p in enumerate(resultado.productos, start=1)
    )
    sentido = f", {_SENTIDO[orden]}" if orden else ""
    return f"PRODUCTOS ({len(resultado.productos)} de {resultado.total} que coinciden{sentido}):\n{lineas}"


def inventario(
    category_id: str,
    termino: str,
    precio_max: Optional[Decimal] = None,
    orden: Optional[str] = None,
) -> str:
    try:
        resultado = inventario_repository.buscar_productos(category_id, termino, precio_max, orden)
    except Exception as exc:
        return error_tecnico("consultar el inventario", exc)

    if not resultado.total:
        presupuesto = f" con precio de hasta {pesos(precio_max)}" if precio_max is not None else ""
        return (
            f"SIN_RESULTADOS: no hay productos disponibles que coincidan con '{termino}'{presupuesto}, "
            "ni en la categoría elegida ni en el resto del catálogo. Díselo al cliente y ofrécele "
            "ajustar la búsqueda (otra palabra, otra marca u otro presupuesto). Nunca inventes productos."
        )
    aviso = (
        "(interno) En la categoría elegida no había coincidencias: estos resultados vienen de "
        "otras categorías del catálogo. No digas que pertenecen a la categoría que pidió el cliente.\n"
        if resultado.en_otras_categorias else ""
    )
    if not resultado.productos:
        return aviso + _texto_afinar(resultado, termino)
    return aviso + _texto_productos(resultado, orden)
