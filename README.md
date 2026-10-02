# TecniElectronics — Orquestador + Servicio Técnico en Python

Traducción del sistema multiagente de n8n (Orquestador + Agente_Servicio_Tecnico)
a un servicio Python con tool use nativo sobre OpenRouter. Es el **paso 2-4 del
plan de migración**: un piloto con solo estos dos agentes, antes de tocar Ventas
ni de conectar esto a n8n.

> **Plan de mejoras en curso:** ver [`docs/PLAN_DE_MEJORAS.md`](docs/PLAN_DE_MEJORAS.md)
> (seguridad, resiliencia del LLM, presupuesto de turno, validación, etc.), con
> el registro detallado de cada cambio. Algunas secciones de este README
> describen el estado anterior a esas mejoras; el plan manda cuando difieren.
> Tests: `python tests/correr_todos.py`. Para agregar `Agente_Ventas`, ver la
> guía al final de ese documento.

## Qué SÍ está implementado

- El loop genérico de tool use (`llm_loop.py`), con fallback entre varios
  modelos `:free` de OpenRouter.
- El Agente de Servicio Técnico completo (`agents/servicio_tecnico_agent.py`),
  con las 6 tools de sus 4 flujos (agendar, modificar, cancelar, consultar estado).
- El Orquestador (`agents/orquestador.py`), enrutando por tool use nativo — el
  modelo decide si invoca `Agente_Servicio_Tecnico`, no un if/else escrito a mano.
- Las 4 herramientas de citas (`tools/citas_tools.py`), sobre una capa de
  repositorio de Supabase (`tools/supabase_client.py` +
  `tools/citas_repository.py`). **Google Calendar ya no se usa** — Supabase
  es la única fuente de verdad, y la propia base de datos impide con un
  constraint que dos citas confirmadas se crucen. Ver la sección
  "Se quitó Google Calendar" más abajo para el detalle completo. Cada
  resumen se arma leyendo la fila REAL de Supabase, así el agente ve
  automáticamente cualquier columna nueva sin tocar código. El catálogo de
  servicios + consulta de citas (`tools/catalog_tools.py`) sigue el mismo
  principio de columnas dinámicas.
- Una nota de optimización de flujo en el prompt del Agente de Servicio
  Técnico (`NOTA_OPTIMIZACION_AGENDAMIENTO` en `agents/servicio_tecnico_agent.py`,
  agregada igual que la nota temporal del orquestador, sin tocar el prompt
  original) para reducir turnos de conversación al agendar — ver detalle en
  "Optimización del flujo de agendamiento" más abajo.
- Dos "front doors" para probar los agentes, compartiendo el mismo
  `AlmacenSesiones` (`sesiones.py`): un arnés de pruebas por consola
  (`main.py`) y una interfaz de chat por navegador (`web/app.py`, FastAPI).
- Memoria conversacional persistente: el historial de ambos agentes por
  `session_id` se guarda en la tabla `conversaciones` de Supabase (esquema
  en `supabase/migrations/20260927000001_conversaciones.sql`), no en la RAM del proceso — sobrevive a
  reinicios y es la misma entre instancias, requisito para desplegar en un
  hosting como Render.
- Tests que corren gratis, sin gastar cuota real: `tests/test_llm_loop.py`
  (mecánica del loop de tool use), `tests/test_citas_repository.py`
  (columnas dinámicas, rollback de `crear_evento`, soft-delete, fecha actual,
  normalización de zona horaria) y `tests/test_sesiones.py` (memoria en
  Supabase).

## Qué falta (a propósito, en este orden)

1. **Agente_Ventas** — no existe todavía. El orquestador tiene una nota
   temporal (ver `agents/orquestador.py`) para no intentar invocarlo mientras
   tanto. Bórrala cuando lo conectes.
2. **Conectar con n8n** — por ahora esto corre solo, por consola. Cuando el
   piloto se comporte bien, el paso siguiente es exponer `orquestador.run()`
   detrás de un endpoint HTTP (FastAPI) y que n8n lo llame en vez del nodo AI Agent.

## Cosas que TIENES que verificar antes de correr esto en serio

