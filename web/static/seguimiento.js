/**
 * seguimiento.js — panel de detalle compartido por las pestañas "Servicio
 * técnico" (admin.js) y "Ventas" (ventas.js), Fase 13.
 *
 * Muestra los datos de una orden o pedido, su estado actual y el historial de
 * notas, y permite:
 *   - registrar una novedad: una nota, con o sin cambio de estado;
 *   - editar una nota existente.
 * Toda escritura exige la nota y el nombre de quien la registra (lo valida
 * también el backend y la base de datos). El nombre se recuerda en este
 * navegador para no escribirlo cada vez.
 *
 * Las reglas (quién puede pasar a qué estado, devolver stock al cancelar...)
 * viven en el backend: aquí solo se muestran sus mensajes de error.
 *
 * Todo el texto se pinta con textContent: las notas las escriben personas y
 * nunca se interpretan como HTML.
 */

import { apiFetch } from "./api.js";

const CLAVE_RESPONSABLE = "tecnielectronics_responsable";

const el = {
  panel: document.getElementById("panel-detalle"),
  titulo: document.getElementById("panel-detalle-titulo"),
  cuerpo: document.getElementById("panel-detalle-cuerpo"),
  cerrarBtn: document.getElementById("panel-detalle-cerrar-btn"),
  fondo: document.getElementById("panel-detalle-fondo"),
};

// ---------------------------------------------------------------------
// Utilidades compartidas (también las usan admin.js y ventas.js)
// ---------------------------------------------------------------------

export function crearBadge({ texto, clase }) {
  const span = document.createElement("span");
  span.className = `badge-estado ${clase}`;
  span.textContent = texto;
  return span;
}

export function estadoVisible(mapa, valor) {
  return mapa[valor] || { texto: valor || "Sin registro", clase: "badge-info" };
}

export function formatearFechaHora(iso) {
  if (!iso) return "—";
  const fecha = new Date(iso);
  if (Number.isNaN(fecha.getTime())) return "—";
  return fecha.toLocaleString("es-CO", { dateStyle: "medium", timeStyle: "short" });
}

function leerResponsable() {
  try {
    return localStorage.getItem(CLAVE_RESPONSABLE) || "";
  } catch {
    return "";
  }
}

function recordarResponsable(nombre) {
  try {
    localStorage.setItem(CLAVE_RESPONSABLE, nombre);
  } catch {
    // Solo es una comodidad: sin localStorage se escribe el nombre cada vez.
  }
}

/** Mensaje legible de una respuesta de error de FastAPI. */
async function mensajeDeError(resp) {
  try {
    const cuerpo = await resp.json();
    if (typeof cuerpo.detail === "string") return cuerpo.detail;
    if (Array.isArray(cuerpo.detail)) {
      return cuerpo.detail.map((e) => `${(e.loc || []).slice(-1)[0] || "dato"}: ${e.msg}`).join("; ");
    }
  } catch {
    // Respuesta sin JSON: se usa el código HTTP.
  }
  return `Error del servidor (HTTP ${resp.status}).`;
}

async function enviar(url, metodo, cuerpo) {
  const resp = await apiFetch(url, {
    method: metodo,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(cuerpo),
  });
  if (!resp.ok) throw new Error(await mensajeDeError(resp));
  return resp.json();
}

// ---------------------------------------------------------------------
// Renderizado
// ---------------------------------------------------------------------

function crearCampo(etiqueta, control) {
  const label = document.createElement("label");
  label.className = "detalle-campo";
  const texto = document.createElement("span");
  texto.textContent = etiqueta;
  label.append(texto, control);
  return label;
}

function crearInputResponsable() {
  const input = document.createElement("input");
  input.type = "text";
  input.required = true;
  input.maxLength = 80;
  input.placeholder = "Tu nombre";
  input.value = leerResponsable();
  return input;
}

function crearTextareaNota(valor = "") {
  const area = document.createElement("textarea");
  area.required = true;
  area.maxLength = 2000;
  area.rows = 3;
  area.value = valor;
  return area;
}

