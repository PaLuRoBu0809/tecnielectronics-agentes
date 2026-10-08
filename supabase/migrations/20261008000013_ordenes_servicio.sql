-- Servicio Técnico por día de entrega (docs/PLAN_DE_MEJORAS.md, Fase 13).
--
-- Antes el agente agendaba citas con hora y técnico (servicios_agendados,
-- tecnicos). En la realidad el cliente DEJA el equipo en la sede y la empresa
-- reparte el trabajo internamente. Ahora:
--   - el cliente elige el DÍA en que lleva el equipo (+ una hora aproximada,
--     solo informativa: no hay cupos);
--   - la empresa cambia el ESTADO del equipo desde el dashboard, y cada
--     cambio exige una nota y el nombre de quien la registra;
--   - el historial de notas (seguimiento) se puede ver y editar, y el agente
--     lo usa para contarle al cliente cómo va su equipo.
--
-- servicios_agendados y tecnicos NO se tocan: quedan como historial.
--
-- Reglas en Postgres (la garantía, igual que en ventas):
--   - el cliente solo modifica o cancela SUS órdenes y solo mientras están en
--     PENDIENTE_RECEPCION (todavía no llevó el equipo);
--   - nota y responsable son obligatorios en todo seguimiento.
-- Las reglas de calendario (días con atención, festivos, hora dentro del
-- horario) viven en tools/agenda_entregas.py; aquí solo se impide una fecha
-- pasada.
--
-- Errores de negocio: `raise exception 'CODIGO'` (ver migración 006).
-- Re-aplicable.

do $$ begin
    create type public.estado_orden_servicio as enum (
        'PENDIENTE_RECEPCION', 'RECIBIDO', 'EN_DIAGNOSTICO', 'EN_REPARACION',
        'LISTO_PARA_RECOGER', 'ENTREGADO', 'CANCELADO'
    );
exception when duplicate_object then null; end $$;

create table if not exists public.ordenes_servicio (
    numero integer generated always as identity primary key,
    session_id text not null,
    cliente_nombre text not null check (length(trim(cliente_nombre)) > 0),
    cliente_telefono text not null check (length(trim(cliente_telefono)) > 0),
    servicio_id integer not null references public.servicios_tecnicos(id),
    equipo text not null check (length(trim(equipo)) > 0),
    descripcion text not null check (length(trim(descripcion)) > 0),
    fecha_entrega date not null,
    hora_aproximada time,
    estado public.estado_orden_servicio not null default 'PENDIENTE_RECEPCION',
    creado_en timestamptz not null default now(),
    actualizado_en timestamptz not null default now()
);
create index if not exists idx_ordenes_servicio_session on public.ordenes_servicio (session_id, creado_en desc);
create index if not exists idx_ordenes_servicio_fecha on public.ordenes_servicio (fecha_entrega);

create table if not exists public.seguimiento_orden_servicio (
    id bigint generated always as identity primary key,
    numero integer not null references public.ordenes_servicio(numero) on delete cascade,
    -- Estado de la orden cuando se escribió la nota.
    estado public.estado_orden_servicio not null,
    nota text not null check (length(trim(nota)) > 0),
    responsable text not null check (length(trim(responsable)) > 0),
    creado_en timestamptz not null default now(),
    editado_en timestamptz,
    editado_por text
);
create index if not exists idx_seguimiento_orden_servicio on public.seguimiento_orden_servicio (numero, creado_en);

alter table public.ordenes_servicio enable row level security;
alter table public.seguimiento_orden_servicio enable row level security;

-- ---------------------------------------------------------------------------
-- Funciones
-- ---------------------------------------------------------------------------

create or replace function public.validar_nota_y_responsable(p_nota text, p_responsable text)
returns void
language plpgsql
immutable
set search_path = public
as $$
begin
    if length(trim(coalesce(p_nota, ''))) = 0 then
        raise exception 'NOTA_REQUERIDA';
    end if;
    if length(trim(coalesce(p_responsable, ''))) = 0 then
        raise exception 'RESPONSABLE_REQUERIDO';
    end if;
end;
$$;

-- Hoy en Colombia (el servidor de Supabase corre en UTC).
create or replace function public.hoy_colombia()
returns date
language sql
stable
as $$ select (now() at time zone 'America/Bogota')::date $$;

create or replace function public.crear_orden_servicio(
    p_session_id text,
    p_cliente_nombre text,
    p_cliente_telefono text,
    p_servicio_id integer,
    p_equipo text,
    p_descripcion text,
    p_fecha_entrega date,
    p_hora_aproximada time default null
)
returns public.ordenes_servicio
language plpgsql
set search_path = public
as $$
declare
    v_orden public.ordenes_servicio;
