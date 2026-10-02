/**
 * flujo.js — Monitor de Flujo en Vivo, estilo canvas + historial de
 * ejecuciones de n8n.
 *
 * Tiene DOS modos:
 *   - "En vivo" (por defecto): cada evento real del agente (GET
 *     /api/flujo/stream, ver web/app.py + tools/eventos_agente.py) hace
 *     pulsar de forma PASAJERA el nodo/conexión correspondiente del
 *     diagrama, y se agrega al log de abajo sin filtrar.
 *   - "Viendo una corrida": al hacer click en un ítem del sidebar (un
 *     mensaje real del cliente, GET /api/flujo/corridas), el diagrama se
 *     queda FIJO mostrando qué nodos SÍ participaron en ESE turno puntual
 *     (coloreados) y cuáles no (apagados) — igual que el historial de
 *     ejecuciones de n8n. Click en un nodo muestra su detalle exacto
 *     ("participó" / "no participó en esta corrida").
 *
 * Organización (capas):
 *   1. Acceso a datos    — conectarStream(), obtenerCorridas(), obtenerCorrida().
 *   2. Dominio            — layout del grafo, resolverActivaciones(), nodoDeEvento().
 *   3. Renderizado grafo  — dibujarNodos/Conexiones, pulsar(), aplicarEstadoCorrida().
 *   4. Renderizado log/sidebar/detalle — crearTarjetaEvento, renderizarCorridasLista.
 *   5. Interacción        — click en nodo/corrida, toggle "en vivo".
 */

import { alIniciarSesion, apiFetch } from "./api.js";

// Tras un 401 el navegador cierra el EventSource y no lo reintenta: cuando se
// abre la sesión del panel (desde cualquier pestaña del dashboard), se reconecta.
alIniciarSesion(() => conectarStream());

// ---------------------------------------------------------------------
// 2. DOMINIO — el grafo fijo que representa la arquitectura real.
// ---------------------------------------------------------------------

const ANCHO_NODO = 168;
const ALTO_NODO = 46;

// Columna de tools de cada sub-agente: una franja por agente (Servicio
// Técnico arriba, Ventas abajo), separadas PASO_TOOL px en vertical.
const X_TOOLS = 980;
const PASO_TOOL = 54;

/** Nodos de las tools de un agente, en columna a partir de `yInicial`. */
function columnaDeTools(tools, yInicial) {
  return tools.map(([id, etiqueta], i) => ({ id, etiqueta, x: X_TOOLS, y: yInicial + i * PASO_TOOL, grupo: "tool" }));
}

// Nombre real de cada tool (el del evento) -> id de nodo, por agente.
const TOOLS_POR_AGENTE = {
  servicio_tecnico: [
    ["tool_catalogo", "Servicio_tecnico"],
    ["tool_consultar_eventos", "Consultar_eventos"],
    ["tool_crear_evento", "Crear_evento"],
    ["tool_actualizar_evento", "Actualizar_evento"],
    ["tool_eliminar_evento", "Eliminar_evento"],
    ["tool_consultar_cita", "Consultar_servicio_agendado"],
  ],
  ventas: [
    ["tool_categorias", "Categorias_inventario"],
    ["tool_inventario", "Inventario"],
    ["tool_anadir", "Anadir_elemento"],
    ["tool_consultar_carrito", "Consultar_carrito"],
    ["tool_modificar_elemento", "Modificar_elemento"],
    ["tool_eliminar_elemento", "Eliminar_elemento"],
    ["tool_crear_orden", "Crear_orden"],
    ["tool_consultar_orden", "Consultar_orden"],
    ["tool_modificar_orden", "Modificar_orden"],
    ["tool_cancelar_orden", "Cancelar_orden"],
  ],
};

// Tools que además hablan con MercadoPago (crear el link / conciliar el pago).
const TOOLS_CON_MERCADOPAGO = ["tool_crear_orden", "tool_consultar_orden"];

