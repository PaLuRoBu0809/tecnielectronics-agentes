"""
web/app.py

Interfaz de chat vía HTTP para probar el Orquestador + Agente de Servicio
Técnico desde el navegador, en vez de por consola (`main.py`). Es el mismo
arnés de pruebas, expuesto como el próximo paso que ya anunciaba el README:
"exponer orquestador.run() detrás de un endpoint HTTP (FastAPI)".

Cómo correr:
    uvicorn web.app:app --reload

Y abrir http://127.0.0.1:8000 en el navegador.

Usa el mismo `AlmacenSesiones` que `main.py` (ver `sesiones.py`): el
historial de cada conversación se guarda en la tabla `conversaciones` de
Supabase al final de cada turno, así que sobrevive a reinicios del servidor
y es el mismo sin importar desde qué instancia se atienda la petición.
"""
from __future__ import annotations

import logging
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agents import orquestador
from sesiones import AlmacenSesiones

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
load_dotenv()

_almacen = AlmacenSesiones()
_static_dir = Path(__file__).parent / "static"

app = FastAPI(title="TecniElectronics — Panel de pruebas de agentes")


class ChatRequest(BaseModel):
    """Cuerpo esperado por `POST /api/chat`."""

    session_id: str
    mensaje: str


class ChatResponse(BaseModel):
    """Respuesta de `POST /api/chat`: el texto ya redactado para el cliente."""

    respuesta: str


@app.post("/api/chat", response_model=ChatResponse)
def chat(payload: ChatRequest) -> ChatResponse:
    """Recibe un mensaje "del cliente" para una sesión (teléfono) puntual,
    lo pasa por el Orquestador (que a su vez delega al Agente de Servicio
    Técnico si aplica) y devuelve la respuesta final ya redactada.

    El historial de ambos agentes para esa sesión se lee de Supabase antes
    del turno y se guarda de vuelta al terminarlo (ver `sesiones.py`).
    """
    sesion = _almacen.obtener(payload.session_id)
    respuesta, historial_orq, historial_st = orquestador.run(
        mensaje_cliente=payload.mensaje,
        session_id=payload.session_id,
        orquestador_historial=sesion["orq"],
        servicio_tecnico_historial=sesion["st"],
    )
    _almacen.guardar(payload.session_id, historial_orq, historial_st)
    return ChatResponse(respuesta=respuesta)


@app.get("/api/sesiones")
def listar_sesiones() -> dict:
    """Lista los `session_id` con conversación guardada en Supabase — la
    interfaz web la usa para poblar el selector de "clientes simulados" al
    cargar la página."""
    return {"sesiones": _almacen.ids()}


# Se monta AL FINAL: las rutas /api/* de arriba deben registrarse antes que
# este montaje "catch-all" en "/" (sirve index.html y los estáticos de
# web/static/), o lo taparían.
app.mount("/", StaticFiles(directory=str(_static_dir), html=True), name="static")
