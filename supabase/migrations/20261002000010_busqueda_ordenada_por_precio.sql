-- Búsqueda ordenada por precio (docs/PLAN_DE_MEJORAS.md, Fase 12).
--
-- Problema real (prueba en el chat, 2026-10-01): el cliente pidió "los más
-- caros" y la búsqueda no sabía ordenar de mayor a menor. El agente adivinó
-- a partir de los rangos de las 8 marcas del resumen, pero una marca fuera
-- de ese top podía tener algo más caro.
--
-- `p_mostrar_siempre` (traer los más baratos aunque sean muchos) se reemplaza
-- por `p_orden`:
--   NULL          -> como antes: si son pocos se muestran; si son muchos,
--                    solo el resumen para afinar. Con presupuesto, de lo
--                    mejor que alcanza a lo más barato.
--   'mas_baratos' -> se muestran SIEMPRE, del más barato al más caro.
--   'mas_caros'   -> se muestran SIEMPRE, del más caro al más barato.
-- El resumen (total, rango, marcas) se devuelve igual en todos los casos.
--
-- Re-aplicable.

drop function if exists public.buscar_productos_venta(jsonb, uuid, numeric, integer, boolean);

create or replace function public.buscar_productos_venta(
    p_grupos jsonb,
    p_category_id uuid default null,
    p_precio_max numeric default null,
    p_umbral integer default 5,
    p_orden text default null
)
returns jsonb
language sql
stable
-- `extensions` en el search_path: unaccent(text) busca su diccionario ahí.
set search_path = public, extensions
as $$
    with coincidencias as (
        select p.id, p.name, p.description, p.sale_price, p.category_id,
               coalesce(nullif(trim(p.brand), ''), 'Otras marcas') as marca,
               -- Clave de orden: más caros primero si lo piden, o si hay
               -- presupuesto (lo mejor que alcanza); si no, más baratos.
               case when p_orden = 'mas_caros' or (p_orden is null and p_precio_max is not null)
                    then -p.sale_price else p.sale_price end as clave_orden
        from public.products p
        where (p_category_id is null or p.category_id = p_category_id)
          and coalesce(p.is_active, false)
          and p.stock > 0
          and (p_precio_max is null or p.sale_price <= p_precio_max)
          -- Ningún grupo puede quedar sin al menos una palabra que coincida.
          and not exists (
              select 1
              from jsonb_array_elements(coalesce(p_grupos, '[]'::jsonb)) as grupo
              where not exists (
                  select 1
                  from jsonb_array_elements_text(grupo) as palabra
                  where palabra <> ''
                    -- \m = inicio de palabra. La palabra se escapa: sus
                    -- símbolos (%, +, ., paréntesis...) se buscan literalmente.
                    and extensions.unaccent(p.name || ' ' || coalesce(p.description, '') || ' ' || p.code)
                        ~* ('\m' || regexp_replace(extensions.unaccent(palabra), '([.^$*+?()\[\]{}|\\-])', '\\\1', 'g'))
              )
          )
    ),
    resumen as (
        select count(*) as total, min(sale_price) as precio_min, max(sale_price) as precio_max
        from coincidencias
    ),
    marcas as (
        select marca, count(*) as cantidad, min(sale_price) as precio_min, max(sale_price) as precio_max
        from coincidencias
        group by marca
        order by count(*) desc, marca
        limit 8
    ),
    a_mostrar as (
        select *
        from coincidencias
        where p_orden in ('mas_baratos', 'mas_caros') or (select total from resumen) <= p_umbral
        order by clave_orden, name
        limit greatest(coalesce(p_umbral, 5), 1)
    )
    select jsonb_build_object(
        'total', r.total,
        'precio_min', r.precio_min,
        'precio_max', r.precio_max,
        'marcas', coalesce((
            select jsonb_agg(jsonb_build_object(
                       'marca', m.marca, 'cantidad', m.cantidad,
                       'precio_min', m.precio_min, 'precio_max', m.precio_max
                   ) order by m.cantidad desc, m.marca)
            from marcas m
        ), '[]'::jsonb),
        'productos', coalesce((
            select jsonb_agg(jsonb_build_object(
                       'id', a.id, 'name', a.name, 'description', a.description,
                       'sale_price', a.sale_price, 'category_id', a.category_id, 'marca', a.marca
                   ) order by a.clave_orden, a.name)
            from a_mostrar a
        ), '[]'::jsonb)
    )
    from resumen r;
$$;

-- Solo el backend (service_role), igual que el resto de funciones de ventas.
do $$
declare
    firma constant text := 'public.buscar_productos_venta(jsonb, uuid, numeric, integer, text)';
    rol text;
begin
    execute format('revoke execute on function %s from public', firma);
    foreach rol in array array['anon', 'authenticated'] loop
        if exists (select 1 from pg_roles where rolname = rol) then
            execute format('revoke execute on function %s from %I', firma, rol);
        end if;
    end loop;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        execute format('grant execute on function %s to service_role', firma);
    end if;
end $$;