const LAYOUT_NODOS = [
  { id: "cliente", etiqueta: "💬 Cliente (WhatsApp/Chat)", x: 10, y: 500, grupo: "entrada" },
  { id: "orquestador", etiqueta: "Orquestador", x: 230, y: 500, grupo: "agente" },
  { id: "modelo", etiqueta: "🧠 Modelo (OpenRouter)", x: 470, y: 40, grupo: "modelo" },
  { id: "memoria", etiqueta: "🗄️ Memoria (conversaciones)", x: 470, y: 960, grupo: "memoria" },
  { id: "servicio_tecnico", etiqueta: "Servicio Técnico", x: 710, y: 175, grupo: "agente" },
  { id: "ventas", etiqueta: "🛒 Ventas", x: 710, y: 760, grupo: "agente" },
  ...columnaDeTools(TOOLS_POR_AGENTE.servicio_tecnico, 40),
  ...columnaDeTools(TOOLS_POR_AGENTE.ventas, 520),
  { id: "supabase", etiqueta: "🗄️ Supabase", x: 1250, y: 500, grupo: "datos" },
  { id: "mercadopago", etiqueta: "💳 MercadoPago", x: 1250, y: 880, grupo: "datos" },
];

const CANVAS_ANCHO = 1460;
const CANVAS_ALTO = 1040;

const AGENTES_CON_NODO = { Servicio_Tecnico: "servicio_tecnico", Ventas: "ventas" };

/** Nodo del agente que publicó un evento (el orquestador por defecto). */
function nodoDeAgente(agente) {
  return AGENTES_CON_NODO[agente] || "orquestador";
}

const NOMBRE_TOOL_A_NODO = {
  Agente_Servicio_Tecnico: "servicio_tecnico",
  Agente_Ventas: "ventas",
};
// Qué agente usa cada tool: define la conexión que se ilumina.
const AGENTE_DE_TOOL = { servicio_tecnico: "orquestador", ventas: "orquestador" };
for (const [agente, tools] of Object.entries(TOOLS_POR_AGENTE)) {
  for (const [idNodo, nombre] of tools) {
    NOMBRE_TOOL_A_NODO[nombre] = idNodo;
    AGENTE_DE_TOOL[idNodo] = agente;
  }
}

// Todas las tools de los sub-agentes leen o escriben en Supabase.
const NOMBRES_TOOLS_CON_SUPABASE = Object.values(TOOLS_POR_AGENTE).flat().map(([idNodo]) => idNodo);

const LAYOUT_CONEXIONES = [
  { id: "cliente-orquestador", desde: "cliente", hasta: "orquestador" },
  // Modelo/Memoria son "sub-nodos de utilidad" del agente (mismo lenguaje
  // visual que usa n8n para Chat Model/Memory colgando de un nodo AI Agent):
  // línea punteada, no la línea sólida del flujo principal de la conversación.
  ...["orquestador", "servicio_tecnico", "ventas"].flatMap((agente) => [
    { id: `${agente}-modelo`, desde: agente, hasta: "modelo", utilidad: true },
    { id: `${agente}-memoria`, desde: agente, hasta: "memoria", utilidad: true },
  ]),
  { id: "orquestador-servicio_tecnico", desde: "orquestador", hasta: "servicio_tecnico" },
  { id: "orquestador-ventas", desde: "orquestador", hasta: "ventas" },
  ...Object.entries(TOOLS_POR_AGENTE).flatMap(([agente, tools]) =>
    tools.map(([idNodo]) => ({ id: `${agente}-${idNodo}`, desde: agente, hasta: idNodo }))),
  ...NOMBRES_TOOLS_CON_SUPABASE.map((idTool) => ({ id: `${idTool}-supabase`, desde: idTool, hasta: "supabase" })),
  ...TOOLS_CON_MERCADOPAGO.map((idTool) => ({ id: `${idTool}-mercadopago`, desde: idTool, hasta: "mercadopago" })),
  { id: "memoria-supabase", desde: "memoria", hasta: "supabase", utilidad: true },
];

/** A qué nodo del diagrama corresponde un evento — usado tanto para el
 * pulso en vivo como para el estado fijo de una corrida. */
function nodoDeEvento(evento) {
  if (evento.tipo === "memoria_lectura" || evento.tipo === "memoria_escritura") return "memoria";
  if (evento.tipo === "tool_llamada" || evento.tipo === "tool_resultado") {
    return NOMBRE_TOOL_A_NODO[evento.nombre] || null;
  }
  if (evento.tipo === "llamada_modelo" || evento.tipo === "respuesta_modelo" || evento.tipo === "modelo_fallo") {
    return "modelo";
  }
  return nodoDeAgente(evento.agente);
}

