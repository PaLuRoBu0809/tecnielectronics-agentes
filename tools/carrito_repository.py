"""
tools/carrito_repository.py

Carrito de compras de cada cliente (`carrito_compras`, una fila por
producto, clave `(session_id, producto_id)`).

Reglas, aplicadas por las funciones de Postgres de
`supabase/migrations/20261001000006_ventas.sql`:
- `agregar`: si el producto ya está, SUMA la cantidad; nunca más que el stock.
- `fijar_cantidad`: fija la cantidad FINAL (el agente la calcula); nunca más
  que el stock.
- `eliminar`: borra el producto entero del carrito.
El carrito NO aparta unidades: el stock se descuenta al crear la orden.

Los errores de negocio salen como `ErrorNegocio` (ver
`tools/errores_negocio.py`); cualquier otro error se propaga tal cual.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal

from tools import supabase_client
from tools.errores_negocio import ErrorNegocio


@dataclass(frozen=True)
class LineaCarrito:
    producto_id: str
    nombre: str
    cantidad: int
    precio_unitario: Decimal

    @property
    def subtotal(self) -> Decimal:
        return self.precio_unitario * self.cantidad


@dataclass(frozen=True)
class Carrito:
    lineas: tuple

    @property
    def total(self) -> Decimal:
        return sum((linea.subtotal for linea in self.lineas), Decimal(0))

    @property
    def vacio(self) -> bool:
        return not self.lineas


def _tabla_carrito() -> str:
    return os.environ.get("SUPABASE_TABLE_CARRITO", "carrito_compras")


def leer(session_id: str) -> Carrito:
    """El carrito del cliente, con nombre y precio ACTUALES de cada producto."""
    filas = supabase_client.get_rows(
        _tabla_carrito(),
        params={
            "session_id": f"eq.{session_id}",
            "select": "producto_id,cantidad,products(name,sale_price)",
            "order": "creado_en",
        },
    )
    return Carrito(tuple(
        LineaCarrito(
            producto_id=f["producto_id"],
            nombre=f["products"]["name"],
            cantidad=int(f["cantidad"]),
            precio_unitario=Decimal(str(f["products"]["sale_price"])),
        )
        for f in filas
    ))


def agregar(session_id: str, producto_id: str, cantidad: int) -> int:
    """Suma `cantidad` del producto al carrito; devuelve la cantidad final."""
    return supabase_client.rpc_con_reglas(
        "agregar_al_carrito",
        {"p_session_id": session_id, "p_producto_id": producto_id, "p_cantidad": cantidad},
    )


def fijar_cantidad(session_id: str, producto_id: str, cantidad: int) -> int:
    """Fija la cantidad final de un producto que ya está en el carrito."""
    return supabase_client.rpc_con_reglas(
        "fijar_cantidad_carrito",
        {"p_session_id": session_id, "p_producto_id": producto_id, "p_cantidad": cantidad},
    )


def eliminar(session_id: str, producto_id: str) -> None:
    """Quita el producto entero del carrito. `ErrorNegocio('NO_ESTA_EN_CARRITO')`
    si no estaba (así el agente no le confirma al cliente algo que no pasó)."""
    borradas = supabase_client.delete_rows(
        _tabla_carrito(),
        params={"session_id": f"eq.{session_id}", "producto_id": f"eq.{producto_id}"},
    )
    if not borradas:
        raise ErrorNegocio("NO_ESTA_EN_CARRITO")
