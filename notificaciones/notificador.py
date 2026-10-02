"""
notificaciones/notificador.py

Cómo le llega al cliente un mensaje que el sistema envía por su cuenta.

`Notificador` es la interfaz: `enviar(session_id, texto)`. Quien produce el
aviso (`tools/servicio_pagos.py`) no sabe por qué canal sale.

- `NotificadorHistorial` (hoy): agrega el mensaje a la conversación guardada
  del cliente (hilos del orquestador y de Ventas), así los agentes saben que
  ya se le avisó y lo ven en su contexto, y lo publica en "Flujo en Vivo".
- Cuando exista la integración con WhatsApp (docs/PLAN_INTEGRACION_WHATSAPP.md,
  fase de envío), se agrega un notificador que además lo envíe por WhatsApp,
  y nada más cambia.

Usa el mismo candado por sesión que los turnos (`turno_exclusivo`): si el
cliente está escribiendo en ese momento, espera a que termine su turno en
vez de pisarle el historial. Si no termina a tiempo, lanza `TurnoEnCurso` y
quien llama reintenta más tarde (el webhook responde error y MercadoPago
reenvía el aviso).
"""
from __future__ import annotations

from typing import Protocol

from tools import eventos_agente

# Hilos donde queda registrado el aviso: el orquestador (que habla con el
# cliente) y Ventas (que lleva los pedidos).
HILOS = ("orquestador", "ventas")


class Notificador(Protocol):
    def enviar(self, session_id: str, texto: str) -> None: ...


class NotificadorHistorial:
    def __init__(self, almacen):
        self._almacen = almacen

    def enviar(self, session_id: str, texto: str) -> None:
        with self._almacen.turno_exclusivo(session_id):
            historiales = self._almacen.obtener(session_id)
            for hilo in HILOS:
                historiales[hilo] = historiales.get(hilo, []) + [{"role": "assistant", "content": texto}]
            self._almacen.guardar(session_id, historiales)
        eventos_agente.publicar_evento("notificacion_cliente", session_id=session_id, texto=texto)