/** Tono de un evento: "error" si algo falló, "exito" en cualquier otro caso. */
function tonoDeEvento(evento) {
  if (evento.tipo === "modelo_fallo") return "error";
  if (evento.tipo === "tool_resultado" && /^ERROR/.test(evento.resultado || "")) return "error";
  return "exito";
}

/** A partir de UN evento en vivo, decide qué nodos/conexiones deben pulsar
 * (de forma pasajera) y con qué tono. */
function resolverActivaciones(evento) {
  const agenteNodo = nodoDeAgente(evento.agente);
  const tono = tonoDeEvento(evento);

  switch (evento.tipo) {
    case "llamada_modelo":
      return { nodos: [agenteNodo, "modelo"], conexiones: [`${agenteNodo}-modelo`], tono: "activo" };
    case "respuesta_modelo":
      return { nodos: [agenteNodo, "modelo"], conexiones: [`${agenteNodo}-modelo`], tono };
    case "modelo_fallo":
      return { nodos: ["modelo"], conexiones: [`${agenteNodo}-modelo`], tono: "error" };
    case "tool_llamada":
    case "tool_resultado": {
      const idTool = NOMBRE_TOOL_A_NODO[evento.nombre];
      if (!idTool) return { nodos: [], conexiones: [], tono };
      const conexiones = [`${AGENTE_DE_TOOL[idTool]}-${idTool}`];
      const nodos = [idTool];
      if (NOMBRES_TOOLS_CON_SUPABASE.includes(idTool)) {
        conexiones.push(`${idTool}-supabase`);
        nodos.push("supabase");
      }
      if (TOOLS_CON_MERCADOPAGO.includes(idTool)) {
        conexiones.push(`${idTool}-mercadopago`);
        nodos.push("mercadopago");
      }
      return { nodos, conexiones, tono: evento.tipo === "tool_resultado" ? tono : "activo" };
    }
    case "memoria_lectura":
    case "memoria_escritura":
      return {
        nodos: ["memoria", "supabase"],
        conexiones: ["orquestador-memoria", "servicio_tecnico-memoria", "ventas-memoria", "memoria-supabase"],
        tono: "activo",
      };
    case "respuesta_final":
      // El desenlace REAL de este agente para el cliente: si fallback=true,
      // el modelo falló tantas veces que el agente terminó respondiendo con
      // un mensaje de respaldo genérico (nunca uno inventado) — eso debe
      // reflejarse en el propio nodo del AGENTE, no solo en "Modelo".
      return {
        nodos: [agenteNodo],
        conexiones: [agenteNodo === "orquestador" ? "cliente-orquestador" : `orquestador-${agenteNodo}`],
        tono: evento.fallback ? "error" : "exito",
      };
    default:
      return { nodos: [], conexiones: [], tono: "activo" };
  }
}

// ---------------------------------------------------------------------
// 1. ACCESO A DATOS
// ---------------------------------------------------------------------

async function obtenerCorridas() {
  const resp = await apiFetch("/api/flujo/corridas");
  if (!resp.ok) throw new Error(`No se pudieron cargar las corridas (HTTP ${resp.status})`);
  return resp.json();
}

async function obtenerCorrida(runId) {
  const resp = await apiFetch(`/api/flujo/corridas/${runId}`);
  if (!resp.ok) throw new Error(`No se pudo cargar la corrida ${runId} (HTTP ${resp.status})`);
  return resp.json();
}

// ---------------------------------------------------------------------
// 3. RENDERIZADO DEL GRAFO
// ---------------------------------------------------------------------

const SVG_NS = "http://www.w3.org/2000/svg";

function centroDerecho(nodo) {
  return { x: nodo.x + ANCHO_NODO, y: nodo.y + ALTO_NODO / 2 };
}
function centroIzquierdo(nodo) {
  return { x: nodo.x, y: nodo.y + ALTO_NODO / 2 };
}
function trazarCurva(origen, destino) {
  const a = centroDerecho(origen);
  const b = centroIzquierdo(destino);
  const controlX = (a.x + b.x) / 2;
  return `M ${a.x} ${a.y} C ${controlX} ${a.y}, ${controlX} ${b.y}, ${b.x} ${b.y}`;
}