function crearDatos(datos) {
  const dl = document.createElement("dl");
  dl.className = "detalle-datos";
  for (const [etiqueta, valor] of datos) {
    const dt = document.createElement("dt");
    dt.textContent = etiqueta;
    const dd = document.createElement("dd");
    if (valor instanceof Node) dd.appendChild(valor);
    else dd.textContent = valor ?? "—";
    dl.append(dt, dd);
  }
  return dl;
}

function crearFormularioNovedad(config, detalle, alGuardar) {
  const form = document.createElement("form");
  form.className = "detalle-form";

  const titulo = document.createElement("h4");
  titulo.textContent = "Registrar novedad";

  const select = document.createElement("select");
  const mantener = document.createElement("option");
  mantener.value = "";
  mantener.textContent = `Mantener: ${estadoVisible(config.estados, detalle.estado).texto}`;
  select.appendChild(mantener);
  for (const [valor, { texto }] of Object.entries(config.estados)) {
    if (valor === detalle.estado) continue;
    const opcion = document.createElement("option");
    opcion.value = valor;
    opcion.textContent = `Cambiar a: ${texto}`;
    select.appendChild(opcion);
  }

  const nota = crearTextareaNota();
  nota.placeholder = "Ej.: Llegó con cargador, la pantalla tiene un rayón.";
  const responsable = crearInputResponsable();
  const aviso = document.createElement("p");
  aviso.className = "detalle-aviso";
  aviso.textContent = "El asistente lee estas notas para contarle al cliente cómo va: escríbelas pensando en él.";
  const error = document.createElement("p");
  error.className = "detalle-error";
  const boton = document.createElement("button");
  boton.type = "submit";
  boton.textContent = "Guardar";

  form.append(
    titulo,
    crearCampo("Estado", select),
    crearCampo("Nota (obligatoria)", nota),
    crearCampo("Quién la registra (obligatorio)", responsable),
    aviso, error, boton,
  );

  form.addEventListener("submit", async (evento) => {
    evento.preventDefault();
    const datos = { nota: nota.value.trim(), responsable: responsable.value.trim() };
    if (!datos.nota || !datos.responsable) {
      error.textContent = "La nota y el nombre de quien la registra son obligatorios.";
      return;
    }
    const advertencia = select.value && config.confirmarEstado?.[select.value];
    if (advertencia && !window.confirm(advertencia)) return;

    boton.disabled = true;
    error.textContent = "";
    try {
      if (select.value) await enviar(config.urlEstado, "POST", { ...datos, estado: select.value });
      else await enviar(config.urlNota, "POST", datos);
      recordarResponsable(datos.responsable);
      await alGuardar();
    } catch (err) {
      error.textContent = err.message;
      boton.disabled = false;
    }
  });
  return form;
}

function crearEditorNota(item, config, alGuardar, alCancelar) {
  const form = document.createElement("form");
  form.className = "detalle-editor";
  const nota = crearTextareaNota(item.nota);
  const responsable = crearInputResponsable();
  const error = document.createElement("p");
  error.className = "detalle-error";
  const acciones = document.createElement("div");
  acciones.className = "detalle-acciones";
  const guardar = document.createElement("button");
  guardar.type = "submit";
  guardar.textContent = "Guardar cambios";
  const cancelar = document.createElement("button");
  cancelar.type = "button";
  cancelar.className = "secundario";
  cancelar.textContent = "Cancelar";
  cancelar.addEventListener("click", alCancelar);
  acciones.append(guardar, cancelar);
  form.append(nota, crearCampo("Quién la edita (obligatorio)", responsable), error, acciones);

  form.addEventListener("submit", async (evento) => {
    evento.preventDefault();
    const datos = { nota: nota.value.trim(), responsable: responsable.value.trim() };
    if (!datos.nota || !datos.responsable) {
      error.textContent = "La nota y el nombre de quien la edita son obligatorios.";
      return;
    }
    guardar.disabled = true;
    try {
      await enviar(config.urlEditarNota(item.id), "PATCH", datos);
      recordarResponsable(datos.responsable);
      await alGuardar();
    } catch (err) {
      error.textContent = err.message;
      guardar.disabled = false;
    }
  });
  return form;
}