begin
    if not exists (select 1 from public.servicios_tecnicos where id = p_servicio_id) then
        raise exception 'SERVICIO_NO_EXISTE';
    end if;
    if p_fecha_entrega is null or p_fecha_entrega < public.hoy_colombia() then
        raise exception 'FECHA_PASADA';
    end if;
    insert into public.ordenes_servicio (
        session_id, cliente_nombre, cliente_telefono, servicio_id, equipo, descripcion,
        fecha_entrega, hora_aproximada
    ) values (
        p_session_id, trim(p_cliente_nombre), trim(p_cliente_telefono), p_servicio_id, trim(p_equipo),
        trim(p_descripcion), p_fecha_entrega, p_hora_aproximada
    )
    returning * into v_orden;
    insert into public.seguimiento_orden_servicio (numero, estado, nota, responsable)
    values (v_orden.numero, v_orden.estado, 'Orden creada por el cliente desde el chat.', 'Asistente virtual');
    return v_orden;
end;
$$;

-- Regla para que el CLIENTE cambie o cancele: la orden es suya y el equipo
-- todavía no llegó. Devuelve la orden bloqueada (FOR UPDATE).
create or replace function public.orden_servicio_modificable(p_numero integer, p_session_id text)
returns public.ordenes_servicio
language plpgsql
set search_path = public
as $$
declare
    v_orden public.ordenes_servicio;
begin
    select * into v_orden from public.ordenes_servicio
    where numero = p_numero and session_id = p_session_id
    for update;
    if not found then
        raise exception 'ORDEN_SERVICIO_NO_ENCONTRADA';
    end if;
    if v_orden.estado <> 'PENDIENTE_RECEPCION' then
        raise exception 'ORDEN_SERVICIO_NO_MODIFICABLE' using detail = v_orden.estado::text;
    end if;
    return v_orden;
end;
$$;

-- Cambios pedidos por el cliente. NULL = dejar igual. Deja en el seguimiento
-- qué cambió (valor anterior -> nuevo).
create or replace function public.modificar_orden_servicio(
    p_numero integer,
    p_session_id text,
    p_fecha_entrega date default null,
    p_hora_aproximada time default null,
    p_cliente_nombre text default null,
    p_cliente_telefono text default null,
    p_equipo text default null,
    p_descripcion text default null,
    p_servicio_id integer default null
)
returns public.ordenes_servicio
language plpgsql
set search_path = public
as $$
declare
    v_antes public.ordenes_servicio;
    v_orden public.ordenes_servicio;
    v_cambios text[] := '{}';
begin
    v_antes := public.orden_servicio_modificable(p_numero, p_session_id);
    if p_fecha_entrega is not null and p_fecha_entrega < public.hoy_colombia() then
        raise exception 'FECHA_PASADA';
    end if;
    if p_servicio_id is not null and not exists (select 1 from public.servicios_tecnicos where id = p_servicio_id) then
        raise exception 'SERVICIO_NO_EXISTE';
    end if;

    update public.ordenes_servicio set
        fecha_entrega = coalesce(p_fecha_entrega, fecha_entrega),
        hora_aproximada = coalesce(p_hora_aproximada, hora_aproximada),
        cliente_nombre = coalesce(trim(p_cliente_nombre), cliente_nombre),
        cliente_telefono = coalesce(trim(p_cliente_telefono), cliente_telefono),
        equipo = coalesce(trim(p_equipo), equipo),
        descripcion = coalesce(trim(p_descripcion), descripcion),
        servicio_id = coalesce(p_servicio_id, servicio_id),
        actualizado_en = now()
    where numero = p_numero
    returning * into v_orden;

    if v_orden.fecha_entrega is distinct from v_antes.fecha_entrega then
        v_cambios := v_cambios || format('día de entrega %s -> %s', v_antes.fecha_entrega, v_orden.fecha_entrega);
    end if;
    if v_orden.hora_aproximada is distinct from v_antes.hora_aproximada then
        v_cambios := v_cambios || format('hora aproximada %s -> %s',
            coalesce(to_char(v_antes.hora_aproximada, 'HH24:MI'), 'sin hora'), to_char(v_orden.hora_aproximada, 'HH24:MI'));
    end if;
    if v_orden.cliente_nombre is distinct from v_antes.cliente_nombre then
        v_cambios := v_cambios || format('nombre %s -> %s', v_antes.cliente_nombre, v_orden.cliente_nombre);
    end if;
    if v_orden.cliente_telefono is distinct from v_antes.cliente_telefono then
        v_cambios := v_cambios || format('teléfono %s -> %s', v_antes.cliente_telefono, v_orden.cliente_telefono);
    end if;
    if v_orden.equipo is distinct from v_antes.equipo then
        v_cambios := v_cambios || format('equipo %s -> %s', v_antes.equipo, v_orden.equipo);
    end if;
    if v_orden.descripcion is distinct from v_antes.descripcion then
        v_cambios := v_cambios || 'descripción del problema actualizada';
    end if;
    if v_orden.servicio_id is distinct from v_antes.servicio_id then
        v_cambios := v_cambios || format('servicio %s -> %s', v_antes.servicio_id, v_orden.servicio_id);
    end if;

    if array_length(v_cambios, 1) > 0 then
        insert into public.seguimiento_orden_servicio (numero, estado, nota, responsable)
        values (p_numero, v_orden.estado,
                'El cliente cambió desde el chat: ' || array_to_string(v_cambios, '; ') || '.', 'Asistente virtual');
    end if;
    return v_orden;
end;
$$;