const elCanvas = document.getElementById("flujo-canvas");
const elSvg = document.getElementById("flujo-svg");
const nodosPorId = Object.fromEntries(LAYOUT_NODOS.map((n) => [n.id, n]));
const elementosNodo = {};
const elementosConexion = {};

function dibujarNodos() {
  for (const nodo of LAYOUT_NODOS) {
    const div = document.createElement("div");
    div.className = `flujo-nodo flujo-nodo-${nodo.grupo}`;
    div.style.left = `${nodo.x}px`;
    div.style.top = `${nodo.y}px`;
    div.style.width = `${ANCHO_NODO}px`;
    div.style.height = `${ALTO_NODO}px`;
    div.dataset.nodo = nodo.id;
    div.addEventListener("click", () => onClickNodo(nodo.id));

    const etiqueta = document.createElement("span");
    etiqueta.className = "flujo-nodo-etiqueta";
    etiqueta.textContent = nodo.etiqueta;
    const badge = document.createElement("span");
    badge.className = "flujo-nodo-badge";
    badge.hidden = true;
    div.append(etiqueta, badge);

    elCanvas.appendChild(div);
    elementosNodo[nodo.id] = div;
  }
}

function dibujarConexiones() {
  // El tamaño del lienzo sale de aquí (una sola fuente), no del CSS.
  elCanvas.style.width = `${CANVAS_ANCHO}px`;
  elCanvas.style.height = `${CANVAS_ALTO}px`;
  elSvg.setAttribute("viewBox", `0 0 ${CANVAS_ANCHO} ${CANVAS_ALTO}`);
  for (const conexion of LAYOUT_CONEXIONES) {
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", trazarCurva(nodosPorId[conexion.desde], nodosPorId[conexion.hasta]));
    const clases = ["flujo-conexion"];
    if (conexion.discontinua) clases.push("flujo-conexion-discontinua");
    if (conexion.utilidad) clases.push("flujo-conexion-utilidad");
    path.setAttribute("class", clases.join(" "));
    elSvg.appendChild(path);
    elementosConexion[conexion.id] = path;
  }
}

function pulsar(elemento, clase, duracionMs) {
  elemento.classList.remove(clase);
  void elemento.offsetWidth; // fuerza reflow para poder repetir la animación
  elemento.classList.add(clase);
  setTimeout(() => elemento.classList.remove(clase), duracionMs);
}

/** Modo "en vivo": ilumina de forma PASAJERA lo que activó este evento. Solo
 * corre cuando no se está viendo una corrida fija del sidebar. */
function pulsarEnVivo(evento) {
  const { nodos, conexiones, tono } = resolverActivaciones(evento);
  const claseNodo = tono === "error" ? "flujo-nodo-pulso-error" : "flujo-nodo-pulso";
  const claseConexion = tono === "error" ? "flujo-conexion-pulso-error" : "flujo-conexion-pulso";
  for (const idNodo of nodos) if (elementosNodo[idNodo]) pulsar(elementosNodo[idNodo], claseNodo, 1200);
  for (const idConexion of conexiones) if (elementosConexion[idConexion]) pulsar(elementosConexion[idConexion], claseConexion, 1200);
}

const CLASES_ESTADO_CORRIDA = [
  "flujo-nodo-inactivo-corrida",
  "flujo-nodo-visitado-exito",
  "flujo-nodo-visitado-error",
];
const CLASES_CONEXION_CORRIDA = [
  "flujo-conexion-inactiva-corrida",
  "flujo-conexion-visitada-exito",
  "flujo-conexion-visitada-error",
];

/** Deja el diagrama en su apariencia neutra (modo "en vivo"), quitando
 * cualquier estado fijo que hubiera dejado una corrida seleccionada. */
function limpiarEstadoCorridaEnGrafo() {
  for (const nodo of Object.values(elementosNodo)) nodo.classList.remove(...CLASES_ESTADO_CORRIDA);
  for (const conexion of Object.values(elementosConexion)) conexion.classList.remove(...CLASES_CONEXION_CORRIDA);
}

