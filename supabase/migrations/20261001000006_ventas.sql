-- Base de datos del Agente de Ventas (docs/PLAN_DE_MEJORAS.md, Fase 12 — Fase A).
--
-- Las tablas product_categories, products, carrito_compras y orders ya
-- existían (época de n8n). Esta migración NO las crea: las endurece y agrega
-- las funciones atómicas que reemplazan el subflujo de n8n.
--
--   1) ENUM para Metodo_pago, payment_status y shipping_status, previa
--      normalización de los datos viejos ("en linea", "PAGA EN CASA", ...).
--      Un error de tipeo del agente ya no puede caer en silencio al flujo
--      equivocado: Postgres lo rechaza.
--   2) `items` guardado como texto con JSON adentro (órdenes de n8n desde la
--      27) se convierte a JSON real.
--   3) Columnas nuevas en orders: referencia y link de MercadoPago, si la
--      orden reservó stock, cuándo vence la reserva y qué estado de pago ya
--      se le notificó al cliente.
--   4) carrito_compras gana su clave primaria (session_id, producto_id).
--   5) products: el stock nunca puede quedar negativo.
--   6) Funciones (RPC) atómicas: buscar productos, agregar/fijar cantidad en
--      el carrito, crear orden desde el carrito, modificar datos, cancelar,
--      aplicar un estado de pago y liberar reservas vencidas.
--   7) Solo service_role puede ejecutar estas funciones (en Supabase, por
--      defecto, también podían anon y authenticated).
--   8) pg_cron ejecuta la liberación de reservas cada 15 minutos.
--
-- Stock: se descuenta al CREAR la orden (no al añadir al carrito), así un
-- carrito abandonado nunca encierra unidades. Una orden en línea que no se
-- paga antes de `reserva_expira_en` se cancela sola y devuelve su stock.
-- Las órdenes creadas por n8n nunca descontaron stock: quedan con
-- stock_reservado = false y cancelarlas NO devuelve unidades.
--
-- Errores de negocio: las funciones lanzan excepciones con un mensaje-código
-- en MAYÚSCULAS (ej. 'STOCK_INSUFICIENTE') y el detalle en DETAIL. PostgREST
-- los devuelve en `message`/`details`, y tools/*_repository.py los traduce a
-- texto para el agente.
--
-- Re-aplicable: cada paso es idempotente (ver tests/test_integracion_postgres.py).

create schema if not exists extensions;
create extension if not exists unaccent with schema extensions;

-- ---------------------------------------------------------------------------
-- 1) Tipos ENUM
-- ---------------------------------------------------------------------------
do $$ begin
    create type public.metodo_pago_orden as enum ('contra_entrega', 'en_linea');
exception when duplicate_object then null; end $$;

do $$ begin
    create type public.estado_pago_orden as enum ('PENDIENTE', 'APROBADO', 'RECHAZADO', 'CONTRAENTREGA');
exception when duplicate_object then null; end $$;

do $$ begin
    create type public.estado_envio_orden as enum ('PENDIENTE_DESPACHO', 'DESPACHADO', 'ENTREGADO', 'CANCELADO');
exception when duplicate_object then null; end $$;

-- ---------------------------------------------------------------------------
-- 2) Normalización de datos viejos (antes de cambiar los tipos)
-- ---------------------------------------------------------------------------
-- Los ::text hacen que estas comparaciones funcionen también al re-aplicar,
-- cuando las columnas ya son ENUM.
update public.orders set items = (items #>> '{}')::jsonb
where jsonb_typeof(items) = 'string';

update public.orders set "Metodo_pago" = 'en_linea'
where "Metodo_pago"::text = 'en linea';

update public.orders set payment_status = 'CONTRAENTREGA'
where payment_status::text = 'PAGA EN CASA';

-- La cancelación vive en shipping_status. Un pago "CANCELADO" de contra
-- entrega es CONTRAENTREGA; uno en línea nunca se completó: PENDIENTE.
-- (Literales sueltos, no un CASE: así sirven tanto si la columna es texto
-- como si ya es ENUM al re-aplicar.)
update public.orders set payment_status = 'CONTRAENTREGA'
where payment_status::text = 'CANCELADO' and "Metodo_pago"::text = 'contra_entrega';
update public.orders set payment_status = 'PENDIENTE'
where payment_status::text = 'CANCELADO';

-- Mensaje claro si queda algún valor que el ENUM no acepta.
do $$
declare
    invalidas text;
begin
    select string_agg(order_number::text, ', ') into invalidas
    from public.orders
    where ("Metodo_pago" is not null and "Metodo_pago"::text not in ('contra_entrega', 'en_linea'))
       or (payment_status is not null and payment_status::text not in ('PENDIENTE', 'APROBADO', 'RECHAZADO', 'CONTRAENTREGA'))
       or shipping_status::text not in ('PENDIENTE_DESPACHO', 'DESPACHADO', 'ENTREGADO', 'CANCELADO');
    if invalidas is not null then
        raise exception 'Órdenes con valores fuera del ENUM: %. Corrígelas y vuelve a aplicar la migración.', invalidas;
    end if;
end $$;

alter table public.orders
    alter column "Metodo_pago" type public.metodo_pago_orden using "Metodo_pago"::text::public.metodo_pago_orden,
    alter column payment_status type public.estado_pago_orden using payment_status::text::public.estado_pago_orden;

alter table public.orders alter column shipping_status drop default;
alter table public.orders
    alter column shipping_status type public.estado_envio_orden using shipping_status::text::public.estado_envio_orden;
alter table public.orders alter column shipping_status set default 'PENDIENTE_DESPACHO';

-- ---------------------------------------------------------------------------
-- 3) Columnas nuevas de orders
-- ---------------------------------------------------------------------------
alter table public.orders
    -- external_reference de la preferencia de MercadoPago (la genera Python).
    add column if not exists payment_reference uuid,
    add column if not exists payment_link text,
    -- true solo si esta orden descontó stock (las de n8n, no).
    add column if not exists stock_reservado boolean not null default false,
    -- Solo órdenes en línea: si no se paga antes, se cancela y libera stock.
    add column if not exists reserva_expira_en timestamptz,
    -- Último estado de pago que ya se le avisó al cliente (aprobado/rechazado).
    add column if not exists estado_pago_notificado public.estado_pago_orden;

create unique index if not exists orders_payment_reference_key on public.orders (payment_reference);
create index if not exists idx_orders_session on public.orders (session_id, created_at desc);

-- ---------------------------------------------------------------------------
-- 4) Clave primaria del carrito
-- ---------------------------------------------------------------------------
-- Si hubiera filas repetidas, se consolidan sumando cantidades (la misma
-- regla de "añadir dos veces suma").
do $$
begin
    if not exists (select 1 from pg_constraint where conname = 'carrito_compras_pkey') then
        create temporary table carrito_consolidado on commit drop as
            select session_id, producto_id, sum(cantidad)::integer as cantidad,
                   min(creado_en) as creado_en, max(actualizado_en) as actualizado_en
            from public.carrito_compras
            group by session_id, producto_id;
        delete from public.carrito_compras;
        insert into public.carrito_compras (session_id, producto_id, cantidad, creado_en, actualizado_en)
            select session_id, producto_id, cantidad, creado_en, actualizado_en from carrito_consolidado;
        alter table public.carrito_compras
            add constraint carrito_compras_pkey primary key (session_id, producto_id);
    end if;
end $$;

-- ---------------------------------------------------------------------------
-- 5) Stock nunca negativo
-- ---------------------------------------------------------------------------
alter table public.products drop constraint if exists products_stock_no_negativo;
alter table public.products add constraint products_stock_no_negativo check (stock >= 0) not valid;
alter table public.products validate constraint products_stock_no_negativo;

-- ---------------------------------------------------------------------------
-- 6) Funciones
-- ---------------------------------------------------------------------------

-- Búsqueda para el agente: solo activos con stock, de UNA categoría. Cada
-- palabra del término debe aparecer (sin importar tildes ni mayúsculas) en el
-- nombre, la descripción o el código. %, _ y \ se escapan: se buscan
-- literalmente. Nunca devuelve cost_price.
create or replace function public.buscar_productos_venta(
    p_category_id uuid,
    p_termino text,
    p_limite integer default 10
)
returns table (id uuid, name character varying, description text, sale_price numeric)
language sql
stable
-- `extensions` en el search_path: unaccent(text) busca su diccionario ahí.
set search_path = public, extensions
as $$
    select p.id, p.name, p.description, p.sale_price
    from public.products p
    where p.category_id = p_category_id
      and coalesce(p.is_active, false)
      and p.stock > 0
      and not exists (
          select 1
          from regexp_split_to_table(trim(coalesce(p_termino, '')), '\s+') as palabra
          where palabra <> ''
            and extensions.unaccent(p.name || ' ' || coalesce(p.description, '') || ' ' || p.code)
                not ilike '%' || replace(replace(replace(extensions.unaccent(palabra), '\', '\\'), '%', '\%'), '_', '\_') || '%'
      )
    order by p.name
    limit least(greatest(coalesce(p_limite, 10), 1), 20);
$$;

-- Añadir al carrito: si el producto ya está, SUMA la cantidad. Valida que el
-- total en el carrito no supere el stock (no reserva: eso pasa al crear la
-- orden). Devuelve la cantidad final del producto en el carrito.
create or replace function public.agregar_al_carrito(p_session_id text, p_producto_id uuid, p_cantidad integer)
returns integer
language plpgsql
set search_path = public
as $$
declare
    v_producto public.products%rowtype;
    v_en_carrito integer;
begin
    if p_cantidad is null or p_cantidad < 1 then
        raise exception 'CANTIDAD_INVALIDA';
    end if;
    select * into v_producto from public.products where id = p_producto_id;
    if not found or not coalesce(v_producto.is_active, false) then
        raise exception 'PRODUCTO_NO_DISPONIBLE';
    end if;
    select cantidad into v_en_carrito
    from public.carrito_compras
    where session_id = p_session_id and producto_id = p_producto_id
    for update;
    if coalesce(v_en_carrito, 0) + p_cantidad > v_producto.stock then
        raise exception 'STOCK_INSUFICIENTE'
            using detail = json_build_object('stock', v_producto.stock, 'en_carrito', coalesce(v_en_carrito, 0))::text;
    end if;
    insert into public.carrito_compras (session_id, producto_id, cantidad)
    values (p_session_id, p_producto_id, p_cantidad)
    on conflict (session_id, producto_id)
        do update set cantidad = public.carrito_compras.cantidad + excluded.cantidad, actualizado_en = now()
    returning cantidad into v_en_carrito;
    return v_en_carrito;
end;
$$;

-- Fijar la cantidad FINAL de un producto que ya está en el carrito (el agente
-- calcula ese valor). Para quitarlo del todo se borra la fila (Eliminar).
create or replace function public.fijar_cantidad_carrito(p_session_id text, p_producto_id uuid, p_cantidad integer)
returns integer
language plpgsql
set search_path = public
as $$
declare
    v_stock integer;
begin
    if p_cantidad is null or p_cantidad < 1 then
        raise exception 'CANTIDAD_INVALIDA';
    end if;
    perform 1 from public.carrito_compras
    where session_id = p_session_id and producto_id = p_producto_id
    for update;
    if not found then
        raise exception 'NO_ESTA_EN_CARRITO';
    end if;
    select stock into v_stock from public.products where id = p_producto_id and coalesce(is_active, false);
    if v_stock is null then
        raise exception 'PRODUCTO_NO_DISPONIBLE';
    end if;
    if p_cantidad > v_stock then
        raise exception 'STOCK_INSUFICIENTE' using detail = json_build_object('stock', v_stock)::text;
    end if;
    update public.carrito_compras set cantidad = p_cantidad, actualizado_en = now()
    where session_id = p_session_id and producto_id = p_producto_id;
    return p_cantidad;
end;
$$;

-- Reemplaza el subflujo "crear orden" de n8n, en UNA transacción: bloquea los
-- productos del carrito, verifica stock y total, descuenta stock, inserta la
-- orden y vacía el carrito. Si algo falla, no queda nada a medias.
-- p_total_esperado: el total con el que Python creó el link de pago; si los
-- precios cambiaron entre medias, se rechaza (TOTAL_CAMBIO). NULL = no comparar.
create or replace function public.crear_orden_desde_carrito(
    p_session_id text,
    p_customer_name text,
    p_customer_phone text,
    p_customer_address text,
    p_city text,
    p_metodo_pago public.metodo_pago_orden,
    p_total_esperado numeric default null,
    p_payment_reference uuid default null,
    p_payment_link text default null,
    p_reserva_expira_en timestamptz default null
)
returns public.orders
language plpgsql
set search_path = public
as $$
declare
    v_sin_stock text;
    v_items jsonb;
    v_total numeric;
    v_orden public.orders;
begin
    if p_metodo_pago is null then
        raise exception 'METODO_PAGO_REQUERIDO';
    end if;
    if p_metodo_pago = 'en_linea' and (p_payment_reference is null or p_payment_link is null or p_reserva_expira_en is null) then
        raise exception 'DATOS_PAGO_EN_LINEA_REQUERIDOS';
    end if;

    -- Orden fijo de bloqueo (por id de producto) para evitar interbloqueos.
    perform 1 from public.carrito_compras c
    where c.session_id = p_session_id
    for update;
    perform 1 from public.products p
    where p.id in (select producto_id from public.carrito_compras where session_id = p_session_id)
    order by p.id
    for update;

    select string_agg(p.name, ', ' order by p.name) into v_sin_stock
    from public.carrito_compras c
    join public.products p on p.id = c.producto_id
    where c.session_id = p_session_id
      and (not coalesce(p.is_active, false) or c.cantidad > p.stock);
    if v_sin_stock is not null then
        raise exception 'STOCK_INSUFICIENTE' using detail = v_sin_stock;
    end if;

    select jsonb_agg(jsonb_build_object(
               'producto_id', p.id,
               'nombre', p.name,
               'cantidad', c.cantidad,
               'precio_unitario', p.sale_price,
               'subtotal', p.sale_price * c.cantidad
           ) order by p.name),
           sum(p.sale_price * c.cantidad)
      into v_items, v_total
    from public.carrito_compras c
    join public.products p on p.id = c.producto_id
    where c.session_id = p_session_id;

    if v_items is null or v_total <= 0 then
        raise exception 'CARRITO_VACIO';
    end if;
    if p_total_esperado is not null and p_total_esperado <> v_total then
        raise exception 'TOTAL_CAMBIO' using detail = v_total::text;
    end if;

    update public.products p
    set stock = p.stock - c.cantidad, updated_at = now()
    from public.carrito_compras c
    where c.session_id = p_session_id and c.producto_id = p.id;

    insert into public.orders (
        customer_name, customer_phone, customer_address, city, items, total_amount,
        "Metodo_pago", payment_status, session_id,
        payment_reference, payment_link, stock_reservado, reserva_expira_en
    ) values (
        p_customer_name, p_customer_phone, p_customer_address, p_city, v_items, v_total,
        p_metodo_pago,
        case when p_metodo_pago = 'contra_entrega' then 'CONTRAENTREGA' else 'PENDIENTE' end::public.estado_pago_orden,
        p_session_id,
        p_payment_reference, p_payment_link, true,
        case when p_metodo_pago = 'en_linea' then p_reserva_expira_en end
    )
    returning * into v_orden;

    delete from public.carrito_compras where session_id = p_session_id;
    return v_orden;
end;
$$;

-- Regla del negocio para modificar o cancelar: la orden debe ser la ÚLTIMA
-- creada por ese cliente y seguir en PENDIENTE_DESPACHO. Devuelve la orden
-- bloqueada (FOR UPDATE) para que quien llama la cambie en la misma
-- transacción.
create or replace function public.orden_modificable(p_order_number integer, p_session_id text)
returns public.orders
language plpgsql
set search_path = public
as $$
declare
    v_orden public.orders;
    v_ultima integer;
begin
    select * into v_orden from public.orders
    where order_number = p_order_number and session_id = p_session_id
    for update;
    if not found then
        raise exception 'ORDEN_NO_ENCONTRADA';
    end if;
    select order_number into v_ultima from public.orders
    where session_id = p_session_id
    order by created_at desc, order_number desc
    limit 1;
    if v_ultima <> p_order_number then
        raise exception 'NO_ES_ULTIMA_ORDEN' using detail = v_ultima::text;
    end if;
    if v_orden.shipping_status <> 'PENDIENTE_DESPACHO' then
        raise exception 'ORDEN_NO_PENDIENTE' using detail = v_orden.shipping_status::text;
    end if;
    return v_orden;
end;
$$;

-- Solo datos personales. NULL = dejar el valor actual.
create or replace function public.modificar_datos_orden(
    p_order_number integer,
    p_session_id text,
    p_customer_name text default null,
    p_customer_phone text default null,
    p_customer_address text default null,
    p_city text default null
)
returns public.orders
language plpgsql
set search_path = public
as $$
declare
    v_orden public.orders;
begin
    perform public.orden_modificable(p_order_number, p_session_id);
    update public.orders set
        customer_name = coalesce(p_customer_name, customer_name),
        customer_phone = coalesce(p_customer_phone, customer_phone),
        customer_address = coalesce(p_customer_address, customer_address),
        city = coalesce(p_city, city),
        updated_at = now()
    where order_number = p_order_number
    returning * into v_orden;
    return v_orden;
end;
$$;

-- Devuelve al inventario el stock de una orden que lo había reservado.
create or replace function public.devolver_stock_orden(p_order_number integer)
returns void
language sql
set search_path = public
as $$
    update public.products p
    set stock = p.stock + (item ->> 'cantidad')::integer, updated_at = now()
    from public.orders o, jsonb_array_elements(o.items) as item
    where o.order_number = p_order_number
      and o.stock_reservado
      and p.id = (item ->> 'producto_id')::uuid;
    update public.orders set stock_reservado = false where order_number = p_order_number;
$$;

-- Cancelación por el cliente. Una orden con pago APROBADO no se cancela
-- aquí: el reembolso lo gestiona la empresa directamente.
create or replace function public.cancelar_orden(p_order_number integer, p_session_id text)
returns public.orders
language plpgsql
set search_path = public
as $$
declare
    v_orden public.orders;
begin
    v_orden := public.orden_modificable(p_order_number, p_session_id);
    if v_orden.payment_status = 'APROBADO' then
        raise exception 'PAGO_APROBADO';
    end if;
    perform public.devolver_stock_orden(p_order_number);
    update public.orders
    set shipping_status = 'CANCELADO', reserva_expira_en = null, updated_at = now()
    where order_number = p_order_number
    returning * into v_orden;
    return v_orden;
end;
$$;

-- La usa el webhook de MercadoPago (y la conciliación). Solo cambia la fila
-- si el estado es distinto y la orden no estaba ya APROBADA (los avisos se
-- repiten y llegan desordenados). Devuelve la fila SOLO si cambió: eso es lo
-- que decide si se notifica al cliente, así se notifica una sola vez.
create or replace function public.aplicar_estado_pago(p_payment_reference uuid, p_estado public.estado_pago_orden)
returns setof public.orders
language sql
set search_path = public
as $$
    update public.orders
    set payment_status = p_estado,
        -- Pagada: ya no vence. Rechazada: se conserva la reserva para reintentar.
        reserva_expira_en = case when p_estado = 'APROBADO' then null else reserva_expira_en end,
        updated_at = now()
    where payment_reference = p_payment_reference
      and payment_status is distinct from 'APROBADO'
      and payment_status is distinct from p_estado
    returning *;
$$;

-- Limpieza automática (pg_cron, cada 15 minutos):
--   - órdenes en línea sin pagar cuya reserva venció -> CANCELADO + stock de vuelta;
--   - carritos sin actividad en p_horas_carrito horas -> se borran.
-- SKIP LOCKED: si una orden está siendo pagada o cancelada en ese momento, se
-- deja para la siguiente pasada.
create or replace function public.liberar_reservas_vencidas(p_horas_carrito integer default 24)
returns jsonb
language plpgsql
set search_path = public
as $$
declare
    v_orden integer;
    v_ordenes integer := 0;
    v_carritos integer;
begin
    for v_orden in
        select order_number from public.orders
        where "Metodo_pago" = 'en_linea'
          and shipping_status = 'PENDIENTE_DESPACHO'
          and payment_status in ('PENDIENTE', 'RECHAZADO')
          and reserva_expira_en < now()
        for update skip locked
    loop
        perform public.devolver_stock_orden(v_orden);
        update public.orders
        set shipping_status = 'CANCELADO', reserva_expira_en = null, updated_at = now()
        where order_number = v_orden;
        v_ordenes := v_ordenes + 1;
    end loop;

    delete from public.carrito_compras
    where coalesce(actualizado_en, creado_en) < now() - make_interval(hours => p_horas_carrito);
    get diagnostics v_carritos = row_count;

    return jsonb_build_object('ordenes_canceladas', v_ordenes, 'items_carrito_borrados', v_carritos);
end;
$$;

-- ---------------------------------------------------------------------------
-- 7) Permisos: solo el backend (service_role) ejecuta estas funciones
-- ---------------------------------------------------------------------------
do $$
declare
    funcion text;
    rol text;
begin
    foreach funcion in array array[
        'public.buscar_productos_venta(uuid, text, integer)',
        'public.agregar_al_carrito(text, uuid, integer)',
        'public.fijar_cantidad_carrito(text, uuid, integer)',
        'public.crear_orden_desde_carrito(text, text, text, text, text, public.metodo_pago_orden, numeric, uuid, text, timestamptz)',
        'public.orden_modificable(integer, text)',
        'public.modificar_datos_orden(integer, text, text, text, text, text)',
        'public.devolver_stock_orden(integer)',
        'public.cancelar_orden(integer, text)',
        'public.aplicar_estado_pago(uuid, public.estado_pago_orden)',
        'public.liberar_reservas_vencidas(integer)'
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
end $$;

-- ---------------------------------------------------------------------------
-- 8) Programación con pg_cron (solo donde exista, ej. Supabase)
-- ---------------------------------------------------------------------------
do $$
begin
    if exists (select 1 from pg_available_extensions where name = 'pg_cron') then
        create extension if not exists pg_cron;
        -- Con el mismo nombre, cron.schedule actualiza el trabajo existente.
        perform cron.schedule(
            'liberar-reservas-vencidas',
            '*/15 * * * *',
            'select public.liberar_reservas_vencidas(24)'
        );
    end if;
end $$;
