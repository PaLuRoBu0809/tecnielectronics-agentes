/**
 * ventas.js — pestaña "Ventas": listado de pedidos (tabla `orders`).
 *
 * Mismas 4 capas que admin.js, cada una con una única responsabilidad:
 *   1. ACCESO A DATOS — obtenerPedidos(): solo fetch, devuelve JSON o lanza.
 *   2. DOMINIO        — funciones puras: formato de pesos y fechas, texto de
 *      búsqueda, etiquetas de estado.
 *   3. RENDERIZADO    — construye filas y badges a partir de datos listos.
 *   4. INTERACCIÓN    — filtros, botón actualizar y carga inicial.
 *
 * Solo lectura: consume GET /api/pedidos (ver web/app.py), que lee directo
 * de la base de datos sin pasar por ningún agente. Los pedidos los crea el
 * agente de ventas cuando el cliente confirma.
 */

import { apiFetch } from "./api.js";

// ---------------------------------------------------------------------
// Constantes
// ---------------------------------------------------------------------

const API_PEDIDOS = "/api/pedidos";
const COLUMNAS = 9;

// Valor del ENUM en la base -> texto y estilo del badge.
const ESTADOS_PAGO = {
  APROBADO: { texto: "Aprobado", clase: "badge-confirmado" },
  CONTRAENTREGA: { texto: "Contra entrega", clase: "badge-info" },
  PENDIENTE: { texto: "Pendiente", clase: "badge-pendiente" },
  RECHAZADO: { texto: "Rechazado", clase: "badge-cancelado" },
};
const ESTADOS_ENVIO = {
  PENDIENTE_DESPACHO: { texto: "Por despachar", clase: "badge-pendiente" },
  DESPACHADO: { texto: "Despachado", clase: "badge-info" },
  ENTREGADO: { texto: "Entregado", clase: "badge-confirmado" },
  CANCELADO: { texto: "Cancelado", clase: "badge-cancelado" },
};
const METODOS = { contra_entrega: "Contra entrega", en_linea: "En línea" };

// ---------------------------------------------------------------------
// 1. ACCESO A DATOS
// ---------------------------------------------------------------------

async function obtenerPedidos() {
  const resp = await apiFetch(API_PEDIDOS);
  if (!resp.ok) {
    throw new Error(`No se pudieron cargar los pedidos (HTTP ${resp.status})`);
  }
  return resp.json();
}

// ---------------------------------------------------------------------
// 2. DOMINIO — funciones puras
// ---------------------------------------------------------------------

const formatoPesos = new Intl.NumberFormat("es-CO", {
  style: "currency", currency: "COP", maximumFractionDigits: 0,
});

function formatearPesos(valor) {
  return valor == null ? "—" : formatoPesos.format(Number(valor));
}

function formatearFecha(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("es-CO", { dateStyle: "medium", timeStyle: "short" });
}

/** `items` es una lista de {nombre, cantidad, ...}. */
function resumenProductos(items) {
  if (!Array.isArray(items) || items.length === 0) return "—";
  return items.map((i) => `${i.nombre || "Producto"} x${i.cantidad ?? 1}`).join(", ");
}

function textoBusqueda(pedido) {
  return [
    `#${pedido.order_number}`,
    pedido.customer_name,
    pedido.customer_phone,
    pedido.session_id,
    pedido.city,
    pedido.customer_address,
    resumenProductos(pedido.items),
  ].join(" ").toLowerCase();
}

function estadoVisible(mapa, valor) {
  return mapa[valor] || { texto: valor || "Sin registro", clase: "badge-info" };
}

// ---------------------------------------------------------------------
// 3. RENDERIZADO
// ---------------------------------------------------------------------

const el = {
  tablaBody: document.getElementById("tabla-pedidos-body"),
  vacio: document.getElementById("pedidos-vacio"),
  filtroTexto: document.getElementById("filtro-pedidos"),
  filtroPago: document.getElementById("filtro-pago"),
  filtroEnvio: document.getElementById("filtro-envio"),
  contador: document.getElementById("pedidos-contador"),
  actualizarBtn: document.getElementById("pedidos-actualizar-btn"),
};

let pedidosCache = [];

function crearBadge({ texto, clase }) {
  const span = document.createElement("span");
  span.className = `badge-estado ${clase}`;
  span.textContent = texto;
  return span;
}

