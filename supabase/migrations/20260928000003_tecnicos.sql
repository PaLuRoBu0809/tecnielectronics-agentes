-- Agenda por técnico: cada tipo de servicio tiene un técnico responsable
-- asignado automáticamente por el agente, y la disponibilidad / el
-- constraint anti-choque de horarios pasan de ser GLOBALES a estar
-- ACOTADOS POR TÉCNICO — dos técnicos distintos sí pueden tener citas
-- confirmadas a la misma hora; el mismo técnico, no.
-- Ver README.md, sección "Agendamiento por técnico".

-- Necesaria para usar "tecnico_id WITH =" dentro de un EXCLUDE USING GIST.
-- La migración 002 ya la crea, pero se repite aquí (es idempotente) para que
-- esta migración no dependa de que la 002 se haya aplicado en el mismo
-- entorno. Ver docs/PLAN_DE_MEJORAS.md, Fase 10.1.
create extension if not exists btree_gist;

-- 1) Tabla de técnicos.
create table if not exists public.tecnicos (
    id bigint generated always as identity primary key,
    nombre text not null,
    telefono text,
    activo boolean not null default true
);

-- Datos de EJEMPLO para poder probar el flujo de inmediato — reemplázalos
-- por tus técnicos reales (por SQL o desde Supabase Studio) antes de producción.
insert into public.tecnicos (nombre, telefono, activo)
select * from (values
    ('Técnico de ejemplo 1', null::text, true),
    ('Técnico de ejemplo 2', null::text, true)
) as datos(nombre, telefono, activo)
where not exists (select 1 from public.tecnicos);

-- 2) Catálogo: qué técnico atiende cada tipo de servicio (1 técnico por
--    tipo de servicio, según se definió en la conversación de arquitectura).
alter table public.servicios_tecnicos
    add column if not exists tecnico_id bigint references public.tecnicos(id);

-- Asignación de EJEMPLO: todos los servicios sin técnico quedan con el
-- primer técnico de ejemplo, solo para que la demo funcione de inmediato.
-- AJUSTA esto manualmente a tu asignación real (qué técnico atiende cada
-- tipo de servicio) antes de producción.
update public.servicios_tecnicos
set tecnico_id = (select id from public.tecnicos order by id limit 1)
where tecnico_id is null;

-- 3) Citas: qué técnico quedó asignado a CADA cita puntual. Se llena solo
--    automáticamente al crear/actualizar la cita (ver tools/citas_tools.py,
--    resolver_tecnico_para_servicio) — el agente nunca lo pide ni lo inventa.
alter table public.servicios_agendados
    add column if not exists tecnico_id bigint references public.tecnicos(id);

create index if not exists idx_servicios_agendados_tecnico_fecha
    on public.servicios_agendados (tecnico_id, fecha_hora_inicio)
    where estado = 'confirmado';

-- 4) El constraint anti-choque de la migración 002 era GLOBAL (ninguna cita
--    confirmada podía solaparse con otra, sin importar el técnico). Se
--    reemplaza por uno acotado por tecnico_id: ahora dos citas confirmadas
--    solo "chocan" si son del MISMO técnico y se solapan en el tiempo.
--    btree_gist ya está instalado desde la migración 002 (permite usar "="
--    sobre tecnico_id dentro de un EXCLUDE USING GIST).
alter table public.servicios_agendados
    drop constraint if exists no_solapamiento_citas_confirmadas;

alter table public.servicios_agendados
    add constraint no_solapamiento_citas_confirmadas
    exclude using gist (tecnico_id with =, periodo with &&) where (estado = 'confirmado');