/** Modo "viendo corrida": apaga el diagrama completo y luego enciende, de
 * forma FIJA (no pasajera), solo lo que de verdad participó en esa corrida.
 *
 * Semántica IMPORTANTE (bug real reportado por el usuario viendo una
 * corrida real): el color final de cada nodo/conexión es el del ÚLTIMO
 * evento cronológico que lo tocó, NO "si alguna vez hubo un error queda
 * rojo para siempre". Un modelo puede fallar 5 veces por rate-limit y
 * responder bien a la sexta — eso debe verse VERDE al final (fue lo que
 * realmente pasó), no rojo. Los reintentos fallidos no se pierden: se
 * cuentan aparte y se muestran como una insignia "⚠N" sobre el nodo del
 * agente, para que sí se note que hubo turbulencia en el camino.
 *
 * El nodo del propio AGENTE (Orquestador/Servicio Técnico) siempre queda
 * determinado por su evento `respuesta_final` (el último evento de su
 * turno) — verde si respondió con contenido real del modelo, rojo si tuvo
 * que recurrir al mensaje de respaldo genérico (`fallback: true`, ver
 * llm_loop.py). Antes ese camino de fallback no publicaba ningún evento y
 * el diagrama no reflejaba que el agente sí le contestó algo al cliente.
 */
function aplicarEstadoCorrida(corrida) {
  limpiarEstadoCorridaEnGrafo();
  limpiarBadgesReintento();
  for (const nodo of Object.values(elementosNodo)) nodo.classList.add("flujo-nodo-inactivo-corrida");
  for (const conexion of Object.values(elementosConexion)) conexion.classList.add("flujo-conexion-inactiva-corrida");

  const tonoPorNodo = {};
  const tonoPorConexion = {};
  const reintentosPorNodo = {};

  for (const evento of corrida.eventos) {
    const { nodos, conexiones, tono } = resolverActivaciones(evento);
    const tonoFinal = tono === "error" ? "error" : "exito";
    for (const idNodo of nodos) tonoPorNodo[idNodo] = tonoFinal; // el último evento manda, no el peor
    for (const idConexion of conexiones) tonoPorConexion[idConexion] = tonoFinal;

    if (evento.tipo === "modelo_fallo") {
      const agenteNodo = nodoDeAgente(evento.agente);
      reintentosPorNodo[agenteNodo] = (reintentosPorNodo[agenteNodo] || 0) + 1;
    }
  }
  for (const [idNodo, tono] of Object.entries(tonoPorNodo)) {
    const nodoEl = elementosNodo[idNodo];
    if (!nodoEl) continue;
    nodoEl.classList.remove("flujo-nodo-inactivo-corrida");
    nodoEl.classList.add(tono === "error" ? "flujo-nodo-visitado-error" : "flujo-nodo-visitado-exito");
  }
  for (const [idConexion, tono] of Object.entries(tonoPorConexion)) {
    const conexionEl = elementosConexion[idConexion];
    if (!conexionEl) continue;
    conexionEl.classList.remove("flujo-conexion-inactiva-corrida");
    conexionEl.classList.add(tono === "error" ? "flujo-conexion-visitada-error" : "flujo-conexion-visitada-exito");
  }
  for (const [idNodo, cantidad] of Object.entries(reintentosPorNodo)) {
    actualizarBadgeReintento(idNodo, cantidad);
  }
}

function actualizarBadgeReintento(idNodo, cantidad) {
  const badge = elementosNodo[idNodo]?.querySelector(".flujo-nodo-badge");
  if (!badge) return;
  badge.textContent = `⚠${cantidad}`;
  badge.hidden = false;
  badge.title = `${cantidad} intento(s) de modelo fallaron por saturación/rate-limit antes de la respuesta final`;
}

function limpiarBadgesReintento() {
  for (const nodo of Object.values(elementosNodo)) {
    const badge = nodo.querySelector(".flujo-nodo-badge");
    if (badge) badge.hidden = true;
  }
}

// ---------------------------------------------------------------------
// 4. RENDERIZADO — log de detalle, sidebar de corridas, panel de nodo
// ---------------------------------------------------------------------

