"""
tests/test_resiliencia_llm.py

Fases 2 y 3 del plan de mejoras (`docs/PLAN_DE_MEJORAS.md`): valida con un
LLM simulado (sin llamar a OpenRouter) la clasificación de errores, el
disyuntor por modelo, el presupuesto compartido del turno, la razón de
parada, que un aborto a mitad de turno deja un historial válido con el que
el turno siguiente funciona, y los reintentos de lectura de Supabase.

Corre con:
    python tests/test_resiliencia_llm.py
"""
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("OPENROUTER_API_KEY", "fake-key-para-el-mock")
os.environ.setdefault("OPENROUTER_MODELS", "modelo-simulado:free")
os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "fake-key")

import openai  # noqa: E402

import llm_loop  # noqa: E402
from llm_loop import PresupuestoTurno, run_agent_loop, sanear_historial  # noqa: E402
from tools import supabase_client  # noqa: E402

TOOLS = [{"type": "function", "function": {"name": "eco", "parameters": {"type": "object", "properties": {}}}}]


# ---------------------------------------------------------------------------
# Utilidades del LLM simulado
# ---------------------------------------------------------------------------

def _error_api(cls, status=None, headers=None):
    """Instancia una excepción del SDK sin pasar por su constructor (que
    exige un objeto de respuesta HTTP real)."""
    exc = cls.__new__(cls)
    Exception.__init__(exc, f"error simulado {status}")
    exc.message = f"error simulado {status}"
    exc.status_code = status
    exc.response = SimpleNamespace(headers=headers or {})
    exc.body = None
    return exc


def _tool_call(call_id, nombre="eco", args=None):
    args_json = json.dumps(args or {})
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=nombre, arguments=args_json),
        model_dump=lambda: {"id": call_id, "type": "function", "function": {"name": nombre, "arguments": args_json}},
    )


def _respuesta(content=None, tool_calls=None):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=tool_calls))])


def _correr(create, modelos, **kwargs):
    """Corre run_agent_loop con `create` como chat.completions.create."""
    with patch("llm_loop.OpenAI") as mock_openai:
        mock_openai.return_value.chat.completions.create.side_effect = create
        resultado = run_agent_loop(
            system_prompt="prueba",
            messages=kwargs.pop("messages", [{"role": "user", "content": "hola"}]),
            tools_schema=TOOLS,
            tool_functions=kwargs.pop("tool_functions", {"eco": lambda: "resultado eco"}),
            models=modelos,
            **kwargs,
        )
    return resultado, mock_openai


def _secuencia_valida(mensajes):
    """Cada assistant(tool_calls) debe ir seguido de todos sus resultados."""
    pendientes = set()
    for m in mensajes:
        if m["role"] == "tool":
            assert m["tool_call_id"] in pendientes, f"tool sin llamada previa: {m}"
            pendientes.discard(m["tool_call_id"])
            continue
        assert not pendientes, f"faltan resultados de tools antes de {m}"
        if m.get("tool_calls"):
            pendientes = {tc["id"] for tc in m["tool_calls"]}
    assert not pendientes, "el historial termina con tool calls sin resultado"
    return True


# ---------------------------------------------------------------------------
# 1) El cliente se crea sin reintentos del SDK y con timeouts separados.
# ---------------------------------------------------------------------------
llm_loop.reiniciar_disyuntor()
(_, _), mock_openai = _correr(lambda **kw: _respuesta("hola"), ["m1"])
kwargs_cliente = mock_openai.call_args.kwargs
assert kwargs_cliente["max_retries"] == 0, "Los reintentos deben vivir en una sola capa (fallback), no en el SDK"
assert kwargs_cliente["timeout"].connect == 5 and kwargs_cliente["timeout"].read == 45
assert "timeout" in mock_openai.return_value.chat.completions.create.call_args.kwargs, (
    "Cada petición debe llevar su propio timeout acotado por el tiempo restante del turno"
)
print("✅ Cliente LLM con max_retries=0 y timeouts de conexión/lectura separados.")

# ---------------------------------------------------------------------------
# 2) 401 corta el turno sin probar el siguiente modelo (no es fallback).
# ---------------------------------------------------------------------------
llm_loop.reiniciar_disyuntor()
modelos_llamados = []


def _create_401(**kw):
    modelos_llamados.append(kw["model"])
    raise _error_api(openai.AuthenticationError, 401)


presupuesto = PresupuestoTurno()
(texto, historial), _ = _correr(_create_401, ["m1", "m2", "m3"], contexto={"presupuesto": presupuesto})
assert modelos_llamados == ["m1"], f"Con 401 no debe probar más modelos, probó {modelos_llamados}"
assert presupuesto.razones[-1][1] == "credenciales"
assert "dificultades técnicas" in texto
print("✅ 401 corta el turno como 'credenciales' sin disfrazarse de fallback.")

