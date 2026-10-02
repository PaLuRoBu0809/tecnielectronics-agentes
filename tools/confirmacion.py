"""
tools/confirmacion.py

Confirmación explícita obligatoria antes de escribir, como garantía de
CÓDIGO y no solo de prompt. La comparten todos los agentes: Servicio
Técnico (crear/actualizar/cancelar cita) y Ventas (crear/modificar/cancelar
orden).

Por qué existe: los prompts exigen mostrar un resumen y esperar un "sí" del
cliente antes de escribir, pero un modelo puede saltárselo. Bug real: un
modelo de 120B llamó a Crear_evento en el mismo turno en que el cliente
eligió la hora, sin mostrar resumen. Por eso cada escritura es un handshake
de 2 pasos:

1. La primera vez que se propone una combinación de datos (`firma`) NO se
   escribe nada: la tool devuelve el resumen marcado CONFIRMACION_PENDIENTE
   y aquí se recuerda la propuesta.
2. Solo se escribe si esa MISMA propuesta se repite en un TURNO posterior
   (un mensaje nuevo del cliente). Reintentar dentro del mismo turno no
   cuenta: así el modelo no puede "autoconfirmarse".

`id_turno` es el `run_id` del turno (uno por mensaje del cliente, ver
`agents/orquestador.py`). No se compara el texto del mensaje: el
orquestador puede parafrasear dos mensajes distintos con el mismo texto
(ver docs/PLAN_DE_MEJORAS.md, Fase 5).

Cada agente tiene su propia propuesta pendiente por cliente (clave
`(agente, session_id)`): una orden pendiente de Ventas nunca pisa una cita
pendiente de Servicio Técnico del mismo cliente.

Limitación conocida: las propuestas viven en RAM, no en Supabase. Es a
propósito: el peor caso de perderlas (un reinicio a mitad de la
confirmación) es pedirle al cliente que confirme una vez más, nunca
escribir sin permiso. Con más de un worker/proceso habría que moverlas a una
tabla (mismo patrón que `sesiones.py`), porque cada proceso tendría su copia.
"""
from __future__ import annotations

# {(agente, session_id): (firma, id_turno_que_la_propuso)}
_propuestas_pendientes: dict = {}


def requiere_confirmacion(agente: str, session_id: str, id_turno: str, firma: tuple) -> bool:
    """`True` = NO escribir todavía: mostrar el resumen y esperar al cliente.

    Devuelve `True` si:
    - es la primera vez que este agente propone esta `firma` a este cliente, o
    - ya se había propuesto, pero en ESTE mismo turno (el modelo está
      reintentando sin que el cliente haya escrito nada nuevo).

    Devuelve `False` (escribir) únicamente si ya existía exactamente esa
    misma propuesta y estamos en un turno posterior: la única situación que
    puede corresponder a un "sí" real del cliente. La propuesta se consume:
    una nueva escritura vuelve a pedir confirmación.
    """
    clave = (agente, session_id)
    pendiente = _propuestas_pendientes.get(clave)
    if pendiente is None or pendiente[0] != firma or pendiente[1] == id_turno:
        _propuestas_pendientes[clave] = (firma, id_turno)
        return True
    del _propuestas_pendientes[clave]
    return False