| # | Qué | Por qué |
|---|---|---|
| 1 | ~~Campo `fecha_hora_fin2`~~ | ✅ **Verificado 2026-09-27** contra el esquema real de Supabase (proyecto `zvxvtnxifmgubrgqegoh`, tabla `servicios_agendados`, vía MCP): la columna real es **`fecha_hora_fin`** (sin el "2"). Ya corregido en `tools/citas_repository.py` (`CAMPO_FECHA_FIN_DB`). |
| 2 | `Actualizar_evento` y `Eliminar_evento` en `citas_tools.py` | El comportamiento (qué campos actualizar, cuándo cancelar) se replicó de las salidas documentadas en el prompt del Agente de Servicio Técnico, sin JSON de n8n completo de referencia. Ya no hay Calendar de por medio (ver más abajo), así que la superficie de bugs se redujo bastante, pero el comportamiento fino sigue sin poder verificarse contra un n8n real. |
| 3 | ~~`SUPABASE_TABLE_CATALOGO`~~ | ✅ **Verificado 2026-09-27**: las tablas `servicios_tecnicos` y `servicios_agendados` existen tal cual, con los nombres de columna que el código ya asumía. Columnas adicionales que el código no conocía y que ya se muestran automáticamente gracias a `formatear_fila`: `servicios_tecnicos.precio`, `servicios_tecnicos.activo`, `servicios_agendados.Notas_servicio`. |
| 4 | ~~Credenciales OAuth de Google Calendar~~ | ✅ **Ya no aplica** (2026-09-27): se quitó Google Calendar por completo. Ver "Se quitó Google Calendar" más abajo. |
| 5 | `SUPABASE_SERVICE_KEY` | Usa la **service_role key** (Project Settings → API → Project API keys → `service_role`, un JWT que empieza con `eyJ...`) — NO la contraseña de la base de datos ni la `anon` key. Las escrituras aquí no pasan por Row Level Security, igual que en el flujo de n8n. |
| 6 | `servicios_tecnicos.activo` no se filtra | El catálogo (`Servicio_tecnico`) hoy trae TODOS los servicios, incluidos los que tengan `activo = false`. Si esa columna existe para desactivar servicios sin borrarlos, `tools/catalog_tools.py` debería filtrar `activo=eq.true` — pendiente de que confirmes que ese es el uso real de la columna. |

### `Eliminar_evento` pasó a ser soft-delete

Al verificar el esquema real se encontró que `servicios_agendados` tiene una
columna `estado` (default `'confirmado'`), y que el prompt del Agente de
Servicio Técnico (Flujo D, "historial de citas") espera poder mostrarle al
cliente citas con Estado distinto de "Confirmado" — eso solo es posible si
la fila se conserva al cancelar, no si se borra. Por eso `Eliminar_evento`
(`tools/citas_tools.py` + `citas_repository.cancelar_cita`) hace un
**UPDATE de `estado` a `"cancelado por cliente"`** en vez de un `DELETE` de
la fila.

## Se quitó Google Calendar: Supabase es la única fuente de verdad

Decisión tomada el 2026-09-27: la versión final de este negocio no va a
registrar las citas en Google Calendar — va a tener su propia web
application de agendamiento, alimentada por esta misma base de datos.
Mantener Calendar y Supabase sincronizados era, además, la causa raíz de la
mayoría de los bugs reales de esta iteración (el desfase de zona horaria, la
complejidad de hacer rollback entre dos sistemas) y un riesgo real para
desplegar en un hosting sin pantalla como Render (la autenticación OAuth de
Calendar necesita abrir un navegador de verdad la primera vez).

`tools/calendar_tools.py` se renombró a **`tools/citas_tools.py`** y ya no
importa nada de Google:

- `{Consultar_eventos}` ya no golpea la API de Calendar: consulta Supabase
  filtrando por solapamiento de horario
  (`citas_repository.leer_citas_confirmadas_en_rango`).
- `{Crear_evento}`/`{Actualizar_evento}` son ahora una sola escritura a
  Supabase — no hay un segundo sistema que sincronizar, así que tampoco hay
  nada que revertir si algo falla.
- El identificador de cita (`google_calendar_event_id` en el código y en el
  prompt) ya NO es un ID de Google — es un UUID que genera `crear_evento`
  con `uuid.uuid4()`. Se conservó ese nombre de campo a propósito para no
  tener que reescribir la transcripción fiel del prompt de negocio, que lo
  menciona por ese nombre en decenas de lugares.

