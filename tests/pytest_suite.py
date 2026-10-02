"""
tests/pytest_suite.py

Puente para correr los scripts de prueba con pytest (Fase 9.5 de
`docs/PLAN_DE_MEJORAS.md`): un caso por cada `tests/test_*.py`, cada uno en
su PROPIO proceso. Los scripts aplican parches globales a nivel de módulo
(ej. fijar la hora actual), así que importarlos juntos en un solo proceso
haría que se contaminen entre sí.

Corre con:
    pytest            (desde la raíz del proyecto)
    pytest -k citas   (solo los scripts cuyo nombre contenga "citas")

Un script que imprime "OMITIDO" (ej. el de Postgres sin base disponible) se
reporta como `skipped`, no como aprobado.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

CARPETA = Path(__file__).parent
SCRIPTS = sorted(CARPETA.glob("test_*.py"))


@pytest.mark.parametrize("script", SCRIPTS, ids=[s.stem for s in SCRIPTS])
def test_script(script: Path) -> None:
    proceso = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=300,
    )
    if proceso.returncode == 0 and "OMITIDO" in proceso.stdout:
        pytest.skip(next(linea for linea in proceso.stdout.splitlines() if "OMITIDO" in linea).strip())
    assert proceso.returncode == 0, f"{script.name} falló:\n{proceso.stdout[-3000:]}\n{proceso.stderr[-4000:]}"
