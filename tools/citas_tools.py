"""
tools/citas_tools.py  (antes tools/calendar_tools.py)

Gestiona el ciclo de vida completo de una cita — consultar disponibilidad,
crear, modificar, cancelar — usando Supabase como ÚNICA fuente de verdad.
Google Calendar YA NO se usa: se quitó porque la versión final de este
negocio no va a registrar las citas en Calendar (tendrán su propia web
application de agendamiento, alimentada por esta misma base de datos), y
porque mantener dos sistemas sincronizados era la fuente de la mayoría de
los bugs reales encontrados en la iteración anterior:
- El desfase de zona horaria (una cita de las 4:00 PM de Bogotá terminaba
  guardada como si esas 4:00 ya fueran UTC), causado por confiar en el
  formato de fecha que devolvía la respuesta de la API de Calendar.
- La complejidad de hacer rollback entre dos sistemas si el segundo paso
  fallaba después de que el primero ya había tenido éxito.
- El riesgo de que la autenticación OAuth interactiva de Calendar
  (`InstalledAppFlow.run_local_server`, que abre un navegador de verdad)
  fuera imposible de completar en un servidor sin pantalla como Render.

Qué cambia respecto a antes:
- {Consultar_eventos} ya no golpea la API de Calendar: consulta Supabase
  filtrando por solapamiento de horario (`citas_repository.
  leer_citas_confirmadas_en_rango`).
- {Crear_evento}/{Actualizar_evento} son ahora una sola escritura a
  Supabase — no hay un segundo sistema que sincronizar, así que tampoco hay
  nada que revertir si algo falla.
- La propia base de datos garantiza, con el constraint
  `no_solapamiento_citas_confirmadas` (ver
  `sql/002_disponibilidad_sin_calendar.sql`), que dos citas "confirmado"
  nunca puedan cruzarse en el tiempo — antes esto dependía por completo de
  que el LLM calculara bien la disponibilidad leyendo texto. Si de todas
  formas dos conversaciones casi simultáneas intentan agendar el mismo
  horario, Postgres rechaza la segunda escritura de forma atómica, y ese
  caso se traduce aquí en un mensaje amable ("ese horario ya no está
  disponible") en vez de un error técnico.

Nota sobre el nombre `google_calendar_event_id`: se conserva tal cual en el
código y en el prompt de negocio (`agents/servicio_tecnico_agent.py`, que lo
menciona por ese nombre en decenas de lugares) para no tener que reescribir
la transcripción fiel del prompt original. Pero ya NO es un ID de Google
Calendar — ahora es un identificador opaco (UUID) generado por
`crear_evento` al crear la cita. Para el modelo es indistinguible: sigue
siendo "el identificador que se obtiene de Consultar_servicio_agendado y se
usa en Actualizar_evento/Eliminar_evento", solo que ya no viene de una API
externa.

Cada función pública sigue devolviendo un STRING — el mismo "resumen" que
exige el prompt del Agente de Servicio Técnico ("nunca reconstruyas el
resumen de memoria"). Ese string se arma leyendo la fila REAL de Supabase
después de escribir (ver `tools.supabase_client.formatear_fila`), no a mano
con una lista fija de campos.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from tools import supabase_client
from tools.citas_repository import (
    CAMPO_FECHA_FIN_DB,
    actualizar_cita,
    cancelar_cita,
    insertar_cita,
    leer_cita_por_event_id,
    leer_citas_confirmadas_en_rango,
)

_DIAS = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
_MESES = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]


def formatear_fecha_legible(iso_str: str) -> str:
    """'2026-07-28T11:00:00-05:00' -> 'Martes 28 de Julio de 2026 a las 11:00 AM'."""
    dt = datetime.fromisoformat(iso_str)
    dia_semana = _DIAS[dt.weekday()]
    mes = _MESES[dt.month - 1].capitalize()
    hora = dt.strftime("%I:%M %p").lstrip("0")
    return f"{dia_semana} {dt.day} de {mes} de {dt.year} a las {hora}"


def zona_horaria_configurada() -> timezone:
    """Lee `TIMEZONE_OFFSET` del `.env` (ej. "-05:00") y arma un `timezone`
    de Python con ese desfase. Si el valor falta o viene mal formado, cae en
    UTC-05:00 (Colombia) como valor por defecto razonable para este negocio.

    Única fuente de la zona horaria del negocio: la reutilizan tanto
    `normalizar_fecha_hora` (abajo) como `agents/servicio_tecnico_agent.py`
    para anunciarle al modelo la fecha/hora actuales en cada turno."""
    crudo = os.environ.get("TIMEZONE_OFFSET", "-05:00")
    signo = -1 if crudo.startswith("-") else 1
    crudo = crudo.lstrip("+-")
    horas_str, _, minutos_str = crudo.partition(":")
    try:
        horas, minutos = int(horas_str), int(minutos_str or "0")
    except ValueError:
        horas, minutos, signo = 5, 0, -1
    return timezone(signo * timedelta(hours=horas, minutes=minutos))


def normalizar_fecha_hora(iso_str: str) -> str:
    """Garantiza que un datetime ISO 8601 tenga SIEMPRE un offset de zona
    horaria explícito antes de guardarse en Supabase — si llega "naive"
    (sin offset), se asume que ya está en la zona horaria del negocio
    (`TIMEZONE_OFFSET`). Bug real que esto corrige: ver el docstring del
    módulo (desfase de 5 horas en una cita reprogramada)."""
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=zona_horaria_configurada())
    return dt.isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# {Consultar_eventos}
# ---------------------------------------------------------------------------

def consultar_eventos(fecha_inicio: str, fecha_fin: str) -> str:
    """Devuelve, en el mismo formato de texto de siempre, los horarios ya
    ocupados (citas confirmadas) dentro de `[fecha_inicio, fecha_fin)`.

    Es una operación de solo lectura sobre Supabase — antes hacía lo mismo
    contra la API de Calendar; el formato de salida no cambió para no tener
    que ajustar el prompt.
    """
    try:
        citas = leer_citas_confirmadas_en_rango(fecha_inicio, fecha_fin)
    except Exception as exc:
        return (
            f"ERROR: No se pudo consultar la disponibilidad. "
            f"Rango solicitado: {fecha_inicio} a {fecha_fin} | Estado: fallido | "
            f"Detalle técnico: {exc}"
        )

    if not citas:
        return "Sin eventos ocupados en el rango consultado"

    por_fecha: dict = {}
    for cita in citas:
        inicio = cita["fecha_hora_inicio"]
        fin = cita[CAMPO_FECHA_FIN_DB]
        dt_inicio = datetime.fromisoformat(inicio)
        dt_fin = datetime.fromisoformat(fin)
        clave = dt_inicio.date().isoformat()
        legible = formatear_fecha_legible(inicio).split(" a las")[0]
        por_fecha.setdefault(clave, {"legible": legible, "rangos": []})
        por_fecha[clave]["rangos"].append(
            f"{dt_inicio.strftime('%I:%M %p').lstrip('0')} a {dt_fin.strftime('%I:%M %p').lstrip('0')}"
        )

    bloques = [
        f"{datos['legible']}: ocupado {', '.join(datos['rangos'])}"
        for _, datos in sorted(por_fecha.items())
    ]
    return " | ".join(bloques)


# ---------------------------------------------------------------------------
# {Crear_evento}
# ---------------------------------------------------------------------------

def crear_evento(
    servicio_id: str,
    cliente_nombre: str,
    cliente_telefono: str,
    descripcion: str,
    fecha_hora_inicio: str,
    fecha_hora_fin: str,
) -> str:
    """Crea la cita con una sola escritura a Supabase. Si el horario ya
    choca con otra cita confirmada, la propia base de datos rechaza la
    escritura (constraint `no_solapamiento_citas_confirmadas`) y esa
    situación se traduce en un mensaje amable, no en un error técnico.

    El resumen final se arma a partir de la fila REAL devuelta por Supabase,
    no de los parámetros de entrada — así incluye cualquier columna que la
    tabla tenga (ver `tools.supabase_client.formatear_fila`).
    """
    fecha_hora_inicio = normalizar_fecha_hora(fecha_hora_inicio)
    fecha_hora_fin = normalizar_fecha_hora(fecha_hora_fin)
    event_id = uuid.uuid4().hex

    try:
        fila = insertar_cita(
            {
                "cliente_nombre": cliente_nombre,
                "cliente_telefono": cliente_telefono,
                "servicio_id": servicio_id,
                "fecha_hora_inicio": fecha_hora_inicio,
                CAMPO_FECHA_FIN_DB: fecha_hora_fin,
                "google_calendar_event_id": event_id,
                "estado": "confirmado",
                "Descripcion": descripcion,
                "session_id": cliente_telefono,
            }
        )
    except requests.exceptions.HTTPError as exc:
        if supabase_client.es_violacion_de_solapamiento(exc):
            return (
                f"ERROR: Ese horario ya no está disponible (se cruza con otra cita ya confirmada). "
                f"Servicio ID: {servicio_id} | Cliente: {cliente_nombre} | "
                f"Fecha solicitada: {fecha_hora_inicio} | Estado: fallido"
            )
        return (
            f"ERROR: No se pudo guardar la cita en este momento. Servicio ID: {servicio_id} | "
            f"Cliente: {cliente_nombre} | Teléfono: {cliente_telefono} | "
            f"Fecha solicitada: {fecha_hora_inicio} | Estado: fallido | Detalle técnico: {exc}"
        )
    except Exception as exc:
        return (
            f"ERROR: No se pudo guardar la cita en este momento. Servicio ID: {servicio_id} | "
            f"Cliente: {cliente_nombre} | Teléfono: {cliente_telefono} | "
            f"Fecha solicitada: {fecha_hora_inicio} | Estado: fallido | Detalle técnico: {exc}"
        )

    inicio_legible = formatear_fecha_legible(fila["fecha_hora_inicio"])
    fin_legible = formatear_fecha_legible(fila[CAMPO_FECHA_FIN_DB])
    return (
        f"Inicio: {inicio_legible} | Fin: {fin_legible} | "
        f"{supabase_client.formatear_fila(fila)}"
    )


# ---------------------------------------------------------------------------
# {Actualizar_evento}
# ---------------------------------------------------------------------------

def actualizar_evento(
    google_calendar_event_id: str,
    fecha_hora_inicio: Optional[str] = None,
    fecha_hora_fin: Optional[str] = None,
    cliente_nombre: Optional[str] = None,
    cliente_telefono: Optional[str] = None,
    descripcion: Optional[str] = None,
    servicio_id: Optional[str] = None,
) -> str:
    """Modifica una cita ya agendada con una sola escritura a Supabase. Si
    el nuevo horario choca con otra cita confirmada, la base de datos
    rechaza el cambio (mismo constraint que en `crear_evento`) y nada queda
    a medias — no hay Calendar que revertir.
    """
    registro_original = leer_cita_por_event_id(google_calendar_event_id)
    if registro_original is None:
        return (
            f"ERROR: No se encontró ninguna cita con Event ID {google_calendar_event_id} "
            f"para actualizar. Estado: fallido"
        )

    if fecha_hora_inicio:
        fecha_hora_inicio = normalizar_fecha_hora(fecha_hora_inicio)
    if fecha_hora_fin:
        fecha_hora_fin = normalizar_fecha_hora(fecha_hora_fin)

    patch_body = {
        "cliente_nombre": cliente_nombre or registro_original["cliente_nombre"],
        "cliente_telefono": cliente_telefono or registro_original["cliente_telefono"],
        "servicio_id": servicio_id or registro_original["servicio_id"],
        "fecha_hora_inicio": fecha_hora_inicio or registro_original["fecha_hora_inicio"],
        CAMPO_FECHA_FIN_DB: fecha_hora_fin or registro_original[CAMPO_FECHA_FIN_DB],
        "Descripcion": descripcion if descripcion is not None else registro_original.get("Descripcion", ""),
    }

    try:
        fila = actualizar_cita(google_calendar_event_id, patch_body)
    except requests.exceptions.HTTPError as exc:
        if supabase_client.es_violacion_de_solapamiento(exc):
            return (
                "ERROR: Ese nuevo horario ya no está disponible (se cruza con otra cita ya "
                "confirmada). No se modificó nada de la cita original. Estado: fallido"
            )
        return (
            f"ERROR: No fue posible aplicar el cambio en este momento. Estado: fallido | "
            f"Detalle técnico: {exc}"
        )
    except Exception as exc:
        return (
            f"ERROR: No fue posible aplicar el cambio en este momento. Estado: fallido | "
            f"Detalle técnico: {exc}"
        )

    inicio_legible = formatear_fecha_legible(fila["fecha_hora_inicio"])
    fin_legible = formatear_fecha_legible(fila[CAMPO_FECHA_FIN_DB])
    return (
        f"Cita actualizada. Inicio: {inicio_legible} | Fin: {fin_legible} | "
        f"{supabase_client.formatear_fila(fila)}"
    )


# ---------------------------------------------------------------------------
# {Eliminar_evento}
# ---------------------------------------------------------------------------

def eliminar_evento(google_calendar_event_id: str) -> str:
    """Cancela una cita: marca su `estado` como cancelada (soft-delete, ver
    `citas_repository.cancelar_cita`) con una sola escritura a Supabase —
    ya no hay que eliminar nada en Calendar primero.
    """
    try:
        fila_cancelada = cancelar_cita(google_calendar_event_id)
    except RuntimeError:
        return (
            f"ERROR: No se encontró ningún registro activo con Event ID {google_calendar_event_id} "
            f"para cancelar. Estado: fallido"
        )
    except Exception as exc:
        return (
            f"ERROR: No fue posible cancelar la cita en este momento. Estado: fallido | "
            f"Detalle técnico: {exc}"
        )

    inicio_legible = formatear_fecha_legible(fila_cancelada["fecha_hora_inicio"])
    return (
        f"Cita cancelada exitosamente. Inicio original: {inicio_legible} | "
        f"{supabase_client.formatear_fila(fila_cancelada)}"
    )
