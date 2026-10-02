"""
tools/inventario_repository.py

Acceso de SOLO LECTURA al inventario: categorías, sinónimos de búsqueda y
búsqueda de productos. Es la frontera con la fuente del inventario: hoy
Supabase; cuando la empresa migre a Siigo, se reescribe este módulo por
dentro y las tools del Agente de Ventas no cambian.

Búsqueda (`buscar_productos`), en este orden:
1. Se expande el término del cliente con los sinónimos SIEMPRE, no solo
   cuando no hay resultados: "audífonos" debe traer también los productos
   que se llaman "Auriculares", aunque haya alguno que se llame "Audífonos".
2. Se busca en la categoría que eligió el agente.
3. Si no hay nada, se busca en todo el catálogo, y el resultado lo dice
   (`en_otras_categorias=True`) para que el agente no presente el producto
   como si fuera de la categoría que pidió el cliente.
4. Si coinciden más de `MAX_RESULTADOS` productos, NO se devuelven
   productos sino un resumen (total, rango de precios y opciones por marca)
   y `hay_que_afinar=True`: el agente le pregunta al cliente por marca o
   presupuesto ofreciendo opciones que existen, y vuelve a buscar con la
   marca en el término o con `precio_max`. Si el cliente pide "los más
   baratos" o "los más caros" (o no quiere afinar), `orden` los trae igual,
   ordenados por precio. La regla vive aquí, no en el prompt: el modelo no
   puede saltársela.

Cómo se combinan las palabras: entre sinónimos basta UNA ("portátil" O
"laptop"); entre palabras distintas del cliente deben estar TODAS
("portátil hp" exige portátil/laptop Y hp). La función de Postgres
`buscar_productos_venta` recibe esos grupos ya armados (ver
`supabase/migrations/20261001000008_sinonimos_busqueda.sql` y
`20261001000009_busqueda_por_marca_y_precio.sql`).

Categorías y sinónimos cambian poco: se guardan en memoria
`SEGUNDOS_CACHE` segundos. Un cambio de la empresa en la tabla de
sinónimos se nota, como mucho, a los 10 minutos.
"""
from __future__ import annotations

import os
import re
import time
import unicodedata
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Callable, Optional

from tools import supabase_client

SEGUNDOS_CACHE = 600
MAX_RESULTADOS = 5
# Valores de `orden`: mostrar SIEMPRE, ordenado por precio en ese sentido.
ORDENES = ("mas_baratos", "mas_caros")

# Palabras que no ayudan a encontrar un producto. Con coincidencia al
# inicio de palabra, "de" encontraría casi todo ("Garantía directa de 12
# meses" está en todas las descripciones).
PALABRAS_VACIAS = frozenset({
    "a", "al", "con", "de", "del", "el", "en", "la", "las", "lo", "los",
    "o", "para", "por", "que", "sin", "u", "un", "una", "uno", "unos", "unas", "y",
})


@dataclass(frozen=True)
class Categoria:
    id: str
    nombre: str


@dataclass(frozen=True)
class Producto:
    id: str
    nombre: str
    descripcion: str
    precio: Decimal
    categoria_id: Optional[str]
    marca: str


@dataclass(frozen=True)
class OpcionMarca:
    marca: str
    cantidad: int
    precio_min: Decimal
    precio_max: Decimal


@dataclass(frozen=True)
class ResultadoBusqueda:
    total: int
    productos: tuple
    marcas: tuple = ()
    precio_min: Optional[Decimal] = None
    precio_max: Optional[Decimal] = None
    # True si la categoría pedida no tenía nada y esto viene del catálogo completo.
    en_otras_categorias: bool = False

    @property
    def hay_que_afinar(self) -> bool:
        """Hay más coincidencias de las que se muestran: preguntar marca o presupuesto."""
        return self.total > len(self.productos)


def _tabla_categorias() -> str:
    return os.environ.get("SUPABASE_TABLE_CATEGORIAS", "product_categories")


def _tabla_sinonimos() -> str:
    return os.environ.get("SUPABASE_TABLE_SINONIMOS", "sinonimos_busqueda")


class _Cache:
    """Un valor cargado bajo demanda que vence a los `SEGUNDOS_CACHE`."""

    def __init__(self, cargar: Callable[[], object], reloj: Callable[[], float] = time.monotonic):
        self._cargar = cargar
        self._reloj = reloj
        self._valor: object = None
        self._cargado_en: Optional[float] = None

    def obtener(self):
        ahora = self._reloj()
        if self._cargado_en is None or ahora - self._cargado_en > SEGUNDOS_CACHE:
            self._valor = self._cargar()
            self._cargado_en = ahora
        return self._valor

    def invalidar(self) -> None:
        self._cargado_en = None


# ---------------------------------------------------------------------------
# Normalización y expansión del término (funciones puras)
# ---------------------------------------------------------------------------

def normalizar(texto: str) -> str:
    """Minúsculas, sin tildes y solo letras/números separados por espacios:
    "¡Portátil  HP-15!" -> "portatil hp 15"."""
    sin_tildes = "".join(
        c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c)
    )
    # La ñ también pierde su tilde (n), igual que hace unaccent en Postgres.
    return " ".join(re.findall(r"[a-z0-9]+", sin_tildes.lower()))


