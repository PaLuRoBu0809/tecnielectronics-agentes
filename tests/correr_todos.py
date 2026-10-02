"""
tests/correr_todos.py

Corre todos los scripts de prueba de `tests/` (cada uno en su propio
proceso, porque son scripts con asserts a nivel de módulo, no funciones de
pytest) y resume cuáles pasaron. Fuerza UTF-8 en la salida de cada script:
la consola de Windows usa cp1252 por defecto y los "✅" de los mensajes
harían fallar un test que en realidad pasó.

Corre con:
    python tests/correr_todos.py
"""
import os
import subprocess
import sys
from pathlib import Path

CARPETA = Path(__file__).parent


def main() -> int:
    # La propia salida de este script también puede llevar caracteres fuera
    # de cp1252 (los mensajes de los tests), así que se fuerza UTF-8.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    entorno = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    scripts = sorted(p for p in CARPETA.glob("test_*.py"))
    fallidos, omitidos = [], []
    for script in scripts:
        proceso = subprocess.run(
            [sys.executable, str(script)], capture_output=True, text=True, encoding="utf-8", env=entorno
        )
        if proceso.returncode == 0 and "OMITIDO" in proceso.stdout:
            omitidos.append(script.name)
            motivo = next(linea for linea in proceso.stdout.splitlines() if "OMITIDO" in linea)
            print(f"SKIP {script.name} — {motivo.strip()}")
        elif proceso.returncode == 0:
            print(f"PASS {script.name}")
        else:
            fallidos.append(script.name)
            print(f"FAIL {script.name}\n{proceso.stdout[-2000:]}{proceso.stderr[-3000:]}")
    pasaron = len(scripts) - len(fallidos) - len(omitidos)
    print(f"\n{pasaron} pasaron, {len(omitidos)} omitidos, {len(fallidos)} fallaron (de {len(scripts)}).")
    return 1 if fallidos else 0


if __name__ == "__main__":
    sys.exit(main())