# Clave ausente: tampoco explota (antes era un KeyError fuera del try -> 500).
llm_loop.reiniciar_disyuntor()
with patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}):
    presupuesto = PresupuestoTurno()
    texto, _ = run_agent_loop("p", [{"role": "user", "content": "x"}], TOOLS, {}, models=["m1"],
                              contexto={"presupuesto": presupuesto})
assert presupuesto.razones[-1][1] == "credenciales", "Sin clave debe terminar como 'credenciales', no con excepción"
print("✅ OPENROUTER_API_KEY ausente termina en un mensaje amable, no en un 500.")

# ---------------------------------------------------------------------------
# 3) Disyuntor: un 429 pausa el modelo; en la siguiente llamada se saltea.
#    Un 400 NO pausa (se registra y se prueba el siguiente).
# ---------------------------------------------------------------------------
llm_loop.reiniciar_disyuntor()
modelos_llamados = []


def _create_429_luego_ok(**kw):
    modelos_llamados.append(kw["model"])
    if kw["model"] == "m1":
        raise _error_api(openai.RateLimitError, 429, headers={"retry-after": "120"})
    if kw["model"] == "m2":
        raise _error_api(openai.BadRequestError, 400)
    return _respuesta("respondió m3")


(texto, _), _ = _correr(_create_429_luego_ok, ["m1", "m2", "m3"])
assert texto == "respondió m3" and modelos_llamados == ["m1", "m2", "m3"]
modelos_llamados.clear()
(texto, _), _ = _correr(_create_429_luego_ok, ["m1", "m2", "m3"])
assert modelos_llamados == ["m2", "m3"], f"m1 (429) debe saltearse; m2 (400) no se pausa. Se llamó {modelos_llamados}"
print("✅ Disyuntor: tras un 429 el modelo se saltea; un 400 no lo pausa.")

# Si todos están en pausa, se prueba el que sale antes (no se queda sin responder).
llm_loop.reiniciar_disyuntor()
llm_loop._pausar_modelo("m1", 500)
llm_loop._pausar_modelo("m2", 100)
assert llm_loop._modelos_a_intentar(["m1", "m2"]) == ["m2"]
print("✅ Con todos los modelos en pausa, se intenta el que expira antes.")

# 404 -> pausa larga; timeout -> pausa corta.
assert llm_loop._clasificar_error(_error_api(openai.NotFoundError, 404)) == (
    "no_disponible", llm_loop.PAUSA_NO_DISPONIBLE
)
assert llm_loop._clasificar_error(_error_api(openai.APITimeoutError))[0] == "transitorio"
assert llm_loop._clasificar_error(_error_api(openai.APIStatusError, 402))[0] == "credenciales"
assert llm_loop._clasificar_error(_error_api(openai.PermissionDeniedError, 403))[0] == "prohibido", (
    "403 en OpenRouter puede ser moderación: no debe cortar el turno como credenciales"
)
print("✅ Clasificación de errores: 402 credenciales, 403 prohibido, 404 no disponible, timeout transitorio.")

# ---------------------------------------------------------------------------
# 4) Presupuesto: un modelo que pide tools sin parar se corta por llamadas.
# ---------------------------------------------------------------------------
llm_loop.reiniciar_disyuntor()
contador = {"n": 0}


def _create_bucle(**kw):
    contador["n"] += 1
    return _respuesta(tool_calls=[_tool_call(f"c{contador['n']}")])


presupuesto = PresupuestoTurno(max_llamadas=3, segundos=60)
(texto, historial), _ = _correr(_create_bucle, ["m1"], contexto={"presupuesto": presupuesto})
assert contador["n"] == 3, f"Debe hacer exactamente 3 llamadas, hizo {contador['n']}"
assert presupuesto.razones[-1][1] == "presupuesto_llamadas"
assert _secuencia_valida(historial) and historial[-1]["role"] == "assistant"
print("✅ El presupuesto de llamadas corta el turno y deja un historial válido.")

# Deadline con reloj simulado.
reloj = {"t": 0.0}
presupuesto = PresupuestoTurno(max_llamadas=100, segundos=10, reloj=lambda: reloj["t"])


def _create_lento(**kw):
    reloj["t"] += 6  # cada llamada "tarda" 6 s
    return _respuesta(tool_calls=[_tool_call(f"d{reloj['t']}")])


(_, historial), _ = _correr(_create_lento, ["m1"], contexto={"presupuesto": presupuesto})
assert presupuesto.razones[-1][1] == "deadline" and presupuesto.llamadas == 2
assert _secuencia_valida(historial)
print("✅ El tiempo límite del turno corta el loop con razón 'deadline'.")

# ---------------------------------------------------------------------------
# 5) Presupuesto COMPARTIDO: el sub-agente (una tool) gasta el presupuesto;
#    el orquestador reenvía la respuesta ya obtenida en vez de perderla.
# ---------------------------------------------------------------------------
llm_loop.reiniciar_disyuntor()
presupuesto = PresupuestoTurno(max_llamadas=2, segundos=60)


