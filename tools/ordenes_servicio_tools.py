"""
tools/ordenes_servicio_tools.py

Tools del Agente de Servicio Técnico (Fase 13): el cliente agenda el DÍA en
que lleva su equipo a la sede (+ una hora aproximada, solo informativa),
puede cambiar ese día o cancelar mientras no lo haya llevado, y consulta el
estado y las novedades de su equipo.

- Las reglas de calendario (días con atención, festivos, hora dentro del
  horario) se validan aquí con `tools/agenda_entregas.py`, ANTES de pedir
  confirmación: el cliente nunca confirma algo imposible.
- Las reglas de propiedad y estado (solo sus órdenes, solo en
  PENDIENTE_RECEPCION) las garantiza Postgres; aquí se verifican antes solo
  para no pedir confirmación de algo que va a fallar.
- Las escrituras usan la confirmación de dos turnos de
  `tools/confirmacion.py`. `session_id` e `id_turno` los inyecta
  `agents/servicio_tecnico_agent.py`: el modelo nunca los ve.

Nunca lanzan excepciones: devuelven texto para el modelo.
"""
from __future__ import annotations

import logging
from datetime import date, time
from typing import Callable, Optional

from tools import catalog_tools, info_empresa, ordenes_servicio_repository as repo
from tools.agenda_entregas import validar_dia_entrega, validar_hora_aproximada
from tools.confirmacion import requiere_confirmacion
from tools.errores_negocio import ErrorNegocio
from tools.formato_servicio import (
    dia_legible,
    estado_legible,
    hora_legible,
    mensaje_error_negocio,
    resumen_orden_servicio,
)
from tools.formato_ventas import error_tecnico

logger = logging.getLogger(__name__)

AGENTE = "servicio_tecnico"
PREFIJO_EXITO_CREAR = "OK: ORDEN DE SERVICIO REGISTRADA"


def _nombres_servicios() -> dict:
    """{id: nombre} del catálogo. Si no se puede leer, vacío (se muestra el id)."""
    try:
        return {int(s["id"]): s.get("nombre") for s in catalog_tools.listar_catalogo()}
    except Exception:
        logger.warning("No se pudo leer el catálogo de servicios", exc_info=True)
        return {}


def _nombre(servicios: dict, servicio_id) -> str:
    return servicios.get(int(servicio_id)) or f"Servicio #{servicio_id}"


def _validar_agenda(fecha: date, hora: Optional[time]) -> Optional[str]:
    return validar_dia_entrega(fecha) or validar_hora_aproximada(fecha, hora)


def _datos_sede() -> str:
    """Horario y dirección para el mensaje de éxito, copiados de info_empresa."""
    try:
        temas = {t.tema: t.contenido for t in info_empresa.listar()}
    except Exception:
        return "(no se pudo leer el horario: usa los DATOS DE LA SEDE de tu prompt)"
    return f"Dirección: {temas.get('ubicacion', '(sin registro)')}\nHorario:\n{temas.get('horario', '(sin registro)')}"


# ---------------------------------------------------------------------------
# Crear
# ---------------------------------------------------------------------------

