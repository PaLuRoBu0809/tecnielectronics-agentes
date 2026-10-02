-- Esquema base MÍNIMO para el test de integración de las migraciones
-- (tests/test_integracion_postgres.py). NO es una migración.
--
-- Las tablas servicios_tecnicos y servicios_agendados se crearon en Supabase
-- antes de este proyecto (en la época de n8n), así que ninguna migración de
-- supabase/migrations/ las crea. Esto las reproduce con las columnas que el
-- código usa y que el README documenta como verificadas contra el esquema
-- real (2026-09-27). Si el esquema real cambia, actualiza este archivo.

create table public.servicios_tecnicos (
    id bigint generated always as identity primary key,
    nombre text not null,
    descripcion text,
    duracion_minutos integer,
    precio numeric,
    activo boolean default true
);

create table public.servicios_agendados (
    -- Nombre real de la clave primaria (verificado 2026-09-30 contra Supabase).
    "Id_servicio_agendado" bigint generated always as identity primary key,
    cliente_nombre text,
    cliente_telefono text,
    servicio_id bigint references public.servicios_tecnicos(id),
    fecha_hora_inicio timestamptz not null,
    fecha_hora_fin timestamptz not null,
    google_calendar_event_id text,
    estado text default 'confirmado',
    "Descripcion" text,
    "Notas_servicio" text,
    session_id text,
    creado_en timestamptz default now()
);

-- Tablas de ventas, también de la época de n8n (verificadas 2026-10-01
-- contra Supabase): tipos, nulos y defaults reales, ANTES de la migración 006.
-- carrito_compras sin clave primaria y orders con estados en texto libre,
-- tal como estaban.
create table public.product_categories (
    id uuid primary key default gen_random_uuid(),
    code varchar not null unique,
    name varchar not null,
    created_at timestamptz default now()
);

create table public.products (
    id uuid primary key default gen_random_uuid(),
    code varchar not null unique,
    name varchar not null,
    description text,
    category_id uuid references public.product_categories(id) on delete set null,
    cost_price numeric not null default 0,
    sale_price numeric not null default 0,
    tax_percentage numeric not null default 19.00,
    stock integer not null default 0,
    unit_of_measure varchar default 'Unidad',
    is_active boolean default true,
    created_at timestamptz default now(),
    updated_at timestamptz default now()
);

create table public.carrito_compras (
    session_id text not null,
    producto_id uuid not null references public.products(id) on delete cascade,
    cantidad integer not null default 1 check (cantidad > 0),
    creado_en timestamptz default timezone('utc'::text, now()),
    actualizado_en timestamptz default timezone('utc'::text, now())
);

create sequence public.orders_contraentrega_order_number_seq;
create table public.orders (
    order_number integer primary key default nextval('public.orders_contraentrega_order_number_seq'),
    customer_name varchar not null,
    customer_phone varchar not null,
    customer_address text not null,
    city varchar not null,
    notes text,
    items jsonb not null,
    total_amount numeric not null,
    shipping_status varchar not null default 'PENDIENTE_DESPACHO',
    created_at timestamptz default now(),
    updated_at timestamptz default now(),
    "Metodo_pago" text,
    payment_status varchar,
    session_id text
);
create index idx_orders_shipping_status on public.orders (shipping_status);
