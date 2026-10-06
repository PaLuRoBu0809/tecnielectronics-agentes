/**
 * chat.js — lógica de la interfaz de chat de pruebas.
 *
 * El "cerebro" real de la conversación (el historial que usan los agentes
 * para no perder contexto) vive en el servidor (ver web/app.py + sesiones.py).
 * Este script solo se encarga de:
 *   1. Mostrar visualmente los mensajes de cada sesión (guardados en
 *      localStorage SOLO como conveniencia para no perder la vista al
 *      recargar la página — nunca como fuente de verdad de los agentes).
 *   2. Mandar el mensaje del "cliente" a POST /api/chat y pintar la
 *      respuesta.
 *   3. Permitir cambiar entre varias sesiones (teléfonos) simuladas.
 */

import { apiFetch } from "./api.js";

const STORAGE_KEY = "tecnielectronics_transcripts_v1";
const SESION_POR_DEFECTO = "3000000000";

/** Lee el mapa {session_id: [{quien, texto}]} guardado en localStorage.
 * Envuelto en try/catch porque el acceso a localStorage puede fallar
 * (navegación privada, storage bloqueado, etc.) y eso nunca debe romper
 * el chat — en el peor caso, simplemente se empieza sin historial visual. */
function leerTranscripts() {
  try {
    const crudo = localStorage.getItem(STORAGE_KEY);
    return crudo ? JSON.parse(crudo) : {};
  } catch {
    return {};
  }
}

function guardarTranscripts(transcripts) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(transcripts));
  } catch {
    // Conveniencia de UI únicamente: si falla, el chat sigue funcionando,
    // solo no se recuerda el historial visual al recargar la página.
  }
}

let transcripts = leerTranscripts();
let sesionActual = null;

const el = {
  sessionInput: document.getElementById("session-input"),
  sessionSwitchBtn: document.getElementById("session-switch-btn"),
  newSessionBtn: document.getElementById("new-session-btn"),
  sesionesLista: document.getElementById("sesiones-lista"),
  chatSessionLabel: document.getElementById("chat-session-label"),
  mensajes: document.getElementById("mensajes"),
  form: document.getElementById("form-mensaje"),
  input: document.getElementById("input-mensaje"),
};

// Negrita (**x** o *x*, estilo WhatsApp) y enlaces. Lo demás va como texto.
const PATRON_FORMATO = /(\*\*[^*\n]+\*\*|\*[^*\n]+\*|https?:\/\/[^\s<>()]+)/g;

/** Texto del agente -> nodos del DOM con negritas y enlaces. Se construye
 * con nodos (nunca innerHTML): el texto del modelo jamás se interpreta como
 * HTML, así que no puede inyectar nada en la página. */
function formatearTexto(texto) {
  const fragmento = document.createDocumentFragment();
  for (const parte of texto.split(PATRON_FORMATO)) {
    if (!parte) continue;
    if (/^https?:\/\//.test(parte)) {
      const enlace = document.createElement("a");
      enlace.href = parte;
      enlace.textContent = parte;
      enlace.target = "_blank";
      enlace.rel = "noopener noreferrer";
      fragmento.appendChild(enlace);
    } else if (parte.length > 2 && parte.startsWith("*") && parte.endsWith("*")) {
      // Solo los tokens capturados por PATRON_FORMATO empiezan y terminan en "*".
      const negrita = document.createElement("strong");
      negrita.textContent = parte.replace(/^\*+|\*+$/g, "");
      fragmento.appendChild(negrita);
    } else {
      fragmento.appendChild(document.createTextNode(parte));
    }
  }
  return fragmento;
}

function crearBurbuja(quien, texto) {
  const div = document.createElement("div");
  div.className = `burbuja burbuja-${quien}`;
  // Lo que escribe el cliente se muestra tal cual; solo se formatea al agente.
  if (quien === "cliente") div.textContent = texto;
  else div.appendChild(formatearTexto(texto));
  return div;
}

function renderMensajes() {
  el.mensajes.innerHTML = "";
  const historial = transcripts[sesionActual] || [];
  for (const m of historial) {
    el.mensajes.appendChild(crearBurbuja(m.quien, m.texto));
  }
  el.mensajes.scrollTop = el.mensajes.scrollHeight;
}