const ETIQUETAS_TIPO = {
  llamada_modelo: { texto: "→ Modelo", clase: "evento-llamada-modelo" },
  respuesta_modelo: { texto: "← Modelo", clase: "evento-respuesta-modelo" },
  modelo_fallo: { texto: "✕ Modelo falló", clase: "evento-modelo-fallo" },
  tool_llamada: { texto: "→ Tool", clase: "evento-tool-llamada" },
  tool_resultado: { texto: "← Tool", clase: "evento-tool-resultado" },
  respuesta_final: { texto: "✓ Respuesta final", clase: "evento-respuesta-final" },
  memoria_lectura: { texto: "→ Memoria", clase: "evento-memoria" },
  memoria_escritura: { texto: "← Memoria", clase: "evento-memoria" },
};

function formatearHoraEvento(timestampUnix) {
  return new Date(timestampUnix * 1000).toLocaleTimeString("es-CO", { hour12: false });
}

function formatearTiempoRelativo(timestampUnix) {
  const segundos = Math.max(0, Math.round(Date.now() / 1000 - timestampUnix));
  if (segundos < 60) return "hace un momento";
  const minutos = Math.round(segundos / 60);
  if (minutos < 60) return `hace ${minutos} min`;
  const horas = Math.round(minutos / 60);
  if (horas < 24) return `hace ${horas} h`;
  return `hace ${Math.round(horas / 24)} d`;
}

function construirMeta(evento) {
  const partes = [];
  if (evento.agente) partes.push(evento.agente);
  if (evento.modelo) partes.push(evento.modelo);
  if (evento.nombre) partes.push(evento.nombre);
  if (typeof evento.latencia_ms === "number") partes.push(`${evento.latencia_ms} ms`);
  if (typeof evento.tiene_tool_calls === "boolean") partes.push(evento.tiene_tool_calls ? "pidió tool" : "respuesta en texto");
  if (typeof evento.encontrada === "boolean") partes.push(evento.encontrada ? "sesión existente" : "sesión nueva");
  if (evento.mensajes_por_agente && typeof evento.mensajes_por_agente === "object") {
    // Un conteo por agente ("orquestador: 12 msj / servicio_tecnico: 30 msj"),
    // sin nombres fijos: un sub-agente nuevo aparece solo (Fase 11).
    partes.push(
      Object.entries(evento.mensajes_por_agente)
        .map(([agente, cantidad]) => `${agente}: ${cantidad} msj`)
        .join(" / "),
    );
  }
  return partes.join(" · ");
}

function extraerPayload(evento) {
  return evento.argumentos ?? evento.resultado ?? evento.texto ?? evento.error ?? "";
}

function crearTarjetaEvento(evento) {
  const info = ETIQUETAS_TIPO[evento.tipo] || { texto: evento.tipo, clase: "evento-generico" };
  const tarjeta = document.createElement("div");
  tarjeta.className = `evento-tarjeta ${info.clase}`;

  const cabecera = document.createElement("div");
  cabecera.className = "evento-cabecera";
  const hora = document.createElement("span");
  hora.className = "evento-hora";
  hora.textContent = formatearHoraEvento(evento.timestamp);
  const tipo = document.createElement("span");
  tipo.className = "evento-tipo-badge";
  tipo.textContent = info.texto;
  const meta = document.createElement("span");
  meta.className = "evento-meta";
  meta.textContent = construirMeta(evento);
  cabecera.append(hora, tipo, meta);
  tarjeta.appendChild(cabecera);

  const payload = extraerPayload(evento);
  if (payload) {
    const pre = document.createElement("pre");
    pre.className = "evento-payload";
    pre.textContent = payload;
    tarjeta.appendChild(pre);
  }
  return tarjeta;
}

const el = {
  log: document.getElementById("flujo-log"),
  estado: document.getElementById("flujo-estado"),
  autoscroll: document.getElementById("flujo-autoscroll"),
  filtroLabel: document.getElementById("flujo-nodo-filtro"),
  corridasLista: document.getElementById("flujo-corridas-lista"),
  enVivoToggle: document.getElementById("flujo-en-vivo"),
  corridaTitulo: document.getElementById("flujo-corrida-titulo"),
  volverVivoBtn: document.getElementById("flujo-volver-vivo-btn"),
  detallePanel: document.getElementById("flujo-detalle-nodo"),
  detalleTitulo: document.getElementById("flujo-detalle-titulo"),
  detalleCuerpo: document.getElementById("flujo-detalle-cuerpo"),
  detalleCerrarBtn: document.getElementById("flujo-detalle-cerrar-btn"),
};

