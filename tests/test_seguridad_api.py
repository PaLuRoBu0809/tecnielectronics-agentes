"""
tests/test_seguridad_api.py

Fase 1.2 y 1.4 del plan de mejoras (`docs/PLAN_DE_MEJORAS.md`): valida,
sin tocar Supabase ni OpenRouter, que el API exige la clave cuando
`API_KEY` está definida y que el límite de mensajes por sesión corta antes
de llegar al LLM. Mismo estilo que los demás tests: un script con asserts.

Corre con:
    python tests/test_seguridad_api.py
"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake-key")
os.environ.setdefault("OPENROUTER_API_KEY", "fake-key-para-el-mock")
os.environ.setdefault("OPENROUTER_MODELS", "modelo-simulado:free")
os.environ["API_KEY"] = "clave-de-prueba"
os.environ["LIMITE_MENSAJES_POR_MINUTO"] = "2"

from fastapi.testclient import TestClient  # noqa: E402

from web import app as modulo_app  # noqa: E402
from web.seguridad import LimitadorDeUso  # noqa: E402

cliente = TestClient(modulo_app.app)
CABECERA_OK = {"X-API-Key": "clave-de-prueba"}

# ---------------------------------------------------------------------------
# 1) Sin clave o con clave incorrecta -> 401, en todas las rutas del API.
# ---------------------------------------------------------------------------
with patch.object(modulo_app.catalog_tools, "listar_catalogo", return_value=[]):
    assert cliente.get("/api/catalogo").status_code == 401, "Sin clave debe responder 401"
    assert cliente.get("/api/catalogo", headers={"X-API-Key": "otra"}).status_code == 401
    assert cliente.get("/api/catalogo", headers=CABECERA_OK).status_code == 200
    # La clave en la URL ya NO se acepta: uvicorn escribe las URLs completas
    # en su log de accesos, así que la clave quedaría registrada.
    assert cliente.get("/api/catalogo?api_key=clave-de-prueba").status_code == 401
assert cliente.post("/api/chat", json={"session_id": "1", "mensaje": "hola"}).status_code == 401
pedido = {"order_number": 36, "customer_name": "Ana", "shipping_status": "CANCELADO"}
with patch.object(modulo_app.ordenes_repository, "listar_todas", return_value=[pedido]):
    assert cliente.get("/api/pedidos").status_code == 401, "Los pedidos (datos de clientes) exigen la clave"
    respuesta = cliente.get("/api/pedidos", headers=CABECERA_OK)
    assert respuesta.status_code == 200 and respuesta.json() == [pedido]
print("✅ Con API_KEY definida, el API rechaza peticiones sin la clave correcta (401), también si va en la URL.")

# ---------------------------------------------------------------------------
# 1a) Sesión del panel: la clave se envía UNA vez y se cambia por una cookie
#     HttpOnly con un token firmado que caduca (nunca la clave).
# ---------------------------------------------------------------------------
from web import seguridad  # noqa: E402

navegador = TestClient(modulo_app.app, base_url="https://panel.ejemplo.com")
assert navegador.post("/api/panel/sesion").status_code == 401, "Abrir sesión exige la clave"
abrir = navegador.post("/api/panel/sesion", headers=CABECERA_OK)
assert abrir.status_code == 200
set_cookie = abrir.headers["set-cookie"]
for atributo in ("HttpOnly", "SameSite=strict", "Secure", "Path=/api"):
    assert atributo.lower() in set_cookie.lower(), f"La cookie debe llevar {atributo}: {set_cookie}"
assert "clave-de-prueba" not in set_cookie, "La cookie NO debe contener la clave"
with patch.object(modulo_app.catalog_tools, "listar_catalogo", return_value=[]):
    assert navegador.get("/api/catalogo").status_code == 200, "Con la cookie, no hace falta la cabecera"
    navegador.delete("/api/panel/sesion")
    assert navegador.get("/api/catalogo").status_code == 401, "Tras cerrar la sesión, vuelve a pedir la clave"
print("✅ La sesión del panel usa una cookie HttpOnly/Secure/SameSite con un token, sin la clave.")

token = seguridad.crear_token_sesion(ahora=1000)
assert seguridad.token_sesion_valido(token, ahora=1000 + 60)
assert not seguridad.token_sesion_valido(token, ahora=1000 + seguridad.DURACION_SESION_S + 1), "Debe caducar"
vence, _, firma = token.partition(".")
token_alterado = f"{int(vence) + 999999}.{firma}"
assert not seguridad.token_sesion_valido(token_alterado, ahora=1000), "Alterar el vencimiento invalida"
assert not seguridad.token_sesion_valido("basura", ahora=1000)
with patch.dict(os.environ, {"API_KEY": "clave-rotada"}):
    assert not seguridad.token_sesion_valido(token, ahora=1000), "Cambiar API_KEY invalida las sesiones abiertas"
print("✅ El token de sesión caduca, no se puede alterar y se invalida al rotar API_KEY.")

# En desarrollo local (http://localhost) la cookie no lleva Secure, o el
# navegador no la guardaría.
local = TestClient(modulo_app.app, base_url="http://localhost:8000")
assert "secure" not in local.post("/api/panel/sesion", headers=CABECERA_OK).headers["set-cookie"].lower()
print("✅ En localhost la cookie funciona sin HTTPS.")

# Los estáticos (index.html) siguen públicos: el dashboard debe poder cargar
# para pedir la clave.
assert cliente.get("/").status_code == 200, "index.html no debe exigir clave"
print("✅ Los archivos estáticos del dashboard no exigen clave.")

# ---------------------------------------------------------------------------
# 1b) Fase 9.2: /health y /ready NO exigen clave (el hosting los consulta sin
#     cabeceras); /ready responde 503 si Supabase no responde.
# ---------------------------------------------------------------------------
assert cliente.get("/health").json() == {"estado": "ok"}
with patch.object(modulo_app.supabase_client, "get_rows", return_value=[]):
    assert cliente.get("/ready").status_code == 200
with patch.object(modulo_app.supabase_client, "get_rows", side_effect=ConnectionError("caído")):
    listo = cliente.get("/ready")
assert listo.status_code == 503 and listo.json()["supabase"] == "error"
assert "caído" not in listo.text, "/ready no debe exponer detalles del error"
print("✅ /health y /ready son públicos; /ready responde 503 si Supabase no responde.")

# ---------------------------------------------------------------------------
# 2) Límite por sesión: el tercer mensaje en un minuto recibe 429 y NUNCA
#    llega al orquestador (no gasta cuota del LLM).
# ---------------------------------------------------------------------------
with (
    patch.object(modulo_app._almacen, "obtener", return_value={}),
    patch.object(modulo_app._almacen, "guardar"),
    patch.object(modulo_app.orquestador, "run", return_value=("ok", {})) as run_mock,
):
    codigos = [
        cliente.post(
            "/api/chat", json={"session_id": "spam", "mensaje": f"m{i}"}, headers=CABECERA_OK
        ).status_code
        for i in range(3)
    ]
    otra_sesion = cliente.post(
        "/api/chat", json={"session_id": "otra", "mensaje": "hola"}, headers=CABECERA_OK
    ).status_code

assert codigos == [200, 200, 429], f"El 3er mensaje en un minuto debe recibir 429, se obtuvo {codigos}"
assert run_mock.call_count == 3, "El mensaje rechazado no debe llegar al orquestador (2 de 'spam' + 1 de 'otra')"
assert otra_sesion == 200, "El límite es por sesión: otra sesión no se ve afectada"
print("✅ El límite de mensajes por sesión responde 429 antes de gastar llamadas al LLM.")

# Mensaje demasiado largo -> 422 (validación de Pydantic), sin llegar al LLM.
largo = cliente.post("/api/chat", json={"session_id": "x", "mensaje": "a" * 2001}, headers=CABECERA_OK)
assert largo.status_code == 422, "Un mensaje de más de 2000 caracteres debe rechazarse"
print("✅ Mensajes y session_id tienen longitud máxima.")

# ---------------------------------------------------------------------------
# 2b) Fase 13: estado + notas desde el panel. Nota y responsable obligatorios;
#     las reglas de Postgres llegan como 404/409 con un mensaje legible.
# ---------------------------------------------------------------------------
from tools import ordenes_servicio_repository  # noqa: E402
from tools.errores_negocio import ErrorNegocio  # noqa: E402

cambio = {"estado": "RECIBIDO", "nota": "  Llegó con cargador ", "responsable": "Laura"}
assert cliente.post("/api/ordenes-servicio/25/estado", json=cambio).status_code == 401, "El panel exige la clave"
with patch.object(ordenes_servicio_repository, "cambiar_estado", return_value={"numero": 25}) as cambiar_mock:
    assert cliente.post("/api/ordenes-servicio/25/estado", json=cambio, headers=CABECERA_OK).status_code == 200
    for invalido in ({**cambio, "nota": "   "}, {**cambio, "responsable": ""}, {**cambio, "estado": "PERDIDO"},
                     {k: v for k, v in cambio.items() if k != "responsable"}):
        r = cliente.post("/api/ordenes-servicio/25/estado", json=invalido, headers=CABECERA_OK)
        assert r.status_code == 422, (invalido, r.status_code)
cambiar_mock.assert_called_once_with(25, "RECIBIDO", "Llegó con cargador", "Laura")

no_existe = ErrorNegocio("ORDEN_SERVICIO_NO_ENCONTRADA")
with patch.object(ordenes_servicio_repository, "agregar_nota", side_effect=no_existe):
    r = cliente.post("/api/ordenes-servicio/99/notas", json={"nota": "x", "responsable": "Laura"}, headers=CABECERA_OK)
assert r.status_code == 404 and "no existe" in r.json()["detail"], r.text
with patch.object(modulo_app.ordenes_repository, "cambiar_estado_envio", side_effect=ErrorNegocio("MISMO_ESTADO")):
    r = cliente.post("/api/pedidos/36/estado", json={**cambio, "estado": "DESPACHADO"}, headers=CABECERA_OK)
assert r.status_code == 409 and "Agregar nota" in r.json()["detail"], r.text
with patch.object(modulo_app.ordenes_repository, "editar_nota", return_value={"id": 7}) as editar_mock:
    r = cliente.patch("/api/pedidos/notas/7", json={"nota": "Guía 123", "responsable": "Pedro"}, headers=CABECERA_OK)
assert r.status_code == 200 and editar_mock.call_args.args == (7, "Guía 123", "Pedro")
with patch.object(ordenes_servicio_repository, "listar_todas", return_value=[]) as listar_mock:
    assert cliente.get("/api/ordenes-servicio?desde=2026-10-01&hasta=2026-10-31", headers=CABECERA_OK).status_code \
        == 200
    assert cliente.get("/api/ordenes-servicio?desde=ayer", headers=CABECERA_OK).status_code == 422
listar_mock.assert_called_once_with("2026-10-01", "2026-10-31")
print("✅ Panel: estado y notas exigen clave, nota y responsable; las reglas de negocio responden 404/409 legibles.")

# ---------------------------------------------------------------------------
# 3) LimitadorDeUso con reloj simulado: la ventana es deslizante.
# ---------------------------------------------------------------------------
reloj = {"t": 0.0}
limitador = LimitadorDeUso(por_minuto=2, por_dia=3, reloj=lambda: reloj["t"])
assert limitador.registrar("s") is None
assert limitador.registrar("s") is None
assert "por minuto" in limitador.registrar("s")
reloj["t"] = 61  # pasó un minuto: se libera la ventana por minuto
assert limitador.registrar("s") is None
reloj["t"] = 122
assert "por día" in limitador.registrar("s"), "Tras 3 mensajes en el día debe aplicar el límite diario"
reloj["t"] = 86400 + 200  # pasó un día completo
assert limitador.registrar("s") is None, "Tras 24 h la ventana diaria se libera"
print("✅ LimitadorDeUso aplica ventanas deslizantes por minuto y por día.")

print("\n✅ Todos los tests de seguridad del API pasaron.")
