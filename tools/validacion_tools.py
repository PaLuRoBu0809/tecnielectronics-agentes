"""
tools/validacion_tools.py

Validación de los argumentos que el modelo envía a cada tool, ANTES de
ejecutar la función Python (Fase 4 de `docs/PLAN_DE_MEJORAS.md`). Hay un
diccionario de modelos por agente: `ARGUMENTOS_POR_TOOL` (Servicio Técnico)
y `ARGUMENTOS_VENTAS` (Ventas: ids UUID, cantidades >= 1, método de pago
cerrado a "contra_entrega"/"en_linea").

Qué valida aquí (forma de los datos) vs. en `tools/citas_tools.py` (reglas
de negocio que necesitan el catálogo o la hora actual):
- Aquí: campos obligatorios, fechas ISO 8601 parseables, fin > inicio,
  teléfono con una cantidad razonable de dígitos, textos no vacíos.
- En `citas_tools.validar_horario_cita`: horario operativo, días hábiles,
  fechas futuras, duración según el catálogo.

Si la validación falla, la tool NO se ejecuta y el modelo recibe un texto
"ERROR: argumentos inválidos para X: ..." en español, que puede leer y
corregir. Antes, un argumento faltante o de más terminaba en un `TypeError`
de Python mostrado tal cual al modelo.

`TOOLS_SCHEMA` (lo que ve el modelo) se sigue escribiendo a mano en cada
agente y NO se genera desde estos modelos: así no cambia ni una coma de lo
que el modelo lee. La coherencia la garantizan `verificar_coherencia_tools`
y los tests de cada agente (`tests/test_validacion_tools.py`,
`tests/test_ventas_tools.py`).
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Callable, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from tools.citas_tools import zona_horaria_configurada


class _ArgsBase(BaseModel):
    # extra="ignore": si el modelo agrega un campo que la tool no conoce, se
    #   descarta en vez de fallar (antes producía un TypeError).
    # coerce_numbers_to_str: servicio_id llega a veces como número (3) y el
    #   schema lo declara string ("3").
    # str_strip_whitespace: "  Juan " -> "Juan".
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True, str_strip_whitespace=True)


def _fecha_iso(valor: Optional[str]) -> Optional[str]:
    if valor is None:
        return None
    try:
        datetime.fromisoformat(valor)
    except ValueError as exc:
        raise ValueError("debe ser una fecha y hora ISO 8601, ej. 2026-07-28T11:00:00-05:00") from exc
    return valor


def _no_vacio(valor: Optional[str]) -> Optional[str]:
    if valor is not None and not valor:
        raise ValueError("no puede estar vacío")
    return valor


def _telefono(valor: Optional[str]) -> Optional[str]:
    """Valida sin CORREGIR: el prompt prohíbe completar o reformatear el
    teléfono que dio el cliente, así que el valor se guarda tal cual."""
    if valor is None:
        return None
    if re.search(r"[^\d\s+\-().]", valor):
        raise ValueError("solo puede contener dígitos, espacios, +, -, paréntesis o puntos")
    digitos = re.sub(r"\D", "", valor)
    if not 7 <= len(digitos) <= 15:
        raise ValueError("debe tener entre 7 y 15 dígitos; confírmalo con el cliente")
    return valor


def _a_datetime(valor: str) -> datetime:
    """Una fecha sin offset se interpreta en la zona del negocio (igual que
    `citas_tools.normalizar_fecha_hora`), para poder comparar una fecha con
    offset contra otra sin él sin que Python lance TypeError."""
    dt = datetime.fromisoformat(valor)
    return dt if dt.tzinfo else dt.replace(tzinfo=zona_horaria_configurada())


def _fin_posterior(inicio: Optional[str], fin: Optional[str], nombres=("fecha_hora_inicio", "fecha_hora_fin")) -> None:
    if inicio and fin and _a_datetime(fin) <= _a_datetime(inicio):
        raise ValueError(f"{nombres[1]} debe ser posterior a {nombres[0]}")


class ServicioTecnicoArgs(_ArgsBase):
    pass


class ConsultarEventosArgs(_ArgsBase):
    fecha_inicio: str
    fecha_fin: str
    servicio_id: str

    v_fechas = field_validator("fecha_inicio", "fecha_fin")(_fecha_iso)
    v_servicio = field_validator("servicio_id")(_no_vacio)

    @model_validator(mode="after")
    def _rango(self):
        _fin_posterior(self.fecha_inicio, self.fecha_fin, ("fecha_inicio", "fecha_fin"))
        return self


class CrearEventoArgs(_ArgsBase):
    servicio_id: str
    cliente_nombre: str
    cliente_telefono: str
    descripcion: str
    fecha_hora_inicio: str
    fecha_hora_fin: str

    v_textos = field_validator("servicio_id", "cliente_nombre", "descripcion")(_no_vacio)
    v_telefono = field_validator("cliente_telefono")(_telefono)
    v_fechas = field_validator("fecha_hora_inicio", "fecha_hora_fin")(_fecha_iso)

    @model_validator(mode="after")
    def _orden(self):
        _fin_posterior(self.fecha_hora_inicio, self.fecha_hora_fin)
        return self


class ActualizarEventoArgs(_ArgsBase):
    google_calendar_event_id: str
    fecha_hora_inicio: Optional[str] = None
    fecha_hora_fin: Optional[str] = None
    cliente_nombre: Optional[str] = None
    cliente_telefono: Optional[str] = None
    descripcion: Optional[str] = None
    servicio_id: Optional[str] = None

    v_textos = field_validator("google_calendar_event_id", "cliente_nombre", "servicio_id")(_no_vacio)
    v_telefono = field_validator("cliente_telefono")(_telefono)
    v_fechas = field_validator("fecha_hora_inicio", "fecha_hora_fin")(_fecha_iso)

    @model_validator(mode="after")
    def _fechas_juntas(self):
        # El prompt exige enviar SIEMPRE inicio y fin completos cuando cambia
        # la fecha/hora (FASE 3, "Ejecución"); uno solo es un error del modelo.
        if bool(self.fecha_hora_inicio) != bool(self.fecha_hora_fin):
            raise ValueError("si cambia la fecha/hora, envía fecha_hora_inicio y fecha_hora_fin juntos")
        _fin_posterior(self.fecha_hora_inicio, self.fecha_hora_fin)
        return self


class EliminarEventoArgs(_ArgsBase):
    google_calendar_event_id: str

    v_id = field_validator("google_calendar_event_id")(_no_vacio)


class ConsultarServicioAgendadoArgs(_ArgsBase):
    pass


# Nombre de la tool (exactamente como en TOOLS_SCHEMA) -> modelo de argumentos.
ARGUMENTOS_POR_TOOL: dict = {
    "Servicio_tecnico": ServicioTecnicoArgs,
    "Consultar_eventos": ConsultarEventosArgs,
    "Crear_evento": CrearEventoArgs,
    "Actualizar_evento": ActualizarEventoArgs,
    "Eliminar_evento": EliminarEventoArgs,
    "Consultar_servicio_agendado": ConsultarServicioAgendadoArgs,
}


# ---------------------------------------------------------------------------
# Agente de Ventas
# ---------------------------------------------------------------------------
# Los campos de solo lectura (notes, shipping_status, payment_status) no
# existen en estos modelos: si el modelo los envía, `extra="ignore"` los
# descarta y nunca llegan a la base de datos.

def _uuid(valor: Optional[str]) -> Optional[str]:
    if valor is None:
        return None
    try:
        return str(uuid.UUID(valor))
    except ValueError as exc:
        raise ValueError(
            "debe ser un id copiado literalmente de la respuesta de la herramienta (UUID); no lo escribas de memoria"
        ) from exc


class CategoriasInventarioArgs(_ArgsBase):
    pass


class InventarioArgs(_ArgsBase):
    category_id: str
    termino: str
    precio_max: Optional[Decimal] = Field(default=None, gt=0)
    orden: Optional[Literal["mas_baratos", "mas_caros"]] = None

    v_categoria = field_validator("category_id")(_uuid)
    v_termino = field_validator("termino")(_no_vacio)


class AnadirElementoArgs(_ArgsBase):
    producto_id: str
    cantidad: int = Field(default=1, ge=1)

    v_producto = field_validator("producto_id")(_uuid)


class ConsultarCarritoArgs(_ArgsBase):
    pass


class ModificarElementoArgs(_ArgsBase):
    producto_id: str
    cantidad_nueva: int = Field(ge=1)

    v_producto = field_validator("producto_id")(_uuid)


class EliminarElementoArgs(_ArgsBase):
    producto_id: str

    v_producto = field_validator("producto_id")(_uuid)


class CrearOrdenArgs(_ArgsBase):
    customer_name: str
    customer_phone: str
    city: str
    customer_address: str
    # Solo estos dos valores: un error de tipeo se rechaza en vez de caer en
    # silencio al pago en línea (era el comportamiento de n8n).
    metodo_pago: Literal["contra_entrega", "en_linea"]

    v_textos = field_validator("customer_name", "city", "customer_address")(_no_vacio)
    v_telefono = field_validator("customer_phone")(_telefono)


class ConsultarOrdenArgs(_ArgsBase):
    order_number: Optional[int] = Field(default=None, ge=1)


class ModificarOrdenArgs(_ArgsBase):
    order_number: int = Field(ge=1)
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    customer_address: Optional[str] = None
    city: Optional[str] = None

    v_textos = field_validator("customer_name", "customer_address", "city")(_no_vacio)
    v_telefono = field_validator("customer_phone")(_telefono)

    @model_validator(mode="after")
    def _algun_cambio(self):
        if not any((self.customer_name, self.customer_phone, self.customer_address, self.city)):
            raise ValueError("envía al menos un dato personal que el cliente quiera cambiar")
        return self


class CancelarOrdenArgs(_ArgsBase):
    order_number: int = Field(ge=1)


# Nombre de la tool (exactamente como en ventas_agent.TOOLS_SCHEMA) -> modelo.
ARGUMENTOS_VENTAS: dict = {
    "Categorias_inventario": CategoriasInventarioArgs,
    "Inventario": InventarioArgs,
    "Anadir_elemento": AnadirElementoArgs,
    "Consultar_carrito": ConsultarCarritoArgs,
    "Modificar_elemento": ModificarElementoArgs,
    "Eliminar_elemento": EliminarElementoArgs,
    "Crear_orden": CrearOrdenArgs,
    "Consultar_orden": ConsultarOrdenArgs,
    "Modificar_orden": ModificarOrdenArgs,
    "Cancelar_orden": CancelarOrdenArgs,
}


def _describir_errores(exc: ValidationError) -> str:
    partes = []
    for error in exc.errors():
        campo = ".".join(str(p) for p in error.get("loc", ())) or "argumentos"
        if error.get("type") == "missing":
            mensaje = "falta este campo obligatorio"
        else:
            mensaje = str(error.get("msg", "")).removeprefix("Value error, ")
        partes.append(f"{campo}: {mensaje}")
    return "; ".join(partes)


def con_validacion(nombre_tool: str, funcion: Callable, modelos: Optional[dict] = None) -> Callable:
    """Envuelve `funcion` para que valide sus argumentos con el modelo de
    `modelos[nombre_tool]` antes de ejecutarse. Solo se pasan a la función
    los campos que el modelo realmente envió (`exclude_unset`), así los
    opcionales de `Actualizar_evento` siguen distinguiendo "no cambiar" de
    "cambiar".

    `modelos` por defecto es `ARGUMENTOS_POR_TOOL` (Servicio Técnico); otro
    agente (ej. Ventas) pasa su propio diccionario {nombre_tool: modelo}."""
    modelo = (ARGUMENTOS_POR_TOOL if modelos is None else modelos)[nombre_tool]

    def envoltorio(**kwargs) -> str:
        try:
            args = modelo.model_validate(kwargs)
        except ValidationError as exc:
            return (
                f"ERROR: argumentos inválidos para {nombre_tool}: {_describir_errores(exc)}. "
                f"No se ejecutó nada. Corrige los datos y vuelve a intentar. Estado: fallido"
            )
        return funcion(**args.model_dump(exclude_unset=True))

    envoltorio.__name__ = f"validado_{nombre_tool}"
    envoltorio.funcion_original = funcion  # type: ignore[attr-defined]  # para inspección en tests
    return envoltorio


def verificar_coherencia_tools(tools_schema: list, tool_functions: dict, modelos: dict) -> list:
    """Compara lo que ve el modelo (`tools_schema`), las funciones Python
    (`tool_functions`) y los modelos Pydantic (`modelos`) de UN agente.
    Devuelve la lista de diferencias (vacía si están sincronizados).

    Pensada para que el test de cada agente (Servicio Técnico hoy, Ventas
    mañana) la llame con sus propias piezas: un nombre, campo u obligatorio
    desincronizado se detecta en los tests en vez de fallar en silencio con
    un cliente real."""
    problemas = []
    nombres_schema = {t["function"]["name"] for t in tools_schema}
    for origen, nombres in (("tool_functions", set(tool_functions)), ("modelos Pydantic", set(modelos))):
        if nombres != nombres_schema:
            problemas.append(
                f"Nombres distintos entre TOOLS_SCHEMA {sorted(nombres_schema)} y {origen} {sorted(nombres)}"
            )
    for tool in tools_schema:
        nombre = tool["function"]["name"]
        if nombre not in modelos:
            continue
        parametros = tool["function"].get("parameters", {})
        modelo = modelos[nombre]
        propiedades = set(parametros.get("properties", {}))
        requeridos = set(parametros.get("required", []))
        campos = set(modelo.model_fields)
        obligatorios = {n for n, c in modelo.model_fields.items() if c.is_required()}
        if propiedades != campos:
            problemas.append(
                f"{nombre}: propiedades del schema {sorted(propiedades)} != campos Pydantic {sorted(campos)}"
            )
        if requeridos != obligatorios:
            problemas.append(
                f"{nombre}: required del schema {sorted(requeridos)} != obligatorios Pydantic {sorted(obligatorios)}"
            )
    return problemas
