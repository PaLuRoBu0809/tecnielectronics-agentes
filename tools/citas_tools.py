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
  `supabase/migrations/20260927000002_disponibilidad_sin_calendar.sql`), que dos citas "confirmado"
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

Agendamiento por técnico (ver `supabase/migrations/20260928000003_tecnicos.sql`): cada tipo de servicio
tiene un técnico responsable (columna `tecnico_id` del catálogo), resuelto
por `tools.catalog_tools.resolver_tecnico_para_servicio`. La disponibilidad
y el constraint anti-choque de la base de datos pasaron de ser GLOBALES a
estar acotados POR TÉCNICO: dos técnicos distintos sí pueden tener citas a
la misma hora. Por eso {Consultar_eventos} ahora EXIGE `servicio_id` — sin
saber el servicio no se puede saber de qué técnico consultar la agenda. La
asignación del técnico a cada cita es automática (el agente nunca la pide
ni la menciona al cliente, ver la nota del prompt en
`agents/servicio_tecnico_agent.py`).

Salvaguarda de confirmación explícita: las 3 tools de escritura
({Crear_evento}/{Actualizar_evento}/{Eliminar_evento}) fuerzan un handshake
de 2 pasos A NIVEL DE CÓDIGO (Regla 3 del prompt): la primera vez devuelven
el resumen marcado "CONFIRMACION_PENDIENTE" sin escribir nada, y solo
escriben si la MISMA propuesta se repite en un turno posterior. La lógica
la comparte con el Agente de Ventas en `tools/confirmacion.py`, donde está
explicado el porqué. `session_id` y `id_turno` se inyectan desde
`agents/servicio_tecnico_agent.py` (el modelo nunca los ve ni los pasa).
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, time, timedelta, timezone
from typing import Optional

import requests

