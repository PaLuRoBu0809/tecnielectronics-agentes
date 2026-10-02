/**
 * admin.js — calendario de citas + listado de registros.
 *
 * Organizado en 4 capas, cada una con una única responsabilidad (mismo
 * criterio de "SRP" que pide el skill para los hooks de React, aplicado
 * aquí sin build step ni framework, con funciones simples de JS):
 *   1. ACCESO A DATOS   — obtenerCitas() / obtenerCatalogo(): solo hacen
 *      fetch() y devuelven JSON o lanzan error. No tocan el DOM.
 *   2. DOMINIO           — funciones puras que transforman los datos
 *      (agrupar por día, mapear servicio_id -> nombre, formatear fechas).
 *      No hacen fetch ni tocan el DOM.
 *   3. RENDERIZADO       — construyen y pintan elementos del DOM a partir
 *      de datos ya listos. No deciden CUÁNDO pintar, solo CÓMO.
 *   4. INTERACCIÓN       — listeners de eventos y orquestación (decide
 *      cuándo llamar a las otras tres capas).
 *
 * Esta página es de solo lectura: nunca escribe en Supabase ni pasa por
 * ningún agente/LLM — solo consume GET /api/citas y GET /api/catalogo
 * (ver web/app.py), que a su vez leen directo de la base de datos.
 */

import { apiFetch } from "./api.js";

// ---------------------------------------------------------------------
// Constantes — cero "magic strings" sueltos en el resto del archivo.
// ---------------------------------------------------------------------

const API_CITAS = "/api/citas";
const API_CATALOGO = "/api/catalogo";
const API_TECNICOS = "/api/tecnicos";

const DIAS_SEMANA = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"];
const MESES = [
  "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
  "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
];
const MAX_CHIPS_POR_CELDA = 3;
const PREFIJO_ESTADO_CANCELADO = "cancel"; // cubre "cancelado por cliente", "cancelada", etc.

// ---------------------------------------------------------------------
// 1. ACCESO A DATOS
// ---------------------------------------------------------------------

/** Trae TODAS las citas registradas (cualquier estado) desde el backend. */
async function obtenerCitas() {
  const resp = await apiFetch(API_CITAS);
  if (!resp.ok) {
    throw new Error(`No se pudieron cargar las citas (HTTP ${resp.status})`);
  }
  return resp.json();
}

/** Trae el catálogo completo de servicios técnicos desde el backend. */
async function obtenerCatalogo() {
  const resp = await apiFetch(API_CATALOGO);
  if (!resp.ok) {
    throw new Error(`No se pudo cargar el catálogo (HTTP ${resp.status})`);
  }
  return resp.json();
}

/** Trae la lista de técnicos desde el backend — solo se usa aquí, en la
 * interfaz de administración; el agente nunca la consulta. */
async function obtenerTecnicos() {
  const resp = await apiFetch(API_TECNICOS);
  if (!resp.ok) {
    throw new Error(`No se pudieron cargar los técnicos (HTTP ${resp.status})`);
  }
  return resp.json();
}

// ---------------------------------------------------------------------
// 2. DOMINIO — funciones puras (mismos datos de entrada -> mismo resultado)
// ---------------------------------------------------------------------

/** Agrupa una lista de citas por día calendario (clave "YYYY-MM-DD"),
 * tomando el día de `fecha_hora_inicio`. */
function agruparCitasPorDia(citas) {
  const mapa = {};
  for (const cita of citas) {
    const clave = (cita.fecha_hora_inicio || "").slice(0, 10);
    if (!clave) continue;
    if (!mapa[clave]) mapa[clave] = [];
    mapa[clave].push(cita);
  }
  return mapa;
}

/** Arma un mapa {id_servicio (string) -> fila del catálogo}, para no andar
 * recorriendo el catálogo completo cada vez que se necesita un nombre. */
function indexarCatalogoPorId(catalogo) {
  const mapa = {};
  for (const servicio of catalogo) {
    mapa[String(servicio.id)] = servicio;
  }
  return mapa;
}

/** Nombre legible de un servicio a partir de su id — si el catálogo no
 * trae ese id (borrado, o catálogo aún cargando), cae en un texto de
 * respaldo en vez de romper el render. */
function nombreServicio(servicioId, catalogoPorId) {
  const servicio = catalogoPorId[String(servicioId)];
  return servicio ? servicio.nombre : `Servicio #${servicioId ?? "?"}`;
}