def formas_singulares(palabra: str) -> list:
    """La palabra y sus posibles singulares, de la más larga a la más
    corta. Sin diccionario no se sabe si "parlantes" es "parlante" + "s" o
    si "monitores" es "monitor" + "es", así que se prueban ambas:

        "monitores" -> ["monitores", "monitore", "monitor"]
        "laptops"   -> ["laptops", "laptop"]

    Palabras cortas o con números no se tocan ("ssd", "16gb", "mas")."""
    if any(c.isdigit() for c in palabra) or len(palabra) <= 3 or not palabra.endswith("s"):
        return [palabra]
    formas = [palabra, palabra[:-1]]
    if palabra.endswith("es"):
        formas.append(palabra[:-2])
    return formas


def expandir_termino(termino: str, grupos_sinonimos: list) -> list:
    """El término del cliente como grupos para `buscar_productos_venta`:
    una lista por palabra, con sus sinónimos.

        expandir_termino("portátiles HP", [["portatil", "laptop"]])
        -> [["portatil", "laptop"], ["hp"]]

    Para buscar se usa el singular más corto: la búsqueda es por inicio de
    palabra, así que "parlant" encuentra "Parlante" y "monitor" encuentra
    "Monitores". Para el sinónimo sirve cualquier forma que esté en la
    tabla. Una palabra puede estar en varios grupos de sinónimos: se unen
    todos. Palabras repetidas en el término cuentan una sola vez.
    """
    indice: dict = {}
    for grupo in grupos_sinonimos:
        palabras = [normalizar(p) for p in grupo if normalizar(p)]
        for p in palabras:
            indice.setdefault(p, []).extend(palabras)

    grupos, vistas = [], set()
    for palabra in normalizar(termino).split():
        if palabra in PALABRAS_VACIAS:
            continue
        formas = formas_singulares(palabra)
        busqueda = formas[-1]
        if busqueda in vistas:
            continue
        vistas.add(busqueda)
        sinonimos_de_palabra = next((indice[f] for f in formas if f in indice), [])
        grupos.append(list(dict.fromkeys([busqueda, *sinonimos_de_palabra])))  # sin duplicados, en orden
    return grupos


# ---------------------------------------------------------------------------
# Lecturas
# ---------------------------------------------------------------------------

def _cargar_categorias() -> tuple:
    filas = supabase_client.get_rows(_tabla_categorias(), params={"select": "id,name", "order": "name"})
    return tuple(Categoria(id=f["id"], nombre=f["name"]) for f in filas)


def _cargar_sinonimos() -> list:
    filas = supabase_client.get_rows(_tabla_sinonimos(), params={"select": "palabras"})
    return [f["palabras"] for f in filas]


_categorias = _Cache(_cargar_categorias)
_sinonimos = _Cache(_cargar_sinonimos)


def listar_categorias() -> tuple:
    return _categorias.obtener()


def sinonimos() -> list:
    return _sinonimos.obtener()


def _decimal(valor) -> Optional[Decimal]:
    return None if valor is None else Decimal(str(valor))


def _producto(fila: dict) -> Producto:
    return Producto(
        id=fila["id"],
        nombre=fila["name"],
        descripcion=fila.get("description") or "",
        precio=_decimal(fila["sale_price"]),
        categoria_id=fila.get("category_id"),
        marca=fila["marca"],
    )


def _buscar(grupos: list, category_id: Optional[str], precio_max: Optional[Decimal],
            orden: Optional[str]) -> ResultadoBusqueda:
    resumen = supabase_client.rpc(
        "buscar_productos_venta",
        {
            "p_grupos": grupos,
            "p_category_id": category_id,
            "p_precio_max": None if precio_max is None else str(precio_max),
            "p_umbral": MAX_RESULTADOS,
            "p_orden": orden,
        },
        solo_lectura=True,
    )
    return ResultadoBusqueda(
        total=int(resumen["total"]),
        productos=tuple(_producto(f) for f in resumen["productos"]),
        marcas=tuple(
            OpcionMarca(m["marca"], int(m["cantidad"]), _decimal(m["precio_min"]), _decimal(m["precio_max"]))
            for m in resumen["marcas"]
        ),
        precio_min=_decimal(resumen["precio_min"]),
        precio_max=_decimal(resumen["precio_max"]),
    )


def buscar_productos(
    category_id: str,
    termino: str,
    precio_max: Optional[Decimal] = None,
    orden: Optional[str] = None,
) -> ResultadoBusqueda:
    """Productos activos con stock que coinciden con `termino` (y cuestan
    como mucho `precio_max`), primero en `category_id` y, si no hay ninguno,
    en todo el catálogo. Ver el docstring del módulo."""
    grupos = expandir_termino(termino, sinonimos())
    resultado = _buscar(grupos, category_id, precio_max, orden)
    if resultado.total:
        return resultado
    resultado = _buscar(grupos, None, precio_max, orden)
    return replace(resultado, en_otras_categorias=bool(resultado.total))
