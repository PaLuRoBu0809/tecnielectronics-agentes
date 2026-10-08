/**
 * admin.js — pestaña "Servicio técnico": calendario de entregas de equipos +
 * listado de órdenes de servicio (Fase 13).
 *
 * Cada orden tiene el DÍA en que el cliente trae su equipo a la sede (y una
 * hora aproximada, solo informativa). El calendario cuenta los equipos que
 * llegan cada día; al abrir una orden se ve su detalle, se cambia su estado
 * y se escriben o editan notas (seguimiento.js), siempre con el nombre de
 * quien las registra.
 *
 * Mismas 4 capas que ventas.js:
 *   1. ACCESO A DATOS — fetch y nada más.
 *   2. DOMINIO        — funciones puras (agrupar por día, nombres, fechas).
 *   3. RENDERIZADO    — construyen DOM a partir de datos listos.
 *   4. INTERACCIÓN    — listeners y orquestación.
 */

import { apiFetch } from "./api.js";
import { abrirDetalle, crearBadge, estadoVisible } from "./seguimiento.js";

// ---------------------------------------------------------------------
// Constantes
// ---------------------------------------------------------------------

const API_ORDENES = "/api/ordenes-servicio";
const API_CATALOGO = "/api/catalogo";
const COLUMNAS = 8;

const DIAS_SEMANA = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"];
const MESES = [
  "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
  "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
];
const MAX_CHIPS_POR_CELDA = 3;

// Valor del ENUM estado_orden_servicio -> texto y estilo del badge.
export const ESTADOS_SERVICIO = {
  PENDIENTE_RECEPCION: { texto: "Pendiente de recibir", clase: "badge-pendiente" },
  RECIBIDO: { texto: "Recibido", clase: "badge-info" },
  EN_DIAGNOSTICO: { texto: "En diagnóstico", clase: "badge-info" },
  EN_REPARACION: { texto: "En reparación", clase: "badge-info" },
  LISTO_PARA_RECOGER: { texto: "Listo para recoger", clase: "badge-confirmado" },
  ENTREGADO: { texto: "Entregado", clase: "badge-confirmado" },
  CANCELADO: { texto: "Cancelada", clase: "badge-cancelado" },
};
const NO_LLEGO = { texto: "No llegó", clase: "badge-cancelado" };

// ---------------------------------------------------------------------
// 1. ACCESO A DATOS
// ---------------------------------------------------------------------

async function obtenerJson(url, queCosa) {
  const resp = await apiFetch(url);
  if (!resp.ok) throw new Error(`No se pudo cargar ${queCosa} (HTTP ${resp.status})`);
  return resp.json();
}

// ---------------------------------------------------------------------
// 2. DOMINIO — funciones puras
// ---------------------------------------------------------------------

/** "YYYY-MM-DD" de una fecha en hora LOCAL (toISOString daría la de UTC). */
function fechaLocalISO(fecha) {
  const mes = String(fecha.getMonth() + 1).padStart(2, "0");
  const dia = String(fecha.getDate()).padStart(2, "0");
  return `${fecha.getFullYear()}-${mes}-${dia}`;
}

function agruparPorDia(ordenes) {
  const mapa = {};
  for (const orden of ordenes) {
    (mapa[orden.fecha_entrega] ||= []).push(orden);
  }
  return mapa;
}

function nombreServicio(servicioId, catalogoPorId) {
  return catalogoPorId[String(servicioId)]?.nombre || `Servicio #${servicioId ?? "?"}`;
}

/** "09:30:00" -> "9:30 a. m."; sin hora -> "—". */
function formatearHora(hora) {
  if (!hora) return "—";
  const [h, m] = hora.split(":").map(Number);
  return new Date(2000, 0, 1, h, m).toLocaleTimeString("es-CO", { hour: "numeric", minute: "2-digit" });
}

function formatearDia(fechaISO) {
  const [anio, mes, dia] = fechaISO.split("-").map(Number);
  return new Date(anio, mes - 1, dia).toLocaleDateString("es-CO", {
    weekday: "long", day: "numeric", month: "long", year: "numeric",
  });
}

/** Pasó el día y el equipo nunca llegó: la empresa debería llamar al cliente. */
function noLlego(orden, hoyISO) {
  return orden.estado === "PENDIENTE_RECEPCION" && orden.fecha_entrega < hoyISO;
}

function estadoDeOrden(orden, hoyISO) {
  return noLlego(orden, hoyISO) ? NO_LLEGO : estadoVisible(ESTADOS_SERVICIO, orden.estado);
}

function ordenarPorHora(a, b) {
  return (a.hora_aproximada || "99").localeCompare(b.hora_aproximada || "99");
}

// ---------------------------------------------------------------------
// Estado de la página
// ---------------------------------------------------------------------

let mesVisible = new Date();
let ordenesCache = [];
let catalogoPorId = {};

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
  filtroTexto: document.getElementById("filtro-registros"),
  filtroEstado: document.getElementById("filtro-estado"),
  contador: document.getElementById("registros-contador"),
  tablaBody: document.getElementById("tabla-registros-body"),
  actualizarBtn: document.getElementById("servicio-actualizar-btn"),
};