/** Arma un mapa {id_tecnico (string) -> fila de tecnicos}. */
function indexarTecnicosPorId(tecnicos) {
  const mapa = {};
  for (const tecnico of tecnicos) {
    mapa[String(tecnico.id)] = tecnico;
  }
  return mapa;
}

/** Nombre legible de un técnico a partir de su id — "Sin asignar" si la
 * cita todavía no tiene tecnico_id (datos previos a esta funcionalidad). */
function nombreTecnico(tecnicoId, tecnicosPorId) {
  if (tecnicoId === null || tecnicoId === undefined) return "Sin asignar";
  const tecnico = tecnicosPorId[String(tecnicoId)];
  return tecnico ? tecnico.nombre : `Técnico #${tecnicoId}`;
}

/** True si un estado de cita debe tratarse visualmente como "cancelado"
 * (soft-delete: el estado es texto libre, ej. "cancelado por cliente"). */
function esEstadoCancelado(estado) {
  return (estado || "").toLowerCase().includes(PREFIJO_ESTADO_CANCELADO);
}

function formatearHora(iso) {
  if (!iso) return "—";
  const dt = new Date(iso);
  if (Number.isNaN(dt.getTime())) return "—";
  return dt.toLocaleTimeString("es-CO", { hour: "2-digit", minute: "2-digit" });
}