### Restricción de no-solapamiento a nivel de base de datos

Antes, nada garantizaba de forma atómica que dos citas no se cruzaran si dos
conversaciones casi simultáneas agendaban el mismo horario — ni Calendar ni
el código lo prevenían realmente, todo dependía de que el LLM calculara bien
la disponibilidad leyendo texto. Ahora Postgres mismo lo impide
(`supabase/migrations/20260927000002_disponibilidad_sin_calendar.sql`):

```sql
alter table servicios_agendados
    add column periodo tstzrange
    generated always as (tstzrange(fecha_hora_inicio, fecha_hora_fin, '[)')) stored;

alter table servicios_agendados
    add constraint no_solapamiento_citas_confirmadas
    exclude using gist (periodo with &&) where (estado = 'confirmado');
```

Si dos citas confirmadas llegaran a cruzarse, Supabase rechaza la escritura
con `HTTP 400` y `{"code": "23P01"}` — verificado empíricamente contra la API
REST real (no es 409, como cabría esperar). `tools/citas_tools.py` reconoce
ese error puntual (`supabase_client.es_violacion_de_solapamiento`) y lo
traduce en un mensaje amable para el cliente ("ese horario ya no está
disponible") en vez de un error técnico genérico.

La misma migración agrega dos índices que antes no hacían falta (la consulta
de disponibilidad la resolvía Calendar, no Supabase): uno en
`fecha_hora_inicio` (filtrado a `estado='confirmado'`) para que la consulta
de disponibilidad no escanee toda la tabla, y otro en
`(session_id, fecha_hora_inicio desc)` para `Consultar_servicio_agendado`
—esta segunda falta ya existía antes de esta migración, independientemente
de Calendar.

### Qué se queda igual (a propósito)

`TOOLS_SCHEMA` y el prompt de negocio (`ORIGINAL_SYSTEM_PROMPT`) en
`agents/servicio_tecnico_agent.py` **no cambiaron ni una línea** — los
nombres de las tools, sus parámetros y el formato del resumen que reciben
son idénticos a como funcionaban con Calendar. El modelo nunca ve el código,
solo el resultado de cada tool. El prompt original todavía describe algunas
tools como "(Google Calendar)" en `<HERRAMIENTAS_DISPONIBLES>` — es texto
desactualizado que se deja tal cual por ser transcripción fiel del negocio,
sin efecto en el comportamiento real.

### Ya no hacen falta

`credentials.json`, `token.json`, y las variables `GOOGLE_CALENDAR_*` del
`.env` — ya no los lee ningún archivo del proyecto. Si quieres, puedes
revocar el acceso de esa app en https://myaccount.google.com/permissions,
aunque no es urgente (las credenciales simplemente quedaron sin uso).

## Agendamiento por técnico

Cada tipo de servicio técnico (fila del catálogo `servicios_tecnicos`) tiene
un técnico responsable asignado (columna `tecnico_id`, ver
`supabase/migrations/20260928000003_tecnicos.sql`). La disponibilidad y el constraint anti-choque de la
migración 002 dejaron de ser GLOBALES: ahora están acotados **por técnico**
— dos técnicos distintos sí pueden tener citas confirmadas a la misma hora.

- `tools/catalog_tools.resolver_tecnico_para_servicio(servicio_id)` es el
  punto único donde se resuelve qué técnico atiende un servicio.
- `{Consultar_eventos}` ahora **exige** `servicio_id` (además de
  `fecha_inicio`/`fecha_fin`) — es el único cambio real al `TOOLS_SCHEMA` del
  agente desde que existe el proyecto; todo lo demás de esta funcionalidad es
  interno (`tools/citas_tools.py`).
- `{Crear_evento}`/`{Actualizar_evento}` resuelven y guardan el `tecnico_id`
  automáticamente — el agente nunca lo pide, nunca lo recibe como parámetro,
  y el prompt (`NOTA_ASIGNACION_TECNICOS`) le prohíbe mencionarle un técnico
  al cliente.
- Si un servicio no tiene técnico configurado, todas las operaciones
  (`Consultar_eventos`, `Crear_evento`, `Actualizar_evento`) fallan con un
  mensaje explícito en vez de asumir disponibilidad o asignar cualquier
  técnico.

**Migración pendiente de aplicar manualmente:** el conector MCP de Supabase
que antes aplicaba las migraciones automáticamente se desconectó a mitad de
este proyecto. Para activar esta funcionalidad, copia y pega el contenido
completo de `supabase/migrations/20260928000003_tecnicos.sql` en el SQL Editor de tu proyecto de
Supabase (Supabase Studio → SQL Editor → New query → pegar → Run). Crea la
tabla `tecnicos` con 2 técnicos de ejemplo, agrega `tecnico_id` al catálogo y
a las citas, y reemplaza el constraint anti-choque por la versión acotada
por técnico. Después, entra a la tabla `servicios_tecnicos` en Supabase
Studio y reasigna manualmente cada servicio al técnico real que corresponda
(la migración deja todos apuntando al primer técnico de ejemplo, solo para
que la demo funcione de inmediato) — y agrega tus técnicos reales a la tabla
`tecnicos` (o bórrale los de ejemplo).

## Dashboard unificado (`web/static/index.html`)

Una sola página con 4 pestañas (sin recargar al cambiar de una a otra —
`dashboard.js` solo muestra/oculta secciones que ya están en el DOM):

- **Citas** — calendario mensual con las citas reales de Supabase y una
  tabla con todos los registros (cliente, teléfono, servicio, técnico,
  fechas, descripción, estado), con buscador y filtro por estado. Es de
  solo lectura, no pasa por el LLM: consume `GET /api/citas`,
  `GET /api/catalogo` y `GET /api/tecnicos` (ver `web/app.py`), que a su vez
  leen directo de Supabase vía `tools/citas_repository.py`,
  `tools/catalog_tools.py` y `tools/tecnicos_repository.py`. Lógica en
  `admin.js`/`admin.css` (nombre heredado de cuando era una página aparte).
- **Ventas** — esqueleto vacío ("Empty State" estilo Shopify: tabla con
  encabezados + mensaje "Todavía no hay pedidos"), a propósito sin datos ni
  lógica — se activa cuando exista `Agente_Ventas`.
- **Chat prueba** — el mismo panel de pruebas de siempre (`chat.js` +
  `chat.css`), para conversar con los agentes como si fueras el cliente.
- **Flujo en vivo** — monitor en tiempo real de lo que hace el agente por
  dentro (ver sección siguiente).

Los 3 scripts (`admin.js`, `chat.js`, `flujo.js`) se cargan como módulos ES
(`type="module"`) para que cada uno tenga su propio scope — evita que sus
variables de nivel superior (ej. los tres declaran `const el`) choquen entre
sí al convivir ahora en una sola página. Los tres corren siempre (no solo
cuando su pestaña está visible), así que sus datos ya están cargados al
cambiar de pestaña.

## Monitor de Flujo en Vivo (debug del agente en tiempo real, estilo n8n)

La pestaña "Flujo en vivo" tiene 3 partes:

- **Sidebar "Corridas"** (izquierda) — un ítem por cada mensaje real de un
  cliente (una "corrida" = un turno completo, identificado por `run_id`),
  con título (el mensaje), sesión, tiempo relativo y puntos de estado
  (verde/rojo si hubo error) — igual que el historial de ejecuciones de n8n.
- **Diagrama del flujo** (canvas SVG con posiciones fijas) — nodos:
  Cliente → Orquestador → (Modelo / Memoria / Servicio Técnico / **Ventas**,
  esta última punteada porque `Agente_Ventas` no existe todavía) →
  Servicio Técnico → (Modelo / Memoria / sus 6 tools) → Supabase.
  - **Modo "en vivo"** (por defecto): cada evento real hace pulsar de forma
    pasajera el nodo/conexión correspondiente.
  - **Al hacer click en una corrida del sidebar**: el diagrama se congela
    mostrando SOLO lo que participó en ESE turno puntual (coloreado), con
    lo demás apagado — click en un nodo muestra su detalle exacto
    ("participó" / "no participó en esta corrida").
- **Log de detalle** (abajo) — el payload completo de entrada/salida de
  cada evento, sin resumir.

Arquitectura backend (piezas nuevas):

1. **`tools/eventos_agente.py`** — bus de eventos en memoria (una cola
   `queue.Queue` thread-safe por cliente SSE conectado) + historial de las
   últimas 50 corridas (`iniciar_corrida`, `listar_corridas`,
   `obtener_corrida`). `publicar_evento(...)` nunca bloquea al agente real:
   si un suscriptor está lleno, se descarta su evento más viejo.
2. **`llm_loop.py`** y **`sesiones.py`** instrumentados — publican un evento
   en cada intento de modelo, cada tool, y cada lectura/escritura de memoria
   (con latencia y payload). El parámetro `contexto={"agente", "session_id",
   "run_id"}` de `run_agent_loop` solo ETIQUETA esos eventos — no cambia el
   comportamiento real del loop. `run_id` se genera una vez por
   `POST /api/chat` (`web/app.py`) y se propaga a través de
   `orquestador.run()` → `servicio_tecnico_agent.run()`, así los eventos de
   AMBOS agentes en un mismo turno quedan bajo la misma corrida.
3. **`GET /api/flujo/stream`** (Server-Sent Events, `EventSource` nativo) +
   **`GET /api/flujo/corridas`** / **`GET /api/flujo/corridas/{run_id}`**
   para el sidebar de historial.

**Limitación conocida:** tanto el bus de eventos como el historial de
corridas viven en memoria de UN solo proceso — si en el futuro despliegas
con varios workers, cada worker tendría su propia copia y un dashboard
conectado a un worker no vería lo que procesa otro. Igual que la limitación
ya documentada de `_propuestas_pendientes` en `tools/citas_tools.py`,
aceptable para desarrollo/debug; en producción con varios workers
necesitaría un backend de pub/sub y almacenamiento real (Redis, etc.).

## Cómo correr

**Antes de la primera ejecución** aplica las migraciones pendientes en
Supabase (hoy faltan la 004 y la 005; sin la 005 cada turno falla al guardar
y `/ready` responde 503). Pasos en `docs/PLAN_DE_MEJORAS.md`, Fase 9.4.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # y completa tus valores reales (incluida API_KEY)
python main.py         # consola: chatea como si fueras el cliente de WhatsApp
```

Si falta una variable obligatoria, el programa no arranca y dice cuál
(`config.py`).

## Cómo correr la interfaz web (dashboard + chat de prueba)

```bash
uvicorn web.app:app --reload
```

Abre `http://127.0.0.1:8000`. Si definiste `API_KEY`, el dashboard la pide
una vez y la recuerda en este navegador. Desde la barra lateral puedes
escribir un "teléfono" (session_id) distinto por conversación para simular
varios clientes. El historial real de cada agente se guarda en la columna
`historiales` de la tabla `conversaciones` de Supabase (ver `sesiones.py`);
lo que se ve en pantalla se guarda además en `localStorage` solo como
conveniencia visual.

Endpoints de operación (sin clave, para el hosting): `GET /health` (el
proceso vive) y `GET /ready` (Supabase responde y el esquema está al día).
Métricas: `GET /api/metricas` (con clave).

## Correr con Docker

```bash
docker build -t tecnielectronics .
docker run --env-file .env -p 8000:8000 tecnielectronics
```

Un solo worker a propósito (ver el comentario en el `Dockerfile`).

## Tests y calidad (no necesitan credenciales)

```bash
python tests/correr_todos.py        # todos los scripts, cada uno en su proceso
# o, con las herramientas de desarrollo:
pip install -r requirements-dev.txt
pytest                              # mismos scripts vía tests/pytest_suite.py
ruff check .
mypy .
```

Cada `tests/test_*.py` es un script con asserts que simula el LLM y
Supabase (no gasta cuota ni toca datos reales), salvo
`test_integracion_postgres.py`, que aplica todas las migraciones sobre un
PostgreSQL real (`TEST_DATABASE_URL`, o un clúster temporal con `initdb`
si hay PostgreSQL instalado; si no, se omite). La CI
(`.github/workflows/ci.yml`) corre todo en cada push.

Los tests validan la mecánica, no el comportamiento real del prompt. Para eso,
usa `main.py` o la interfaz web con casos reales (cancelación solo con
confirmación explícita, fecha ambigua, cliente que se arrepiente a mitad de
flujo, conversación de más de 12 turnos, etc.).

## Sobre los modelos de OpenRouter

`OPENROUTER_MODELS` en `.env` es la lista de fallback, en orden. Mientras
desarrollas, modelos `:free` están bien — los límites de 20 req/min y
50-1.000 req/día no deberían estorbar en pruebas manuales. Antes de producción
real con el cliente, revisa la conversación sobre por qué conviene comprar
los $10 de crédito de OpenRouter y dejar un modelo pagado barato
(ej. `openai/gpt-4o-mini`) como último eslabón del fallback.

## Optimización del flujo de agendamiento

`NOTA_OPTIMIZACION_AGENDAMIENTO` en `agents/servicio_tecnico_agent.py` es una
adición explícita (no estaba en el prompt de negocio original) para reducir
turnos de conversación al agendar, agregada al final del prompt con el mismo
patrón que ya usa `orquestador.py` para su nota temporal — el prompt original
(`ORIGINAL_SYSTEM_PROMPT`) queda intacto para poder compararlo contra n8n. En
resumen, la nota le pide al agente:

1. No volver a pedir nombre/teléfono/descripción si el cliente ya los dio en
   cualquier mensaje previo (incluido el primero).
2. Si el cliente ya dio los 3 datos Y un día específico en el mismo mensaje,
   aplicar de inmediato la excepción de "día específico" sin tratarlo como
   dos pasos separados.
3. Cuando el total de franjas libres a ofrecer sea de 6 o menos, mostrar
   día+hora combinados en un solo mensaje (máx. 6 opciones) en vez de forzar
   dos turnos separados — con más de 6 franjas, se mantiene el flujo
   progresivo original (día, luego hora).
4. En Flujos B/C, no volver a preguntar el alcance del cambio si el cliente
   ya lo especificó junto con su intención de modificar/cancelar.

Ninguno de estos puntos afloja la confirmación explícita obligatoria antes de
`Crear_evento`/`Actualizar_evento`/`Eliminar_evento` — esa regla de seguridad
del negocio se mantiene intacta.

## Bugs reales encontrados y corregidos probando la interfaz de chat

### El agente no sabía qué día era "hoy"

El prompt original menciona "$now" varias veces (ej. "{fecha_inicio} SIEMPRE
el día y hora ACTUAL — $now"), asumiendo un motor de templating que se lo
resuelva, como hacía n8n. En esta traducción a Python nada le decía al
modelo qué día era hoy realmente, así que en pruebas reales el modelo
terminó copiando fechas de ejemplo del propio prompt ("Lunes, 27 de Julio")
en vez de calcular la fecha real. Corregido en
`agents/servicio_tecnico_agent.py`: `run()` calcula la fecha/hora real en
cada turno (usando `tools.citas_tools.zona_horaria_configurada()`, que lee
`TIMEZONE_OFFSET` del `.env` — antes tampoco se usaba en ningún lado) y se la
agrega al prompt como una nota aparte, nunca como texto fijo.

### Citas reprogramadas quedaban guardadas con la hora corrida en Supabase

Al reprogramar una cita, la hora terminó guardada en Supabase 5 horas
corrida (una cita de las 4:00 PM de Bogotá quedó en la base de datos como
"16:00:00+00", es decir, tratada como si esas 4:00 ya fueran UTC), aunque en
Google Calendar (todavía en uso en ese momento) se veía perfecto. Causa
real: `crear_evento`/`actualizar_evento` tomaban la fecha de la RESPUESTA de
la API de Calendar para guardarla en Supabase, en vez de controlar y
normalizar ellos mismos el valor — si en algún punto esa respuesta (o lo que
envió el modelo) no traía el offset `-05:00` explícito, Postgres la
interpretaba directo como UTC. Corregido con
`tools.citas_tools.normalizar_fecha_hora()`: garantiza que todo datetime
tenga SIEMPRE un offset explícito antes de guardarse en Supabase (si llega
"naive", se asume la zona horaria del negocio). Este bug fue, de hecho, una
de las razones para terminar quitando Calendar por completo (ver "Se quitó
Google Calendar" más arriba).

### Un modelo `:free` saturado tumbaba el proceso en vez de hacer fallback

Bajo el pool compartido de OpenRouter, un modelo `:free` saturado a veces
responde `200 OK` con un cuerpo vacío/roto (`choices: null`) en vez de
lanzar un error HTTP — algo que `_call_with_fallback` en `llm_loop.py` no
contemplaba, así que el proceso crasheaba más adelante con
`TypeError: 'NoneType' object is not subscriptable` al intentar leer
`response.choices[0]`, en vez de simplemente probar el siguiente modelo del
fallback. Corregido: `_call_with_fallback` ahora valida que `response.choices`
no venga vacío antes de devolver la respuesta; si viene vacío, se trata como
una falla más del modelo y se sigue con el siguiente de la lista.

## Recomendaciones de arquitectura (documentadas, no aplicadas aún)

- ~~Condición de carrera en la disponibilidad~~ — ✅ **Resuelta 2026-09-27**:
  el constraint `no_solapamiento_citas_confirmadas` (ver "Se quitó Google
  Calendar" más arriba) hace que Postgres mismo rechace cualquier choque de
  horario, de forma atómica, sin importar cuántas conversaciones concurrentes
  intenten agendar al mismo tiempo.
- **Estilo de tests**: hoy son scripts con asserts (consistentes con
  `tests/test_llm_loop.py`); si el proyecto crece, vale la pena migrar a
  `pytest` para tener descubrimiento automático y mejores reportes.
- **`servicios_agendados.google_calendar_event_id`**: quedó sin usarse en
  serio como "ID de Calendar" (ahora es un UUID propio, ver más arriba); si
  en algún momento quieren limpiar el esquema, se podría renombrar la
  columna a algo como `id_publico_cita` — no es urgente, es solo un nombre.

## Nota sobre prompt caching

Este proyecto usa OpenRouter (formato OpenAI de tool calling), no la API
directa de Anthropic — por eso `llm_loop.py` no implementa `cache_control`.
Si en algún punto migran el motor de razonamiento a la API de Claude
directamente (no vía OpenRouter), ahí sí aplica el prompt caching que se
discutió — pero mientras el motor sea OpenRouter, ese mecanismo no está
disponible de la misma forma para todos los modelos.

## Estructura

```
.
├── llm_loop.py                  # loop genérico de tool use: fallback, disyuntor, presupuesto de turno, saneo
├── contexto_conversacion.py     # ventana de contexto, ficha y recorte al guardar (Fase 6)
├── config.py                    # validación de variables de entorno al arrancar
├── sesiones.py                  # AlmacenSesiones: historiales por agente en Supabase + lock por sesión
├── main.py                      # arnés de pruebas por consola
├── agents/
│   ├── orquestador.py           # Orquestador + registro de sub-agentes (SUBAGENTES)
│   └── servicio_tecnico_agent.py# Agente de Servicio Técnico (+ notas, ficha de contexto)
├── tools/
│   ├── supabase_client.py       # wrapper del REST de Supabase (timeouts, reintentos de lectura)
│   ├── citas_repository.py      # acceso a la tabla de citas
│   ├── citas_tools.py           # Consultar/Crear/Actualizar/Eliminar_evento (reglas de agenda, confirmación)
│   ├── catalog_tools.py         # Servicio_tecnico, Consultar_servicio_agendado, leer_servicio
│   ├── validacion_tools.py      # modelos Pydantic de argumentos + verificación de coherencia
│   ├── tecnicos_repository.py   # técnicos (solo dashboard)
│   ├── eventos_agente.py        # bus de eventos + logs JSON con PII enmascarada
│   └── metricas.py              # contadores y alertas
├── web/
│   ├── app.py                   # FastAPI: /api/* (con clave), /health, /ready, estáticos
│   ├── seguridad.py             # API key y límite de mensajes por sesión
│   └── static/                  # dashboard (index.html, api.js, chat/admin/flujo/dashboard .js/.css)
├── supabase/migrations/         # migraciones en formato Supabase CLI (001–005)
├── tests/                       # scripts de prueba + correr_todos.py + pytest_suite.py
├── docs/PLAN_DE_MEJORAS.md      # plan, registro de cambios y guía para agregar Ventas
├── Dockerfile, .dockerignore
├── .github/workflows/ci.yml
├── pyproject.toml               # configuración de Ruff, Mypy y pytest
├── requirements.txt, requirements-dev.txt
└── .env.example
```
