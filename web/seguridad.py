"""
web/seguridad.py

Controles de acceso del API HTTP (`web/app.py`), separados de las rutas para
que `app.py` siga siendo un conjunto de "controladores delgados". Ver
`docs/PLAN_DE_MEJORAS.md`, Fase 1.

1. Autenticación (`verificar_api_key`): si la variable `API_KEY` está
   definida, toda ruta `/api/*` exige UNA de dos credenciales:
   - la cabecera `X-API-Key` (clientes servidor-a-servidor, ej. n8n); o
   - la cookie de sesión del panel (`COOKIE_SESION`), que el navegador
     obtiene UNA vez con `POST /api/panel/sesion` enviando la clave.
   La cookie es HttpOnly (ningún script de la página la puede leer) y lleva
   un token firmado con HMAC que caduca, NO la clave. Así la clave nunca
   viaja en una URL (antes el stream SSE la recibía como `?api_key=`, y
   uvicorn escribe las URLs completas en su log de accesos) ni se guarda en
   `localStorage`. Cambiar `API_KEY` invalida todas las sesiones abiertas.
   Si `API_KEY` NO está definida, el API queda abierto (modo desarrollo
   local) y se avisa en el log al arrancar.

2. Límite de uso por sesión (`LimitadorDeUso`): ventana deslizante de
   mensajes por minuto y por día para cada `session_id`. Protege la cuota
   de OpenRouter de un cliente (o un script) que envíe mensajes en bucle —
   cada mensaje cuesta al menos 3 llamadas al LLM.

Limitación conocida: el limitador vive en la RAM de UN proceso (igual que
`tools/eventos_agente.py`). Con varios workers, cada uno cuenta por su lado;
para eso haría falta un contador compartido (Redis o una tabla).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import threading
import time
from collections import defaultdict, deque
from typing import Optional

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)

CABECERA_API_KEY = "X-API-Key"
COOKIE_SESION = "panel_sesion"
DURACION_SESION_S = 12 * 3600


def api_key_configurada() -> Optional[str]:
    return os.environ.get("API_KEY") or None


def _firma(texto: str, clave: str) -> str:
    # Prefijo de dominio: la firma del panel no sirve para ningún otro uso
    # que alguna vez se le dé a la misma clave.
    return hmac.new(f"panel-sesion:{clave}".encode(), texto.encode(), hashlib.sha256).hexdigest()


def crear_token_sesion(ahora: Optional[float] = None) -> str:
    """Token `"<vence_en>.<firma>"`: no contiene la clave, solo prueba que
    quien lo emitió la conocía. Requiere `API_KEY` definida."""
    clave = api_key_configurada()
    if clave is None:
        raise RuntimeError("No hay API_KEY: no se pueden emitir sesiones")
    vence = str(int((time.time() if ahora is None else ahora) + DURACION_SESION_S))
    return f"{vence}.{_firma(vence, clave)}"


def token_sesion_valido(token: Optional[str], ahora: Optional[float] = None) -> bool:
    clave = api_key_configurada()
    if not clave or not token:
        return False
    vence, _, firma = token.partition(".")
    if not vence.isdigit() or int(vence) < (time.time() if ahora is None else ahora):
        return False
    return hmac.compare_digest(firma.encode(), _firma(vence, clave).encode())


def verificar_api_key(request: Request) -> None:
    """Dependencia de FastAPI aplicada a todas las rutas del API: acepta la
    cabecera `X-API-Key` o una cookie de sesión válida.

    Usa `hmac.compare_digest` (comparación en tiempo constante) para que el
    tiempo de respuesta no revele cuántos caracteres de la clave acertó un
    atacante."""
    esperada = api_key_configurada()
    if esperada is None:
        return
    recibida = request.headers.get(CABECERA_API_KEY) or ""
    if recibida and hmac.compare_digest(recibida.encode(), esperada.encode()):
        return
    if token_sesion_valido(request.cookies.get(COOKIE_SESION)):
        return
    raise HTTPException(status_code=401, detail="Falta la clave de API o es inválida.")


def cookie_segura(request: Request) -> bool:
    """`Secure` (solo HTTPS) salvo en desarrollo local. Se decide por el
    nombre del host y no por el esquema, porque detrás del proxy de un
    hosting la app ve `http` aunque el cliente use `https`."""
    return (request.url.hostname or "") not in {"localhost", "127.0.0.1"}


def _entero_env(nombre: str, por_defecto: int) -> int:
    try:
        return int(os.environ.get(nombre, por_defecto))
    except ValueError:
        logger.warning("%s no es un entero válido; se usa %s", nombre, por_defecto)
        return por_defecto


class LimitadorDeUso:
    """Ventana deslizante por `session_id`: como mucho `por_minuto` mensajes
    en los últimos 60 s y `por_dia` en las últimas 24 h."""

    def __init__(self, por_minuto: int, por_dia: int, reloj=time.monotonic):
        self.por_minuto = por_minuto
        self.por_dia = por_dia
        self._reloj = reloj
        self._lock = threading.Lock()
        self._marcas: dict = defaultdict(deque)

    @classmethod
    def desde_entorno(cls) -> "LimitadorDeUso":
        return cls(
            por_minuto=_entero_env("LIMITE_MENSAJES_POR_MINUTO", 10),
            por_dia=_entero_env("LIMITE_MENSAJES_POR_DIA", 200),
        )

    def registrar(self, session_id: str) -> Optional[str]:
        """Registra un mensaje de `session_id`. Devuelve `None` si se permite,
        o un texto explicando qué límite se superó (el mensaje NO se cuenta
        en ese caso, para no castigar más al cliente por reintentar)."""
        ahora = self._reloj()
        with self._lock:
            marcas = self._marcas[session_id]
            while marcas and ahora - marcas[0] > 86400:
                marcas.popleft()
            ultimo_minuto = sum(1 for m in marcas if ahora - m <= 60)
            if ultimo_minuto >= self.por_minuto:
                return f"Máximo {self.por_minuto} mensajes por minuto para esta sesión."
            if len(marcas) >= self.por_dia:
                return f"Máximo {self.por_dia} mensajes por día para esta sesión."
            marcas.append(ahora)
            return None
