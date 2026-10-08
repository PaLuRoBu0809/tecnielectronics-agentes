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

Además de `/api/chat`, expone los endpoints de las pestañas "Servicio
técnico" y "Ventas" del dashboard (`/api/ordenes-servicio...`,
`/api/pedidos...`, `/api/catalogo`): leer órdenes y pedidos, cambiar su
estado y escribir o editar notas de seguimiento (Fase 13; nota y
responsable obligatorios). Son "controladores delgados": no pasan por el
LLM ni llevan lógica de negocio, solo validan la petición y delegan en
`tools/ordenes_servicio_repository.py`, `tools/ordenes_repository.py` y
`tools/catalog_tools.py`, cuyas funciones de Postgres aplican las reglas.

También expone `/api/flujo/stream` (Server-Sent Events) para la pestaña
"Flujo en Vivo": transmite en tiempo real los eventos que publica
`llm_loop.py` (llamadas a modelos, ejecución de tools, latencias) a través
de `tools/eventos_agente.py`, sin que este archivo necesite saber nada de
esa lógica — solo suscribe un cliente y reenvía lo que llega. Además,
`/api/flujo/corridas` y `/api/flujo/corridas/{run_id}` exponen el historial
de "corridas" (un turno completo = todos los eventos de UN mensaje del
cliente) para el sidebar estilo historial de ejecuciones de n8n.

Seguridad (ver `web/seguridad.py` y `docs/PLAN_DE_MEJORAS.md`, Fase 1): con
`API_KEY` definida en el entorno, todas las rutas `/api/*` exigen la
cabecera `X-API-Key`; `/api/chat` además limita mensajes por sesión.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import date
from pathlib import Path
from typing import Literal, Optional

import anyio
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from agents import orquestador
from config import validar_configuracion
from sesiones import AlmacenSesiones, TurnoEnCurso
from notificaciones.notificador import NotificadorHistorial
from tools import catalog_tools, eventos_agente, metricas, ordenes_servicio_repository
from tools import ordenes_repository, pagos, servicio_pagos, supabase_client
from tools.errores_negocio import ErrorNegocio
from web.seguridad import (
    COOKIE_SESION,
    DURACION_SESION_S,
    LimitadorDeUso,
    api_key_configurada,
    cookie_segura,
    crear_token_sesion,
    verificar_api_key,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)
load_dotenv()
# Si falta una variable obligatoria, el servidor NO arranca y dice cuál
# (Fase 9.1): antes el error aparecía recién con el primer mensaje.
validar_configuracion()

_almacen = AlmacenSesiones()
_limitador = LimitadorDeUso.desde_entorno()
_static_dir = Path(__file__).parent / "static"

app = FastAPI(title="TecniElectronics — Panel de pruebas de agentes")

# Todas las rutas /api/* exigen la clave (Fase 1.2). Van en un router aparte
# para que /health y /ready (que el hosting consulta sin cabeceras) y los
# archivos estáticos del dashboard queden fuera de la autenticación.
api = APIRouter(prefix="/api", dependencies=[Depends(verificar_api_key)])


class ChatRequest(BaseModel):
    """Cuerpo esperado por `POST /api/chat`. Los topes de longitud evitan que
    un mensaje gigante consuma tokens sin control (Fase 1.4 del plan)."""

    session_id: str = Field(min_length=1, max_length=64)
    mensaje: str = Field(min_length=1, max_length=2000)


class ChatResponse(BaseModel):
    """Respuesta de `POST /api/chat`: el texto ya redactado para el cliente."""

    respuesta: str


