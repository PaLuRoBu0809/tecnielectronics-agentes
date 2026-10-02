-- Búsqueda que ayuda a afinar (docs/PLAN_DE_MEJORAS.md, Fase 12 — Fase B).
--
-- Problema: "audífonos" encuentra 125 productos y mostrar 3 en orden
-- alfabético no ayuda a nadie. Cuando hay demasiados, el agente debe
-- preguntarle al cliente por marca o presupuesto ofreciendo opciones que
-- EXISTEN en bodega, en vez de mostrar productos al azar.
--
--   1) `products.brand`: la marca como dato propio. Hoy solo viene dentro de
--      la descripción ("Producto informático marca Acer, modelo Evo."), así
--      que se llena desde ahí. Cuando el inventario venga de Siigo, la
--      sincronización llenará esta columna con su campo de marca.
--   2) `buscar_productos_venta` devuelve un RESUMEN en JSON:
--        { total, precio_min, precio_max,
--          marcas: [{marca, cantidad, precio_min, precio_max}, ...],
--          productos: [...] }
--      `productos` solo trae filas si son pocas (total <= p_umbral) o si se
--      pide explícitamente (p_mostrar_siempre, cuando el cliente no quiere
--      afinar más). Si son muchas, viene vacío y el agente pregunta.
--   3) `p_precio_max`: el presupuesto del cliente. Con presupuesto, los
--      productos salen del más caro al más barato (lo mejor que alcanza);
--      sin presupuesto, del más barato al más caro.
--
-- Re-aplicable.

alter table public.products add column if not exists brand character varying;

-- Solo filas sin marca: re-aplicar no pisa una marca ya cargada.
update public.products
set brand = trim(substring(description from 'marca ([^,.]+)'))
where brand is null and description ~ 'marca [^,.]+';

drop function if exists public.buscar_productos_venta(jsonb, uuid, integer);

create or replace function public.buscar_productos_venta(
    p_grupos jsonb,
    p_category_id uuid default null,
    p_precio_max numeric default null,
    p_umbral integer default 5,
    p_mostrar_siempre boolean default false
)
returns jsonb
language sql
stable
-- `extensions` en el search_path: unaccent(text) busca su diccionario ahí.
set search_path = public, extensions
as $$
    with coincidencias as (
        select p.id, p.name, p.description, p.sale_price, p.category_id,
               coalesce(nullif(trim(p.brand), ''), 'Otras marcas') as marca
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
        where p_mostrar_siempre or (select total from resumen) <= p_umbral
        -- Con presupuesto: lo mejor que alcanza primero. Sin él: lo más barato.
        order by case when p_precio_max is null then sale_price else -sale_price end, name
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
                   ) order by case when p_precio_max is null then a.sale_price else -a.sale_price end, a.name)
            from a_mostrar a
        ), '[]'::jsonb)
    )
    from resumen r;
$$;

-- Solo el backend (service_role), igual que el resto de funciones de ventas.
do $$
declare
    firma constant text := 'public.buscar_productos_venta(jsonb, uuid, numeric, integer, boolean)';
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
