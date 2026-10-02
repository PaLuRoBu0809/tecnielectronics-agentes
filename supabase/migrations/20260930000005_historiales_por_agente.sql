-- Memoria conversacional genérica por agente (docs/PLAN_DE_MEJORAS.md, Fase 11).
--
-- Antes: una columna por agente (historial_orquestador,
-- historial_servicio_tecnico). Agregar Agente_Ventas habría exigido otra
-- columna más y cambiar el código que lee/escribe cada una por nombre.
-- Ahora: una sola columna `historiales` = {"orquestador": [...],
-- "servicio_tecnico": [...], "ventas": [...], ...}. Un agente nuevo solo
-- agrega una clave; no hace falta ninguna migración.
--
-- ORDEN DE DESPLIEGUE: aplicar esta migración ANTES de desplegar el código de
-- la Fase 11 (ese código escribe en `historiales`). /ready responde 503 si la
-- columna no existe.
--
-- Las columnas viejas NO se borran aquí: quedan como respaldo para volver a
-- la versión anterior del código si hiciera falta. Se pueden eliminar en una
-- migración posterior, cuando la Fase 11 esté verificada en producción.

alter table public.conversaciones
    add column if not exists historiales jsonb not null default '{}'::jsonb;

-- Copia los historiales existentes a la estructura nueva (solo filas que aún
-- no se migraron, para poder re-aplicarla sin pisar datos nuevos).
update public.conversaciones
set historiales = jsonb_build_object(
    'orquestador', coalesce(historial_orquestador, '[]'::jsonb),
    'servicio_tecnico', coalesce(historial_servicio_tecnico, '[]'::jsonb)
)
where historiales = '{}'::jsonb;
