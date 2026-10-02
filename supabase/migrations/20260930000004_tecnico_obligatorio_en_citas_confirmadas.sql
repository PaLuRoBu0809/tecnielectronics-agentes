-- Cierra un hueco del constraint anti-choque por técnico (migración 003).
-- Ver docs/PLAN_DE_MEJORAS.md, Fase 10.2.
--
-- El constraint es:
--     exclude using gist (tecnico_id with =, periodo with &&)
--     where (estado = 'confirmado')
-- En un EXCLUDE, NULL nunca es "igual" a nada: dos citas confirmadas con
-- tecnico_id NULL que se solapan NO chocan. Las citas creadas antes de la
-- migración 003 quedaron con tecnico_id NULL, así que no estaban protegidas.
--
-- Esta migración:
--   1) completa tecnico_id en esas citas con el técnico de su servicio;
--   2) exige tecnico_id en toda cita CONFIRMADA (las canceladas antiguas
--      pueden quedar sin técnico: no participan del constraint).
--
-- Si falla con "Hay N citas confirmadas sin técnico": esos servicios no
-- tienen técnico asignado en servicios_tecnicos. Asígnalos y vuelve a
-- aplicar la migración (se ejecuta en una transacción: no queda a medias).
-- Si falla con una violación de exclusión (23P01): dos citas antiguas del
-- mismo técnico se solapan; hay que reprogramar o cancelar una a mano.

create extension if not exists btree_gist;

-- 1) Backfill. La comparación como texto evita depender de si servicio_id
--    es text o bigint en el esquema real.
update public.servicios_agendados sa
set tecnico_id = st.tecnico_id
from public.servicios_tecnicos st
where sa.tecnico_id is null
  and st.tecnico_id is not null
  and st.id::text = sa.servicio_id::text;

-- 2) Mensaje claro antes de validar, en vez del error genérico del CHECK.
do $$
declare
    pendientes integer;
begin
    select count(*) into pendientes
    from public.servicios_agendados
    where estado = 'confirmado' and tecnico_id is null;
    if pendientes > 0 then
        raise exception 'Hay % citas confirmadas sin técnico: asigna tecnico_id a sus servicios en servicios_tecnicos y vuelve a aplicar esta migración.', pendientes;
    end if;
end $$;

-- 3) Toda cita confirmada debe tener técnico. NOT VALID + VALIDATE evita
--    bloquear la tabla más de lo necesario mientras se revisan las filas.
alter table public.servicios_agendados
    drop constraint if exists cita_confirmada_con_tecnico;

alter table public.servicios_agendados
    add constraint cita_confirmada_con_tecnico
    check (estado is distinct from 'confirmado' or tecnico_id is not null) not valid;

alter table public.servicios_agendados
    validate constraint cita_confirmada_con_tecnico;
