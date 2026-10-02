-- Prepara servicios_agendados para ser la ÚNICA fuente de verdad de citas
-- (ya no se depende de Google Calendar para calcular disponibilidad ni para
-- prevenir choques de horario). Ver README.md, sección "Se quitó Google
-- Calendar: Supabase es la única fuente de verdad".

-- 1) Índice para la consulta de disponibilidad (equivalente a lo que antes
--    resolvía {Consultar_eventos} contra la API de Calendar): rango de
--    fechas, solo citas activas.
create index if not exists idx_servicios_agendados_fecha_confirmado
    on public.servicios_agendados (fecha_hora_inicio)
    where estado = 'confirmado';

-- 2) Índice para el historial de un cliente ({Consultar_servicio_agendado}):
--    ya hacía falta antes de esta migración, independientemente de Calendar.
create index if not exists idx_servicios_agendados_session_fecha
    on public.servicios_agendados (session_id, fecha_hora_inicio desc);

-- 3) Restricción de NO SOLAPAMIENTO a nivel de base de datos: garantiza que
--    dos citas "confirmado" nunca puedan cruzarse en el tiempo, aunque dos
--    conversaciones intenten agendar el mismo horario casi simultáneamente.
--    Esto es más fuerte que cualquier verificación que haga el LLM leyendo
--    texto — lo hace el motor de la base de datos, de forma atómica.
create extension if not exists btree_gist;

alter table public.servicios_agendados
    add column if not exists periodo tstzrange
    generated always as (tstzrange(fecha_hora_inicio, fecha_hora_fin, '[)')) stored;

alter table public.servicios_agendados
    drop constraint if exists no_solapamiento_citas_confirmadas;

alter table public.servicios_agendados
    add constraint no_solapamiento_citas_confirmadas
    exclude using gist (periodo with &&) where (estado = 'confirmado');