@api.post("/chat", response_model=ChatResponse)
def chat(payload: ChatRequest) -> ChatResponse:
    """Recibe un mensaje "del cliente" para una sesión (teléfono) puntual,
    lo pasa por el Orquestador (que a su vez delega al Agente de Servicio
    Técnico si aplica) y devuelve la respuesta final ya redactada.

    El historial de ambos agentes para esa sesión se lee de Supabase antes
    del turno y se guarda de vuelta al terminarlo (ver `sesiones.py`).

    Genera un `run_id` nuevo por cada mensaje — agrupa todos los eventos de
    este turno (memoria, modelo, tools, de ambos agentes) como UNA "corrida"
    para el sidebar de `/api/flujo/corridas` (ver `tools/eventos_agente.py`).

    Antes de gastar cualquier llamada al LLM, aplica el límite de uso por
    sesión (`web/seguridad.py`): si se supera, responde 429.

    El turno completo (leer historial -> agentes -> guardar) corre bajo
    `turno_exclusivo`: dos mensajes seguidos de la misma sesión se procesan
    en orden, el segundo con el historial ya actualizado por el primero
    (Fase 7). Si el anterior no termina a tiempo, responde 409.
    """
    limite_superado = _limitador.registrar(payload.session_id)
    if limite_superado:
        raise HTTPException(status_code=429, detail=limite_superado)

    try:
        with _almacen.turno_exclusivo(payload.session_id):
            run_id = uuid.uuid4().hex
            eventos_agente.iniciar_corrida(run_id, payload.session_id, titulo=payload.mensaje[:80])

            historiales = _almacen.obtener(payload.session_id, run_id=run_id)
            respuesta, historiales = orquestador.run(
                mensaje_cliente=payload.mensaje,
                session_id=payload.session_id,
                historiales=historiales,
                run_id=run_id,
            )
            _almacen.guardar(payload.session_id, historiales, run_id=run_id)
    except TurnoEnCurso:
        raise HTTPException(
            status_code=409, detail="Todavía se está procesando el mensaje anterior de esta sesión."
        ) from None
    return ChatResponse(respuesta=respuesta)


@api.get("/sesiones")
def listar_sesiones() -> dict:
    """Lista los `session_id` con conversación guardada en Supabase — la
    interfaz web la usa para poblar el selector de "clientes simulados" al
    cargar la página."""
    return {"sesiones": _almacen.ids()}


# ---------------------------------------------------------------------------
# Dashboard: Servicio técnico y Ventas (Fase 13)
# ---------------------------------------------------------------------------

EstadoOrdenServicio = Literal[
    "PENDIENTE_RECEPCION", "RECIBIDO", "EN_DIAGNOSTICO", "EN_REPARACION", "LISTO_PARA_RECOGER", "ENTREGADO",
    "CANCELADO",
]
EstadoEnvio = Literal["PENDIENTE_DESPACHO", "DESPACHADO", "ENTREGADO", "CANCELADO"]


class NotaRequest(BaseModel):
    """Toda nota lleva el nombre de quien la escribe (lo exige el negocio)."""

    nota: str = Field(min_length=1, max_length=2000)
    responsable: str = Field(min_length=1, max_length=80)

    @field_validator("nota", "responsable")
    @classmethod
    def _sin_espacios_sobrantes(cls, valor: str) -> str:
        valor = valor.strip()
        if not valor:
            raise ValueError("no puede estar vacío")
        return valor


class EstadoOrdenServicioRequest(NotaRequest):
    estado: EstadoOrdenServicio


class EstadoPedidoRequest(NotaRequest):
    estado: EstadoEnvio


_ERRORES_PANEL = {
    "ORDEN_SERVICIO_NO_ENCONTRADA": (404, "La orden de servicio no existe."),
    "ORDEN_NO_ENCONTRADA": (404, "El pedido no existe."),
    "NOTA_NO_ENCONTRADA": (404, "La nota no existe."),
    "MISMO_ESTADO": (409, "Ya está en ese estado. Para dejar una novedad sin cambiar el estado, usa Agregar nota."),
    "PEDIDO_CANCELADO": (409, "Un pedido cancelado no se reactiva (su stock ya volvió al inventario)."),
    "NOTA_REQUERIDA": (422, "La nota es obligatoria."),
    "RESPONSABLE_REQUERIDO": (422, "El nombre de quien registra la nota es obligatorio."),
}


