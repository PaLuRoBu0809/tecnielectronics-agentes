# Plan de mejoras — Agente TecniElectronics

Plan de acción para llevar el piloto (Orquestador + Agente de Servicio
Técnico) a un estado apto para producción. Cada fase dice explícitamente
**qué toca**:

- **[PROMPT]** — cambia lo que el modelo lee (prompts, descripciones de tools,
  mensajes inyectados). Puede alterar el comportamiento del agente y hay que
  re-probarlo con conversaciones reales.
- **[TOOLS/INFRA]** — cambia solo el código que rodea al modelo (tools,
  loop, API, base de datos, despliegue). El modelo no ve la diferencia salvo
  por los resultados de las tools.

Regla transversal: `ORIGINAL_SYSTEM_PROMPT` de ambos agentes **no se toca**
(es la transcripción fiel del negocio, ver README).

Cómo correr los tests:

```bash
python tests/correr_todos.py     # sin dependencias extra
pytest                           # con requirements-dev.txt (incluye el test contra PostgreSQL real)
ruff check . && mypy .
```

---

## Estado general

| Fase | Tema | Toca | Estado |
|---|---|---|---|
| 1 | Seguridad de datos | TOOLS/INFRA | ✅ Hecha |
| 2 | Red y resiliencia del LLM | TOOLS/INFRA | ✅ Hecha |
| 3 | Presupuesto de turno, razón de parada, historial válido | TOOLS/INFRA | ✅ Hecha |
| 4 | Validación de argumentos y reglas de negocio en código | TOOLS/INFRA | ✅ Hecha |
| 5 | Guardia de confirmación por identificador de turno | TOOLS/INFRA | ✅ Hecha |
| 6 | Compactación del contexto | TOOLS/INFRA + **[PROMPT]** | ✅ Hecha |
| 7 | Concurrencia por sesión | TOOLS/INFRA | ✅ Hecha |
| 8 | Observabilidad (tokens, costo, logs persistentes, PII) | TOOLS/INFRA | ✅ Hecha |
| 9 | Operación (config, health, Docker, CI, migraciones) | TOOLS/INFRA | ✅ Hecha |
| 10 | Base de datos (constraint y backfill de técnicos) | TOOLS/INFRA | ✅ Hecha |
| 11 | Preparación multi-agente (registro de sub-agentes) | TOOLS/INFRA | ✅ Hecha |
| 12 | Agente de Ventas + orquestador como asesor comercial | TOOLS/INFRA + **[PROMPT]** | 🚧 En curso (ver Fase 12) |
| M | Acciones manuales del dueño del proyecto | — | ⏳ Pendiente (ver abajo) |

(La tabla se actualiza a medida que avanzan las fases.)

**Próximos planes, en este orden:**
1. Terminar la Fase 12 (`Agente_Ventas`): pasos pendientes en su sección.
2. Integración directa con WhatsApp Cloud API y contenido multimedia:
   [`PLAN_INTEGRACION_WHATSAPP.md`](PLAN_INTEGRACION_WHATSAPP.md).

---

## Fase 1 — Seguridad de datos · [TOOLS/INFRA]

1.1 `Actualizar_evento` y `Eliminar_evento` verifican que la cita pertenece
    al `session_id` de la conversación y que sigue activa.
    *Hecho cuando:* un cliente no puede modificar ni cancelar la cita de otro
    aunque conozca su identificador.

1.2 Autenticación de `/api/*` con clave en cabecera `X-API-Key`.
    *Hecho cuando:* con `API_KEY` configurada, toda petición sin la clave
    recibe 401; el dashboard la pide una vez y la recuerda.

1.3 `formatear_fila` oculta al modelo columnas internas (`session_id`,
    `tecnico_id`, `periodo`).
    *Hecho cuando:* ningún resultado de tool expone el teléfono de sesión ni
    el técnico asignado.

1.4 Límite de uso por sesión (mensajes por minuto y por día) en `/api/chat`.
    *Hecho cuando:* un script que envía mensajes en bucle recibe 429 sin
    consumir cuota de OpenRouter.

## Fase 2 — Red y resiliencia del LLM · [TOOLS/INFRA]

2.1 Timeouts separados (conexión / lectura) en el cliente LLM y en
    `supabase_client`. `max_retries=0` en el SDK de OpenAI.
2.2 El cliente de OpenAI se crea dentro del manejo de errores (una clave
    ausente ya no produce un 500).
2.3 Errores clasificados: 401/402/403 cortan el turno y registran un error
    crítico (no se disfrazan de fallback); 400 se registra y se prueba el
    siguiente modelo; 404 saca al modelo por un periodo largo.
2.4 Disyuntor por modelo: tras 429/5xx/timeout el modelo se salta durante un
    periodo (respeta `Retry-After`). Si todos están abiertos, se prueba el
    que expira antes.
2.5 Reintentos con backoff y jitter **solo** en lecturas de Supabase (GET).
    Las escrituras nunca se reintentan a ciegas.

## Fase 3 — Presupuesto de turno y razón de parada · [TOOLS/INFRA]

3.1 Presupuesto por turno **compartido** entre orquestador y sub-agente:
    máximo de llamadas al LLM y tiempo límite.
    *Hecho cuando:* el peor caso de un turno está acotado y medido.
3.2 Razón de parada explícita (`final`, `max_iteraciones`, `deadline`,
    `presupuesto_llamadas`, `modelos_caidos`, `credenciales`,
    `error_interno`) registrada en cada turno.
3.3 `run_agent_loop` nunca propaga excepciones: ante un error inesperado
    devuelve un mensaje amable y el historial saneado, de modo que el turno
    siempre se persiste.
3.4 Historial válido: al guardar, se eliminan mensajes del asistente con
    `tool_calls` sin sus resultados.
    *Hecho cuando:* un test simula un aborto a mitad y el turno siguiente
    funciona.

## Fase 4 — Validación y reglas de negocio en código · [TOOLS/INFRA]

4.1 Modelos Pydantic para los argumentos de cada tool; se validan antes de
    ejecutar y el error vuelve al modelo como texto legible.
4.2 Reglas que hoy solo vivían en el prompt, ahora también en código:
    horario operativo (lunes a viernes, 7:00–18:00), `fin > inicio`,
    duración = `duracion_minutos` del catálogo, no agendar en el pasado.
4.3 Test de coherencia `TOOLS_SCHEMA` ↔ modelos Pydantic ↔ `tool_functions`.
    `TOOLS_SCHEMA` se mantiene escrito a mano (no se regenera desde Pydantic)
    para no cambiar ni una coma de lo que ve el modelo.

## Fase 5 — Guardia de confirmación por turno · [TOOLS/INFRA]

5.1 El guardia compara un **identificador de turno** (`run_id`), no el texto
    que el orquestador pasa al sub-agente (que puede ser una paráfrasis).
5.2 `orquestador.run()` genera el `run_id` si no viene (consola `main.py`).

## Fase 6 — Compactación del contexto · [TOOLS/INFRA] + [PROMPT]

6.1 Truncar el contenido de resultados de tools antiguos (catálogo,
    disponibilidad) al guardar.
6.2 Ventana deslizante que siempre empieza en un mensaje `user` y nunca
    separa un `assistant(tool_calls)` de sus resultados.
6.3 Ficha estructurada (nombre, teléfono, `servicio_id`, cita activa)
    construida **por código** a partir de argumentos y resultados de tools
    — nunca resumida por un LLM. **[PROMPT]**: se inyecta como nota, hay que
    re-probar conversaciones reales.

## Fase 7 — Concurrencia · [TOOLS/INFRA]

7.1 Lock por sesión en proceso (un solo worker). Si se escala a varios
    workers: columna `version` en `conversaciones` con guardado condicionado
    (un advisory lock de Postgres no sirve por PostgREST).

## Fase 8 — Observabilidad · [TOOLS/INFRA]

8.1 Conteo de tokens (`response.usage`) y costo por turno, modelo y agente.
8.2 Eventos persistidos como logs JSON en stdout, con teléfonos enmascarados.
8.3 Alertas mínimas: tasa de fallback, turnos cortados por presupuesto,
    errores 401/402, errores de tool.

## Fase 9 — Operación · [TOOLS/INFRA]

9.1 Configuración validada al arrancar (si falta una variable, no levanta).
9.2 `/health` y `/ready` (este último comprueba Supabase).
9.3 Dockerfile multi-stage y arranque con un comando.
9.4 Migraciones con Supabase CLI (`supabase/migrations/`), no Alembic.
9.5 CI: pytest + Ruff (+ Mypy laxo) en cada push.