function formatearFechaHoraLegible(iso) {
  if (!iso) return "—";
  const dt = new Date(iso);
  if (Number.isNaN(dt.getTime())) return "—";
  return dt.toLocaleString("es-CO", {
    day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

// ---------------------------------------------------------------------
// Estado de la página (no es "memoria de negocio": solo qué mes se está
// mostrando y la última copia de los datos que trajo el backend).
// ---------------------------------------------------------------------

let mesVisible = new Date();
let citasCache = [];
let catalogoPorId = {};
let tecnicosPorId = {};

const el = {
  mesActualLabel: document.getElementById("mes-actual-label"),
  calendarioGrid: document.getElementById("calendario-grid"),
  mesAnteriorBtn: document.getElementById("mes-anterior-btn"),
  mesSiguienteBtn: document.getElementById("mes-siguiente-btn"),
  mesHoyBtn: document.getElementById("mes-hoy-btn"),
  panelDia: document.getElementById("panel-dia"),
  panelDiaTitulo: document.getElementById("panel-dia-titulo"),
  panelDiaLista: document.getElementById("panel-dia-lista"),
  panelDiaCerrarBtn: document.getElementById("panel-dia-cerrar-btn"),
  filtroRegistros: document.getElementById("filtro-registros"),
  filtroEstado: document.getElementById("filtro-estado"),
  registrosContador: document.getElementById("registros-contador"),
  tablaBody: document.getElementById("tabla-registros-body"),
};

// ---------------------------------------------------------------------
// 3. RENDERIZADO — solo construyen DOM a partir de datos ya listos.
// ---------------------------------------------------------------------

function crearCeldaVacia() {
  const div = document.createElement("div");
  div.className = "calendario-celda calendario-celda-vacia";
  return div;
}

function crearChipCita(cita) {
  const chip = document.createElement("div");
  chip.className = `calendario-chip ${esEstadoCancelado(cita.estado) ? "badge-cancelado" : "badge-confirmado"}`;
  chip.textContent = `${formatearHora(cita.fecha_hora_inicio)} ${cita.cliente_nombre || ""}`.trim();
  return chip;
}

function crearCeldaDia(numeroDia, fechaISO, citasDelDia, esHoy) {
  const celda = document.createElement("div");
  celda.className = "calendario-celda" + (esHoy ? " calendario-celda-hoy" : "");

  const numero = document.createElement("span");
  numero.className = "calendario-numero";
  numero.textContent = String(numeroDia);
  celda.appendChild(numero);

  for (const cita of citasDelDia.slice(0, MAX_CHIPS_POR_CELDA)) {
    celda.appendChild(crearChipCita(cita));
  }
  if (citasDelDia.length > MAX_CHIPS_POR_CELDA) {
    const mas = document.createElement("div");
    mas.className = "calendario-chip-mas";
    mas.textContent = `+${citasDelDia.length - MAX_CHIPS_POR_CELDA} más`;
    celda.appendChild(mas);
  }

  if (citasDelDia.length > 0) {
    celda.classList.add("calendario-celda-con-citas");
    celda.addEventListener("click", () => abrirPanelDia(fechaISO, citasDelDia));
  }

  return celda;
}

/** Repinta la grilla completa del mes actualmente visible (`mesVisible`)
 * usando `citasCache` — no vuelve a pedir datos al backend. */
function renderizarCalendario() {
  const anio = mesVisible.getFullYear();
  const mes = mesVisible.getMonth();
  el.mesActualLabel.textContent = `${MESES[mes]} ${anio}`;

  el.calendarioGrid.innerHTML = "";
  for (const dia of DIAS_SEMANA) {
    const encabezado = document.createElement("div");
    encabezado.className = "calendario-dia-header";
    encabezado.textContent = dia;
    el.calendarioGrid.appendChild(encabezado);
  }

  const primerDiaMes = new Date(anio, mes, 1);
  const offsetInicial = (primerDiaMes.getDay() + 6) % 7; // lunes = 0
  const diasEnMes = new Date(anio, mes + 1, 0).getDate();
  const citasPorDia = agruparCitasPorDia(citasCache);
  const hoyISO = new Date().toISOString().slice(0, 10);

  for (let i = 0; i < offsetInicial; i++) {
    el.calendarioGrid.appendChild(crearCeldaVacia());
  }
  for (let dia = 1; dia <= diasEnMes; dia++) {
    const fechaISO = `${anio}-${String(mes + 1).padStart(2, "0")}-${String(dia).padStart(2, "0")}`;
    const citasDelDia = (citasPorDia[fechaISO] || [])
      .slice()
      .sort((a, b) => a.fecha_hora_inicio.localeCompare(b.fecha_hora_inicio));
    el.calendarioGrid.appendChild(crearCeldaDia(dia, fechaISO, citasDelDia, fechaISO === hoyISO));
  }
}

/** Panel lateral con el detalle completo (todas las citas) de un día —
 * se abre al hacer click en una celda del calendario que tenga citas. */
function abrirPanelDia(fechaISO, citasDelDia) {
  el.panelDia.hidden = false;
  el.panelDiaTitulo.textContent = `Citas del ${fechaISO}`;
  el.panelDiaLista.innerHTML = "";

  for (const cita of citasDelDia) {
    const li = document.createElement("li");

    const filaTitulo = document.createElement("div");
    filaTitulo.className = "fila-titulo";
    const nombreCliente = document.createElement("span");
    nombreCliente.textContent = cita.cliente_nombre || "(sin nombre)";
    const hora = document.createElement("span");
    hora.textContent = `${formatearHora(cita.fecha_hora_inicio)} - ${formatearHora(cita.fecha_hora_fin)}`;
    filaTitulo.append(nombreCliente, hora);

    const filaServicio = document.createElement("div");
    filaServicio.className = "fila-detalle";
    filaServicio.textContent = `${nombreServicio(cita.servicio_id, catalogoPorId)} · ${nombreTecnico(cita.tecnico_id, tecnicosPorId)}`;

    const filaEstado = document.createElement("div");
    filaEstado.className = "fila-detalle";
    filaEstado.textContent = `Estado: ${cita.estado || "—"} · Tel: ${cita.cliente_telefono || "—"}`;

    li.append(filaTitulo, filaServicio, filaEstado);
    el.panelDiaLista.appendChild(li);
  }
}

function crearBadgeEstado(estado) {
  const span = document.createElement("span");
  span.className = `badge-estado ${esEstadoCancelado(estado) ? "badge-cancelado" : "badge-confirmado"}`;
  span.textContent = estado || "—";
  return span;
}

function crearFilaRegistro(cita) {
  const tr = document.createElement("tr");

  const tdEstado = document.createElement("td");
  tdEstado.appendChild(crearBadgeEstado(cita.estado));

  const valores = [
    cita.cliente_nombre || "—",
    cita.cliente_telefono || "—",
    nombreServicio(cita.servicio_id, catalogoPorId),
    nombreTecnico(cita.tecnico_id, tecnicosPorId),
    formatearFechaHoraLegible(cita.fecha_hora_inicio),
    formatearFechaHoraLegible(cita.fecha_hora_fin),
    cita.Descripcion || "—",
    cita.google_calendar_event_id || "—",
  ];

  tr.appendChild(tdEstado);
  for (const valor of valores) {
    const td = document.createElement("td");
    td.textContent = valor;
    tr.appendChild(td);
  }
  return tr;
}

/** Repinta la tabla de registros aplicando el texto y estado que el
 * usuario haya escrito/elegido en los filtros — no vuelve a pedir datos
 * al backend, filtra sobre `citasCache` en memoria. */
function renderizarTablaRegistros() {
  const textoFiltro = (el.filtroRegistros.value || "").toLowerCase().trim();
  const estadoFiltro = el.filtroEstado.value;

  const filas = citasCache.filter((cita) => {
    const coincideTexto = !textoFiltro || [
      cita.cliente_nombre,
      cita.cliente_telefono,
      nombreServicio(cita.servicio_id, catalogoPorId),
      nombreTecnico(cita.tecnico_id, tecnicosPorId),
    ].some((campo) => (campo || "").toLowerCase().includes(textoFiltro));
    const coincideEstado = !estadoFiltro || cita.estado === estadoFiltro;
    return coincideTexto && coincideEstado;
  });

  el.tablaBody.innerHTML = "";
  if (filas.length === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 8;
    td.className = "aviso-vacio";
    td.textContent = "No hay registros que coincidan con el filtro.";
    tr.appendChild(td);
    el.tablaBody.appendChild(tr);
  } else {
    for (const cita of filas) {
      el.tablaBody.appendChild(crearFilaRegistro(cita));
    }
  }
  el.registrosContador.textContent = `${filas.length} de ${citasCache.length} registros`;
}

/** Reconstruye las opciones del <select> de estados a partir de los
 * valores reales presentes en `citasCache` — nunca una lista fija a mano,
 * porque el estado es texto libre definido por el negocio (soft-delete). */
function poblarFiltroEstados() {
  const estados = [...new Set(citasCache.map((c) => c.estado).filter(Boolean))].sort();
  const valorPrevio = el.filtroEstado.value;

  el.filtroEstado.innerHTML = "";
  const opcionTodos = document.createElement("option");
  opcionTodos.value = "";
  opcionTodos.textContent = "Todos los estados";
  el.filtroEstado.appendChild(opcionTodos);

  for (const estado of estados) {
    const opcion = document.createElement("option");
    opcion.value = estado;
    opcion.textContent = estado;
    el.filtroEstado.appendChild(opcion);
  }
  el.filtroEstado.value = estados.includes(valorPrevio) ? valorPrevio : "";
}

function mostrarErrorCarga(err) {
  el.calendarioGrid.innerHTML = "";
  const aviso = document.createElement("p");
  aviso.className = "aviso-error";
  aviso.textContent = `⚠️ No se pudieron cargar los datos: ${err.message}`;
  el.calendarioGrid.appendChild(aviso);
}

// ---------------------------------------------------------------------
// 4. INTERACCIÓN — listeners y orquestación (decide CUÁNDO llamar arriba).
// ---------------------------------------------------------------------

function cambiarMes(delta) {
  mesVisible = new Date(mesVisible.getFullYear(), mesVisible.getMonth() + delta, 1);
  el.panelDia.hidden = true;
  renderizarCalendario();
}

el.mesAnteriorBtn.addEventListener("click", () => cambiarMes(-1));
el.mesSiguienteBtn.addEventListener("click", () => cambiarMes(1));
el.mesHoyBtn.addEventListener("click", () => {
  mesVisible = new Date();
  el.panelDia.hidden = true;
  renderizarCalendario();
});
el.panelDiaCerrarBtn.addEventListener("click", () => {
  el.panelDia.hidden = true;
});
el.filtroRegistros.addEventListener("input", renderizarTablaRegistros);
el.filtroEstado.addEventListener("change", renderizarTablaRegistros);

/** Punto de entrada: trae citas + catálogo en paralelo y pinta ambas
 * secciones. Si algo falla (backend caído, Supabase inalcanzable), lo
 * muestra en pantalla en vez de dejar la página en blanco sin explicación. */
async function iniciar() {
  try {
    const [citas, catalogo, tecnicos] = await Promise.all([
      obtenerCitas(),
      obtenerCatalogo(),
      obtenerTecnicos(),
    ]);
    citasCache = citas;
    catalogoPorId = indexarCatalogoPorId(catalogo);
    tecnicosPorId = indexarTecnicosPorId(tecnicos);
    poblarFiltroEstados();
    renderizarCalendario();
    renderizarTablaRegistros();
  } catch (err) {
    mostrarErrorCarga(err);
  }
}

iniciar();