from tools import supabase_client
from tools.confirmacion import requiere_confirmacion
from tools.catalog_tools import leer_servicio, resolver_tecnico_para_servicio
from tools.citas_repository import (
    CAMPO_FECHA_FIN_DB,
    ESTADO_CONFIRMADO,
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
    """'2026-07-28T11:00:00-05:00' -> 'Martes 28 de Julio de 2026 a las 11:00 AM'.

    SIEMPRE convierte a la zona horaria del negocio antes de formatear. Bug
    real detectado probando un agendamiento real: Supabase devuelve todo
    `timestamptz` normalizado a UTC (comportamiento estándar de Postgres,
    sin importar en qué offset se haya insertado el valor) — sin esta
    conversión, una cita de las 10:00 AM Colombia (15:00 UTC) se le mostraba
    al cliente como "3:00 PM" en la confirmación final de {Crear_evento}.
    """
    dt = datetime.fromisoformat(iso_str).astimezone(zona_horaria_configurada())
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
# Reglas de agenda en código (Fase 4 de docs/PLAN_DE_MEJORAS.md)
# ---------------------------------------------------------------------------
# Antes estas reglas vivían SOLO en el prompt (Regla 4 de
# <REGLAS_INFALIBLES_Y_RESTRICCIONES> y la descripción de fecha_hora_fin):
# nada en el código impedía agendar un domingo a las 3 AM, en el pasado, o
# con una duración calculada mal por el modelo. Ahora se validan antes de la
# salvaguarda de confirmación, así el cliente nunca confirma algo inválido.
# El prompt no cambió: estas reglas solo hacen cumplir lo que ya decía.

HORA_APERTURA = time(7, 0)
HORA_CIERRE = time(18, 0)


def _ahora() -> datetime:
    """Hora actual en la zona del negocio (función aparte para poder fijarla en tests)."""
    return datetime.now(zona_horaria_configurada())


def validar_horario_cita(fecha_hora_inicio: str, fecha_hora_fin: str, duracion_minutos=None) -> Optional[str]:
    """Devuelve `None` si el horario cumple las reglas del negocio, o un
    texto "ERROR: ..." que el modelo puede leer y corregir:
    - fin posterior a inicio, e inicio en el futuro;
    - lunes a viernes, entre 7:00 AM y 6:00 PM, inicio y fin el mismo día;
    - duración exacta = `duracion_minutos` del catálogo (si se conoce)."""
    tz = zona_horaria_configurada()
    inicio = datetime.fromisoformat(fecha_hora_inicio).astimezone(tz)
    fin = datetime.fromisoformat(fecha_hora_fin).astimezone(tz)
    sufijo = " No se guardó nada. Corrige el horario antes de mostrarle un resumen al cliente. Estado: fallido"
    if fin <= inicio:
        return "ERROR: fecha_hora_fin debe ser posterior a fecha_hora_inicio." + sufijo
    if inicio <= _ahora():
        return "ERROR: La fecha y hora elegidas ya pasaron; solo se pueden agendar horarios futuros." + sufijo
    if inicio.weekday() >= 5:
        return "ERROR: Solo se atiende de lunes a viernes; la fecha elegida cae en fin de semana." + sufijo
    if inicio.date() != fin.date() or inicio.time() < HORA_APERTURA or fin.time() > HORA_CIERRE:
        return "ERROR: La cita debe empezar y terminar entre 7:00 AM y 6:00 PM del mismo día." + sufijo
    try:
        duracion = int(duracion_minutos) if duracion_minutos not in (None, "") else None
    except (TypeError, ValueError):
        duracion = None
    if duracion and fin - inicio != timedelta(minutes=duracion):
        fin_correcto = (inicio + timedelta(minutes=duracion)).isoformat(timespec="seconds")
        return (
            f"ERROR: Este servicio dura {duracion} minutos (duracion_minutos del catálogo), así que "
            f"fecha_hora_fin debe ser {fin_correcto}." + sufijo
        )
    return None


def _servicio_con_tecnico(servicio_id) -> tuple:
    """Lee el servicio del catálogo y verifica que tenga técnico asignado.
    Devuelve `(fila_servicio, None)` o `(None, mensaje_de_error)`."""
    try:
        fila = leer_servicio(servicio_id)
    except Exception as exc:
        return None, f"ERROR: No se pudo consultar el catálogo de servicios. Estado: fallido | Detalle técnico: {exc}"
    if fila is None:
        return None, (
            f"ERROR: No existe el servicio_id {servicio_id} en el catálogo. Vuelve a consultar "
            f"Servicio_tecnico. Estado: fallido"
        )
    if fila.get("tecnico_id") is None:
        return None, (
            f"ERROR: No hay un técnico configurado para el servicio_id {servicio_id}, no se pudo "
            f"procesar la cita. Servicio ID: {servicio_id} | Estado: fallido"
        )
    return fila, None


AGENTE = "servicio_tecnico"


def _requiere_confirmacion(session_id: str, id_turno: str, firma: tuple) -> bool:
    """Ver `tools/confirmacion.py`."""
    return requiere_confirmacion(AGENTE, session_id, id_turno, firma)


def _cita_activa_del_cliente(google_calendar_event_id: str, session_id: str):
    """Localiza la cita que el modelo quiere modificar o cancelar y verifica
    que (a) pertenece a ESTE cliente y (b) sigue activa.

    Devuelve `(fila, None)` si todo está bien, o `(None, mensaje_de_error)`.
    Si la cita existe pero es de otro cliente, el mensaje es IDÉNTICO al de
    "no encontrada" a propósito: no se le revela a nadie que ese
    identificador existe. Antes, cualquiera que obtuviera un Event ID
    (por ejemplo, convenciendo al modelo por prompt injection) podía
    modificar o cancelar la cita de otro cliente."""
    no_encontrada = (
        f"ERROR: No se encontró ninguna cita activa con Event ID {google_calendar_event_id} "
        f"para este cliente. Vuelve a consultar Consultar_servicio_agendado. Estado: fallido"
    )
    try:
        fila = leer_cita_por_event_id(google_calendar_event_id)
    except Exception as exc:
        return None, f"ERROR: No se pudo consultar la cita en este momento. Estado: fallido | Detalle técnico: {exc}"
    if fila is None or fila.get("session_id") != session_id:
        return None, no_encontrada
    if fila.get("estado") != ESTADO_CONFIRMADO:
        return None, (
            f"ERROR: La cita con Event ID {google_calendar_event_id} no está activa "
            f"(estado actual: {fila.get('estado')}). No se modificó nada. Estado: fallido"
        )
    return fila, None


# ---------------------------------------------------------------------------
# {Consultar_eventos}
# ---------------------------------------------------------------------------

def consultar_eventos(fecha_inicio: str, fecha_fin: str, servicio_id: str) -> str:
    """Devuelve, en el mismo formato de texto de siempre, los horarios ya
    ocupados (citas confirmadas) dentro de `[fecha_inicio, fecha_fin)` —
    PERO solo del técnico que atiende `servicio_id`, no de toda la empresa
    (ver la nota de agendamiento por técnico en el docstring del módulo).

    Es una operación de solo lectura sobre Supabase — antes hacía lo mismo
    contra la API de Calendar; el formato de salida no cambió para no tener
    que ajustar el prompt.
    """
    tecnico_id = resolver_tecnico_para_servicio(servicio_id)
    if tecnico_id is None:
        return (
            f"ERROR: No hay un técnico configurado para el servicio_id {servicio_id}. "
            f"No es posible calcular disponibilidad en este momento. Estado: fallido"
        )

    try:
        citas = leer_citas_confirmadas_en_rango(fecha_inicio, fecha_fin, tecnico_id=tecnico_id)
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
        # Supabase devuelve fecha_hora_inicio/fin en UTC crudo (ver el
        # docstring de formatear_fecha_legible) — se convierte aquí también,
        # porque estas horas se formatean directo con strftime en vez de
        # pasar por formatear_fecha_legible, y el agrupado por día (`clave`)
        # debe hacerse sobre la fecha de Colombia, no la de UTC (una cita de
        # las 7:00 PM Colombia ya es medianoche del día siguiente en UTC).
        dt_inicio = datetime.fromisoformat(inicio).astimezone(zona_horaria_configurada())
        dt_fin = datetime.fromisoformat(fin).astimezone(zona_horaria_configurada())
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
    session_id: str,
    id_turno: str,
) -> str:
    """Crea la cita con una sola escritura a Supabase. Si el horario ya
    choca con otra cita confirmada, la propia base de datos rechaza la
    escritura (constraint `no_solapamiento_citas_confirmadas`) y esa
    situación se traduce en un mensaje amable, no en un error técnico.

    El resumen final se arma a partir de la fila REAL devuelta por Supabase,
    no de los parámetros de entrada — así incluye cualquier columna que la
    tabla tenga (ver `tools.supabase_client.formatear_fila`).

    `session_id`/`id_turno` los inyecta
    `agents/servicio_tecnico_agent.py` (no son parámetros que el modelo vea
    ni pase) — ver `_requiere_confirmacion` en el docstring del módulo: la
    primera llamada con una combinación de datos nueva NUNCA escribe, solo
    devuelve el resumen pendiente de confirmar.

    Bug real detectado probando el flujo de consulta de citas: la fila se
    guardaba con `"session_id": cliente_telefono` (el teléfono de CONTACTO
    que el cliente dicta en la conversación), en vez de `session_id` real de
    la conversación. Ambos suelen coincidir en producción (en WhatsApp real,
    el session_id ES el teléfono desde el que escribe el cliente), pero en
    esta interfaz de pruebas el "teléfono simulado" de la sesión y el
    "teléfono de contacto" que se dicta en el flujo pueden ser valores
    distintos a propósito (para simular clientes) — si un cliente agenda
    una cita dando un teléfono de contacto distinto al de su sesión activa,
    {Consultar_servicio_agendado} (que sí filtra por el session_id real)
    nunca encontraba esa cita después, aunque existiera. Ahora se guarda el
    `session_id` real inyectado, no `cliente_telefono`.
    """
    fecha_hora_inicio = normalizar_fecha_hora(fecha_hora_inicio)
    fecha_hora_fin = normalizar_fecha_hora(fecha_hora_fin)

    # El técnico y las reglas de horario se verifican ANTES de la salvaguarda
    # de confirmación: si el servicio no tiene técnico o el horario es
    # inválido, la cita nunca podría agendarse sin importar cuántas veces se
    # confirme, así que no tiene sentido pedirle al cliente que confirme algo
    # imposible de cumplir.
    servicio, error = _servicio_con_tecnico(servicio_id)
    if error:
        return error
    tecnico_id = servicio["tecnico_id"]
    error = validar_horario_cita(fecha_hora_inicio, fecha_hora_fin, servicio.get("duracion_minutos"))
    if error:
        return error

    firma = (
        "crear", str(servicio_id), cliente_nombre, cliente_telefono, descripcion,
        fecha_hora_inicio, fecha_hora_fin,
    )
    if _requiere_confirmacion(session_id, id_turno, firma):
        inicio_legible = formatear_fecha_legible(fecha_hora_inicio)
        fin_legible = formatear_fecha_legible(fecha_hora_fin)
        return (
            "CONFIRMACION_PENDIENTE: Todavía NO se ha agendado nada en el sistema. Muestra este "
            "resumen al cliente (Resumen y Confirmación) y espera su 'sí' explícito en un mensaje "
            "NUEVO antes de volver a llamar Crear_evento con estos MISMOS datos exactos. Nunca le "
            "digas al cliente que la cita ya quedó agendada. "
            f"Servicio ID: {servicio_id} | Cliente: {cliente_nombre} | Teléfono: {cliente_telefono} | "
            f"Descripción: {descripcion} | Inicio: {inicio_legible} | Fin: {fin_legible} | "
            f"Estado: pendiente_confirmacion"
        )

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
                "session_id": session_id,
                "tecnico_id": tecnico_id,
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
    session_id: str,
    id_turno: str,
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

    `session_id`/`id_turno` los inyecta
    `agents/servicio_tecnico_agent.py` — ver `_requiere_confirmacion`: la
    primera llamada con un cambio nuevo NUNCA escribe, solo devuelve el
    resumen pendiente de confirmar.
    """
    registro_original, error = _cita_activa_del_cliente(google_calendar_event_id, session_id)
    if error:
        return error

    if fecha_hora_inicio:
        fecha_hora_inicio = normalizar_fecha_hora(fecha_hora_inicio)
    if fecha_hora_fin:
        fecha_hora_fin = normalizar_fecha_hora(fecha_hora_fin)

    # Igual que en crear_evento: técnico y reglas de horario se verifican
    # ANTES de la salvaguarda de confirmación. Solo hace falta si cambia el
    # horario o el servicio (un cambio de nombre/teléfono/descripción no
    # afecta la agenda). El horario EFECTIVO combina lo nuevo con lo que ya
    # tenía la cita, y la duración sale del servicio efectivo — así se
    # detecta, por ejemplo, un cambio a un servicio más largo que ya no cabe.
    tecnico_id = registro_original.get("tecnico_id")
    if fecha_hora_inicio or fecha_hora_fin or servicio_id:
        servicio, error = _servicio_con_tecnico(servicio_id or registro_original["servicio_id"])
        if error:
            return error + " No se modificó nada de la cita original."
        # Citas creadas antes de la migración 003 pueden no tener técnico: se
        # completa con el del servicio al moverlas.
        if servicio_id or tecnico_id is None:
            tecnico_id = servicio["tecnico_id"]
        error = validar_horario_cita(
            fecha_hora_inicio or registro_original["fecha_hora_inicio"],
            fecha_hora_fin or registro_original[CAMPO_FECHA_FIN_DB],
            servicio.get("duracion_minutos"),
        )
        if error:
            return error

    firma = (
        "actualizar", google_calendar_event_id, fecha_hora_inicio, fecha_hora_fin,
        cliente_nombre, cliente_telefono, descripcion, str(servicio_id) if servicio_id else None,
    )
    if _requiere_confirmacion(session_id, id_turno, firma):
        return (
            "CONFIRMACION_PENDIENTE: Todavía NO se ha modificado nada. Muestra el resumen del "
            "cambio (Anterior -> Nuevo, campos que cambian) y espera el 'sí' explícito del cliente "
            "en un mensaje NUEVO antes de volver a llamar Actualizar_evento con estos MISMOS datos. "
            "Nunca le digas al cliente que el cambio ya se aplicó. Estado: pendiente_confirmacion"
        )

    patch_body = {
        "cliente_nombre": cliente_nombre or registro_original["cliente_nombre"],
        "cliente_telefono": cliente_telefono or registro_original["cliente_telefono"],
        "servicio_id": servicio_id or registro_original["servicio_id"],
        "fecha_hora_inicio": fecha_hora_inicio or registro_original["fecha_hora_inicio"],
        CAMPO_FECHA_FIN_DB: fecha_hora_fin or registro_original[CAMPO_FECHA_FIN_DB],
        "Descripcion": descripcion if descripcion is not None else registro_original.get("Descripcion", ""),
        "tecnico_id": tecnico_id,
    }

    try:
        fila = actualizar_cita(google_calendar_event_id, patch_body, session_id=session_id)
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

def eliminar_evento(google_calendar_event_id: str, session_id: str, id_turno: str) -> str:
    """Cancela una cita: marca su `estado` como cancelada (soft-delete, ver
    `citas_repository.cancelar_cita`) con una sola escritura a Supabase —
    ya no hay que eliminar nada en Calendar primero.

    `session_id`/`id_turno` los inyecta
    `agents/servicio_tecnico_agent.py` — ver `_requiere_confirmacion`: la
    primera llamada NUNCA cancela, solo devuelve la confirmación pendiente
    ("¿seguro que deseas cancelar?" ya lo exige el prompt en FASE 4, esto lo
    hace también obligatorio a nivel de código).

    Antes de la salvaguarda se verifica que la cita es de este cliente y
    sigue activa (`_cita_activa_del_cliente`): no tiene sentido pedir
    confirmación para cancelar algo que no se puede cancelar.
    """
    _, error = _cita_activa_del_cliente(google_calendar_event_id, session_id)
    if error:
        return error

    firma = ("eliminar", google_calendar_event_id)
    if _requiere_confirmacion(session_id, id_turno, firma):
        return (
            "CONFIRMACION_PENDIENTE: Todavía NO se ha cancelado nada. Pregunta explícitamente si "
            "el cliente está seguro de cancelar y espera su respuesta afirmativa en un mensaje "
            "NUEVO antes de volver a llamar Eliminar_evento con el mismo Event ID. Nunca le digas "
            "al cliente que la cita ya quedó cancelada. Estado: pendiente_confirmacion"
        )

    try:
        fila_cancelada = cancelar_cita(google_calendar_event_id, session_id=session_id)
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
