"""
main.py — arnés de pruebas por consola.

Simula lo que en producción hará n8n: recibe un mensaje "del cliente", lo
pasa al orquestador, y muestra la respuesta. El historial de ambos agentes
se guarda en Supabase después de cada turno (ver `sesiones.py`), así que si
vuelves a abrir la consola con el mismo teléfono, la conversación continúa.

Para probar lo mismo desde el navegador en vez de la consola, ver
`web/app.py` (usa el mismo `AlmacenSesiones` de `sesiones.py`).

Uso:
    python main.py
"""
from __future__ import annotations

import logging

from dotenv import load_dotenv

from agents import orquestador
from sesiones import AlmacenSesiones

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

load_dotenv()

_almacen = AlmacenSesiones()


def main():
    print("=== TecniElectronics — arnés de pruebas (Orquestador + Servicio Técnico) ===")
    session_id = input("Teléfono del cliente a simular (cualquier texto sirve como id): ").strip() or "3000000000"
    print(f"Sesión iniciada para {session_id}. Escribe 'salir' para terminar.\n")

    sesion = _almacen.obtener(session_id)

    while True:
        mensaje = input("Cliente: ").strip()
        if mensaje.lower() in {"salir", "exit", "quit"}:
            break
        if not mensaje:
            continue

        respuesta, sesion["orq"], sesion["st"] = orquestador.run(
            mensaje_cliente=mensaje,
            session_id=session_id,
            orquestador_historial=sesion["orq"],
            servicio_tecnico_historial=sesion["st"],
        )
        _almacen.guardar(session_id, sesion["orq"], sesion["st"])
        print(f"\nTecniElectronics: {respuesta}\n")


if __name__ == "__main__":
    main()