// ---------------------------------------------------------------------
// 3. RENDERIZADO
// ---------------------------------------------------------------------

function crearChip(orden, hoyISO) {
  const chip = document.createElement("div");
  chip.className = `calendario-chip ${estadoDeOrden(orden, hoyISO).clase}`;
  const hora = orden.hora_aproximada ? `${formatearHora(orden.hora_aproximada)} ` : "";
  chip.textContent = `${hora}${orden.cliente_nombre || ""}`.trim();
  return chip;
}

function crearCeldaDia(numeroDia, fechaISO, ordenesDelDia, hoyISO) {
  const celda = document.createElement("div");
  celda.className = "calendario-celda" + (fechaISO === hoyISO ? " calendario-celda-hoy" : "");

  const numero = document.createElement("span");
  numero.className = "calendario-numero";
  numero.textContent = String(numeroDia);
  celda.appendChild(numero);

  const activas = ordenesDelDia.filter((o) => o.estado !== "CANCELADO");
  if (activas.length > 0) {
    const conteo = document.createElement("span");
    conteo.className = "calendario-conteo";
    conteo.textContent = `${activas.length} equipo${activas.length === 1 ? "" : "s"}`;
    celda.appendChild(conteo);
  }
  for (const orden of ordenesDelDia.slice(0, MAX_CHIPS_POR_CELDA)) {
    celda.appendChild(crearChip(orden, hoyISO));
  }
  if (ordenesDelDia.length > MAX_CHIPS_POR_CELDA) {
    const mas = document.createElement("div");
    mas.className = "calendario-chip-mas";
    mas.textContent = `+${ordenesDelDia.length - MAX_CHIPS_POR_CELDA} más`;
    celda.appendChild(mas);
  }
  if (ordenesDelDia.length > 0) {
    celda.classList.add("calendario-celda-con-citas");
    celda.addEventListener("click", () => abrirPanelDia(fechaISO, ordenesDelDia));
  }
  return celda;
}

function renderizarCalendario() {
  const anio = mesVisible.getFullYear();
  const mes = mesVisible.getMonth();
  el.mesActualLabel.textContent = `${MESES[mes]} ${anio}`;
  el.calendarioGrid.replaceChildren();
  for (const dia of DIAS_SEMANA) {
    const encabezado = document.createElement("div");
    encabezado.className = "calendario-dia-header";
    encabezado.textContent = dia;
    el.calendarioGrid.appendChild(encabezado);
  }

  const offsetInicial = (new Date(anio, mes, 1).getDay() + 6) % 7; // lunes = 0
  const diasEnMes = new Date(anio, mes + 1, 0).getDate();
  const porDia = agruparPorDia(ordenesCache);
  const hoyISO = fechaLocalISO(new Date());

  for (let i = 0; i < offsetInicial; i++) {
    const vacia = document.createElement("div");
    vacia.className = "calendario-celda calendario-celda-vacia";
    el.calendarioGrid.appendChild(vacia);
  }
  for (let dia = 1; dia <= diasEnMes; dia++) {
    const fechaISO = fechaLocalISO(new Date(anio, mes, dia));
    const delDia = (porDia[fechaISO] || []).slice().sort(ordenarPorHora);
    el.calendarioGrid.appendChild(crearCeldaDia(dia, fechaISO, delDia, hoyISO));
  }
}

function abrirPanelDia(fechaISO, ordenesDelDia) {
  const hoyISO = fechaLocalISO(new Date());
  el.panelDia.hidden = false;
  el.panelDiaTitulo.textContent = `Equipos del ${formatearDia(fechaISO)}`;
  el.panelDiaLista.replaceChildren();
  for (const orden of ordenesDelDia) {
    const li = document.createElement("li");
    li.className = "panel-dia-item";
    li.tabIndex = 0;

    const filaTitulo = document.createElement("div");
    filaTitulo.className = "fila-titulo";
    const nombre = document.createElement("span");
    nombre.textContent = `#${orden.numero} · ${orden.cliente_nombre}`;
    const hora = document.createElement("span");
    hora.textContent = formatearHora(orden.hora_aproximada);
    filaTitulo.append(nombre, hora);

    const filaEquipo = document.createElement("div");
    filaEquipo.className = "fila-detalle";
    filaEquipo.textContent = `${orden.equipo} · ${nombreServicio(orden.servicio_id, catalogoPorId)}`;

    const filaEstado = document.createElement("div");
    filaEstado.className = "fila-detalle";
    filaEstado.appendChild(crearBadge(estadoDeOrden(orden, hoyISO)));

    li.append(filaTitulo, filaEquipo, filaEstado);
    li.addEventListener("click", () => abrirOrden(orden.numero));
    li.addEventListener("keydown", (e) => e.key === "Enter" && abrirOrden(orden.numero));
    el.panelDiaLista.appendChild(li);
  }
}

function crearCelda(contenido) {
  const td = document.createElement("td");
  if (contenido instanceof Node) td.appendChild(contenido);
  else td.textContent = contenido;
  return td;
}