create or replace function public.cancelar_orden_servicio(p_numero integer, p_session_id text)
returns public.ordenes_servicio
language plpgsql
set search_path = public
as $$
declare
    v_orden public.ordenes_servicio;
begin
    perform public.orden_servicio_modificable(p_numero, p_session_id);
    update public.ordenes_servicio set estado = 'CANCELADO', actualizado_en = now()
    where numero = p_numero
    returning * into v_orden;
    insert into public.seguimiento_orden_servicio (numero, estado, nota, responsable)
    values (p_numero, 'CANCELADO', 'Cancelada por el cliente desde el chat.', 'Asistente virtual');
    return v_orden;
end;
$$;

-- Dashboard: la empresa cambia el estado. Estado + nota en la misma
-- transacción: no existe un cambio de estado sin su nota.
create or replace function public.cambiar_estado_orden_servicio(
    p_numero integer,
    p_estado public.estado_orden_servicio,
    p_nota text,
    p_responsable text
)
returns public.ordenes_servicio
language plpgsql
set search_path = public
as $$
declare
    v_orden public.ordenes_servicio;
begin
    perform public.validar_nota_y_responsable(p_nota, p_responsable);
    select * into v_orden from public.ordenes_servicio where numero = p_numero for update;
    if not found then
        raise exception 'ORDEN_SERVICIO_NO_ENCONTRADA';
    end if;
    if v_orden.estado = p_estado then
        raise exception 'MISMO_ESTADO' using detail = p_estado::text;
    end if;
    update public.ordenes_servicio set estado = p_estado, actualizado_en = now()
    where numero = p_numero
    returning * into v_orden;
    insert into public.seguimiento_orden_servicio (numero, estado, nota, responsable)
    values (p_numero, p_estado, trim(p_nota), trim(p_responsable));
    return v_orden;
end;
$$;

create or replace function public.agregar_nota_orden_servicio(p_numero integer, p_nota text, p_responsable text)
returns public.seguimiento_orden_servicio
language plpgsql
set search_path = public
as $$
declare
    v_estado public.estado_orden_servicio;
    v_nota public.seguimiento_orden_servicio;
begin
    perform public.validar_nota_y_responsable(p_nota, p_responsable);
    select estado into v_estado from public.ordenes_servicio where numero = p_numero;
    if not found then
        raise exception 'ORDEN_SERVICIO_NO_ENCONTRADA';
    end if;
    insert into public.seguimiento_orden_servicio (numero, estado, nota, responsable)
    values (p_numero, v_estado, trim(p_nota), trim(p_responsable))
    returning * into v_nota;
    return v_nota;
end;
$$;

-- Editar una nota: se conserva quién la escribió y se marca quién la editó.
create or replace function public.editar_nota_orden_servicio(p_id bigint, p_nota text, p_responsable text)
returns public.seguimiento_orden_servicio
language plpgsql
set search_path = public
as $$
declare
    v_nota public.seguimiento_orden_servicio;
begin
    perform public.validar_nota_y_responsable(p_nota, p_responsable);
    update public.seguimiento_orden_servicio
    set nota = trim(p_nota), editado_en = now(), editado_por = trim(p_responsable)
    where id = p_id
    returning * into v_nota;
    if not found then
        raise exception 'NOTA_NO_ENCONTRADA';
    end if;
    return v_nota;
end;
$$;

-- ---------------------------------------------------------------------------
-- Permisos: solo el backend (service_role)
-- ---------------------------------------------------------------------------
do $$
declare
    funcion text;
    tabla text;
    rol text;
begin
    foreach funcion in array array[
        'public.validar_nota_y_responsable(text, text)',
        'public.hoy_colombia()',
        'public.crear_orden_servicio(text, text, text, integer, text, text, date, time)',
        'public.orden_servicio_modificable(integer, text)',
        'public.modificar_orden_servicio(integer, text, date, time, text, text, text, text, integer)',
        'public.cancelar_orden_servicio(integer, text)',
        'public.cambiar_estado_orden_servicio(integer, public.estado_orden_servicio, text, text)',
        'public.agregar_nota_orden_servicio(integer, text, text)',
        'public.editar_nota_orden_servicio(bigint, text, text)'
    ] loop
        execute format('revoke execute on function %s from public', funcion);
        foreach rol in array array['anon', 'authenticated'] loop
            if exists (select 1 from pg_roles where rolname = rol) then
                execute format('revoke execute on function %s from %I', funcion, rol);
            end if;
        end loop;
        if exists (select 1 from pg_roles where rolname = 'service_role') then
            execute format('grant execute on function %s to service_role', funcion);
        end if;
    end loop;

    foreach tabla in array array['public.ordenes_servicio', 'public.seguimiento_orden_servicio'] loop
        foreach rol in array array['anon', 'authenticated'] loop
            if exists (select 1 from pg_roles where rolname = rol) then
                execute format('revoke all on table %s from %I', tabla, rol);
            end if;
        end loop;
        if exists (select 1 from pg_roles where rolname = 'service_role') then
            execute format('grant select, insert, update, delete on table %s to service_role', tabla);
        end if;
    end loop;
end $$;
