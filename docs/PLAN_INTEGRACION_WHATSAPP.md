# Plan: integración directa con WhatsApp Cloud API y contenido multimedia

**Estado:** ⏳ Pendiente. Se ejecuta **después** de que `Agente_Ventas` esté
100 % funcional (ver la guía "Cómo agregar `Agente_Ventas`" en
[`PLAN_DE_MEJORAS.md`](PLAN_DE_MEJORAS.md)).

**Objetivo:** que los clientes hablen con los agentes por WhatsApp usando la
**API oficial de Meta**, conectada directamente a este servidor FastAPI (sin
n8n ni WAHA), y que el sistema entienda **texto, audio, imágenes y video**.

Cada fase indica qué toca, igual que el plan de mejoras:

- **[PROMPT]**: cambia lo que lee el modelo. Hay que volver a probar con
  conversaciones reales.
- **[TOOLS/INFRA]**: solo cambia el código alrededor de los agentes.

---

## 1. Decisiones tomadas

| # | Decisión | Por qué |
|---|---|---|
| D1 | **WAHA → WhatsApp Cloud API oficial** | WAHA automatiza WhatsApp Web, algo que Meta prohíbe: riesgo alto de baneo del número del cliente. La API oficial no tiene ese riesgo si se cumplen las políticas. |
| D2 | **Sin n8n: el webhook de Meta llega directo a FastAPI** | Un solo servidor que mantener y pagar, todo en código con tests. Las piezas difíciles (agentes, seguridad, memoria, concurrencia, métricas) ya existen. |
| D3 | **Los agentes siguen siendo de solo texto** | Los multimedia se convierten a texto **en la entrada**, con intérpretes especializados. La lista de respaldo de modelos no necesita soportar imágenes o audio, y el historial no se infla con archivos. |
| D4 | **Los intérpretes no son agentes** | Son adaptadores sin estado: reciben un archivo y devuelven texto. Las reglas (tamaños, duraciones, formatos) viven en su código, no en prompts. |
| D5 | **Una sola función de atención para todas las entradas** | Consola, dashboard y WhatsApp pasan por el mismo camino (lock por sesión, límite de mensajes, presupuesto, registro). Nada se duplica. |

## 2. Decisiones pendientes (resolver en la Fase 0)

| # | Pregunta | Opciones |
|---|---|---|
| P1 | Proveedor para **audio** | Groq Whisper (rápido, con nivel gratuito) · OpenAI Whisper · Gemini |
| P2 | Proveedor para **imágenes** | Gemini · un modelo de visión en OpenRouter · OpenAI |
| P3 | Qué hacer con **video** | (a) responder que lo describa o mande una foto · (b) transcribir solo su audio · (c) describirlo con Gemini |
| P4 | ¿Se aceptan las **condiciones de datos** del proveedor? | En algunos niveles gratuitos el proveedor puede usar lo enviado para entrenar. Aquí serían fotos y audios de clientes. |
| P5 | **Ventana del buffer** | Sugerido: 4 s desde el último mensaje, con un máximo de 15 s de espera total |
| P6 | **Número** a registrar | Uno dedicado al negocio. No puede estar en uso en la app de WhatsApp mientras esté en la API. |

---

## 3. Principios de diseño (código limpio)

1. **Puertos y adaptadores.** El núcleo (`atender_mensaje`) no sabe que
   existe WhatsApp. Cada canal (consola, dashboard, WhatsApp) es un
   **adaptador** que traduce su formato al del núcleo y de vuelta.
2. **Una responsabilidad por módulo.** Recibir, validar la firma, parsear,
   deduplicar, interpretar, agrupar, atender, formatear y enviar son piezas
   separadas, cada una testeable sola.
3. **Abierto a extensión, cerrado a modificación.** Un tipo de contenido
   nuevo (por ejemplo, documentos PDF) es **un intérprete nuevo más una línea
   en el registro**, sin tocar el webhook ni el núcleo.