def _sub_agente():
    presupuesto.registrar_llamada()  # el sub-agente consumió la última llamada
    return "Tu cita quedó agendada para el lunes."


(texto, _), _ = _correr(
    lambda **kw: _respuesta(tool_calls=[_tool_call("o1")]), ["m1"],
    tool_functions={"eco": _sub_agente},
    contexto={"presupuesto": presupuesto, "agente": "Orquestador"},
    reenviar_ultima_tool_si_se_agota=True,
)
assert texto == "Tu cita quedó agendada para el lunes.", (
    "Si el presupuesto se agota tras una respuesta válida del sub-agente, el orquestador debe reenviarla"
)
print("✅ Con el presupuesto agotado, el orquestador reenvía la respuesta que el sub-agente ya dio.")

# ---------------------------------------------------------------------------
# 6) Aborto a mitad de turno: el modelo pide 2 tools y el proceso falla
#    DESPUÉS del primer resultado y ANTES del segundo (el peor momento: el
#    historial en memoria queda con una tool call sin respuesta) -> razón
#    'error_interno', historial válido, y el turno SIGUIENTE funciona.
# ---------------------------------------------------------------------------
llm_loop.reiniciar_disyuntor()
presupuesto = PresupuestoTurno()
with patch.object(llm_loop, "_ejecutar_tool", side_effect=["resultado x1", RuntimeError("aborto simulado")]):
    (texto, historial), _ = _correr(
        lambda **kw: _respuesta(tool_calls=[_tool_call("x1"), _tool_call("x2")]), ["m1"],
        contexto={"presupuesto": presupuesto},
    )
assert presupuesto.razones[-1][1] == "error_interno"
assert _secuencia_valida(historial)
assert not any(m.get("tool_calls") for m in historial), "La llamada incompleta (x1 sin x2) debe descartarse"

recibido = {}


def _create_turno_siguiente(**kw):
    recibido["mensajes"] = kw["messages"]
    return _respuesta("todo bien en el turno siguiente")


(texto, _), _ = _correr(
    _create_turno_siguiente, ["m1"], messages=historial + [{"role": "user", "content": "¿sigues ahí?"}]
)
assert texto == "todo bien en el turno siguiente"
assert _secuencia_valida(recibido["mensajes"][1:]), "El modelo del turno siguiente debe recibir un historial válido"
print("✅ Un aborto a mitad de turno deja un historial válido y el turno siguiente funciona.")

# ---------------------------------------------------------------------------
# 7) sanear_historial: descarta tool calls incompletas y tools huérfanas.
# ---------------------------------------------------------------------------
roto = [
    {"role": "user", "content": "a"},
    {"role": "assistant", "content": None, "tool_calls": [{"id": "t1"}, {"id": "t2"}]},
    {"role": "tool", "tool_call_id": "t1", "content": "solo uno"},
    {"role": "tool", "tool_call_id": "huerfano", "content": "?"},
    {"role": "user", "content": "b"},
    {"role": "assistant", "content": None, "tool_calls": [{"id": "t3"}]},
    {"role": "tool", "tool_call_id": "t3", "content": "ok"},
    {"role": "assistant", "content": "fin"},
]
limpio = sanear_historial(roto)
assert [m["role"] for m in limpio] == ["user", "user", "assistant", "tool", "assistant"], limpio
assert _secuencia_valida(limpio)
print("✅ sanear_historial descarta tool calls incompletas y resultados huérfanos.")

# ---------------------------------------------------------------------------
# 8) Supabase: GET reintenta con backoff ante errores de red; POST nunca.
# ---------------------------------------------------------------------------
ok = MagicMock(status_code=200)
ok.json.return_value = [{"id": 1}]
with (
    patch.object(supabase_client.requests, "get",
                 side_effect=[requests.exceptions.ConnectionError(), MagicMock(status_code=503), ok]) as get_mock,
    patch.object(supabase_client, "_esperar_backoff") as espera_mock,
):
    filas = supabase_client.get_rows("tabla")
assert filas == [{"id": 1}] and get_mock.call_count == 3 and espera_mock.call_count == 2
assert get_mock.call_args.kwargs["timeout"] == (5.0, 15.0), "Timeout debe separar conexión y lectura"

with (
    patch.object(supabase_client.requests, "post", side_effect=requests.exceptions.Timeout()) as post_mock,
    patch.object(supabase_client, "_esperar_backoff"),
):
    try:
        supabase_client.insert_row("tabla", {"a": 1})
        raise AssertionError("insert_row debía propagar el timeout")
    except requests.exceptions.Timeout:
        pass
assert post_mock.call_count == 1, "Una escritura nunca se reintenta a ciegas (podría duplicar una cita)"
print("✅ Supabase: lecturas con reintento y backoff; escrituras sin reintento; timeouts separados.")

print("\n✅ Todos los tests de resiliencia del LLM pasaron.")
