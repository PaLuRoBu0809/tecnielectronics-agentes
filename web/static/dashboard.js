/**
 * dashboard.js — cambia qué pestaña del dashboard está visible (Citas /
 * Ventas / Chat prueba / Flujo en vivo).
 *
 * Las 4 vistas YA están en el DOM desde que carga la página (ver
 * index.html) y sus scripts (chat.js, admin.js, flujo.js) corren igual sin
 * importar cuál esté visible — este archivo SOLO decide qué sección se
 * muestra (con el atributo `hidden`), nunca recarga ni reinicia nada.
 *
 * Script clásico (no `type="module"`): no declara ningún nombre que choque
 * con chat.js/admin.js/flujo.js, así que no necesita su propio scope
 * aislado.
 */

const botones = document.querySelectorAll(".tab-btn");
const vistas = document.querySelectorAll(".dashboard-vista");
const CLAVE_VISTA_ACTIVA = "tecnielectronics_vista_activa";

function activarVista(nombre) {
  for (const boton of botones) {
    boton.classList.toggle("activo", boton.dataset.tab === nombre);
  }
  for (const vista of vistas) {
    vista.hidden = vista.dataset.vista !== nombre;
  }
  try {
    localStorage.setItem(CLAVE_VISTA_ACTIVA, nombre);
  } catch {
    // Conveniencia de UI únicamente: si falla, el cambio de pestaña sigue
    // funcionando, solo no se recuerda cuál estaba activa al recargar.
  }
}

for (const boton of botones) {
  boton.addEventListener("click", () => activarVista(boton.dataset.tab));
}

let vistaInicial = "citas";
try {
  vistaInicial = localStorage.getItem(CLAVE_VISTA_ACTIVA) || "citas";
} catch {
  // Sin acceso a localStorage: se usa la pestaña por defecto (Citas).
}
activarVista(vistaInicial);