## Fase 10 — Base de datos · [TOOLS/INFRA]

10.1 `003_tecnicos.sql` incluye `create extension if not exists btree_gist`.
10.2 Backfill de `tecnico_id` en citas existentes y `NOT NULL` (en un
     `EXCLUDE ... tecnico_id WITH =` los NULL nunca chocan).
10.3 Test de integración del constraint contra un Postgres real.

## Fase 12 — Agente de Ventas · [TOOLS/INFRA] + [PROMPT]

Decisiones del negocio: MercadoPago como pasarela; `orders` es la tabla de
pedidos (`pedidos` no se usa); n8n está retirado (todo es FastAPI); el stock
se descuenta al CREAR la orden, no al añadir al carrito; modificar o
cancelar solo el ÚLTIMO pedido y solo en `PENDIENTE_DESPACHO`; un pedido
pagado no se cancela por el chat (reembolso con la empresa); el inventario
migrará a Siigo (por eso todo el acceso pasa por `tools/*_repository.py`).

| Paso | Qué | Estado |
|---|---|---|
| A | Base de datos: migraciones 006 (ENUM, PK del carrito, funciones atómicas, pg_cron) y 007 (sin acceso `anon`) | ✅ Aplicada en Supabase |
| B | Repositorios + búsqueda: sinónimos (008), resumen por marca y precio (009), orden por precio (010) | ✅ Aplicadas en Supabase |
| D | Confirmación de dos turnos compartida (`tools/confirmacion.py`, clave por agente) | ✅ |
| E | Tools y agente de Ventas, registrado en el orquestador | ✅ |
| E.1 | Ajustes tras la primera prueba real (ver abajo) | ✅ código · ⏳ **re-probar en el chat** |
| C | MercadoPago: preferencia de pago, webhook firmado, conciliación, mensajes de pago aprobado/rechazado | ✅ Probado contra el sandbox · ⏳ clave del webhook y URL pública |
| G | Panel: pestaña **Pedidos** en el dashboard, nodo Ventas en "Flujo en Vivo", formato Markdown en el chat web | ✅ |

### E.1 Mejoras del orquestador y de Ventas tras la primera prueba real (2026-10-01)

Problemas vistos en la conversación de prueba y su corrección:

1. **El orquestador parecía solo de citas.** "Quiero saber los servicios
   disponibles" se delegó a Servicio Técnico, y después un simple "buenas"
   también (por la regla de continuidad). → `NOTA_ASESOR_COMERCIAL`
   **[PROMPT]**: un saludo en cualquier momento y las preguntas generales
   presentan las DOS líneas de negocio con la plantilla fija.
2. **No ofrecía todo lo que vende la empresa.** → Venta cruzada **[PROMPT]**:
   al cerrar un pedido o una cita, una sola línea ofreciendo la otra línea de
   negocio (máximo una vez por conversación, nunca si el cliente la rechazó).
3. **Doble delegación en el mismo turno.** El orquestador invocó a Ventas dos
   veces con un mismo mensaje, y otra vez con un "¿Confirmas que todo es
   correcto?" inventado como si fuera del cliente: el cliente tuvo que
   confirmar dos veces, y repetir una delegación puede repetir escrituras.
   → Garantía de código en `orquestador.run()`: una sola delegación por
   mensaje; un segundo intento se bloquea sin ejecutar al sub-agente.
4. **"Los más caros" se adivinaba.** La búsqueda no ordenaba de mayor a
   menor y el agente dedujo la marca más cara de los rangos de las 8 marcas
   del resumen. → Migración 010 + parámetro `orden` (`mas_baratos` /
   `mas_caros`) en `Inventario`, que reemplaza a `mostrar_siempre`.
5. **"Dame el más barato" lo agregó al carrito sin preguntar.** → Nota 8 de
   Ventas **[PROMPT]**: pedir "el más barato" es pedir verlo; solo se agrega
   sin preguntar si el cliente dice "agrégalo", "me lo llevo", etc.
6. **"La app no guarda los pedidos".** El pedido sí se guardó (#36, luego
   cancelado por el propio cliente); lo que falta es verlo en el dashboard →
   paso G.

### C. MercadoPago (2026-10-02)

- `tools/pagos.py`: única pieza que habla con la API. Preferencia (Checkout
  Pro) en COP con `external_reference` propia, `X-Idempotency-Key` (un
  reintento no crea otro cobro) y vencimiento 10 min antes que la reserva.
- `tools/servicio_pagos.py`: el estado SIEMPRE se lee de la API (un aviso
  falso no marca nada como pagado); un aviso por estado final
  (`orders.estado_pago_notificado`); si el envío falla no se marca y el
  siguiente aviso lo reintenta; pago de un pedido ya cancelado -> aviso al
  cliente y log para gestión manual.
- `POST /webhooks/mercadopago` (fuera de `/api`): firma `x-signature`
  obligatoria si hay `MERCADOPAGO_WEBHOOK_SECRET`; 500 si falla, para que
  MercadoPago reintente.
- Red de seguridad: `Consultar_orden` concilia con MercadoPago los pedidos
  en línea sin aprobar antes de responder. En local sin URL pública es el
  único camino, y basta.
- `notificaciones/`: textos de pago aprobado / rechazado (con link y plazo)
  armados por código; `NotificadorHistorial` los deja en la conversación
  (orquestador + Ventas) con el candado de la sesión. Cuando exista
  WhatsApp, se agrega un notificador que además los envíe.
- Verificado contra el sandbox real: la preferencia se crea y devuelve el
  link de pago de Colombia (MCO).

### G. Panel (2026-10-02)

- Pestaña **Ventas**: `GET /api/pedidos` + `web/static/ventas.js` (tabla con
  búsqueda, filtros por estado de pago y de envío, link de pago de los
  pedidos sin pagar). Verificado con los 27 pedidos reales.
- "Flujo en Vivo": nodo Ventas activo, sus 10 tools y el nodo MercadoPago.
- Chat web: negritas (`**x**` / `*x*`) y enlaces clicables, sin `innerHTML`.

### E.2 Rendimiento (2026-10-07)

Medido en Render: cada llamada al modelo tarda ~3,5 s (mediana; p90 9,2 s) y
una búsqueda típica en Ventas hacía 5 llamadas (~22 s). Cambios:

- **Modelos**: fuera `qwen3.8-27b:free` (dejó de ser gratis), `gemma-4-31b:free`
  (siempre 429) e `inkling:free` (403). Lista elegida con una prueba real de
  enrutamiento con tools: nemotron-3-super, nemotron-3-ultra,
  nemotron-3.5-lightning y cohere north-mini-code.
- **Respuesta directa del sub-agente** (`tools_terminales` en `run_agent_loop`):
  el orquestador ya no hace una 2ª llamada solo para copiar la respuesta. La
  venta cruzada pasó al código (`SubAgente.tool_de_cierre` / `venta_cruzada`:
  tras `Crear_orden` o `Crear_evento` exitosos, una vez por conversación). La
  línea del "tema pendiente" ya no aplica.
- **Categorías en el prompt de Ventas** (`nota_categorias`): con las categorías
  en el prompt, el turno se arma sin `Categorias_inventario`. Solo con la nota
  el modelo la seguía llamando (el prompt original dice "SIEMPRE"); si no se
  pueden leer las categorías, la tool vuelve a ofrecerse.
- Resultado medido ("busco unos audífonos", 3 corridas): **3 llamadas** al
  modelo (antes 5) y **7–13 s** (antes ~22 s de mediana).

### Pasos pendientes para terminar la Fase 12

1. **Re-probar en el chat** los casos de E.1 y una compra **en línea**
   completa: link de pago -> pagar en el sandbox con una tarjeta de prueba
   -> "ya pagué" -> debe confirmar el pago leyendo MercadoPago.
2. **Para los avisos automáticos (webhook)**: URL pública (Render, o un
   túnel como `cloudflared` en local) en `URL_PUBLICA_BASE`, y la clave
   secreta del webhook en `MERCADOPAGO_WEBHOOK_SECRET` (Tus integraciones ->
   Webhooks, evento "Pagos", URL `<URL_PUBLICA_BASE>/webhooks/mercadopago`).
3. Antes de producción: credenciales de producción de MercadoPago y
   `CONTACTO_EMPRESA`.
4. Revisar `MAX_LLAMADAS_LLM_POR_TURNO` con conversaciones reales de compra.

## Acciones manuales (no las puede hacer el código)

