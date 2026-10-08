"""
tools/info_empresa.py

Conocimiento de la empresa para el orquestador: contacto, redes, ubicación,
medios de pago, quiénes somos, políticas. Vive en la tabla `info_empresa`
(ver `supabase/migrations/20261007000011_info_empresa.sql`), que la empresa
mantiene desde el Table Editor sin tocar código.

Dos formas de llegar al modelo, según la columna `uso`:
- 'siempre': `nota_para_prompt()` los pone en el prompt del orquestador en
  cada mensaje (datos cortos y frecuentes, sin llamadas extra al modelo).
- 'bajo_demanda': la tool {Info_empresa}(tema) los entrega solo cuando el
  cliente pregunta (textos largos que no conviene cargar en cada mensaje).

La tabla se guarda en memoria 10 minutos (`tools/cache.py`). Si no se puede
leer (Supabase caído, migración sin aplicar), el agente sigue funcionando:
sin la nota, y la tool responde con un error para que ofrezca el contacto.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from tools import supabase_client
from tools.cache import CacheConVencimiento

logger = logging.getLogger(__name__)

USO_SIEMPRE = "siempre"
USO_BAJO_DEMANDA = "bajo_demanda"


@dataclass(frozen=True)
class TemaEmpresa:
    tema: str
    titulo: str
    contenido: str
    uso: str


def _tabla() -> str:
    return os.environ.get("SUPABASE_TABLE_INFO_EMPRESA", "info_empresa")


def _cargar() -> tuple:
    filas = supabase_client.get_rows(_tabla(), params={"select": "tema,titulo,contenido,uso", "order": "tema"})
    return tuple(TemaEmpresa(f["tema"], f["titulo"], f["contenido"], f["uso"]) for f in filas)


_temas = CacheConVencimiento(_cargar)


def listar() -> tuple:
    return _temas.obtener()


ENCABEZADO_NOTA = """

---
INFORMACIÓN DE LA EMPRESA (inyectada automáticamente desde la base de datos;
es la ÚNICA fuente válida sobre la empresa):
"""


def nota_para_prompt() -> str:
    """Los temas de uso 'siempre', más la lista de temas que se pueden
    consultar con {Info_empresa}. Vacía si la tabla no se puede leer."""
    try:
        temas = listar()
    except Exception:
        logger.warning("No se pudo leer info_empresa para el prompt del orquestador", exc_info=True)
        return ""
    if not temas:
        return ""
    siempre = "\n".join(f"- {t.titulo}: {t.contenido}" for t in temas if t.uso == USO_SIEMPRE)
    consultables = ", ".join(f"{t.tema} ({t.titulo})" for t in temas if t.uso == USO_BAJO_DEMANDA)
    texto = ENCABEZADO_NOTA + siempre
    if consultables:
        texto += f"\nTemas que puedes consultar con {{Info_empresa}}: {consultables}"
    return texto


ENCABEZADO_NOTA_SUBAGENTE = """

---
DATOS DE LA SEDE (inyectados automáticamente desde la base de datos; son la
ÚNICA fuente válida: cópialos tal cual, nunca los inventes ni los cambies):
"""

# Lo que los sub-agentes necesitan para decirle al cliente cuándo y dónde
# llevar o recoger algo (Fase 13).
TEMAS_SEDE = ("horario", "ubicacion", "contacto")


def nota_temas(temas: tuple = TEMAS_SEDE) -> str:
    """Nota con el contenido de esos temas, para el prompt de un sub-agente
    (Servicio Técnico, Ventas). Vacía si la tabla no se puede leer."""
    try:
        por_tema = {t.tema: t for t in listar()}
    except Exception:
        logger.warning("No se pudo leer info_empresa para el prompt de un sub-agente", exc_info=True)
        return ""
    lineas = [f"- {por_tema[t].titulo}: {por_tema[t].contenido}" for t in temas if t in por_tema]
    return ENCABEZADO_NOTA_SUBAGENTE + "\n".join(lineas) if lineas else ""


def info_empresa(tema: str) -> str:
    """{Info_empresa}: el contenido de un tema. Nunca lanza excepciones."""
    try:
        temas = listar()
    except Exception as exc:
        return (
            "ERROR: No se pudo consultar la información de la empresa en este momento. No inventes la "
            f"respuesta: ofrece al cliente la línea de contacto. Detalle técnico (interno): {exc}"
        )
    clave = (tema or "").strip().lower()
    encontrado = next((t for t in temas if t.tema == clave), None)
    if encontrado is None:
        disponibles = ", ".join(t.tema for t in temas)
        return (
            f"TEMA_NO_ENCONTRADO: no hay información registrada sobre '{tema}'. Temas disponibles: "
            f"{disponibles}. Si ninguno responde lo que pregunta el cliente, dile que no tienes ese dato "
            "y ofrécele la línea de contacto. Nunca lo inventes."
        )
    return f"{encontrado.titulo.upper()}:\n{encontrado.contenido}"
