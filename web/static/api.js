/**
 * api.js — acceso autenticado al backend, compartido por chat.js, admin.js
 * y flujo.js (ver docs/PLAN_DE_MEJORAS.md, Fase 1.2 y "Secretos").
 *
 * Si el servidor tiene API_KEY configurada, las rutas /api/* exigen
 * autenticación. El panel la resuelve así:
 *   1. La primera petición recibe 401.
 *   2. Se pide la clave UNA vez y se envía a POST /api/panel/sesion en la
 *      cabecera X-API-Key.
 *   3. El servidor responde con una cookie HttpOnly (un token firmado que
 *      caduca, no la clave). Desde ahí el navegador la manda sola en cada
 *      petición, incluido el stream SSE.
 * La clave NO se guarda en ningún lado del navegador ni viaja en una URL.
 * Si el servidor corre sin API_KEY (modo desarrollo), nunca se pide nada.
 */

// Versiones anteriores guardaban la clave en localStorage: se borra.
try {
  localStorage.removeItem("tecnielectronics_api_key");
} catch {
  // Sin acceso a localStorage no hay nada que borrar.
}

const oyentesSesion = [];
let iniciandoSesion = null;

/** Registra una función a ejecutar cuando se abre la sesión (ej. flujo.js
 * reconecta el stream SSE, que no se reintenta solo tras un 401). */
export function alIniciarSesion(funcion) {
  oyentesSesion.push(funcion);
}

/** Formulario de acceso dentro de la página (en vez de window.prompt, que el
 * navegador puede bloquear sin avisar si alguna vez se marcó "impedir que
 * esta página cree cuadros de diálogo"). Resuelve `true` cuando la sesión
 * queda abierta; se queda visible mientras la clave sea incorrecta. */
function abrirSesion() {
  return new Promise((resolver) => {
    const fondo = document.createElement("div");
    fondo.setAttribute("role", "dialog");
    fondo.setAttribute("aria-modal", "true");
    fondo.style.cssText =
      "position:fixed;inset:0;z-index:9999;display:flex;align-items:center;justify-content:center;" +
      "background:rgba(15,23,42,.55);padding:16px;font-family:inherit";
    fondo.innerHTML = `
      <form style="background:#fff;color:#111827;border-radius:12px;padding:24px;width:100%;max-width:360px;
                   box-shadow:0 20px 40px rgba(0,0,0,.25);display:flex;flex-direction:column;gap:12px">
        <strong style="font-size:16px">Acceso al panel</strong>
        <label style="font-size:13px;color:#4b5563">Clave de API (valor de API_KEY en tu .env)
          <input type="password" autocomplete="current-password" required
                 style="margin-top:6px;width:100%;box-sizing:border-box;padding:10px;border:1px solid #d1d5db;
                        border-radius:8px;font-size:14px">
        </label>
        <p data-error style="margin:0;color:#b91c1c;font-size:13px;min-height:1em"></p>
        <button type="submit" style="padding:10px;border:0;border-radius:8px;background:#111827;color:#fff;
                                     font-size:14px;cursor:pointer">Entrar</button>
      </form>`;
    const form = fondo.querySelector("form");
    const input = fondo.querySelector("input");
    const error = fondo.querySelector("[data-error]");
    const boton = fondo.querySelector("button");

    form.addEventListener("submit", async (evento) => {
      evento.preventDefault();
      const clave = input.value.trim();
      if (!clave) return;
      boton.disabled = true;
      error.textContent = "";
      try {
        const resp = await fetch("/api/panel/sesion", { method: "POST", headers: { "X-API-Key": clave } });
        if (!resp.ok) {
          error.textContent = resp.status === 401 ? "Clave incorrecta." : `Error del servidor (HTTP ${resp.status}).`;
          input.select();
          return;
        }
        fondo.remove();
        for (const funcion of oyentesSesion) funcion();
        resolver(true);
      } catch {
        error.textContent = "No se pudo contactar al servidor. ¿Está corriendo uvicorn?";
      } finally {
        boton.disabled = false;
      }
    });

    document.body.appendChild(fondo);
    input.focus();
  });
}

// Si varias peticiones reciben 401 a la vez, se muestra un solo prompt.
function iniciarSesion() {
  if (!iniciandoSesion) {
    iniciandoSesion = abrirSesion().finally(() => {
      iniciandoSesion = null;
    });
  }
  return iniciandoSesion;
}

/** fetch() que, ante un 401, abre la sesión del panel y reintenta una vez. */
export async function apiFetch(url, opciones = {}) {
  let resp = await fetch(url, opciones);
  if (resp.status === 401 && (await iniciarSesion())) {
    resp = await fetch(url, opciones);
  }
  return resp;
}