Estado verificado el 2026-09-30 contra el Supabase real (solo lecturas).

- M1. ✅ **Hecho (2026-09-30).** El dueño del proyecto revocó el acceso de
  la app de Google (conexión "sheets1n8n", permiso de Calendar completo) y
  borró sus credenciales en Google Cloud; luego se eliminaron
  `credentials.json` y `token.json` de la carpeta. Ningún archivo del código
  los usaba.
- M2. ✅ **Hecho (2026-09-30).** `API_KEY` generada (48 caracteres
  aleatorios) y agregada al `.env` sin mostrarla. Falta definirla también
  en el hosting cuando se despliegue. Ver "Gestión de secretos".
- M3. ✅ **Hecho (2026-09-30).** Migraciones 004 y 005 aplicadas por el
  dueño del proyecto en el SQL Editor. Verificado (solo lecturas): 0 citas
  confirmadas sin técnico (antes 5); las 11 conversaciones tienen
  `historiales` idéntico a sus columnas viejas; `/ready` responde 200 y
  `AlmacenSesiones.obtener()` lee sesiones reales con el código nuevo.
  Pendiente opcional: pasar a la CLI de Supabase (Fase 9.4) marcando
  **001–005** como aplicadas en el `migration repair`.
- M4. Verificar que los backups de Supabase están activos en tu plan y
  probar una restauración.
- M5. Dar a este proyecto su **propio repositorio git** (hoy la raíz del
  repositorio es `C:\Users\pablo`). Es requisito para que GitHub ejecute
  `.github/workflows/ci.yml`, y evita subir archivos personales por error.
- M6. Probar con conversaciones reales (ver "Pendiente de validar" en las
  Fases 4 y 6): agendar, reprogramar, cancelar, y una conversación de más de
  12 turnos.

---

# Registro de cambios

Cada entrada documenta qué se cambió, en qué archivos, por qué, y cómo se
verificó.

## Fase 1 — Seguridad de datos ✅ · 2026-09-30 · [TOOLS/INFRA]

**Ningún prompt cambió.** El modelo solo nota la diferencia porque ahora
algunas tools devuelven un error nuevo o muestran menos columnas.

### 1.1 La cita pertenece al cliente y sigue activa

- `tools/citas_tools.py`: nueva función `_cita_activa_del_cliente`, usada por
  `actualizar_evento` y `eliminar_evento` **antes** de la salvaguarda de
  confirmación. Rechaza citas de otro `session_id`, inexistentes o no
  confirmadas.
- Una cita ajena devuelve **el mismo mensaje** que una inexistente: no se
  revela que ese identificador existe.
- `tools/citas_repository.py`: `actualizar_cita` y `cancelar_cita` ahora
  exigen `session_id` y lo incluyen como filtro en el propio `PATCH`
  (segunda barrera: aunque falle la verificación previa, la base de datos
  no toca filas de otro cliente). Nueva constante `ESTADO_CONFIRMADO`.
- **Por qué:** antes bastaba con conocer un Event ID (por ejemplo,
  obteniéndolo por prompt injection) para modificar o cancelar la cita de
  otra persona.

### 1.2 Autenticación del API

- Nuevo `web/seguridad.py` con `verificar_api_key`: si `API_KEY` está
  definida, todas las rutas `/api/*` exigen `X-API-Key` (comparación en
  tiempo constante). El stream SSE la acepta como `?api_key=` porque
  `EventSource` no permite cabeceras.
- `web/app.py`: la dependencia se aplica a nivel de app; los archivos
  estáticos siguen públicos (no contienen datos). Sin `API_KEY` el servidor
  arranca con un aviso de "API abierto".
- Nuevo `web/static/api.js` (`apiFetch`, `urlConClave`): agrega la clave a
  cada petición, la pide una vez al primer 401 y la recuerda en
  `localStorage`. `chat.js`, `admin.js` y `flujo.js` lo usan; `flujo.js`
  reconecta el stream si la clave se ingresa después de abrirlo.
- **Limitación aceptada:** la clave en query string del stream puede quedar
  en logs de un proxy. Aceptable para un panel interno; si el panel se
  expone a más personas, conviene login con cookie de sesión.
- **Nota:** `/docs` y `/openapi.json` de FastAPI siguen públicos (describen
  el API, no exponen datos).

### 1.3 Columnas internas ocultas al modelo

- `tools/supabase_client.py`: `COLUMNAS_OCULTAS_AL_AGENTE` =
  `session_id`, `tecnico_id`, `periodo`. `formatear_fila` las omite.
- **Desvío deliberado de la propuesta original** (lista de permitidas): se
  usó una lista de ocultas para conservar el diseño documentado del
  proyecto, donde una columna nueva de negocio aparece sola. Lo que el
  modelo ve son datos del propio cliente; el riesgo real eran las columnas
  internas. Si se agrega una columna sensible, hay que sumarla a la lista.
- Efecto colateral bueno: el modelo ya no ve `tecnico_id`, así que no puede
  mencionarle un técnico al cliente (el prompt lo prohibía).

### 1.4 Límite de uso

- `web/seguridad.py`: `LimitadorDeUso` (ventana deslizante por sesión,
  `LIMITE_MENSAJES_POR_MINUTO`=10 y `LIMITE_MENSAJES_POR_DIA`=200 por
  defecto). `/api/chat` responde 429 antes de llamar al LLM.
- `ChatRequest`: `session_id` ≤ 64 y `mensaje` ≤ 2000 caracteres.
- `chat.js` muestra un mensaje claro cuando recibe 429.
- **Limitación aceptada:** contador en RAM de un proceso (igual que el bus
  de eventos). Con varios workers haría falta Redis o una tabla.

### Verificación

- `tests/test_citas_repository.py`: tests 6/6b adaptados (las citas
  simuladas llevan dueño y estado) + bloque 6c nuevo (cita ajena,
  inexistente y cancelada; mensaje idéntico para ajena e inexistente;
  nunca escribe) + test de columnas ocultas.
- Nuevo `tests/test_seguridad_api.py` con `TestClient`: 401 sin clave, 200
  con cabecera o query param, estáticos públicos, 429 al tercer mensaje sin
  llegar al orquestador, 422 con mensaje largo, ventanas del limitador con
  reloj simulado.
- Nuevo `tests/correr_todos.py` (corre todos los scripts con UTF-8).
- Resultado: **4/4 scripts pasan.**
- `.env.example`: `API_KEY`, `LIMITE_MENSAJES_POR_MINUTO`,
  `LIMITE_MENSAJES_POR_DIA`.

## Fases 2 y 3 — Resiliencia del LLM y presupuesto de turno ✅ · 2026-09-30 · [TOOLS/INFRA]

**Ningún prompt cambió.** Se hicieron juntas porque ambas viven en
`llm_loop.py`. Lo único nuevo que el cliente puede ver son los textos de
respaldo cuando un turno se corta (ver `_MENSAJES_RESPALDO`).

### 2.1 y 2.2 Timeouts y cliente

- `llm_loop._client()`: `max_retries=0` y `Timeout(45, connect=5)`
  (configurables con `LLM_TIMEOUT_*`). Cada petición usa además
  `min(lectura, tiempo restante del turno)`.
- **Por qué:** el SDK reintentaba 2 veces por defecto *dentro* de cada
  intento del fallback, y no había timeout: una llamada colgada bloqueaba un
  hilo de FastAPI indefinidamente.
- El cliente se crea dentro del manejo de errores: una
  `OPENROUTER_API_KEY` ausente termina en mensaje amable, no en un 500.
- `tools/supabase_client.py`: `timeout=(5, 15)` (conexión, lectura),
  configurable con `SUPABASE_TIMEOUT_*`, en todas las llamadas.

### 2.3 Errores clasificados (`_clasificar_error`)

| Código | Acción |
|---|---|
| 401 / 402 | Corta el turno (`credenciales`), `logger.critical`. No prueba otros modelos. |
| 400 / 422 | `logger.error` (posible bug nuestro o modelo incompatible), sigue con el siguiente, sin pausa. |
| 403 | Igual que 400. **No** se trata como credenciales: en OpenRouter la moderación de algunos modelos devuelve 403. |
| 404 | Pausa de 1 h (el modelo ya no existe o dejó de ser `:free`). |
| 429 | Pausa según `Retry-After` (60 s si no viene, máx. 10 min). |
| 5xx, timeout, conexión, 200 sin `choices` | Pausa de 30 s. |

El evento `modelo_fallo` del panel ahora incluye `tipo_error`.