4. **Inversión de dependencias.** El flujo depende de la interfaz
   `Interprete` (un `Protocol`), no de Groq ni de Gemini. Cambiar de
   proveedor es cambiar una implementación.
5. **Tipos de dominio inmutables.** `MensajeEntrante`, `ContenidoInterpretado`
   y `MensajeSaliente` son `dataclass(frozen=True)`. Nada viaja como `dict`
   anónimo entre capas.
6. **Lógica pura separada de la entrada/salida.** Parsear el JSON de Meta,
   dar formato para WhatsApp y dividir mensajes largos son funciones puras
   (sin red), probadas con ejemplos reales de payloads de Meta.
7. **Fallar de forma segura.** Nada lanza excepciones hacia Meta: el
   webhook siempre responde 200 rápido. Si un intérprete falla, el cliente
   recibe un mensaje amable y el turno sigue. Es el mismo criterio de
   `run_agent_loop`.
8. **Configuración validada al arrancar** (`config.py`) y **secretos solo en
   variables de entorno**. Tokens y URLs de archivos nunca aparecen en logs
   (ampliar `enmascarar_pii`).
9. **Nombres en español y convenciones del proyecto**: módulos en
   `snake_case`, docstrings que explican el *porqué*, tests como scripts en
   `tests/` corridos por `correr_todos.py` y `pytest_suite.py`.

---

## 4. Arquitectura objetivo

```
                              ┌──────────────── canales/whatsapp ─────────────────┐
Meta ─POST /webhook/whatsapp─►│ webhook → firma → parser → duplicados              │
                              │     │                                              │
                              │     ▼ (segundo plano; a Meta ya se le respondió 200)│
                              │ media (descarga) → interpretes → buffer (ráfaga)   │
                              └─────────────────────────────┬──────────────────────┘
                                                            ▼
consola (main.py) ────────────────────────────► atencion.atender_mensaje(session_id, texto)
dashboard (/api/chat) ─────────────────────────►   lock · límite · orquestador · guardar
                                                            │
                              ┌─────────────────────────────▼──────────────────────┐
                              │ formato (Markdown → WhatsApp) → envio (Graph API)  │──► Meta
                              └────────────────────────────────────────────────────┘
```

### Estructura de archivos propuesta

```
atencion.py                     # NÚCLEO: atender_mensaje(); lo usan las 3 entradas
canales/
  __init__.py
  whatsapp/
    __init__.py
    config.py                   # META_* validadas (se registran en config.py global)
    modelos.py                  # MensajeEntrante, MensajeSaliente (dataclasses inmutables)
    webhook.py                  # APIRouter: GET verificación, POST recepción
    firma.py                    # validar X-Hub-Signature-256 (HMAC-SHA256 con el App Secret)
    parser.py                   # PURO: payload de Meta -> list[MensajeEntrante]
    duplicados.py               # ids de mensajes ya procesados (Meta reintenta)
    media.py                    # cliente Graph API: media_id -> bytes (con límites de tamaño)
    buffer.py                   # agrupa ráfagas por sesión (debounce + espera máxima)
    formato.py                  # PURO: **negrita** -> *negrita*, dividir > 4096 caracteres
    envio.py                    # cliente Graph API: enviar texto
interpretes/
  __init__.py
  base.py                       # Protocol Interprete + ContenidoInterpretado
  registro.py                   # tipo de mensaje -> intérprete (extensión sin tocar el flujo)
  texto.py                      # identidad
  audio.py                      # transcripción (proveedor según P1)
  imagen.py                     # descripción (proveedor según P2)
  video.py                      # política según P3
  no_soportado.py               # stickers, ubicación, documentos, contactos -> respuesta cortés
```

---

## 5. Fases

Cada fase se cierra con sus tests en verde, `ruff` y `mypy` limpios, y su
entrada en el registro de cambios al final de este documento.

### Fase 0: Prerrequisitos (acción manual del dueño)

- Cuenta de **Meta Business** y **verificación del negocio**.
- App en **Meta for Developers** con el producto **WhatsApp**.
- **Token permanente** de un *System User*: el token temporal del panel
  caduca en horas.