def crear_orden_servicio(
    servicio_id: int,
    cliente_nombre: str,
    cliente_telefono: str,
    equipo: str,
    descripcion: str,
    fecha_entrega: date,
    session_id: str,
    id_turno: str,
    hora_aproximada: Optional[time] = None,
) -> str:
    error = _validar_agenda(fecha_entrega, hora_aproximada)
    if error:
        return error
    try:
        servicio = catalog_tools.leer_servicio(servicio_id)
    except Exception as exc:
        return error_tecnico("consultar el catálogo de servicios", exc)
    if servicio is None:
        return mensaje_error_negocio(ErrorNegocio("SERVICIO_NO_EXISTE"))

    firma = ("crear_orden_servicio", int(servicio_id), cliente_nombre, cliente_telefono, equipo, descripcion,
             fecha_entrega, hora_aproximada)
    if requiere_confirmacion(AGENTE, session_id, id_turno, firma):
        return (
            "CONFIRMACION_PENDIENTE: Todavía NO se ha registrado nada. Muestra al cliente este resumen y espera "
            "su confirmación explícita en un mensaje NUEVO; cuando confirme, llama Crear_orden_servicio con "
            "EXACTAMENTE estos mismos datos. Nunca digas que la orden ya quedó registrada.\n"
            f"RESUMEN:\n   Servicio: {servicio.get('nombre')}\n   Equipo: {equipo}\n   Problema: {descripcion}\n"
            f"   Día para llevar el equipo: {dia_legible(fecha_entrega)}\n"
            f"   Hora aproximada: {hora_legible(hora_aproximada)}\n"
            f"   Cliente: {cliente_nombre} | Teléfono: {cliente_telefono}"
        )

    try:
        orden = repo.crear(session_id, cliente_nombre, cliente_telefono, int(servicio_id), equipo, descripcion,
                           fecha_entrega, hora_aproximada)
    except ErrorNegocio as exc:
        return mensaje_error_negocio(exc)
    except Exception as exc:
        return error_tecnico("registrar la orden de servicio", exc)
    return (
        f"{PREFIJO_EXITO_CREAR}. Confírmale al cliente el número de orden y el día, y recuérdale que lleve el "
        "equipo a la sede ese día dentro del horario de atención (cópialo tal cual de aquí abajo). Dile que con "
        "el número de orden puede preguntar por el estado de su equipo cuando quiera.\n"
        f"{resumen_orden_servicio(orden, servicio.get('nombre') or f'Servicio #{servicio_id}')}\n"
        f"{_datos_sede()}"
    )


# ---------------------------------------------------------------------------
# Consultar
# ---------------------------------------------------------------------------

def consultar_ordenes_servicio(session_id: str, numero: Optional[int] = None) -> str:
    try:
        if numero is not None:
            orden = repo.leer_del_cliente(session_id, numero)
            ordenes = [orden] if orden else []
        else:
            ordenes = repo.listar_del_cliente(session_id)
        notas = repo.seguimiento([o["numero"] for o in ordenes])
    except Exception as exc:
        return error_tecnico("consultar las órdenes de servicio", exc)

    if not ordenes:
        if numero is not None:
            return mensaje_error_negocio(ErrorNegocio("ORDEN_SERVICIO_NO_ENCONTRADA"))
        return "SIN_ORDENES: el cliente no tiene órdenes de servicio registradas."
    servicios = _nombres_servicios()
    resumenes = "\n".join(
        resumen_orden_servicio(o, _nombre(servicios, o["servicio_id"]), notas.get(o["numero"], [])) for o in ordenes
    )
    return (
        "ÓRDENES DE SERVICIO DEL CLIENTE (de la más reciente a la más antigua). Cuéntale el estado y las "
        "novedades tal como están; nunca inventes diagnósticos, costos ni fechas que no aparezcan aquí.\n"
        + resumenes
    )


# ---------------------------------------------------------------------------
# Modificar y cancelar
# ---------------------------------------------------------------------------

def _orden_modificable(session_id: str, numero: int) -> tuple:
    """`(orden, None)` si el cliente todavía puede cambiarla, o `(None, mensaje)`."""
    try:
        orden = repo.leer_del_cliente(session_id, numero)
    except Exception as exc:
        return None, error_tecnico("consultar la orden de servicio", exc)
    if orden is None:
        return None, mensaje_error_negocio(ErrorNegocio("ORDEN_SERVICIO_NO_ENCONTRADA"))
    if orden["estado"] != repo.ESTADO_PENDIENTE:
        return None, mensaje_error_negocio(ErrorNegocio("ORDEN_SERVICIO_NO_MODIFICABLE", orden["estado"]))
    return orden, None


