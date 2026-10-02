-- Sinónimos en la búsqueda de productos (docs/PLAN_DE_MEJORAS.md, Fase 12 — Fase B).
--
-- Problema: los clientes escriben "portátil", "celular" o "audífonos", y los
-- productos se llaman "Laptop", "Smartphone" o "Auriculares". Sin
-- sinónimos el agente responde "no tenemos" con el producto en bodega.
--
--   1) Tabla `sinonimos_busqueda`: una fila por GRUPO de palabras
--      equivalentes. La empresa la amplía desde el Table Editor, sin tocar
--      código. Las palabras se escriben en singular, sin tildes ni
--      mayúsculas (el código normaliza igual lo que escribe el cliente).
--   2) `buscar_productos_venta` pasa a recibir el término YA expandido por
--      Python (tools/inventario_repository.py) como grupos:
--          [["portatil", "laptop", "notebook"], ["hp"]]
--      Un producto coincide si, por CADA grupo, contiene AL MENOS UNA de sus
--      palabras (en nombre, descripción o código) al INICIO de una palabra:
--      "laptop" encuentra "Laptops", pero "ram" no encuentra "Program" ni
--      "pc" encontraría "PCIe" por accidente. La expansión vive en
--      Python a propósito: seguirá sirviendo cuando el inventario pase a
--      Siigo.
--   3) `p_category_id` pasa a ser opcional: NULL = todo el catálogo (plan B
--      del repositorio cuando la categoría no trae resultados).
--
-- Re-aplicable.

create table if not exists public.sinonimos_busqueda (
    id bigint generated always as identity primary key,
    palabras text[] not null check (cardinality(palabras) >= 2),
    creado_en timestamptz not null default now()
);

alter table public.sinonimos_busqueda enable row level security;

-- Grupos iniciales, pensados para los tipos de producto del catálogo actual.
-- `where not exists`: re-aplicar no duplica, y no pisa lo que la empresa
-- haya editado.
insert into public.sinonimos_busqueda (palabras)
select grupo
from (values
    (array['portatil', 'laptop', 'notebook', 'computador', 'computadora']),
    (array['celular', 'smartphone', 'telefono', 'movil']),
    (array['audifono', 'auricular', 'diadema', 'casco', 'headset']),
    (array['parlante', 'altavoz', 'bocina', 'speaker']),
    (array['mouse', 'raton']),
    (array['monitor', 'pantalla']),
    (array['tablet', 'tableta']),
    (array['camara', 'webcam']),
    (array['disco', 'ssd', 'hdd']),
    (array['memoria', 'ram']),
    (array['procesador', 'cpu']),
    (array['router', 'enrutador']),
    (array['teclado', 'keyboard']),
    (array['impresora', 'printer'])
) as semilla(grupo)
where not exists (select 1 from public.sinonimos_busqueda);

-- La firma anterior (uuid, text, integer) se reemplaza: solo la usaba este
-- proyecto y todavía ningún código en producción la llamaba.
drop function if exists public.buscar_productos_venta(uuid, text, integer);

create or replace function public.buscar_productos_venta(
    p_grupos jsonb,
    p_category_id uuid default null,
    p_limite integer default 5
)
returns table (id uuid, name character varying, description text, sale_price numeric, category_id uuid)
language sql
stable
-- `extensions` en el search_path: unaccent(text) busca su diccionario ahí.
set search_path = public, extensions
as $$
    select p.id, p.name, p.description, p.sale_price, p.category_id
    from public.products p
    where (p_category_id is null or p.category_id = p_category_id)
      and coalesce(p.is_active, false)
      and p.stock > 0
      -- Ningún grupo puede quedar sin al menos una palabra que coincida.
      and not exists (
          select 1
          from jsonb_array_elements(coalesce(p_grupos, '[]'::jsonb)) as grupo
          where not exists (
              select 1
              from jsonb_array_elements_text(grupo) as palabra
              where palabra <> ''
                -- \m = inicio de palabra. La palabra se escapa: sus símbolos
                -- (%, +, ., paréntesis...) se buscan literalmente.
                and extensions.unaccent(p.name || ' ' || coalesce(p.description, '') || ' ' || p.code)
                    ~* ('\m' || regexp_replace(extensions.unaccent(palabra), '([.^$*+?()\[\]{}|\\-])', '\\\1', 'g'))
          )
      )
    order by p.name
    limit least(greatest(coalesce(p_limite, 5), 1), 20);
$$;

-- Solo el backend (service_role), igual que el resto de funciones de ventas.
do $$
declare
    rol text;
begin
    revoke execute on function public.buscar_productos_venta(jsonb, uuid, integer) from public;
    foreach rol in array array['anon', 'authenticated'] loop
        if exists (select 1 from pg_roles where rolname = rol) then
            execute format('revoke execute on function public.buscar_productos_venta(jsonb, uuid, integer) from %I', rol);
            execute format('revoke all on table public.sinonimos_busqueda from %I', rol);
        end if;
    end loop;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        grant execute on function public.buscar_productos_venta(jsonb, uuid, integer) to service_role;
        grant select, insert, update, delete on table public.sinonimos_busqueda to service_role;
    end if;
end $$;