- **App Secret** (para validar firmas) y un **Verify Token** inventado (para
  la verificación del webhook).
- Para desarrollo, el **número de prueba** que da Meta, que solo puede
  escribir a una lista corta de números autorizados.
- Resolver P1–P6 y crear las claves de los proveedores elegidos.

**Hecho cuando:** existen todas las credenciales y se pueden mandar mensajes
de prueba desde el panel de Meta.

### Fase 1: Núcleo común de atención · [TOOLS/INFRA]

- Extraer de `web/app.py` (`chat()`) la secuencia **leer historial → lock →
  orquestador → guardar** a `atencion.py`:
  `atender_mensaje(session_id, texto) -> Respuesta`.
- `/api/chat` y `main.py` pasan a usarla. El límite de mensajes y el lock
  quedan en el núcleo, no en cada canal.
- Resultado tipado: `Respuesta(texto, razon_parada)` más errores de dominio
  (`LimiteSuperado`, `TurnoEnCurso`), que cada canal traduce a su manera (el
  dashboard a HTTP 429/409; WhatsApp a un mensaje amable).

**Hecho cuando:** las dos entradas actuales usan `atender_mensaje` y **todos
los tests existentes pasan sin cambios de comportamiento**.

### Fase 2: Dominio y parser · [TOOLS/INFRA]

- `modelos.py`: `MensajeEntrante(id_mensaje, telefono, tipo, texto,
  media_id, mime_type, marca_de_tiempo)`.
- `parser.py`, función pura: payload del webhook → `list[MensajeEntrante]`.
  Ignora los avisos de estado (`statuses`: enviado, entregado, leído) y
  las reacciones.
- **Ejemplos reales** de payloads de Meta en `tests/whatsapp/ejemplos/`:
  texto, audio (nota de voz), imagen con y sin texto, video, sticker,
  ubicación, estados, un lote con varios mensajes.

**Hecho cuando:** el parser convierte correctamente todos los ejemplos y
nunca lanza excepciones con payloads inesperados (devuelve lista vacía y
lo registra).

### Fase 3: Webhook seguro · [TOOLS/INFRA]

- `GET /webhook/whatsapp`: verificación de Meta (`hub.mode`,
  `hub.verify_token` → devolver `hub.challenge`).
- `POST /webhook/whatsapp`:
  1. Validar `X-Hub-Signature-256` sobre el **cuerpo crudo** (HMAC-SHA256
     con el App Secret, comparación en tiempo constante). Firma inválida → 401.
  2. Responder **200 de inmediato** y procesar en segundo plano.
  3. **Deduplicar** por `id_mensaje` (Meta reintenta si no recibe 200 a
     tiempo).
- El webhook queda **fuera** de `X-API-Key`: su autenticación es la firma de
  Meta.
- Duplicados: en memoria con vencimiento, igual que el resto del estado de
  una sola instancia (Fase 7 del plan de mejoras). Si en el futuro hay
  varias instancias, pasa a una tabla de Supabase.

**Hecho cuando:** los tests prueban el challenge, rechazan firmas inválidas y
cuerpos alterados, responden 200 sin esperar al agente e ignoran un mensaje
repetido.

### Fase 4: Envío y formato · [TOOLS/INFRA]

- `formato.py`, función pura: convierte el Markdown del agente al de
  WhatsApp (`**negrita**` → `*negrita*`, encabezados a negrita, listas
  intactas) y divide respuestas de más de 4096 caracteres por párrafos.
- `envio.py`: `POST /{phone_number_id}/messages` con timeout y reintento
  solo ante errores de red (**no** reintentar un envío ambiguo: el cliente
  recibiría el mensaje dos veces). Errores de Meta clasificados, con
  alerta en las métricas si el token es inválido.

**Hecho cuando:** hay tests de formato con respuestas reales del agente y
tests de envío con la API de Meta simulada.

### Fase 5: Buffer de ráfagas · [TOOLS/INFRA]

