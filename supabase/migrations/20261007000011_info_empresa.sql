-- Conocimiento de la empresa para el orquestador (docs/PLAN_DE_MEJORAS.md, Fase 12).
--
-- Una fila por tema. La empresa la mantiene desde el Table Editor de
-- Supabase, sin tocar código; los cambios se notan en máximo 10 minutos
-- (caché en tools/info_empresa.py).
--
--   uso = 'siempre'      -> va en el prompt del orquestador en cada mensaje
--                          (datos cortos y frecuentes: contacto, redes...).
--   uso = 'bajo_demanda' -> el orquestador lo consulta con la tool
--                          Info_empresa solo cuando el cliente pregunta
--                          (textos largos: quiénes somos, políticas...).
--
-- Datos iniciales entregados por el negocio el 2026-10-07. El agente
-- responde SOLO con lo que está aquí: lo que falta (horario, historia de
-- fundación) no lo inventa y ofrece la línea de contacto.
--
-- Re-aplicable: `on conflict do nothing` no pisa lo que la empresa edite.

create table if not exists public.info_empresa (
    -- Identificador corto en minúsculas, ej. 'contacto', 'quienes_somos'.
    tema text primary key check (tema ~ '^[a-z0-9_]+$'),
    titulo text not null check (length(trim(titulo)) > 0),
    contenido text not null check (length(trim(contenido)) > 0),
    uso text not null default 'bajo_demanda' check (uso in ('siempre', 'bajo_demanda')),
    actualizado_en timestamptz not null default now()
);

alter table public.info_empresa enable row level security;

insert into public.info_empresa (tema, titulo, contenido, uso) values
(
    'nombre', 'Nombre oficial',
    'TECNIELECTRONIS & CIA SAS',
    'siempre'
),
(
    'contacto', 'Contacto y atención humana',
    $$Línea de atención: 301 208 6262.
Correo de ventas: ventaswebtecnielectronis@outlook.com
Correo para pagos, quejas y reclamos: contabilidad.tecnielectronis@hotmail.com$$,
    'siempre'
),
(
    'ubicacion', 'Dónde estamos',
    'Cartagena, Bolívar. Avenida El Consulado.',
    'siempre'
),
(
    'redes', 'Redes sociales',
    $$Instagram: https://www.instagram.com/tecnielectronis/
Facebook: https://www.facebook.com/tecnielectronis.com.co$$,
    'siempre'
),
(
    'por_que_elegirnos', 'Por qué elegirnos',
    $$Mejores precios del mercado. Envíos a toda Colombia, asegurados y rápidos. Garantía: respondemos por nuestros productos. Nuestras asesoras siempre están dispuestas a ayudar en todo lo que necesites.$$,
    'siempre'
),
(
    'medios_de_pago', 'Medios de pago',
    $$Por este chat: link de pago en línea de MercadoPago, o pago contra entrega.
En la tienda virtual (página web) los pagos se procesan con GOU Pagos: tarjetas de crédito Visa y MasterCard, y cuentas débito de ahorro y corriente por PSE.$$,
    'siempre'
),
(
    'quienes_somos', 'Quiénes somos',
    $$Es un orgullo para nosotros poner a su disposición nuestra empresa con el fin de suministrarle todo lo relacionado con equipos y muebles para su oficina, empresa o institución, como también partes y accesorios de computación, hardware y software; mantenimiento y reparación de computadores, redes de voz y datos, redes eléctricas; puestos de trabajo, escritorios, estantería, tándem, sillas, archivadores, etc. Sistemas de seguridad electrónica, circuitos cerrados de TV, control de acceso de personal; teléfonos, plantas telefónicas, plantas eléctricas. Importación y exportación de bienes y marcas en general.$$,
    'bajo_demanda'
),
(
    'pagos_tienda_web', 'Pagos en la tienda virtual (GOU Pagos)',
    $$GOU Pagos es la plataforma de pagos electrónicos que usa TECNIELECTRONIS & CIA SAS para procesar en línea las transacciones de la tienda virtual (página web). No aplica a los pedidos hechos por este chat, que se pagan con MercadoPago o contra entrega.
- Medios: tarjetas de crédito Visa y MasterCard, y cuentas débito de ahorro y corriente por PSE.
- Seguridad: la captura de los datos sensibles la hace GOU Pagos, que cumple la norma internacional PCI DSS, tiene certificado SSL (GeoTrust), monitoreo de McAfee Secure y firma de mensajes con Certicámara. Durante el pago el navegador muestra el nombre de la organización autenticada y la barra de dirección en verde.
- Horario: se puede pagar los 7 días de la semana, las 24 horas.
- Cambiar la forma de pago: posible mientras no se haya finalizado el pago; una vez finalizada la compra, no.
- Costo: pagar electrónicamente no genera costos adicionales para el comprador.
- Si la transacción no concluyó: revisar si llegó el correo de confirmación al correo registrado; si no llegó, contactar a Norberto Antonio Redondo Iguarán para confirmar el estado.
- Comprobante de pago: por cada transacción aprobada llega un comprobante con la referencia al correo registrado. Si no llega, contactar a Norberto Antonio Redondo Iguarán, a la línea 301 208 6262 o al correo contabilidad.tecnielectronis@hotmail.com para pedir el reenvío.$$,
    'bajo_demanda'
),
(
    'privacidad', 'Términos, condiciones y política de privacidad',
    $$Atención al cliente: línea 301 208 6262 o correo contabilidad.tecnielectronis@hotmail.com para cualquier pregunta, queja o reclamo.
- Recopilamos de manera segura y confidencial información personal como nombre, correo electrónico y otros datos relevantes.
- La almacenamos con medidas de seguridad adecuadas contra accesos no autorizados.
- La usamos para procesar pedidos, informar sobre productos y servicios, mejorar el sitio y dar un servicio personalizado.
- No compartimos datos personales con terceros, salvo cuando sea necesario por requisitos legales o para mejorar nuestros servicios.
- Al usar nuestros canales, el usuario da su consentimiento para el tratamiento de sus datos según esta política.
- El usuario tiene derecho a acceder, rectificar, cancelar u oponerse al tratamiento de sus datos, a través de la línea 301 208 6262 o el correo contabilidad.tecnielectronis@hotmail.com.
- La política puede actualizarse ocasionalmente.$$,
    'bajo_demanda'
)
on conflict (tema) do nothing;

-- Solo el backend (service_role) la lee y escribe; la llave pública no.
do $$
declare
    rol text;
begin
    foreach rol in array array['anon', 'authenticated'] loop
        if exists (select 1 from pg_roles where rolname = rol) then
            execute format('revoke all on table public.info_empresa from %I', rol);
        end if;
    end loop;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        grant select, insert, update, delete on table public.info_empresa to service_role;
    end if;
end $$;
