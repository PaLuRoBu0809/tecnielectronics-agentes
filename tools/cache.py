"""
tools/cache.py

`CacheConVencimiento`: un valor que se carga bajo demanda y se guarda en
memoria hasta que vence. Para datos que cambian poco y se leen en cada
turno (categorías y sinónimos del inventario, información de la empresa):
evita una consulta a Supabase por mensaje, y un cambio hecho desde el Table
Editor se nota, como mucho, al vencer.
"""
from __future__ import annotations

import time
from typing import Callable, Optional

SEGUNDOS_CACHE = 600


class CacheConVencimiento:
    def __init__(self, cargar: Callable[[], object], segundos: float = SEGUNDOS_CACHE,
                 reloj: Callable[[], float] = time.monotonic):
        self._cargar = cargar
        self._segundos = segundos
        self._reloj = reloj
        self._valor: object = None
        self._cargado_en: Optional[float] = None

    def obtener(self):
        """El valor guardado, o uno recién cargado si no hay o ya venció. Si
        la carga falla, la excepción sale y no se guarda nada."""
        ahora = self._reloj()
        if self._cargado_en is None or ahora - self._cargado_en > self._segundos:
            self._valor = self._cargar()
            self._cargado_en = ahora
        return self._valor

    def invalidar(self) -> None:
        self._cargado_en = None