### 2.4 Disyuntor por modelo

- `_pausar_modelo` / `_modelos_a_intentar`: estado del proceso (dict con
  lock). Un modelo en pausa se saltea en las siguientes llamadas. Si
  **todos** están en pausa, se intenta el que sale antes.
- **Limitación aceptada:** estado en RAM de un proceso; con varios workers
  cada uno aprende por su cuenta (no es grave: solo tarda más en aprender).

### 2.5 Reintentos en una sola capa

- LLM: la única capa de reintento es el fallback entre modelos.
- Supabase: `get_rows` reintenta hasta 2 veces ante error de red o
  502/503/504, con backoff exponencial + jitter. **Las escrituras nunca se
  reintentan**: tras un timeout no se sabe si la fila se escribió.

### 3.1 Presupuesto compartido

- `llm_loop.PresupuestoTurno` (`MAX_LLAMADAS_LLM_POR_TURNO`=15,
  `SEGUNDOS_MAX_POR_TURNO`=90). `orquestador.run()` crea uno por turno y lo
  pasa al sub-agente (`servicio_tecnico_agent.run(presupuesto=...)`) vía
  `contexto["presupuesto"]`. Cuenta cada **intento** a un modelo.
- **Peor caso del turno, antes vs. ahora:** antes hasta ~432 peticiones HTTP
  sin límite de tiempo (8 iteraciones × 9 llamadas × 6 modelos, más 2
  reintentos del SDK en cada una). Ahora como mucho 15 peticiones y
  ~90 s + el timeout de la última petición en curso.
- `reenviar_ultima_tool_si_se_agota=True` en el orquestador: si el
  presupuesto se agota justo después de que el sub-agente respondió (quizá
  confirmando una cita), se reenvía esa respuesta en vez de perderla por
  falta de una última llamada al LLM. Es exactamente lo que el prompt del
  orquestador le pide hacer ("reenvía tal cual").

### 3.2 Razón de parada

- Cada salida del loop registra una de `RAZONES_PARADA` en
  `presupuesto.razones`, en el evento `respuesta_final` (campo
  `razon_parada` y `llamadas_llm_turno`) y en el log (`warning` si no es
  `final`). `orquestador.run()` registra un resumen por turno: razones,
  llamadas y duración.

### 3.3 y 3.4 El loop nunca lanza y el historial siempre es válido

- `run_agent_loop` envuelve todo en `try/except`: una excepción inesperada
  termina en razón `error_interno` con mensaje amable, en vez de propagarse.
  Como el sub-agente es una tool del orquestador, esto garantiza que ambos
  historiales llegan a `AlmacenSesiones.guardar()`.
- Nuevo `llm_loop.sanear_historial`: descarta `assistant(tool_calls)` sin
  todos sus resultados y resultados `tool` huérfanos. Se aplica al entrar al
  loop, al salir por cualquier camino, y en `sesiones.guardar()`.
- En toda salida de respaldo, el texto que vio el cliente se agrega al
  historial como mensaje `assistant` (antes no quedaba registrado y el
  modelo no sabía qué se le había dicho al cliente).
- **Sobre "persistir en `finally`":** no hizo falta un `finally` en
  `web/app.py`. Como el loop ya no lanza excepciones, el turno siempre
  llega a `guardar()`. Solo queda sin guardar si falla la propia lectura o
  escritura en Supabase, y ahí no hay nada útil que guardar.

### Verificación

- Nuevo `tests/test_resiliencia_llm.py` (LLM simulado): `max_retries=0` y
  timeouts; 401 sin probar otros modelos; clave ausente; disyuntor tras 429
  y sin pausa tras 400; todos en pausa; clasificación 402/403/404/timeout;
  corte por presupuesto de llamadas y por deadline con reloj simulado;
  presupuesto compartido que reenvía la respuesta del sub-agente; **aborto
  entre dos resultados de tools, con historial válido y turno siguiente
  funcionando**; `sanear_historial`; reintentos de GET y ningún reintento de
  POST en Supabase.
- Resultado: **5/5 scripts pasan.**

## Fase 5 — Guardia de confirmación por turno ✅ · 2026-09-30 · [TOOLS/INFRA]

Se hizo **antes que la Fase 4** porque cambia la firma de las tools de
escritura, que la validación de la Fase 4 va a envolver. **Ningún prompt
cambió.**

- `tools/citas_tools.py`: el parámetro inyectado `mensaje_cliente_turno`
  pasa a llamarse `id_turno` en `crear_evento`, `actualizar_evento`,
  `eliminar_evento` y `_requiere_confirmacion`. La lógica del guardia no
  cambia: bloquea la primera propuesta y cualquier reintento dentro del
  mismo turno; deja pasar la misma propuesta en un turno posterior.
- `agents/servicio_tecnico_agent.py`: inyecta el `run_id` como `id_turno`
  (si se llama sin `run_id`, genera uno por llamada).
- `agents/orquestador.py`: genera el `run_id` si no viene (caso de
  `main.py`), así la consola también tiene turnos distintos.
- **Por qué:** el guardia comparaba el texto que el orquestador le pasa al
  sub-agente. Ese texto puede ser una paráfrasis (el propio prompt del
  orquestador tiene ejemplos así), y dos turnos distintos con la misma
  paráfrasis se veían como el mismo turno: la cita nunca se podía
  confirmar. Fallaba cerrado (nunca agendaba sin permiso) pero dejaba al
  cliente atascado.
- **Efecto adicional:** si el orquestador invoca al sub-agente dos veces
  en el mismo turno, ambas comparten `run_id`, así que la segunda tampoco
  puede autoconfirmarse. Antes dependía de que el texto coincidiera.
- **Dónde vive la confirmación pendiente:** sigue en RAM
  (`_propuestas_pendientes`), decisión documentada y aceptada: perderla
  obliga a confirmar otra vez, nunca agenda sin permiso. Con varios workers
  habría que moverla a Supabase (ver Fase 7).
- **Sobre el sombreado de `mensaje_cliente`** en
  `_tool_agente_servicio_tecnico`: ya no importa para la seguridad, porque
  el guardia no usa el texto. Se deja igual porque el nombre del parámetro
  tiene que coincidir con el `TOOLS_SCHEMA` del orquestador.

### Verificación

- Tests existentes adaptados al nuevo nombre (misma semántica).
- Nuevo bloque 9 en `tests/test_citas_repository.py`: el mismo texto en dos
  turnos distintos produce `id_turno` distintos; `orquestador.run()` sin
  `run_id` genera uno.
- Resultado: **5/5 scripts pasan.**

## Fase 4 — Validación y reglas de negocio en código ✅ · 2026-09-30 · [TOOLS/INFRA]

**Ni el prompt ni `TOOLS_SCHEMA` cambiaron.** Las reglas nuevas solo hacen
cumplir en código lo que el prompt ya pedía. El modelo lo nota únicamente
cuando se equivoca: recibe un `ERROR: ...` explicando qué corregir.

### 4.1 Validación de argumentos con Pydantic

- Nuevo `tools/validacion_tools.py`: un modelo por tool
  (`ARGUMENTOS_POR_TOOL`) y `con_validacion(nombre, funcion)`, que valida
  antes de ejecutar. `agents/servicio_tecnico_agent.py` envuelve las 6 tools.
- Valida la **forma**: campos obligatorios, fechas ISO 8601, fin posterior a
  inicio, teléfono de 7 a 15 dígitos (se valida pero **nunca se
  reformatea**, como exige el prompt), textos no vacíos, y en
  `Actualizar_evento` que inicio y fin vayan juntos.
- Tolerancias deliberadas: campos extra se ignoran (antes daban
  `TypeError`), `servicio_id` numérico se convierte a texto, espacios de
  sobra se recortan.
- Solo llegan a la función los campos que el modelo envió
  (`exclude_unset`), así "no enviado" sigue significando "no cambiar" en
  `Actualizar_evento`.
- Fechas con y sin offset mezcladas se comparan en la zona del negocio (sin
  esto, Python lanzaba `TypeError`).

### 4.2 Reglas de agenda en código

- `tools/citas_tools.validar_horario_cita`: inicio en el futuro, lunes a
  viernes, entre 7:00 AM y 6:00 PM del mismo día (en hora de Colombia), y
  duración exacta según `duracion_minutos` del catálogo. Si la duración no
  cuadra, el error le dice al modelo la hora de fin correcta.
