"""
tools/carrito_tools.py

{Añadir_elemento}, {Consultar_carrito}, {Modificar_elemento} y
{Eliminar_elemento} del Agente de Ventas. `session_id` lo inyecta
`agents/ventas_agent.py`: el modelo nunca lo ve ni lo pasa.

Las tres que modifican el carrito devuelven también el carrito actualizado
(con subtotales y total calculados por el código). Es exactamente lo que el
prompt pide consultar después de cada cambio, y ahorra una llamada al modelo
por operación.

Las reglas (sumar al añadir, nunca más que el stock) viven en
`tools/carrito_repository.py` y en Postgres. Nunca lanzan excepciones.
"""
from __future__ import annotations

from tools import carrito_repository
from tools.errores_negocio import ErrorNegocio
from tools.formato_ventas import error_tecnico, mensaje_error_negocio, resumen_carrito


def _con_carrito_actualizado(confirmacion: str, session_id: str) -> str:
    try:
        carrito = carrito_repository.leer(session_id)
    except Exception as exc:
        return f"{confirmacion}\n" + error_tecnico("leer el carrito actualizado", exc)
    return f"{confirmacion}\n{resumen_carrito(carrito)}"


def consultar_carrito(session_id: str) -> str:
    try:
        return resumen_carrito(carrito_repository.leer(session_id))
    except Exception as exc:
        return error_tecnico("consultar el carrito", exc)


def anadir_elemento(producto_id: str, session_id: str, cantidad: int = 1) -> str:
    try:
        cantidad_final = carrito_repository.agregar(session_id, producto_id, cantidad)
    except ErrorNegocio as exc:
        return mensaje_error_negocio(exc)
    except Exception as exc:
        return error_tecnico("agregar el producto al carrito", exc)
    return _con_carrito_actualizado(
        f"OK: se agregaron {cantidad} unidad(es); ahora hay {cantidad_final} de ese producto en el carrito.",
        session_id,
    )


def modificar_elemento(producto_id: str, cantidad_nueva: int, session_id: str) -> str:
    try:
        carrito_repository.fijar_cantidad(session_id, producto_id, cantidad_nueva)
    except ErrorNegocio as exc:
        return mensaje_error_negocio(exc)
    except Exception as exc:
        return error_tecnico("cambiar la cantidad", exc)
    return _con_carrito_actualizado(f"OK: la cantidad quedó en {cantidad_nueva}.", session_id)


def eliminar_elemento(producto_id: str, session_id: str) -> str:
    try:
        carrito_repository.eliminar(session_id, producto_id)
    except ErrorNegocio as exc:
        return mensaje_error_negocio(exc)
    except Exception as exc:
        return error_tecnico("quitar el producto del carrito", exc)
    return _con_carrito_actualizado("OK: el producto se quitó del carrito.", session_id)
