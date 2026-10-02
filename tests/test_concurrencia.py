"""
tests/test_concurrencia.py

Fase 7 del plan de mejoras (`docs/PLAN_DE_MEJORAS.md`): dos mensajes
simultáneos de la MISMA sesión no deben pisarse el historial; sesiones
distintas deben seguir procesándose en paralelo. Usa una "tabla" en memoria
en vez de Supabase y un orquestador simulado que tarda un poco.

Corre con:
    python tests/test_concurrencia.py
"""
import os
import sys
import threading
import time
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake-key")
os.environ.setdefault("OPENROUTER_API_KEY", "fake-key-para-el-mock")
os.environ.setdefault("OPENROUTER_MODELS", "modelo-simulado:free")
# Vacía (no ausente): load_dotenv() no pisa variables ya definidas, así el
# API_KEY del .env de quien corre los tests no se cuela en este test.
os.environ["API_KEY"] = ""

from fastapi import HTTPException  # noqa: E402

import sesiones  # noqa: E402
from web import app as modulo_app  # noqa: E402

# "Tabla conversaciones" en memoria, con la misma forma que devuelve PostgREST.
tabla: dict = {}
lock_tabla = threading.Lock()


def _get_rows(_tabla, params=None):
    session_id = (params or {}).get("session_id", "").removeprefix("eq.")
    with lock_tabla:
        fila = tabla.get(session_id)
        return [dict(fila)] if fila else []


def _upsert_row(_tabla, payload, on_conflict):
    with lock_tabla:
        tabla[payload["session_id"]] = dict(payload)
    return payload


def _orquestador_lento(mensaje_cliente, session_id, historiales, run_id):
    time.sleep(0.3)  # simula las llamadas al LLM
    nuevo = historiales.get("orquestador", []) + [
        {"role": "user", "content": mensaje_cliente},
        {"role": "assistant", "content": f"eco: {mensaje_cliente}"},
    ]
    return f"eco: {mensaje_cliente}", {**historiales, "orquestador": nuevo}


def _enviar(session_id, mensaje, resultados):
    try:
        resultados.append(modulo_app.chat(modulo_app.ChatRequest(session_id=session_id, mensaje=mensaje)).respuesta)
    except HTTPException as exc:
        resultados.append(exc.status_code)


def _en_paralelo(*envios):
    resultados: list = []
    hilos = [threading.Thread(target=_enviar, args=(*e, resultados)) for e in envios]
    inicio = time.perf_counter()
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()
    return resultados, time.perf_counter() - inicio


with (
    patch.object(sesiones.supabase_client, "get_rows", side_effect=_get_rows),
    patch.object(sesiones.supabase_client, "upsert_row", side_effect=_upsert_row),
    patch.object(modulo_app.orquestador, "run", side_effect=_orquestador_lento),
):
    # 1) Misma sesión, dos mensajes a la vez: ninguno se pierde.
    resultados, duracion = _en_paralelo(("3001", "hola"), ("3001", "quiero agendar"))
    contenidos = [m["content"] for m in tabla["3001"]["historiales"]["orquestador"] if m["role"] == "user"]
    assert sorted(contenidos) == ["hola", "quiero agendar"], (
        f"Ambos mensajes deben quedar en el historial, quedó: {contenidos}"
    )
    assert duracion >= 0.55, "Los dos turnos de la misma sesión deben ejecutarse uno después del otro"
    print("✅ Dos mensajes simultáneos de la misma sesión se procesan en orden y no se pisan el historial.")

    # 2) Sesiones distintas: en paralelo (no se bloquean entre sí).
    _, duracion = _en_paralelo(("A", "hola"), ("B", "hola"))
    assert duracion < 0.55, f"Sesiones distintas no deben esperarse entre sí (tardó {duracion:.2f}s)"
    print("✅ Sesiones distintas se procesan en paralelo.")

    # 3) Los locks se liberan y se borran al terminar (no crecen sin límite).
    assert modulo_app._almacen._locks.activos() == 0, "No deben quedar locks de sesiones sin turnos activos"
    print("✅ Los locks por sesión se liberan y se eliminan al terminar cada turno.")

# 4) Si el turno anterior no termina a tiempo -> TurnoEnCurso (la API responde 409).
almacen = sesiones.AlmacenSesiones()
liberar = threading.Event()


def _turno_largo():
    with almacen.turno_exclusivo("X"):
        liberar.wait(2)


hilo = threading.Thread(target=_turno_largo)
hilo.start()
time.sleep(0.05)
try:
    with almacen.turno_exclusivo("X", timeout=0.1):
        raise AssertionError("No debía obtener el turno mientras otro está en curso")
except sesiones.TurnoEnCurso:
    pass
liberar.set()
hilo.join()
assert almacen._locks.activos() == 0
print("✅ Si el turno anterior no termina a tiempo, se lanza TurnoEnCurso (409 en la API).")

print("\n✅ Todos los tests de concurrencia pasaron.")