- `crear_evento` y `actualizar_evento` (este último solo si cambia horario o
  servicio) leen el servicio con `catalog_tools.leer_servicio` (antes
  `_leer_servicio_por_id`, ahora público) y aplican las reglas **antes** de
  la salvaguarda de confirmación: al cliente nunca se le pide confirmar un
  horario inválido. `consultar_eventos` sigue usando
  `resolver_tecnico_para_servicio`.
- `actualizar_evento` valida el horario **efectivo** (lo nuevo combinado con
  lo que ya tenía la cita) y la duración del servicio efectivo: detecta,
  por ejemplo, un cambio a un servicio más largo que ya no cabe en el mismo
  horario (el prompt le pedía al modelo verificarlo a mano).
- Citas antiguas sin `tecnico_id` (anteriores a la migración 003) reciben el
  técnico del servicio cuando se reprograman.
- `_ahora()` es una función aparte para poder fijar la hora en tests.

### 4.3 Coherencia schema ↔ Pydantic ↔ funciones

- Nuevo `tests/test_validacion_tools.py`: verifica que los nombres de tools,
  los campos y los obligatorios coinciden entre `TOOLS_SCHEMA`, los modelos
  Pydantic y `tool_functions`. Se comprobó que falla si se quita un campo
  obligatorio del schema.
- Decisión: `TOOLS_SCHEMA` **no** se genera desde Pydantic. Generarlo
  cambiaría el JSON exacto que ve el modelo (títulos, `anyOf` para
  opcionales) y eso es un cambio de comportamiento **[PROMPT]** que habría
  que volver a probar con conversaciones reales. El test da la misma
  garantía sin tocar lo que ve el modelo.

### Verificación

- `tests/test_validacion_tools.py`: coherencia, argumentos válidos
  normalizados, 7 casos inválidos que no ejecutan la tool, reglas de
  `Actualizar_evento`, reglas de agenda (incluido 6:00 PM exacto como
  válido y fechas en UTC), y `crear_evento` rechazando un domingo antes de
  pedir confirmación.
- `tests/test_citas_repository.py`: se fija la hora actual (las fechas de
  ejemplo quedarían en el pasado) y los mocks pasan de
  `resolver_tecnico_para_servicio` a `leer_servicio` donde corresponde.
- Resultado: **6/6 scripts pasan.**

## Decisión — No limitar las llamadas al sub-agente por turno · 2026-09-30

**Propuesta descartada:** que el código permita una sola llamada a
`Agente_Servicio_Tecnico` por turno del orquestador.

**Contexto:** el orquestador puede invocar al sub-agente más de una vez en
el mismo turno (varios `tool_calls` en una respuesta, o de nuevo en una
vuelta posterior del loop). El texto que le pasa entra al historial del
sub-agente como si fuera un mensaje del cliente.

**Por qué no hace falta:**
- El riesgo real, que una segunda llamada "confirme" una cita sin un "sí"
  del cliente, ya lo bloquea la salvaguarda de confirmación: ambas llamadas
  comparten el `run_id` del turno (Fase 5).
- El costo lo acota el presupuesto compartido del turno (Fase 3).
- Criterio de diseño del proyecto: las reglas se hacen cumplir en las
  herramientas y el código que rodean al agente, no restringiendo al agente
  cuando las herramientas ya garantizan un resultado seguro.

**Revisar si:** los logs de turno (`razones`, `llamadas_llm`) muestran que
el orquestador llama repetidamente al sub-agente y agota el presupuesto.

## Fase 9.4 — Migraciones en formato Supabase CLI ✅ · 2026-09-30 · [TOOLS/INFRA]

Se adelantó al resto de la Fase 9 porque las Fases 10 y 11 agregan
migraciones nuevas.

- `sql/` → `supabase/migrations/`, con el nombre de archivo que exige la
  CLI (`<timestamp>_<nombre>.sql`):
  - `20260927000001_conversaciones.sql` (antes `001`)
  - `20260927000002_disponibilidad_sin_calendar.sql` (antes `002`)
  - `20260928000003_tecnicos.sql` (antes `003`)
- Se actualizaron todas las referencias en código, `.env.example` y README.
- **Alembic no aplica:** el proyecto no usa SQLAlchemy; habla con Supabase
  por PostgREST.
- **Pendiente manual (M3)**, porque las migraciones 001–003 ya se aplicaron a
  mano en el proyecto real y la CLI no lo sabe:
  ```bash
  supabase init            # crea supabase/config.toml (conserva migrations/)
  supabase link --project-ref zvxvtnxifmgubrgqegoh
  supabase migration repair --status applied 20260927000001 20260927000002 20260928000003
  supabase db push         # aplica solo lo nuevo (004, 005)
  ```
  Si la 003 **no** se llegó a aplicar (el README decía que estaba pendiente),
  no la marques como aplicada: sácala del `repair` y `db push` la aplicará.

## Fase 10 — Base de datos ✅ · 2026-09-30 · [TOOLS/INFRA]

### 10.1 `btree_gist` en la migración 003

- `20260928000003_tecnicos.sql` crea la extensión (`if not exists`) en vez de
  depender de que la 002 se haya aplicado en el mismo entorno.

### 10.2 Nueva migración `20260930000004_tecnico_obligatorio_en_citas_confirmadas.sql`

- **Problema:** en un `EXCLUDE ... (tecnico_id WITH =, ...)`, NULL nunca es
  igual a nada. Las citas creadas antes de la 003 tienen `tecnico_id` NULL,
  así que el constraint anti-choque **no las protegía**.
- Completa `tecnico_id` de esas citas con el técnico de su servicio
  (comparando ids como texto para no depender del tipo de `servicio_id`).
- Agrega `CHECK (estado is distinct from 'confirmado' or tecnico_id is not null)`:
  toda cita **confirmada** exige técnico; las canceladas antiguas pueden
  quedar sin él. Se usó un CHECK en vez de `NOT NULL` precisamente por eso.
- Si quedan citas confirmadas sin técnico (servicio sin técnico asignado),
  aborta con un mensaje claro y no deja nada a medias (transacción).

### 10.3 Test de integración contra PostgreSQL real

- Nuevo `tests/test_integracion_postgres.py` + `tests/integracion/esquema_base_prueba.sql`
  (reproduce las dos tablas que se crearon en Supabase antes de este
  proyecto y que ninguna migración crea).
- Usa `TEST_DATABASE_URL` (CI) o levanta un clúster **desechable** con
  `initdb` en una carpeta temporal y el puerto 55432, sin tocar ningún
  Postgres existente. Sin Postgres o sin `psycopg`, se omite (no falla).
- Verifica: el hueco existe antes de la 004; las 4 migraciones se aplican y
  **se pueden volver a aplicar**; el backfill; choque del mismo técnico
  rechazado (23P01); citas contiguas `[inicio, fin)`, de otro técnico y
  canceladas aceptadas; cita confirmada sin técnico rechazada (23514).
- **Verificado contra PostgreSQL 18 local: pasa.**
- Tropiezo resuelto: `pg_ctl start` con la salida capturada se colgaba en
  Windows (el servidor hereda las tuberías). Se usa `DEVNULL` y el log va a
  `log.txt`.
- **Pendiente manual (M3):** aplicar la 004 en Supabase (`supabase db push`).

## Fase 7 — Concurrencia por sesión ✅ · 2026-09-30 · [TOOLS/INFRA]

- **Problema:** dos mensajes casi simultáneos de la misma sesión (normal en
  WhatsApp: "hola" y enseguida "quiero agendar") leían el mismo historial;
  el último en guardar borraba el turno del otro.
- `sesiones.py`: `AlmacenSesiones.turno_exclusivo(session_id)` con un lock
  por sesión (`_LocksPorSesion`), creado bajo demanda y **borrado** cuando
  ningún turno lo usa. El segundo mensaje **espera** (hasta
  `SEGUNDOS_MAX_POR_TURNO` + 30 s) y lee el historial ya actualizado. Si el
  anterior no termina a tiempo: `TurnoEnCurso` → 409.
- `web/app.py`: `/api/chat` envuelve leer → agentes → guardar en ese lock.
  Sesiones distintas siguen en paralelo.
- **Decisión:** se eligió el lock en proceso y no la columna `version`,
  porque el despliegue es de un solo worker (el Dockerfile de la Fase 9 lo
  fija así). Si algún día se escala a varios workers: `version` en
  `conversaciones` + `PATCH ...?version=eq.N` (un advisory lock no sirve por
  PostgREST). Lo mismo aplica a `_propuestas_pendientes`, al limitador y al
  disyuntor, que también viven en RAM.

### Verificación

