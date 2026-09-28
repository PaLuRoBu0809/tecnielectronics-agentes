-- Memoria conversacional persistente de los agentes (reemplaza el dict en RAM
-- que usaba sesiones.py). Una fila por session_id (teléfono del cliente), con
-- el historial de mensajes de cada agente en su propio hilo, igual que exige
-- agents.orquestador.run().
--
-- RLS habilitado SIN políticas a propósito: los historiales contienen datos
-- personales del cliente, así que la key `anon` no puede leerlos ni
-- escribirlos. El backend usa la service_role key, que ignora RLS.

create table if not exists public.conversaciones (
    session_id text primary key,
    historial_orquestador jsonb not null default '[]'::jsonb,
    historial_servicio_tecnico jsonb not null default '[]'::jsonb,
    creado_en timestamptz not null default now(),
    actualizado_en timestamptz not null default now()
);

alter table public.conversaciones enable row level security;