function crearFila(orden, hoyISO) {
  const tr = document.createElement("tr");
  tr.className = "fila-clicable";
  tr.tabIndex = 0;
  tr.title = "Ver detalle, cambiar estado y notas";
  tr.append(
    crearCelda(`#${orden.numero}`),
    crearCelda(formatearDia(orden.fecha_entrega)),
    crearCelda(formatearHora(orden.hora_aproximada)),
    crearCelda(`${orden.cliente_nombre} · ${orden.cliente_telefono}`),
    crearCelda(nombreServicio(orden.servicio_id, catalogoPorId)),
    crearCelda(orden.equipo),
    crearCelda(orden.descripcion),
    crearCelda(crearBadge(estadoDeOrden(orden, hoyISO))),
  );
  tr.addEventListener("click", () => abrirOrden(orden.numero));
  tr.addEventListener("keydown", (e) => e.key === "Enter" && abrirOrden(orden.numero));
  return tr;
}

function crearFilaAviso(texto, clase) {
  const tr = document.createElement("tr");
  const td = document.createElement("td");
  td.colSpan = COLUMNAS;
  td.className = clase;
  td.textContent = texto;
  tr.appendChild(td);
  return tr;
}

function renderizarTabla() {
  const texto = el.filtroTexto.value.toLowerCase().trim();
  const estado = el.filtroEstado.value;
  const hoyISO = fechaLocalISO(new Date());
  const filas = ordenesCache.filter((orden) => {
    const buscable = [
      `#${orden.numero}`, orden.cliente_nombre, orden.cliente_telefono, orden.equipo, orden.descripcion,
      nombreServicio(orden.servicio_id, catalogoPorId),
    ].join(" ").toLowerCase();
    const coincideEstado = !estado
      || (estado === "NO_LLEGO" ? noLlego(orden, hoyISO) : orden.estado === estado);
    return (!texto || buscable.includes(texto)) && coincideEstado;
  });

  el.tablaBody.replaceChildren();
  if (filas.length === 0) {
    el.tablaBody.appendChild(crearFilaAviso(
      ordenesCache.length ? "No hay órdenes que coincidan con el filtro."
        : "Todavía no hay órdenes de servicio: aparecerán cuando un cliente agende con el asistente.",
      "aviso-vacio",
    ));
  }
  for (const orden of filas) el.tablaBody.appendChild(crearFila(orden, hoyISO));
  el.contador.textContent = `${filas.length} de ${ordenesCache.length} órdenes`;
}

function poblarFiltroEstados() {
  for (const [valor, { texto }] of [...Object.entries(ESTADOS_SERVICIO), ["NO_LLEGO", NO_LLEGO]]) {
    const opcion = document.createElement("option");
    opcion.value = valor;
    opcion.textContent = texto;
    el.filtroEstado.appendChild(opcion);
  }
}

function mostrarErrorCarga(err) {
  el.tablaBody.replaceChildren(crearFilaAviso(`⚠️ No se pudieron cargar los datos: ${err.message}`, "aviso-error"));
  el.contador.textContent = "";
}

// ---------------------------------------------------------------------
// 4. INTERACCIÓN
// ---------------------------------------------------------------------

function abrirOrden(numero) {
  abrirDetalle({
    titulo: `Orden de servicio #${numero}`,
    estados: ESTADOS_SERVICIO,
    urlEstado: `${API_ORDENES}/${numero}/estado`,
    urlNota: `${API_ORDENES}/${numero}/notas`,
    urlEditarNota: (id) => `${API_ORDENES}/notas/${id}`,
    alCambiar: cargar,
    cargar: async () => {
      const { orden, seguimiento } = await obtenerJson(`${API_ORDENES}/${numero}`, "la orden");
      return {
        estado: orden.estado,
        notas: seguimiento,
        datos: [
          ["Cliente", `${orden.cliente_nombre} · ${orden.cliente_telefono}`],
          ["Servicio", nombreServicio(orden.servicio_id, catalogoPorId)],
          ["Equipo", orden.equipo],
          ["Problema", orden.descripcion],
          ["Trae el equipo", `${formatearDia(orden.fecha_entrega)} · ${formatearHora(orden.hora_aproximada)}`],
        ],
      };
    },
  });
}

async function cargar() {
  el.actualizarBtn.disabled = true;
  try {
    const [ordenes, catalogo] = await Promise.all([
      obtenerJson(API_ORDENES, "las órdenes de servicio"),
      obtenerJson(API_CATALOGO, "el catálogo"),
    ]);
    ordenesCache = ordenes;
    catalogoPorId = Object.fromEntries(catalogo.map((s) => [String(s.id), s]));
    renderizarCalendario();
    renderizarTabla();
  } catch (err) {
    mostrarErrorCarga(err);
  } finally {
    el.actualizarBtn.disabled = false;
  }
}

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
el.filtroTexto.addEventListener("input", renderizarTabla);
el.filtroEstado.addEventListener("change", renderizarTabla);
el.actualizarBtn.addEventListener("click", cargar);

poblarFiltroEstados();
cargar();