- Nuevo `tests/test_concurrencia.py` (tabla en memoria, orquestador
  simulado que tarda 0,3 s): dos mensajes simultáneos de la misma sesión
  quedan **ambos** en el historial y se ejecutan en serie; dos sesiones
  distintas en paralelo; los locks se eliminan al terminar; timeout →
  `TurnoEnCurso`.
- `tests/correr_todos.py` distingue `SKIP` de `PASS` (el test de Postgres
  se omite si no hay `psycopg`).
- Resultado: **8/8 scripts** (7 pasan, 1 omitido sin `psycopg`; con
  `psycopg` los 8 pasan).

## Fases 9.1 y 9.2 — Configuración validada, `/health` y `/ready` ✅ · 2026-09-30 · [TOOLS/INFRA]

### 9.1 Configuración validada al arrancar

- Nuevo `config.py`: `validar_configuracion()` revisa las 4 variables
  obligatorias (`OPENROUTER_API_KEY`, `OPENROUTER_MODELS`, `SUPABASE_URL`,
  `SUPABASE_SERVICE_KEY`), detecta los valores de ejemplo de `.env.example`
  como "no configurado", y valida formatos (URL `https://`, lista de
  modelos, `TIMEZONE_OFFSET` ±HH:MM, variables numéricas positivas).
  Reporta **todos** los problemas juntos y el servidor no arranca.
- Lo llaman `web/app.py` y `main.py` justo después de `load_dotenv()`. El
  aviso de "API abierto sin `API_KEY`" se movió aquí.
- Se comprobó que el `.env` real actual pasa la validación (sin imprimir
  valores). `API_KEY` sigue sin definir (acción manual M2).

### 9.2 `/health` y `/ready`

- `web/app.py`: las rutas `/api/*` pasan a un `APIRouter` con la
  dependencia de clave. `/health` y `/ready` quedan **fuera** de la
  autenticación, porque el hosting los consulta sin cabeceras.
- `/health`: el proceso vive; no toca dependencias (un Supabase caído no
  debe provocar reinicios en bucle).
- `/ready`: lee una fila de `conversaciones`; 503 si Supabase no responde,
  sin exponer el detalle del error.

### Verificación

- Nuevo `tests/test_config.py`; `tests/test_seguridad_api.py` suma `/health`
  y `/ready` (público, 200/503, sin filtrar el error) y sigue verificando el
  401 en `/api/*` con el router nuevo.
- Resultado: **8 pasan, 1 omitido** (Postgres sin `psycopg`).

## Fase 8 — Observabilidad ✅ · 2026-09-30 · [TOOLS/INFRA]

Diseño: todo se engancha al bus de eventos que ya existía
(`eventos_agente.publicar_evento`), que ya recibe cada llamada a modelo,
tool y memoria. Agregar una métrica no obliga a tocar el loop ni los agentes.

### 8.1 Tokens y costo

- `PresupuestoTurno.registrar_uso()` suma `usage.prompt_tokens`,
  `usage.completion_tokens` y `usage.cost` (si el proveedor lo informa;
  con modelos `:free` es 0) por turno y por `agente|modelo`.
- El evento `respuesta_modelo` lleva tokens y costo de esa llamada.
- `orquestador.run()` publica un evento **`resumen_turno`** (razones de
  parada, llamadas, duración, tokens, costo, desglose por modelo). Reemplaza
  la línea de log de la Fase 3.
- **No se pide nada extra a OpenRouter** (ningún parámetro nuevo en la
  petición): se lee lo que venga en `usage`, para no arriesgar un 400.

### 8.2 Eventos persistidos como logs JSON, con PII enmascarada

- `tools/eventos_agente.py`: cada evento se escribe como **una línea JSON**
  en stdout (logger `tecnielectronics.eventos`, sin prefijos). Los logs del
  hosting permiten reconstruir un turno de ayer tras reiniciar (filtrando
  por `run_id`). `LOG_EVENTOS_JSON=0` lo desactiva.
- `enmascarar_pii()`: `session_id`, `cliente_nombre` y `cliente_telefono`
  se enmascaran siempre (el teléfono conserva los 3 últimos dígitos para
  correlacionar); en textos libres se enmascaran números de 7+ dígitos,
  correos y los campos `cliente_nombre=...` de los resúmenes. Nunca toca
  `run_id`, fechas ISO, ids hexadecimales ni decimales (hubo que ajustar la
  expresión regular para no confundir `2026-10-05` con un teléfono).
- **Limitación:** un nombre escrito en texto libre ("soy Ana") no se
  detecta. Los textos se truncan a 1000 caracteres en el log.
- El panel "Flujo en Vivo" (autenticado desde la Fase 1) sigue viendo los
  datos sin enmascarar.
- **Decisión:** logs JSON en vez de una tabla en Supabase. Escribir cada
  evento en la base agregaría latencia y un punto de falla a cada turno.
  Si hace falta retención larga, se puede enviar el stdout a un servicio de
  logs sin tocar el código.

### 8.3 Métricas y alertas

- Nuevo `tools/metricas.py`, alimentado por cada evento: turnos, razones
  de parada, llamadas/tokens/costo, fallos de modelo por tipo, errores de
  tools, tasa reciente de respuestas de respaldo. Nunca lanza.
- Alertas como líneas de log `ALERTA ...` (para las alertas por log del
  hosting), cada una silenciada 10 min tras emitirse:
  - credenciales/saldo (401/402) → CRITICAL;
  - más del 30 % de los últimos 20 turnos no terminaron en `final`;
  - más del 50 % de las últimas 30 tools devolvieron `ERROR`.
- `GET /api/metricas` (con clave) devuelve la instantánea.
- **Limitación:** contadores en RAM; se reinician con el proceso. El
  histórico está en los logs JSON.

### Verificación

- Nuevo `tests/test_observabilidad.py`: acumulación de tokens y costo
  (con y sin `cost`); tokens en `respuesta_modelo`; una línea JSON por
  evento sin teléfonos ni nombres, con `run_id` y fechas intactos; el panel
  sigue viendo datos completos; `LOG_EVENTOS_JSON=0`; métricas por turno;
  alerta de tasa de respaldo emitida **una** vez; alerta de credenciales;
  alerta de errores de tools; una métrica rota no interrumpe eventos;
  `/api/metricas`.

## Fase 6 — Compactación del contexto ✅ · 2026-09-30 · [TOOLS/INFRA] + [PROMPT] acotado

**Problema:** el historial de cada agente crecía sin límite: se guardaba
completo y se enviaba completo al modelo en cada llamada (más tokens por
turno cuanto más larga la conversación y, con el tiempo, errores por exceder
la ventana de contexto de los modelos `:free`).

Nuevo módulo `contexto_conversacion.py`, con tres piezas:

### 6.1 Al guardar (`limitar_historial_guardado`, usado por `sesiones.guardar`) · [TOOLS/INFRA]

- Recorta a 300 caracteres (con una marca que dice que se recortó y que se
  puede volver a llamar la herramienta) los resultados de tools **anteriores
  a los últimos 4 turnos** y de más de 1500 caracteres.
- **Nunca recorta el resultado más reciente de cada tool**: el catálogo con
  el `servicio_id` y la duración sigue completo durante todo un agendamiento
  aunque se haya consultado varios turnos atrás.
- Tope de 300 mensajes por agente y sesión, cortando siempre en un mensaje
  del cliente. Se sanea antes y después, así el tope nunca deja una tool
  huérfana.

### 6.2 Al llamar al modelo (`aplicar_ventana`) · [TOOLS/INFRA]

- Cada agente envía solo los últimos 12 turnos del cliente. La ventana empieza
  siempre en un mensaje `user`, así que nunca separa una llamada a tools de
  sus resultados. Lo descartado **no se pierde**: el agente devuelve
  `descartados + ventana_actualizada` y se guarda completo (con el tope de 6.1).
- Aplica a los dos agentes. El orquestador no lleva ficha: solo necesita
  contexto reciente para saber qué tema atiende.

### 6.3 Ficha de contexto (`construir_ficha` + `nota_ficha`) · **[PROMPT]**

- Solo el sub-agente de Servicio Técnico y **solo si la ventana descartó
  mensajes**. En una conversación de menos de 12 turnos el prompt es
  idéntico al de antes (hay un test que lo verifica).
- Se construye **por código** desde los argumentos de `Consultar_eventos`,
  `Crear_evento` y `Actualizar_evento` (`CAMPOS_FICHA`): nombre, teléfono,
  `servicio_id` y descripción. Gana el valor más reciente, por ejemplo un
  teléfono corregido. Ningún LLM resume nada, así se respeta la regla de
  "cero alucinación".
