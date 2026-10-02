-- Cierra el acceso de la llave pública (`anon`) a ventas
-- (docs/PLAN_DE_MEJORAS.md, Fase 12).
--
-- Antes, de la época de n8n, cualquiera con la llave anon podía:
--   - leer TODOS los pedidos (nombre, teléfono, dirección de cada cliente) y
--     crear pedidos directamente en la tabla, sin pasar por el agente;
--   - leer products completo, incluido cost_price (el margen del negocio),
--     por la tabla o por la función search_products.
--
-- Nada de este proyecto usa la llave anon: el backend usa service_role (que
-- no pasa por RLS) y el dashboard habla solo con el backend, protegido con
-- API_KEY. El Table Editor de Supabase tampoco se afecta (entra como admin).
--
-- RLS sigue ACTIVO en las cuatro tablas: sin políticas, anon y authenticated
-- no ven ni escriben ninguna fila.
--
-- n8n ya está retirado: la integración es 100 % FastAPI, así que nada
-- depende de estas políticas.

drop policy if exists "Permitir leer pedidos" on public.orders;
drop policy if exists "Permitir crear pedidos" on public.orders;
drop policy if exists "Permitir lectura pública en productos" on public.products;
drop policy if exists "Permitir lectura pública en categorías" on public.product_categories;

alter table public.orders enable row level security;
alter table public.products enable row level security;
alter table public.product_categories enable row level security;
alter table public.carrito_compras enable row level security;

-- Funciones de la época de n8n: search_products devuelve cost_price. Quedan
-- solo para el backend. to_regprocedure: si no existen (ej. la base de
-- prueba), no hay nada que cerrar.
do $$
declare
    funcion text;
    rol text;
begin
    foreach funcion in array array[
        'public.search_products(uuid, text, boolean, integer)',
        'public.get_categories(text)'
    ] loop
        continue when to_regprocedure(funcion) is null;
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