def _escritura_panel(funcion, *args):
    """Ejecuta una escritura del panel y traduce las reglas de negocio de
    Postgres a respuestas HTTP con un mensaje legible."""
    try:
        return funcion(*args)
    except ErrorNegocio as exc:
        estado, mensaje = _ERRORES_PANEL.get(exc.codigo, (409, f"No se pudo completar ({exc.codigo})."))
        raise HTTPException(status_code=estado, detail=mensaje) from None


@api.get("/ordenes-servicio")
def listar_ordenes_servicio(desde: Optional[date] = None, hasta: Optional[date] = None) -> list:
    """Órdenes de servicio de todos los clientes; `desde`/`hasta` (YYYY-MM-DD)
    acotan por el día en que el cliente trae el equipo."""
    return ordenes_servicio_repository.listar_todas(
        desde.isoformat() if desde else None, hasta.isoformat() if hasta else None
    )


@api.get("/ordenes-servicio/{numero}")
def obtener_orden_servicio(numero: int) -> dict:
    """Una orden con su historial de notas (de la más antigua a la más reciente)."""
    orden = ordenes_servicio_repository.leer(numero)
    if orden is None:
        raise HTTPException(status_code=404, detail="La orden de servicio no existe.")
    return {"orden": orden, "seguimiento": ordenes_servicio_repository.seguimiento([numero]).get(numero, [])}


@api.post("/ordenes-servicio/{numero}/estado")
def cambiar_estado_orden_servicio(numero: int, payload: EstadoOrdenServicioRequest) -> dict:
    return _escritura_panel(
        ordenes_servicio_repository.cambiar_estado, numero, payload.estado, payload.nota, payload.responsable
    )


@api.post("/ordenes-servicio/{numero}/notas")
def agregar_nota_orden_servicio(numero: int, payload: NotaRequest) -> dict:
    return _escritura_panel(ordenes_servicio_repository.agregar_nota, numero, payload.nota, payload.responsable)


@api.patch("/ordenes-servicio/notas/{id_nota}")
def editar_nota_orden_servicio(id_nota: int, payload: NotaRequest) -> dict:
    return _escritura_panel(ordenes_servicio_repository.editar_nota, id_nota, payload.nota, payload.responsable)


@api.get("/pedidos")
def listar_pedidos() -> list:
    """Pedidos de todos los clientes, del más reciente al más antiguo, para
    la pestaña Ventas del panel. Lectura directa de `orders`: no pasa por
    ningún agente ni por el LLM."""
    return ordenes_repository.listar_todas()


@api.get("/pedidos/{order_number}")
def obtener_pedido(order_number: int) -> dict:
    """Un pedido con su historial de notas de envío."""
    pedido = ordenes_repository.leer_para_panel(order_number)
    if pedido is None:
        raise HTTPException(status_code=404, detail="El pedido no existe.")
    return {"pedido": pedido, "seguimiento": ordenes_repository.seguimiento([order_number]).get(order_number, [])}


@api.post("/pedidos/{order_number}/estado")
def cambiar_estado_pedido(order_number: int, payload: EstadoPedidoRequest) -> dict:
    """Nuevo estado de envío; CANCELADO devuelve el stock reservado."""
    return _escritura_panel(
        ordenes_repository.cambiar_estado_envio, order_number, payload.estado, payload.nota, payload.responsable
    )


@api.post("/pedidos/{order_number}/notas")
def agregar_nota_pedido(order_number: int, payload: NotaRequest) -> dict:
    return _escritura_panel(ordenes_repository.agregar_nota, order_number, payload.nota, payload.responsable)


@api.patch("/pedidos/notas/{id_nota}")
def editar_nota_pedido(id_nota: int, payload: NotaRequest) -> dict:
    return _escritura_panel(ordenes_repository.editar_nota, id_nota, payload.nota, payload.responsable)