function renderizarLog(eventos) {
  el.log.innerHTML = "";
  for (const evento of eventos) el.log.appendChild(crearTarjetaEvento(evento));
  if (el.autoscroll.checked) el.log.scrollTop = el.log.scrollHeight;
}

function agregarAlLogEnVivo(evento) {
  el.log.appendChild(crearTarjetaEvento(evento));
  while (el.log.children.length > 500) el.log.removeChild(el.log.firstChild);
  if (el.autoscroll.checked) el.log.scrollTop = el.log.scrollHeight;
}

function renderizarCorridasLista() {
  el.corridasLista.innerHTML = "";
  if (corridasCache.length === 0) {
    const vacio = document.createElement("li");
    vacio.className = "flujo-corrida-vacio";
    vacio.textContent = "Todavía no hay mensajes registrados.";
    el.corridasLista.appendChild(vacio);
    return;
  }
  for (const corrida of corridasCache) {
    const item = document.createElement("li");
    item.className = `flujo-corrida-item${corrida.run_id === corridaSeleccionada ? " activo" : ""}`;
    item.dataset.runId = corrida.run_id;

    const titulo = document.createElement("span");
    titulo.className = "flujo-corrida-titulo";
    titulo.textContent = corrida.titulo || "(mensaje vacío)";

    const meta = document.createElement("div");
    meta.className = "flujo-corrida-meta";
    const sesion = document.createElement("span");
    sesion.className = "flujo-corrida-sesion";
    sesion.textContent = `${corrida.session_id} · ${formatearTiempoRelativo(corrida.iniciado_en)}`;
    const dots = document.createElement("span");
    dots.className = "flujo-corrida-dots";
    const totalDots = Math.min(4, Math.max(1, corrida.total_eventos));
    for (let i = 0; i < totalDots; i++) {
      const dot = document.createElement("span");
      dot.className = `flujo-corrida-dot${corrida.hubo_error ? " flujo-dot-error" : ""}`;
      dots.appendChild(dot);
    }
    meta.append(sesion, dots);

    item.append(titulo, meta);
    item.addEventListener("click", () => seleccionarCorrida(corrida.run_id));
    el.corridasLista.appendChild(item);
  }
}

function mostrarDetalleNodo(idNodo, corrida) {
  const nodo = nodosPorId[idNodo];
  const eventosDelNodo = corrida.eventos.filter((e) => nodoDeEvento(e) === idNodo);

  el.detallePanel.hidden = false;
  el.detalleTitulo.textContent = nodo.etiqueta;
  el.detalleCuerpo.innerHTML = "";

  if (eventosDelNodo.length === 0) {
    const vacio = document.createElement("p");
    vacio.className = "flujo-detalle-vacio";
    vacio.textContent = "Este agente/herramienta no participó en esta corrida.";
    el.detalleCuerpo.appendChild(vacio);
    return;
  }
  for (const evento of eventosDelNodo) el.detalleCuerpo.appendChild(crearTarjetaEvento(evento));
}

// ---------------------------------------------------------------------
// 5. INTERACCIÓN
// ---------------------------------------------------------------------

let corridasCache = [];
let corridaSeleccionada = null; // run_id, o null = modo "en vivo"
let corridaCompletaActual = null;
let nodoFiltrado = null;

function aplicarFiltroLog() {
  const tarjetas = el.log.querySelectorAll(".evento-tarjeta");
  // El filtro por nodo solo tiene sentido dentro del log ya acotado a una
  // corrida — en modo "en vivo" no se filtra el log (nodoFiltrado se
  // resetea al volver a vivo).
  for (const tarjeta of tarjetas) tarjeta.hidden = false;
  if (!nodoFiltrado || !corridaCompletaActual) return;
  const eventosVisibles = corridaCompletaActual.eventos.filter((e) => nodoDeEvento(e) === nodoFiltrado);
  renderizarLog(eventosVisibles);
}