function crearCelda(contenido, titulo) {
  const td = document.createElement("td");
  if (contenido instanceof Node) td.appendChild(contenido);
  else td.textContent = contenido;
  if (titulo) td.title = titulo;
  return td;
}

function crearCeldaPago(pedido) {
  const td = crearCelda(crearBadge(estadoVisible(ESTADOS_PAGO, pedido.payment_status)));
  // Pedido en línea sin pagar: el link sirve para reenviárselo al cliente.
  if (pedido.payment_link && ["PENDIENTE", "RECHAZADO"].includes(pedido.payment_status)) {
    const enlace = document.createElement("a");
    enlace.href = pedido.payment_link;
    enlace.target = "_blank";
    enlace.rel = "noopener noreferrer";
    enlace.className = "pedido-link-pago";
    enlace.textContent = "link de pago";
    td.append(" ", enlace);
  }
  return td;
}

function crearFilaPedido(pedido) {
  const tr = document.createElement("tr");
  const cliente = `${pedido.customer_name || "—"} · ${pedido.customer_phone || "—"}`;
  const entrega = `${pedido.city || "—"} · ${pedido.customer_address || "—"}`;
  const productos = resumenProductos(pedido.items);

  tr.append(
    crearCelda(`#${pedido.order_number}`),
    crearCelda(formatearFecha(pedido.created_at)),
    crearCelda(cliente, pedido.session_id ? `WhatsApp: ${pedido.session_id}` : ""),
    crearCelda(entrega),
    crearCelda(productos, productos),
    crearCelda(formatearPesos(pedido.total_amount)),
    crearCelda(METODOS[pedido.Metodo_pago] || "—"),
    crearCeldaPago(pedido),
    crearCelda(crearBadge(estadoVisible(ESTADOS_ENVIO, pedido.shipping_status))),
  );
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

/** Repinta aplicando los filtros sobre `pedidosCache` (sin volver a pedir datos). */
function renderizarPedidos() {
  const texto = el.filtroTexto.value.toLowerCase().trim();
  const pago = el.filtroPago.value;
  const envio = el.filtroEnvio.value;

  const filas = pedidosCache.filter((p) =>
    (!texto || textoBusqueda(p).includes(texto))
    && (!pago || p.payment_status === pago)
    && (!envio || p.shipping_status === envio));

  el.vacio.hidden = pedidosCache.length > 0;
  el.tablaBody.innerHTML = "";
  if (pedidosCache.length > 0 && filas.length === 0) {
    el.tablaBody.appendChild(crearFilaAviso("No hay pedidos que coincidan con el filtro.", "aviso-vacio"));
  }
  for (const pedido of filas) {
    el.tablaBody.appendChild(crearFilaPedido(pedido));
  }
  el.contador.textContent = `${filas.length} de ${pedidosCache.length} pedidos`;
}

/** Las opciones de los filtros salen de los estados conocidos (ENUM). */
function poblarFiltro(select, mapa) {
  for (const [valor, { texto }] of Object.entries(mapa)) {
    const opcion = document.createElement("option");
    opcion.value = valor;
    opcion.textContent = texto;
    select.appendChild(opcion);
  }
}

function mostrarErrorCarga(err) {
  el.vacio.hidden = true;
  el.tablaBody.innerHTML = "";
  el.tablaBody.appendChild(crearFilaAviso(`⚠️ No se pudieron cargar los pedidos: ${err.message}`, "aviso-error"));
  el.contador.textContent = "";
}

// ---------------------------------------------------------------------
// 4. INTERACCIÓN
// ---------------------------------------------------------------------

async function cargarPedidos() {
  el.actualizarBtn.disabled = true;
  try {
    pedidosCache = await obtenerPedidos();
    renderizarPedidos();
  } catch (err) {
    mostrarErrorCarga(err);
  } finally {
    el.actualizarBtn.disabled = false;
  }
}

poblarFiltro(el.filtroPago, ESTADOS_PAGO);
poblarFiltro(el.filtroEnvio, ESTADOS_ENVIO);
el.filtroTexto.addEventListener("input", renderizarPedidos);
el.filtroPago.addEventListener("change", renderizarPedidos);
el.filtroEnvio.addEventListener("change", renderizarPedidos);
el.actualizarBtn.addEventListener("click", cargarPedidos);

cargarPedidos();