- `buffer.py`: por cada teléfono, espera `P5` segundos desde el último
  mensaje (con una espera máxima total), junta los textos en orden y llama
  **una vez** a `atender_mensaje`.
- Si llega un mensaje mientras el agente responde, se acumula y se atiende
  en el turno siguiente (el lock por sesión ya garantiza el orden).
- El límite de mensajes por sesión cuenta **ráfagas atendidas**, no
  mensajes sueltos.

**Hecho cuando:** los tests, con reloj simulado, prueban que 3 mensajes en
2 s producen 1 turno, que la espera máxima se respeta y que dos clientes no
se mezclan.

**🏁 Hito: WhatsApp con texto funciona de punta a punta** (probar con el
número de prueba de Meta).

### Fase 6: Intérpretes multimedia · [TOOLS/INFRA]

- `interpretes/base.py`: `Protocol Interprete` con
  `interpretar(contenido: bytes, mime_type: str) -> ContenidoInterpretado`,
  donde el resultado lleva el texto, si tuvo éxito y un motivo.
- `media.py`: descarga desde Meta (`media_id` → URL → bytes) con **límite de
  tamaño** antes de descargar todo.
- Implementaciones según P1–P3, cada una con timeout y **límites propios en
  código** (duración máxima del audio, tamaño de la imagen). Si falla, el
  cliente recibe un mensaje amable ("no pude escuchar tu audio, ¿me lo
  escribes?") y el turno sigue.
- Salida con marcador explícito, que entra al buffer como texto:
  `[Audio del cliente, transcrito]: ...` · `[Imagen enviada por el cliente]: ...`
  (más su texto al pie, si lo trae).
- `no_soportado.py`: stickers, ubicación, documentos y contactos reciben una
  respuesta cortés fija, **sin gastar llamadas al LLM**.
- Costo y latencia de cada intérprete se publican como eventos (se suman a
  las métricas de la Fase 8 del plan de mejoras).
- **Formato de las notas de voz:** WhatsApp las envía en `audio/ogg`
  (Opus). Verificar que el proveedor elegido lo acepta; si no, convertir
  antes de enviarlo.

**Hecho cuando:** con archivos de ejemplo (y el proveedor simulado en los
tests), cada tipo produce su texto, los errores producen un mensaje amable y
el registro permite agregar un tipo nuevo sin tocar el flujo.

### Fase 7: Que los agentes entiendan los marcadores · **[PROMPT]**

- Nota corta y separada (mismo patrón que las demás) para el orquestador y
  los sub-agentes: el texto entre corchetes es una transcripción o
  descripción automática, puede tener errores, y ante una duda importante se
  confirma con el cliente.
- **Validar con conversaciones reales:** audio con los datos para agendar,
  foto del equipo dañado, audio mal transcrito.

**Hecho cuando:** las pruebas reales muestran que el agente usa bien el
contenido y confirma lo dudoso.

### Fase 8: Seguridad, privacidad y observabilidad · [TOOLS/INFRA]

- Ampliar `enmascarar_pii`: URLs de archivos de Meta, tokens y `wa_id`.
- Los archivos **no se guardan**: se procesan en memoria y se descartan.
  Solo queda el texto interpretado en el historial.
- Eventos nuevos en el bus: `whatsapp_entrante`, `interpretacion`,
  `whatsapp_enviado`, con sus fallos. El panel "Flujo en vivo" puede
  mostrarlos como un nodo "WhatsApp".
- Alertas: token de Meta inválido, fallos de envío repetidos, tasa de
  errores de los intérpretes.
- **Lista de números permitidos** (variable de entorno) para el periodo de
  pruebas: solo esos números reciben respuesta.

**Hecho cuando:** los logs JSON de una conversación con audio e imagen no
contienen teléfonos, URLs de archivos ni tokens, y las alertas se disparan
en sus tests.

### Fase 9: Despliegue y cambio desde WAHA · [TOOLS/INFRA]

- En Render: variables `META_*` y las de los proveedores; URL del webhook
  configurada en Meta (`https://.../webhook/whatsapp`).
- **Servicio siempre encendido** (plan pago más bajo): Meta necesita que el
  webhook responda rápido. El plan gratuito se duerme.
- Orden del cambio:
  1. Probar todo con el **número de prueba** de Meta.
  2. Registrar el número real del negocio en la API. Deja de funcionar en
     la app de WhatsApp y en WAHA.
  3. Apagar WAHA y el workflow de n8n.
  4. Primeras 48 h vigilando métricas y alertas.
- **Plan de reversa:** documentar cómo devolver el número a la app de
  WhatsApp si hiciera falta.

**Hecho cuando:** clientes reales conversan por WhatsApp con texto, audio e
imágenes, y WAHA está apagado.

---

## 6. Políticas de Meta que el sistema debe respetar

- **Ventana de 24 h:** respuestas libres solo dentro de las 24 h desde el
  último mensaje del cliente. Fuera de ella (por ejemplo, un recordatorio de
  cita) solo con **plantillas aprobadas**. Eso sería un plan aparte.
- **Nada de mensajes masivos** ni escribirle primero a quien no lo pidió.
- **Calidad del número:** si muchos usuarios bloquean o reportan el número,
  Meta lo restringe. Respuestas útiles y breves ayudan.
- **Precios:** consultar la tabla vigente de Meta antes de salir a
  producción. Las respuestas dentro de la ventana de 24 h suelen ser lo más
  barato.

## 7. Riesgos y mitigaciones

| Riesgo | Mitigación |
|---|---|
| El servidor gratuito se duerme y Meta reintenta | Servicio siempre encendido en producción (Fase 9) |
| Mensajes duplicados por reintentos de Meta | Deduplicación por `id_mensaje` (Fase 3) |
| Transcripción o descripción equivocada | Marcadores explícitos y nota [PROMPT] para confirmar lo dudoso (Fases 6–7) |
| Archivos enormes o maliciosos | Límite de tamaño antes de descargar, tipos permitidos, sin guardar a disco (Fases 6 y 8) |
| El proveedor de audio o imagen falla o se agota | Timeout, mensaje amable y alerta; el turno sigue (Fase 6) |
| Filtración de datos de clientes a proveedores | Decisión P4 explícita; condiciones de datos revisadas antes de producción |
| Token de Meta vencido o revocado | Token permanente de System User; alerta CRITICAL (Fase 4) |
| Enviar dos veces la misma respuesta | No reintentar envíos ambiguos (Fase 4) |

## 8. Relación con el Agente de Ventas

- Ventas se agrega **antes**, con la guía de `PLAN_DE_MEJORAS.md`. El canal de
  WhatsApp no depende de qué sub-agentes existan: habla con
  `atender_mensaje`, y el orquestador decide a quién delegar.
- Todo lo que Ventas responda pasa por el mismo `formato.py`, así que sus
  mensajes también se verán bien en WhatsApp.
- Si Ventas necesita mandar **imágenes de productos** (salida, no entrada),
  es una extensión de `envio.py` (mensajes de tipo imagen) que se planifica
  cuando Ventas defina ese requisito.

## 9. Definición de terminado (todo el plan)

- [ ] Texto, audio, imagen y video (según P3) funcionan por WhatsApp con
      clientes reales.
- [ ] Las tres entradas (consola, dashboard, WhatsApp) usan `atender_mensaje`.
- [ ] Firma de Meta validada; duplicados ignorados; nada lanza excepciones
      hacia Meta.
- [ ] Ráfagas agrupadas en un solo turno.
- [ ] Sin datos personales, tokens ni URLs de archivos en los logs.
- [ ] `ruff`, `mypy` y todos los tests en verde; ejemplos reales de Meta en
      los tests.
- [ ] WAHA y el workflow de n8n apagados.
- [ ] Registro de cambios completo al final de este documento.

---

# Registro de cambios

Cada fase agrega aquí qué se cambió, en qué archivos, por qué y cómo se
verificó (mismo formato que `PLAN_DE_MEJORAS.md`).

_(Todavía sin cambios: el plan se ejecuta después de Agente_Ventas.)_