async function seleccionarCorrida(runId) {
  corridaSeleccionada = runId;
  nodoFiltrado = null;
  el.filtroLabel.hidden = true;
  el.detallePanel.hidden = true;
  el.volverVivoBtn.hidden = false;
  renderizarCorridasLista();

  try {
    corridaCompletaActual = await obtenerCorrida(runId);
  } catch (err) {
    el.log.innerHTML = "";
    const aviso = document.createElement("p");
    aviso.className = "aviso-error";
    aviso.textContent = `⚠️ ${err.message}`;
    el.log.appendChild(aviso);
    return;
  }
  el.corridaTitulo.textContent = `Corrida: ${corridaCompletaActual.titulo || corridaCompletaActual.session_id}`;
  aplicarEstadoCorrida(corridaCompletaActual);
  renderizarLog(corridaCompletaActual.eventos);
}

function volverAModoEnVivo() {
  corridaSeleccionada = null;
  corridaCompletaActual = null;
  nodoFiltrado = null;
  el.filtroLabel.hidden = true;
  el.detallePanel.hidden = true;
  el.volverVivoBtn.hidden = true;
  el.corridaTitulo.textContent = "";
  limpiarEstadoCorridaEnGrafo();
  el.log.innerHTML = "";
  renderizarCorridasLista();
}

function onClickNodo(idNodo) {
  if (corridaSeleccionada && corridaCompletaActual) {
    mostrarDetalleNodo(idNodo, corridaCompletaActual);
    nodoFiltrado = nodoFiltrado === idNodo ? null : idNodo;
    el.filtroLabel.hidden = !nodoFiltrado;
    if (nodoFiltrado) el.filtroLabel.textContent = `Filtrando log: ${nodosPorId[idNodo].etiqueta}`;
    aplicarFiltroLog();
    return;
  }
  // En modo "en vivo" no hay una corrida fija que inspeccionar por nodo —
  // el click solo tiene sentido una vez se elige una corrida del sidebar.
}

el.detalleCerrarBtn.addEventListener("click", () => {
  el.detallePanel.hidden = true;
});
el.volverVivoBtn.addEventListener("click", volverAModoEnVivo);

let ultimaActualizacionLista = 0;
async function refrescarListaCorridasThrottled() {
  const ahora = Date.now();
  if (ahora - ultimaActualizacionLista < 500) return;
  ultimaActualizacionLista = ahora;
  try {
    corridasCache = await obtenerCorridas();
    renderizarCorridasLista();
  } catch {
    // Si falla el refresco del sidebar, el stream en vivo sigue funcionando
    // igual — no es motivo para romper el resto del panel.
  }
}

let fuenteActual = null;

function conectarStream() {
  // La autenticación viaja en la cookie de sesión del panel (api.js), que
  // EventSource envía sola por ser del mismo origen: la clave nunca va en la URL.
  if (fuenteActual) fuenteActual.close();
  const fuente = new EventSource("/api/flujo/stream");
  fuenteActual = fuente;

  fuente.addEventListener("open", () => {
    el.estado.textContent = "● Conectado";
    el.estado.className = "flujo-estado flujo-estado-conectado";
  });
  fuente.addEventListener("error", () => {
    el.estado.textContent = "● Reconectando…";
    el.estado.className = "flujo-estado flujo-estado-error";
  });
  fuente.addEventListener("message", (mensaje) => {
    let evento;
    try {
      evento = JSON.parse(mensaje.data);
    } catch {
      return; // línea no-JSON inesperada, se ignora
    }

    if (el.enVivoToggle.checked) refrescarListaCorridasThrottled();

    if (!corridaSeleccionada) {
      pulsarEnVivo(evento);
      agregarAlLogEnVivo(evento);
    }
    // Si se está viendo una corrida específica, los eventos en vivo de
    // OTRAS corridas no deben pisar esa vista fija — se ignoran para el
    // grafo/log (el sidebar sí se sigue refrescando, ver arriba).
  });
}

async function iniciar() {
  try {
    corridasCache = await obtenerCorridas();
    renderizarCorridasLista();
  } catch {
    // Sin corridas previas (servidor recién iniciado) — el sidebar queda
    // vacío hasta el primer mensaje real, no es un error.
  }
}

dibujarNodos();
dibujarConexiones();
conectarStream();
iniciar();
