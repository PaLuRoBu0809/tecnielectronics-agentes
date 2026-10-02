"""
tools/errores_negocio.py

`ErrorNegocio`: una regla del negocio impidió la operación (no hay stock,
la orden ya no es modificable, el carrito está vacío...). Es la única
excepción que los repositorios de ventas (`tools/*_repository.py`) dejan
salir hacia las tools, con un `codigo` estable que no depende de dónde vive
el dato: hoy lo lanza una función de Postgres; cuando el inventario pase a
Siigo, lo lanzará el adaptador de Siigo con el mismo código, y las tools no
cambian.

Cualquier otra excepción (red, credenciales, un bug) NO es un ErrorNegocio
y no debe traducirse como tal: las tools la reportan como un inconveniente
técnico.
"""
from __future__ import annotations

import json
from typing import Optional


class ErrorNegocio(Exception):
    def __init__(self, codigo: str, detalle: Optional[str] = None):
        super().__init__(codigo if detalle is None else f"{codigo}: {detalle}")
        self.codigo = codigo
        self.detalle = detalle

    def detalle_json(self) -> dict:
        """El detalle cuando es un objeto JSON (ej. `{"stock": 3}`); `{}` si no."""
        try:
            valor = json.loads(self.detalle or "")
        except ValueError:
            return {}
        return valor if isinstance(valor, dict) else {}