/** Agrega un mensaje a la conversación de `sesion` (por defecto la abierta).
 * Solo repinta si esa conversación es la que se está viendo: una respuesta
 * que llega tarde nunca se pega en otra conversación. */
function agregarMensaje(quien, texto, sesion = sesionActual) {
  if (!transcripts[sesion]) transcripts[sesion] = [];
  transcripts[sesion].push({ quien, texto });
  guardarTranscripts(transcripts);
  if (sesion === sesionActual) renderMensajes();
}

function renderSesionesConocidas() {
  const ids = Object.keys(transcripts);
  el.sesionesLista.innerHTML = "";
  for (const id of ids) {
    const li = document.createElement("li");
    li.textContent = id;
    li.className = id === sesionActual ? "activa" : "";
    li.addEventListener("click", () => cambiarSesion(id));
    el.sesionesLista.appendChild(li);
  }
}

function cambiarSesion(sessionId) {
  sesionActual = (sessionId || "").trim() || SESION_POR_DEFECTO;
  el.sessionInput.value = sesionActual;
  el.chatSessionLabel.textContent = `Sesión: ${sesionActual}`;
  if (!transcripts[sesionActual]) transcripts[sesionActual] = [];
  renderMensajes();
  renderSesionesConocidas();
}

async function enviarMensaje(mensaje) {
  // La respuesta pertenece a la sesión que preguntó, aunque mientras se
  // espera se cambie a otra conversación (bug real: la respuesta se pegaba
  // en la conversación abierta en ese momento y el cliente no la veía).
  const sesion = sesionActual;
  agregarMensaje("cliente", mensaje, sesion);

  const indicador = crearBurbuja("bot", "TecniElectronics está escribiendo…");
  indicador.classList.add("escribiendo");
  el.mensajes.appendChild(indicador);
  el.mensajes.scrollTop = el.mensajes.scrollHeight;
  el.input.disabled = true;

  try {
    const resp = await apiFetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sesion, mensaje }),
    });
    if (resp.status === 429) {
      const datos = await resp.json().catch(() => ({}));
      throw new Error(`Límite de mensajes alcanzado. ${datos.detail || ""}`.trim());
    }
    if (!resp.ok) {
      throw new Error(`El servidor respondió ${resp.status}`);
    }
    const datos = await resp.json();
    indicador.remove();
    agregarMensaje("bot", datos.respuesta, sesion);
  } catch (err) {
    indicador.remove();
    agregarMensaje("bot", `⚠️ No se pudo obtener respuesta: ${err.message}`, sesion);
  } finally {
    el.input.disabled = false;
    el.input.focus();
  }
}

el.form.addEventListener("submit", (evento) => {
  evento.preventDefault();
  const texto = el.input.value.trim();
  if (!texto) return;
  el.input.value = "";
  enviarMensaje(texto);
});

el.input.addEventListener("keydown", (evento) => {
  if (evento.key === "Enter" && !evento.shiftKey) {
    evento.preventDefault();
    el.form.requestSubmit();
  }
});

el.sessionSwitchBtn.addEventListener("click", () => {
  cambiarSesion(el.sessionInput.value);
});

el.newSessionBtn.addEventListener("click", () => {
  const nueva = prompt("Teléfono del nuevo cliente a simular:", "");
  if (nueva && nueva.trim()) {
    cambiarSesion(nueva.trim());
  }
});

/** Al cargar: intenta recuperar los session_id que el backend ya conoce (por
 * si este navegador no tenía nada en localStorage pero el proceso de
 * FastAPI sí tiene conversaciones activas de otra pestaña/dispositivo), y
 * arranca en la primera sesión disponible o en la de por defecto. */
async function iniciar() {
  try {
    const resp = await apiFetch("/api/sesiones");
    if (resp.ok) {
      const datos = await resp.json();
      for (const id of datos.sesiones || []) {
        if (!transcripts[id]) transcripts[id] = [];
      }
    }
  } catch {
    // Sin conexión al backend todavía; se sigue solo con lo que haya en
    // localStorage — no es un error fatal para pintar la pantalla.
  }

  const idsConocidos = Object.keys(transcripts);
  cambiarSesion(idsConocidos[0] || SESION_POR_DEFECTO);
}

iniciar();