@api.get("/catalogo")
def listar_catalogo() -> list:
    """Catálogo de servicios técnicos: el panel muestra el nombre del
    servicio junto a cada orden (las órdenes solo guardan `servicio_id`)."""
    return catalog_tools.listar_catalogo()


@api.get("/flujo/stream")
async def flujo_stream():
    """Server-Sent Events del panel "Flujo en Vivo" (`web/static/flujo.js`):
    cada llamada a un modelo de OpenRouter y cada ejecución de tool, de
    CUALQUIER sesión y de cualquiera de los dos agentes, llega aquí en
    tiempo real (ver `tools/eventos_agente.py`, alimentado desde
    `llm_loop.py`).

    Se implementa como un generador async que espera (en un thread aparte,
    para no bloquear el event loop de FastAPI) el siguiente evento de una
    cola thread-safe — la instrumentación real corre en threads de trabajo
    (cada request de `/api/chat` se ejecuta en un threadpool), no aquí.
    """
    id_suscriptor, cola = eventos_agente.suscribirse()

    async def generador():
        try:
            while True:
                evento = await anyio.to_thread.run_sync(eventos_agente.recibir_siguiente, cola, 15)
                if evento is None:
                    # Nada nuevo en 15s: un comentario SSE (ignorado por
                    # EventSource) mantiene la conexión viva a través de
                    # proxies que cierran conexiones HTTP inactivas.
                    yield ": keep-alive\n\n"
                    continue
                yield f"data: {json.dumps(evento, ensure_ascii=False)}\n\n"
        finally:
            eventos_agente.desuscribirse(id_suscriptor)

    return StreamingResponse(
        generador(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@api.get("/flujo/corridas")
def listar_corridas() -> list:
    """Resumen de las últimas corridas (turnos completos) guardadas en
    memoria — para el sidebar "Corridas" del panel Flujo en Vivo. Cada
    resumen trae `run_id`, `session_id`, `titulo` (el mensaje del cliente
    recortado), cuántos eventos tuvo y si hubo algún error."""
    return eventos_agente.listar_corridas()


@api.get("/flujo/corridas/{run_id}")
def obtener_corrida(run_id: str) -> dict:
    """Todos los eventos de UNA corrida puntual — el sidebar lo pide al
    hacer click en un ítem, para pintar en el diagrama exactamente qué
    nodos participaron en ese turno."""
    corrida = eventos_agente.obtener_corrida(run_id)
    if corrida is None:
        raise HTTPException(status_code=404, detail="Corrida no encontrada (o salió del historial reciente)")
    return corrida


@api.post("/panel/sesion")
def abrir_sesion_panel(request: Request, response: Response) -> dict:
    """Abre la sesión del dashboard: quien llama ya pasó `verificar_api_key`
    con la cabecera `X-API-Key`, y recibe a cambio una cookie HttpOnly con
    un token firmado que caduca (ver `web/seguridad.py`). A partir de ahí el
    navegador se autentica solo con esa cookie — también el stream SSE —, sin
    guardar la clave ni ponerla en ninguna URL."""
    if api_key_configurada() is None:
        return {"sesion": "no_requerida"}
    response.set_cookie(
        COOKIE_SESION,
        crear_token_sesion(),
        max_age=DURACION_SESION_S,
        httponly=True,
        samesite="strict",
        secure=cookie_segura(request),
        path="/api",
    )
    return {"sesion": "abierta", "vence_en_segundos": DURACION_SESION_S}


@api.delete("/panel/sesion")
def cerrar_sesion_panel(response: Response) -> dict:
    """Cierra la sesión del dashboard (borra la cookie)."""
    response.delete_cookie(COOKIE_SESION, path="/api")
    return {"sesion": "cerrada"}


@api.get("/metricas")
def ver_metricas() -> dict:
    """Contadores desde que arrancó el proceso (Fase 8.3): turnos, razones de
    parada, llamadas/tokens/costo del LLM, fallos de modelo por tipo, errores
    de tools y tasa reciente de respuestas de respaldo. Ver `tools/metricas.py`."""
    return metricas.instantanea()


@app.get("/health")
def health() -> dict:
    """Liveness (Fase 9.2): el proceso está vivo y responde. No toca
    dependencias externas, para que un Supabase caído no haga que el hosting
    reinicie el contenedor en bucle."""
    return {"estado": "ok"}


@app.get("/ready")
def ready():
    """Readiness (Fase 9.2): el servicio puede atender clientes — hoy eso es
    poder leer Supabase. Responde 503 si no, para que el hosting no le mande
    tráfico. No expone datos: solo el estado de cada dependencia."""
    try:
        supabase_client.get_rows(
            os.environ.get("SUPABASE_TABLE_CONVERSACIONES", "conversaciones"),
            # Pedir `historiales` (Fase 11) hace que /ready falle si la
            # migración 20260930000005 no se aplicó: sin esa columna, cada
            # turno fallaría al guardar.
            params={"select": "session_id,historiales", "limit": "1"},
        )
    except Exception as exc:
        logger.warning("/ready: Supabase no responde (%s)", type(exc).__name__)
        return JSONResponse(status_code=503, content={"estado": "no_listo", "supabase": "error"})
    return {"estado": "listo", "supabase": "ok"}


@app.post(pagos.RUTA_WEBHOOK)
async def webhook_mercadopago(request: Request):
    """Aviso de MercadoPago: "el pago X cambió" (Fase 12, paso C). Va FUERA
    del router /api porque MercadoPago no puede enviar X-API-Key; lo protege
    la firma `x-signature` (si MERCADOPAGO_WEBHOOK_SECRET está configurada).

    Controlador delgado: verifica la firma y delega en
    `servicio_pagos.sincronizar_pago`, que consulta el estado REAL en la API
    de MercadoPago (nunca confía en el cuerpo del aviso), actualiza la orden
    y le avisa al cliente una sola vez. Si algo falla responde 500: así
    MercadoPago reenvía el aviso más tarde."""
    params = request.query_params
    try:
        cuerpo = await request.json()
    except ValueError:
        cuerpo = {}
    cuerpo = cuerpo if isinstance(cuerpo, dict) else {}
    tipo = params.get("type") or params.get("topic") or cuerpo.get("type")
    payment_id = params.get("data.id") or (cuerpo.get("data") or {}).get("id") or params.get("id")

    secreto = os.environ.get("MERCADOPAGO_WEBHOOK_SECRET", "").strip()
    if secreto and not pagos.firma_valida(
        request.headers.get("x-signature"), request.headers.get("x-request-id"),
        None if payment_id is None else str(payment_id), secreto,
    ):
        # Diagnóstico sin datos sensibles: qué faltó o en qué formato llegó.
        logger.warning(
            "Webhook de MercadoPago con firma inválida (payment %s): x-signature=%s, x-request-id=%s, "
            "formato=%s, parámetros=%s",
            payment_id,
            "presente" if request.headers.get("x-signature") else "AUSENTE",
            "presente" if request.headers.get("x-request-id") else "AUSENTE",
            "IPN (topic/id)" if params.get("topic") else "Webhook (type/data.id)",
            sorted(params.keys()),
        )
        raise HTTPException(status_code=401, detail="firma inválida")

    if tipo != "payment" or not payment_id:
        return {"ok": True, "procesado": False}
    try:
        await anyio.to_thread.run_sync(
            lambda: servicio_pagos.sincronizar_pago(str(payment_id), NotificadorHistorial(_almacen))
        )
    except Exception:
        logger.exception("No se pudo procesar el aviso de MercadoPago del pago %s", payment_id)
        raise HTTPException(status_code=500, detail="error procesando el aviso") from None
    return {"ok": True, "procesado": True}


app.include_router(api)

# Se monta AL FINAL: las rutas /api/* de arriba deben registrarse antes que
# este montaje "catch-all" en "/" (sirve index.html y los estáticos de
# web/static/), o lo taparían.
app.mount("/", StaticFiles(directory=str(_static_dir), html=True), name="static")