- **No incluye el Event ID**: el prompt prohíbe reutilizarlo sin volver a
  consultar `Consultar_servicio_agendado`, y la nota lo recuerda.
- **Pendiente de validar con conversaciones reales** (más de 12 turnos):
  que el modelo use la ficha y no vuelva a pedir datos ya dados.

### Verificación

- Nuevo `tests/test_contexto.py`: la ventana reconstruye el historial exacto,
  empieza en `user` y no corta tool calls; la ficha usa el valor más reciente
  y no incluye el Event ID; el sub-agente envía 12 turnos, agrega la ficha
  solo en conversaciones largas y devuelve el historial completo; el recorte
  al guardar respeta el último resultado de cada tool y no muta el original;
  el tope deja un historial válido; `guardar()` aplica la compactación.
- Resultado: **10 pasan, 1 omitido.**

## Fase 11 — Preparación multi-agente ✅ · 2026-09-30 · [TOOLS/INFRA]

Fase **agregada** a pedido: dejar el terreno listo para agregar
`Agente_Ventas` de forma limpia. **Lo que ve el modelo no cambió**: se
comprobó que el `TOOLS_SCHEMA` y el `SYSTEM_PROMPT` del orquestador
generados desde el registro son **idénticos** a los anteriores.

**Antes**, agregar un sub-agente obligaba a tocar seis lugares: una columna
nueva en `conversaciones`, un parámetro nuevo en `orquestador.run()`,
`sesiones.py`, `web/app.py`, `main.py` y el `TOOLS_SCHEMA` escrito a mano.

### 11.1 Memoria genérica por agente

- Nueva migración `20260930000005_historiales_por_agente.sql`: columna
  `historiales jsonb` = `{"orquestador": [...], "servicio_tecnico": [...]}`,
  con copia de los datos de las columnas viejas. Las columnas viejas **no se
  borran** (respaldo para volver atrás); eliminarlas en una migración futura.
- `sesiones.py`: `obtener()` devuelve `{clave_agente: [mensajes]}` (y lee las
  columnas viejas si una fila todavía no se migró); `guardar(session_id,
  historiales)` escribe el diccionario completo. El evento
  `memoria_escritura` pasa a `mensajes_por_agente` y el panel (`flujo.js`)
  lo muestra sin nombres fijos.
- **Orden de despliegue:** aplicar la migración 005 **antes** del código.
  `/ready` ahora pide la columna `historiales` y responde 503 si falta.

### 11.2 Registro de sub-agentes en el orquestador

- `agents/orquestador.py`: `SubAgente(tool, clave_historial, descripcion,
  ejecutar)` y la tupla `SUBAGENTES`. De ahí se derivan el `TOOLS_SCHEMA`
  (`construir_tools_schema`), las funciones de cada tool y la clave de
  historial de cada uno.
- `orquestador.run(mensaje, session_id, historiales, run_id)` →
  `(respuesta, historiales)`. `web/app.py` y `main.py` ya no nombran agentes.
- La `NOTA_TEMPORAL_FASE_DESARROLLO` se incluye **solo mientras
  `Agente_Ventas` no esté registrado** (`construir_system_prompt`): al
  registrarlo desaparece sola.

### 11.3 Piezas reutilizables para las tools de Ventas

- `con_validacion(nombre, funcion, modelos=...)` acepta el diccionario de
  modelos Pydantic de cualquier agente.
- `verificar_coherencia_tools(schema, funciones, modelos)` devuelve las
  diferencias entre las tres piezas; el test de Servicio Técnico ya la usa y
  el de Ventas debe usarla igual.
- El resto ya es genérico: `run_agent_loop` (presupuesto, disyuntor,
  razones de parada, saneo), `aplicar_ventana`/`construir_ficha`,
  `supabase_client`, métricas y logs.

### Verificación

- Nuevo `tests/test_orquestador_registro.py`: con un `Agente_Ventas`
  **simulado** registrado, el orquestador lo expone como tool, la nota
  temporal desaparece, y en un turno completo (LLM simulado) Ventas recibe
  **su** historial, el presupuesto y el `run_id` del turno; su historial se
  guarda bajo `"ventas"` sin tocar el de Servicio Técnico.
- `tests/test_sesiones.py` reescrito para el diccionario (incluida la
  lectura desde columnas viejas); `test_concurrencia`, `test_seguridad_api`,
  `test_contexto` adaptados.
- `tests/test_integracion_postgres.py`: la 005 copia correctamente los
  historiales. **Verificado contra PostgreSQL 18: las 5 migraciones pasan.**
- Resultado: **11 pasan, 1 omitido** (Postgres sin `psycopg`).

---

# Cómo agregar `Agente_Ventas`

> Ejecutado en la Fase 12 (ver arriba). Se conserva como guía para agregar
> futuros sub-agentes.

Checklist para cuando llegue el prompt de Ventas. Marca **[PROMPT]** lo que
cambia el comportamiento del modelo y hay que probar con conversaciones
reales.

1. **Crear `agents/ventas_agent.py`** siguiendo el patrón de
   `servicio_tecnico_agent.py`:
   - `ORIGINAL_SYSTEM_PROMPT` con la transcripción fiel del prompt de
     negocio **[PROMPT]** + notas separadas si hacen falta (fecha actual,
     confirmación obligatoria, etc.).
   - `TOOLS_SCHEMA` escrito a mano (inventario, carrito, pago...).
   - `run(mensaje_cliente, session_id, historial=None, run_id=None,
     presupuesto=None) -> (texto, historial)`: usar `aplicar_ventana`,
     `construir_ficha` con sus propios `CAMPOS_FICHA`, y `run_agent_loop` con
     `contexto={"agente": "Ventas", "session_id", "run_id", "presupuesto"}`.
2. **Crear las tools** en `tools/` (ej. `tools/ventas_tools.py`,
   `tools/inventario_repository.py`) sobre `supabase_client`:
   - Devuelven **texto** y nunca lanzan excepciones: errores como
     `"ERROR: ..."`.
   - Reglas de negocio **en la tool**, no solo en el prompt (stock, precios,
     totales calculados por código, no por el modelo).
   - Toda escritura (crear pedido, cobrar) con **confirmación por turno**
     reutilizando el patrón de `_requiere_confirmacion` + `id_turno`, y
     verificando el dueño (`session_id`) de lo que se modifica.
3. **Validación:** modelos Pydantic de sus argumentos en un dict propio
   (ej. `ARGUMENTOS_VENTAS`) y envolver cada tool con
   `con_validacion(nombre, funcion, ARGUMENTOS_VENTAS)`.
4. **Registrar** en `agents/orquestador.py`:
   ```python
   SubAgente(
       tool="Agente_Ventas",            # el nombre que usa el prompt del orquestador
       clave_historial="ventas",
       descripcion="Subagente especializado en venta de equipos: ...",   # [PROMPT]
       ejecutar=lambda **kwargs: ventas_agent.run(**kwargs),
   ),
   ```
   La nota temporal desaparece sola. **[PROMPT]**: el orquestador empieza a
   delegar ventas; probar la clasificación con casos reales (venta clara,
   ambigua, cambio de tema a mitad).
5. **Base de datos:** migraciones nuevas en `supabase/migrations/` (tablas
   de productos, pedidos...), con constraints para lo que no puede fallar
   (stock ≥ 0, un pedido pagado no se modifica). No hace falta tocar
   `conversaciones`.
6. **Tests:** un `tests/test_ventas_*.py` con `verificar_coherencia_tools`,
   las reglas de negocio y la confirmación; si hay constraints nuevos,
   agregarlos a `tests/test_integracion_postgres.py`.
7. **Panel:** el nodo "Ventas" del diagrama (`web/static/flujo.js`) ya
   existe, marcado como deshabilitado. Hay que: quitar `grupo:
   "deshabilitado"` del nodo `ventas` y `discontinua` de la conexión
   `orquestador-ventas`; agregar los nodos de sus tools; y en los tres
   lugares donde hoy se decide el nodo por
   `evento.agente === "Servicio_Tecnico" ? ... : "orquestador"`, sumar el
   caso `"Ventas"`.
8. **Presupuesto:** revisar `MAX_LLAMADAS_LLM_POR_TURNO` si el flujo de
   ventas encadena más tools por turno que el de servicio técnico.