function crearHistorial(notas, config, alGuardar) {
  const contenedor = document.createElement("section");
  const titulo = document.createElement("h4");
  titulo.textContent = `Historial de notas (${notas.length})`;
  const lista = document.createElement("ol");
  lista.className = "detalle-historial";

  // La más reciente primero.
  for (const item of [...notas].reverse()) {
    const li = document.createElement("li");
    const cabecera = document.createElement("div");
    cabecera.className = "detalle-nota-cabecera";
    const fecha = document.createElement("span");
    fecha.textContent = formatearFechaHora(item.creado_en);
    const editar = document.createElement("button");
    editar.type = "button";
    editar.className = "secundario detalle-editar-btn";
    editar.textContent = "Editar";
    cabecera.append(crearBadge(estadoVisible(config.estados, item.estado)), fecha, editar);

    const texto = document.createElement("p");
    texto.className = "detalle-nota-texto";
    texto.textContent = item.nota;
    const autor = document.createElement("p");
    autor.className = "detalle-nota-autor";
    autor.textContent = `— ${item.responsable}`
      + (item.editado_en ? ` · editada por ${item.editado_por} el ${formatearFechaHora(item.editado_en)}` : "");

    li.append(cabecera, texto, autor);
    editar.addEventListener("click", () => {
      const editor = crearEditorNota(item, config, alGuardar, () => editor.replaceWith(texto));
      texto.replaceWith(editor);
    });
    lista.appendChild(li);
  }
  if (notas.length === 0) {
    const vacio = document.createElement("p");
    vacio.className = "aviso-vacio";
    vacio.textContent = "Todavía no hay notas.";
    contenedor.append(titulo, vacio);
  } else {
    contenedor.append(titulo, lista);
  }
  return contenedor;
}

// ---------------------------------------------------------------------
// API pública
// ---------------------------------------------------------------------

let configActual = null;

/**
 * Abre el panel de detalle.
 * @param {object} config
 *   titulo         — texto del encabezado.
 *   cargar()       — async, devuelve { estado, notas, datos: [[etiqueta, valor], ...] }.
 *   estados        — { VALOR: { texto, clase } } con todos los estados posibles.
 *   urlEstado, urlNota, urlEditarNota(id) — endpoints de escritura.
 *   confirmarEstado — opcional, { VALOR: "pregunta" } para estados delicados.
 *   alCambiar()    — se llama tras cada escritura (para refrescar la lista).
 */
export async function abrirDetalle(config) {
  configActual = config;
  el.titulo.textContent = config.titulo;
  el.panel.hidden = false;
  el.fondo.hidden = false;
  await pintar(config);
}

async function pintar(config) {
  el.cuerpo.replaceChildren(Object.assign(document.createElement("p"), { className: "etiqueta", textContent: "Cargando…" }));
  let detalle;
  try {
    detalle = await config.cargar();
  } catch (err) {
    const aviso = document.createElement("p");
    aviso.className = "aviso-error";
    aviso.textContent = `⚠️ ${err.message}`;
    el.cuerpo.replaceChildren(aviso);
    return;
  }
  if (configActual !== config) return; // se abrió otro detalle mientras cargaba

  const alGuardar = async () => {
    await pintar(config);
    config.alCambiar?.();
  };
  el.cuerpo.replaceChildren(
    crearDatos([["Estado actual", crearBadge(estadoVisible(config.estados, detalle.estado))], ...detalle.datos]),
    crearFormularioNovedad(config, detalle, alGuardar),
    crearHistorial(detalle.notas, config, alGuardar),
  );
}

export function cerrarDetalle() {
  configActual = null;
  el.panel.hidden = true;
  el.fondo.hidden = true;
}

el.cerrarBtn.addEventListener("click", cerrarDetalle);
el.fondo.addEventListener("click", cerrarDetalle);
document.addEventListener("keydown", (evento) => {
  if (evento.key === "Escape" && !el.panel.hidden) cerrarDetalle();
});
