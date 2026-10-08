-- Seguimiento de pedidos desde el dashboard (docs/PLAN_DE_MEJORAS.md, Fase 13).
--
-- El mismo esquema de Servicio Técnico, para `orders`: la empresa cambia el
-- estado de ENVÍO (shipping_status) y cada cambio exige una nota y el nombre
-- de quien la registra. Las notas se ven y se editan desde el dashboard, y
-- el agente de ventas las lee al consultar el pedido.
--
-- El estado de PAGO no se toca aquí: lo maneja MercadoPago.
-- La columna vieja orders.notes queda como historial.
--
-- Todo cambio de shipping_status queda en el seguimiento gracias a un
-- trigger: los del dashboard con su nota y responsable, y los demás
-- (el cliente cancela por el chat, un pago en línea vence) con una nota
-- automática del sistema.
--
-- Re-aplicable.

create table if not exists public.seguimiento_pedido (
    id bigint generated always as identity primary key,
    order_number integer not null references public.orders(order_number) on delete cascade,
    -- Estado de envío del pedido cuando se escribió la nota.
    estado public.estado_envio_orden not null,
    nota text not null check (length(trim(nota)) > 0),
    responsable text not null check (length(trim(responsable)) > 0),
    creado_en timestamptz not null default now(),
    editado_en timestamptz,
    editado_por text
);
create index if not exists idx_seguimiento_pedido on public.seguimiento_pedido (order_number, creado_en);

alter table public.seguimiento_pedido enable row level security;

-- Registra cada cambio de shipping_status. Si el cambio lo hizo
-- cambiar_estado_pedido, la nota y el responsable llegan por variables de la
-- transacción (set_config(..., true): se borran al terminar).
create or replace function public.registrar_cambio_envio()
returns trigger
language plpgsql
set search_path = public
as $$
declare
    v_nota text := nullif(current_setting('seguimiento.nota', true), '');
    v_responsable text := nullif(current_setting('seguimiento.responsable', true), '');
begin
    if new.shipping_status is distinct from old.shipping_status then
        insert into public.seguimiento_pedido (order_number, estado, nota, responsable)
        values (
            new.order_number,
            new.shipping_status,
            coalesce(v_nota, case
                when new.shipping_status = 'CANCELADO' and new."Metodo_pago" = 'en_linea'
                     and old.payment_status in ('PENDIENTE', 'RECHAZADO') and old.reserva_expira_en < now()
                    then 'Cancelado automáticamente: el pago en línea no se completó a tiempo.'
                when new.shipping_status = 'CANCELADO'
                    then 'Cancelado por el cliente desde el chat.'
                else 'Cambio de estado registrado por el sistema.'
            end),
            coalesce(v_responsable, 'Sistema')
        );
    end if;
    return new;
end;
$$;

-- `after update` sin lista de columnas a propósito: con `update of
-- shipping_status`, Postgres no deja volver a aplicar la 006 (que cambia el
-- tipo de esa columna). La función ya ignora los updates que no la cambian.
drop trigger if exists trg_seguimiento_envio on public.orders;
create trigger trg_seguimiento_envio
    after update on public.orders
    for each row execute function public.registrar_cambio_envio();

-- Dashboard: cambia el estado de envío con nota y responsable obligatorios.
-- CANCELADO devuelve el stock (si el pedido lo había reservado). Un pedido
-- cancelado no se reactiva: su stock ya volvió al inventario.
create or replace function public.cambiar_estado_pedido(
    p_order_number integer,
    p_estado public.estado_envio_orden,
    p_nota text,
    p_responsable text
)
returns public.orders
language plpgsql
set search_path = public
as $$
declare
    v_orden public.orders;
begin
    perform public.validar_nota_y_responsable(p_nota, p_responsable);
    select * into v_orden from public.orders where order_number = p_order_number for update;
    if not found then
        raise exception 'ORDEN_NO_ENCONTRADA';
    end if;
    if v_orden.shipping_status = p_estado then
        raise exception 'MISMO_ESTADO' using detail = p_estado::text;
    end if;
    if v_orden.shipping_status = 'CANCELADO' then
        raise exception 'PEDIDO_CANCELADO';
    end if;
    if p_estado = 'CANCELADO' then
        perform public.devolver_stock_orden(p_order_number);
    end if;

    perform set_config('seguimiento.nota', trim(p_nota), true);
    perform set_config('seguimiento.responsable', trim(p_responsable), true);
    update public.orders set
        shipping_status = p_estado,
        -- Despachado o cancelado: la limpieza automática ya no debe tocarlo.
        reserva_expira_en = case when p_estado = 'PENDIENTE_DESPACHO' then reserva_expira_en end,
        updated_at = now()
    where order_number = p_order_number
    returning * into v_orden;
    perform set_config('seguimiento.nota', '', true);
    perform set_config('seguimiento.responsable', '', true);
    return v_orden;
end;
$$;

create or replace function public.agregar_nota_pedido(p_order_number integer, p_nota text, p_responsable text)
returns public.seguimiento_pedido
language plpgsql
set search_path = public
as $$
declare
    v_estado public.estado_envio_orden;
    v_nota public.seguimiento_pedido;
begin
    perform public.validar_nota_y_responsable(p_nota, p_responsable);
    select shipping_status into v_estado from public.orders where order_number = p_order_number;
    if not found then
        raise exception 'ORDEN_NO_ENCONTRADA';
    end if;
    insert into public.seguimiento_pedido (order_number, estado, nota, responsable)
    values (p_order_number, v_estado, trim(p_nota), trim(p_responsable))
    returning * into v_nota;
    return v_nota;
end;
$$;

create or replace function public.editar_nota_pedido(p_id bigint, p_nota text, p_responsable text)
returns public.seguimiento_pedido
language plpgsql
set search_path = public
as $$
declare
    v_nota public.seguimiento_pedido;
begin
    perform public.validar_nota_y_responsable(p_nota, p_responsable);
    update public.seguimiento_pedido
    set nota = trim(p_nota), editado_en = now(), editado_por = trim(p_responsable)
    where id = p_id
    returning * into v_nota;
    if not found then
        raise exception 'NOTA_NO_ENCONTRADA';
    end if;
    return v_nota;
end;
$$;

do $$
declare
    funcion text;
    rol text;
begin
    foreach funcion in array array[
        'public.registrar_cambio_envio()',
        'public.cambiar_estado_pedido(integer, public.estado_envio_orden, text, text)',
        'public.agregar_nota_pedido(integer, text, text)',
        'public.editar_nota_pedido(bigint, text, text)'
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

    foreach rol in array array['anon', 'authenticated'] loop
        if exists (select 1 from pg_roles where rolname = rol) then
            execute format('revoke all on table public.seguimiento_pedido from %I', rol);
        end if;
    end loop;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        grant select, insert, update, delete on table public.seguimiento_pedido to service_role;
    end if;
end $$;