## Fases 9.3 y 9.5 — Docker, CI y calidad ✅ · 2026-09-30 · [TOOLS/INFRA]

### 9.3 Docker

- `Dockerfile` multi-etapa: la etapa `dependencias` instala todo en un
  virtualenv (con `build-essential` solo ahí); la etapa `ejecucion` copia el
  virtualenv sobre `python:3.13-slim`, corre como usuario sin privilegios,
  tiene `HEALTHCHECK` contra `/health` y arranca **un solo worker**
  (el estado en memoria de las Fases 1, 2, 5 y 7 lo exige).
- `.dockerignore`: el `.env`, las credenciales de Google, tests, docs y
  migraciones no entran en la imagen.
- **No verificado:** no hay Docker en esta máquina. La CI construye la imagen
  en cada push.

### 9.5 CI, tests y calidad

- `requirements.txt`: rangos acotados a la versión **mayor** probada (antes
  `openai>=1.40` permitía cualquier versión; la instalada es la 3.x, con una
  API distinta). Se agregó `pydantic` y `anyio`, que se usan directamente.
- `requirements-dev.txt`: pytest, ruff, mypy, types-requests, psycopg.
- `pyproject.toml`: Ruff (`E`, `F`, `W`, `B`; las líneas largas de los
  prompts de negocio quedan exentas), Mypy en modo laxo, pytest.
- `tests/pytest_suite.py`: pytest ejecuta cada script de `tests/` en su
  propio proceso (los scripts aplican parches globales y se contaminarían
  entre sí en un mismo proceso). Los omitidos se reportan como `skipped`.
- `.github/workflows/ci.yml`: Ruff + Mypy; pytest en Python 3.13 y 3.14 con
  un servicio `postgres:17` para el test de integración; build de Docker.
- Se corrigieron 13 líneas largas y 3 errores de tipos (anotaciones; ningún
  cambio de lógica).
- **Decisión:** no se migraron los scripts a tests nativos de pytest (sería
  reescribirlos todos). El puente da descubrimiento, filtros (`-k`) y
  reportes de pytest ya. Migrarlos es una mejora futura.

### Verificación

- `ruff check .` → sin hallazgos. `mypy .` → sin errores.
- `pytest` → **12 pasan** (incluido el de PostgreSQL 18 real).
- Servidor real levantado un momento: `/health` 200, estáticos 200,
  `/api/metricas` 200, `/ready` **503**. Se confirmó la causa: falta la
  columna `historiales` en el Supabase real (acción M3).


## Gestión de secretos ✅ · 2026-09-30 · [TOOLS/INFRA]

### Auditoría (sin mostrar ningún valor)

- El repositorio `github.com/PaLuRoBu0809/tecnielectronics-agentes` es
  **público**. `.env`, `credentials.json` y `token.json` están ignorados por
  git y **nunca** entraron en el historial; ninguna versión de ningún archivo
  contiene patrones de claves (OpenRouter `sk-or-v1-`, JWT de Supabase,
  `client_secret`/`refresh_token` de Google).
- Ningún archivo `.py` usa ya las credenciales de Google.
- La `service_role` de Supabase solo la usa el servidor; el frontend habla
  únicamente con `/api/*`.

### Punto débil corregido (introducido en la Fase 1.2)

- El stream SSE recibía la clave en la URL (`?api_key=`) y uvicorn escribe
  las URLs completas en su log de accesos: la clave habría quedado en los
  logs del hosting. Además `api.js` la guardaba en `localStorage`, legible por
  cualquier script de la página.
- **Ahora:** `POST /api/panel/sesion` (con `X-API-Key`) devuelve una cookie
  `panel_sesion` **HttpOnly + SameSite=Strict + Secure** (salvo en
  localhost), con un token `vence.firma` (HMAC-SHA256, 12 h) que **no
  contiene la clave**. Cambiar `API_KEY` invalida todas las sesiones.
  `DELETE /api/panel/sesion` la cierra. `verificar_api_key` acepta la
  cabecera (n8n, servidor a servidor) o la cookie; **ya no acepta la clave
  en la URL**.
- `web/static/api.js`: pide la clave una vez, abre la sesión y **borra** la
  clave que la versión anterior hubiera dejado en `localStorage`.
  `flujo.js` reconecta el stream al abrir la sesión (tras un 401 el
  navegador no lo reintenta solo).

### Otros cambios

- `.gitignore`: `.env.*` (con excepción de `.env.example`); antes un
  `.env.local` o `.env.produccion` se habría subido al repositorio público.
- Los tests que esperan el API abierto fijan `API_KEY=""` en vez de quitarla:
  `load_dotenv()` no pisa variables ya definidas, y así el `.env` de quien
  corre los tests no los afecta (esto hizo fallar `test_observabilidad` al
  agregar la clave al `.env`).

### Verificación

- `tests/test_seguridad_api.py`: la clave en la URL devuelve 401; abrir
  sesión exige la clave; la cookie lleva HttpOnly/SameSite/Secure/Path y no
  contiene la clave; con la cookie no hace falta la cabecera; cerrar sesión
  vuelve a exigirla; el token caduca, no se puede alterar y se invalida al
  rotar `API_KEY`; en localhost funciona sin HTTPS.
- `ruff` y `mypy` sin hallazgos; `pytest` **12/12**.

### Pendiente manual (ver M1 y la guía de la respuesta)

- ~~Revocar Google y borrar los archivos~~ ✅ hecho (M1).
- OpenRouter: límite de crédito en la clave y claves separadas para
  desarrollo y producción.
- GitHub: verificar que *secret scanning* y *push protection* estén activos.

### Ajuste tras la primera prueba real del panel (2026-09-30)

- **Problema:** al abrir el dashboard, todas las peticiones recibían 401 y
  nunca llegaba `POST /api/panel/sesion`: el cuadro `window.prompt()` no se
  llegó a usar (el navegador puede bloquearlo sin avisar si alguna vez se
  marcó "impedir que esta página cree cuadros de diálogo").
- **Cambio:** `web/static/api.js` muestra un **formulario de acceso dentro
  de la página** (campo de contraseña, mensaje de "Clave incorrecta" o de
  servidor caído, sin cerrarse hasta abrir la sesión).
- **Verificado contra el servidor y el Supabase reales:** sin sesión → 401;
  clave incorrecta → 401; clave del `.env` → 200 con cookie; con la cookie
  cargan citas (11), catálogo y técnicos.

## Naturalidad de la conversación ✅ · 2026-09-30 · **[PROMPT]**

**Problema observado en la primera prueba real:** el Agente de Servicio
Técnico (1) repetía la bienvenida que el orquestador ya había dado, porque
su historial estaba vacío la primera vez que se le delegaba y la plantilla
"Para Saludo y Catálogo" del prompt de negocio empieza con un saludo; y (2)
ante "dime como puedo agendar una cita" repitió **palabra por palabra** su
mensaje anterior. No era un problema de memoria: el historial estaba
completo en Supabase. Lo respondió `nvidia/nemotron-3-super-120b-a12b:free`,
porque los dos primeros modelos de la lista estaban agotados (429), con un
prompt de ~14.000 tokens de entrada.

**Por qué en el prompt y no en código:** es un asunto de redacción del
modelo; recortar el saludo del texto por código sería frágil (el modelo no
siempre usa las mismas palabras). Es una excepción consciente al criterio
de "reglas en las tools".

**Cambio:** `NOTA_NATURALIDAD_CONVERSACION` al final del `SYSTEM_PROMPT` de
`agents/servicio_tecnico_agent.py` (el `ORIGINAL_SYSTEM_PROMPT` no cambia):
no dar la bienvenida ni presentarse; nunca repetir textualmente un mensaje
anterior (responder primero a lo que el cliente preguntó y pedir solo los
datos que falten); explicar el proceso en 2-3 pasos si lo preguntan; no
repetir frases exageradas del cliente. No afloja ninguna regla de negocio.

**Verificación con el modelo real** (misma conversación, sesión de prueba,
sin guardar): turno 1 sin bienvenida; turno 2 distinto al 1, explica los
pasos y reconoce que ya tiene la descripción. Pendiente menor: el modelo
todavía repitió "como un conteo regresivo" (punto 4 cumplido a medias).
Tests: 11 pasan, 1 omitido.

**Recomendación abierta (decisión del dueño, tiene costo):** un modelo de
pago barato primero en `OPENROUTER_MODELS`, o como respaldo final. Con un
prompt de este tamaño, los `:free` rinden peor y los dos primeros de la
lista suelen estar agotados (cada turno pierde 2 intentos).