def modificar_orden_servicio(
    numero: int,
    session_id: str,
    id_turno: str,
    fecha_entrega: Optional[date] = None,
    hora_aproximada: Optional[time] = None,
    cliente_nombre: Optional[str] = None,
    cliente_telefono: Optional[str] = None,
    equipo: Optional[str] = None,
    descripcion: Optional[str] = None,
    servicio_id: Optional[int] = None,
) -> str:
    orden, error = _orden_modificable(session_id, numero)
    if error:
        return error

    # Si cambia el día o la hora, se valida el resultado combinado (ej. pasar
    # al sábado con una hora de la tarde que ese día no aplica).
    if fecha_entrega is not None or hora_aproximada is not None:
        fecha = fecha_entrega or date.fromisoformat(str(orden["fecha_entrega"]))
        hora = hora_aproximada
        if hora is None and orden.get("hora_aproximada"):
            hora = time.fromisoformat(str(orden["hora_aproximada"]))
        error = _validar_agenda(fecha, hora)
        if error:
            return error
    servicios = _nombres_servicios()
    # Si el catálogo no se pudo leer, la base de datos lo valida igual.
    if servicio_id is not None and servicios and int(servicio_id) not in servicios:
        return mensaje_error_negocio(ErrorNegocio("SERVICIO_NO_EXISTE"))

    cambios: list[tuple[str, object, str, Callable]] = [
        ("Día para llevar el equipo", fecha_entrega, dia_legible(orden["fecha_entrega"]), dia_legible),
        ("Hora aproximada", hora_aproximada, hora_legible(orden.get("hora_aproximada")), hora_legible),
        ("Nombre", cliente_nombre, orden.get("cliente_nombre"), str),
        ("Teléfono", cliente_telefono, orden.get("cliente_telefono"), str),
        ("Equipo", equipo, orden.get("equipo"), str),
        ("Problema", descripcion, orden.get("descripcion"), str),
        ("Servicio", servicio_id, _nombre(servicios, orden["servicio_id"]), lambda s: _nombre(servicios, s)),
    ]
    firma = ("modificar_orden_servicio", numero, fecha_entrega, hora_aproximada, cliente_nombre, cliente_telefono,
             equipo, descripcion, servicio_id)
    if requiere_confirmacion(AGENTE, session_id, id_turno, firma):
        detalle = "\n".join(
            f"   {etiqueta}: {anterior} -> {formato(nuevo)}" for etiqueta, nuevo, anterior, formato in cambios
            if nuevo is not None
        )
        return (
            f"CONFIRMACION_PENDIENTE: Todavía NO se ha modificado la orden #{numero}. Muéstrale al cliente estos "
            "cambios (Anterior -> Nuevo) y espera su confirmación explícita en un mensaje NUEVO; luego llama "
            f"Modificar_orden_servicio con EXACTAMENTE estos mismos datos.\nCAMBIOS:\n{detalle}"
        )

    try:
        actualizada = repo.modificar(session_id, numero, fecha_entrega, hora_aproximada, cliente_nombre,
                                     cliente_telefono, equipo, descripcion,
                                     None if servicio_id is None else int(servicio_id))
    except ErrorNegocio as exc:
        return mensaje_error_negocio(exc)
    except Exception as exc:
        return error_tecnico("modificar la orden de servicio", exc)
    return (
        "OK: ORDEN DE SERVICIO ACTUALIZADA. Muéstrale al cliente el resumen y, si cambió el día, recuérdale el "
        "horario de atención de ese día (DATOS DE LA SEDE).\n"
        + resumen_orden_servicio(actualizada, _nombre(servicios, actualizada["servicio_id"]))
    )


def cancelar_orden_servicio(numero: int, session_id: str, id_turno: str) -> str:
    orden, error = _orden_modificable(session_id, numero)
    if error:
        return error
    if requiere_confirmacion(AGENTE, session_id, id_turno, ("cancelar_orden_servicio", numero)):
        return (
            f"CONFIRMACION_PENDIENTE: Todavía NO se ha cancelado la orden #{numero}. Pregúntale al cliente "
            "explícitamente si está seguro de cancelarla (muéstrale el resumen) y espera su respuesta en un "
            "mensaje NUEVO; si confirma, llama Cancelar_orden_servicio con este mismo número.\n"
            + resumen_orden_servicio(orden, _nombre(_nombres_servicios(), orden["servicio_id"]))
        )
    try:
        repo.cancelar(session_id, numero)
    except ErrorNegocio as exc:
        return mensaje_error_negocio(exc)
    except Exception as exc:
        return error_tecnico("cancelar la orden de servicio", exc)
    return (
        f"OK: ORDEN DE SERVICIO #{numero} CANCELADA (estado: {estado_legible(repo.ESTADO_CANCELADO)}). "
        "Confírmaselo al cliente y dile que si más adelante necesita el servicio, con gusto le agendas otro día."
    )
