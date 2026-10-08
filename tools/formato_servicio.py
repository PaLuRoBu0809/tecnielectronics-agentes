"""
tools/formato_servicio.py

Texto que las tools de Servicio Técnico le devuelven al MODELO: resumen de
una orden de servicio (con sus novedades) y traducción de los errores de
negocio. Funciones puras, igual que `tools/formato_ventas.py`.

El responsable de cada nota NO se incluye: es un dato interno de la empresa
(el agente no menciona técnicos ni empleados).
"""
from __future__ import annotations

from datetime import date, time
from typing import Optional

from tools.errores_negocio import ErrorNegocio
from tools.fechas import formatear_dia, formatear_fecha_legible, formatear_hora

ESTADOS_LEGIBLES = {
    "PENDIENTE_RECEPCION": "Pendiente de recibir (el cliente todavía no ha llevado el equipo)",
    "RECIBIDO": "Recibido en la sede",
    "EN_DIAGNOSTICO": "En diagnóstico",
    "EN_REPARACION": "En reparación",
    "LISTO_PARA_RECOGER": "Listo para recoger",
    "ENTREGADO": "Entregado al cliente",
    "CANCELADO": "Cancelada",
}
MAX_NOVEDADES = 5


def estado_legible(estado: Optional[str]) -> str:
    return ESTADOS_LEGIBLES.get(estado or "", estado or "sin registro")


def dia_legible(valor) -> str:
    return formatear_dia(valor if isinstance(valor, date) else date.fromisoformat(str(valor)))


def hora_legible(valor) -> str:
    if valor in (None, ""):
        return "sin hora aproximada"
    return formatear_hora(valor if isinstance(valor, time) else time.fromisoformat(str(valor)))


def _novedades(notas: list) -> str:
    recientes = notas[-MAX_NOVEDADES:]
    return "\n".join(
        f"   - {formatear_fecha_legible(n['creado_en'])} [{estado_legible(n.get('estado'))}]: {n['nota']}"
        for n in recientes
    )


def resumen_orden_servicio(orden: dict, nombre_servicio: str, notas: Optional[list] = None) -> str:
    """Una orden en texto. `notas` (de la más antigua a la más reciente):
    se muestran las últimas `MAX_NOVEDADES`."""
    partes = [
        f"Orden de servicio #{orden['numero']}",
        f"   ESTADO DEL EQUIPO: {estado_legible(orden.get('estado'))}",
        f"   Servicio: {nombre_servicio}",
        f"   Equipo: {orden.get('equipo')}",
        f"   Problema: {orden.get('descripcion')}",
        f"   Día para llevar el equipo: {dia_legible(orden['fecha_entrega'])}",
        f"   Hora aproximada: {hora_legible(orden.get('hora_aproximada'))}",
        f"   Cliente: {orden.get('cliente_nombre')} | Teléfono: {orden.get('cliente_telefono')}",
    ]
    if notas is not None:
        partes.append(f"   NOVEDADES (de la más antigua a la más reciente):\n{_novedades(notas)}"
                      if notas else "   NOVEDADES: todavía no hay novedades registradas.")
    return "\n".join(partes)


_MENSAJES = {
    "ORDEN_SERVICIO_NO_ENCONTRADA": (
        "Esa orden de servicio no existe entre las órdenes de este cliente. Consulta sus órdenes con "
        "Consultar_ordenes_servicio para ubicar la correcta."
    ),
    "ORDEN_SERVICIO_NO_MODIFICABLE": (
        "Esta orden ya no se puede cambiar ni cancelar por el chat porque el equipo ya está en manos de la "
        "empresa (estado: {estado}). Explícaselo al cliente con amabilidad y ofrécele la línea de atención "
        "de la empresa (DATOS DE LA SEDE) para cualquier cambio."
    ),
    "FECHA_PASADA": "Ese día ya pasó: pídele al cliente un día desde hoy en adelante.",
    "SERVICIO_NO_EXISTE": "Ese servicio_id no existe en el catálogo. Vuelve a consultar Servicio_tecnico.",
}


def mensaje_error_negocio(exc: ErrorNegocio) -> str:
    plantilla = _MENSAJES.get(exc.codigo, f"La operación no se pudo completar ({exc.codigo}).")
    return f"ERROR: {plantilla.format(estado=estado_legible(exc.detalle))} No se guardó nada. Estado: fallido"
