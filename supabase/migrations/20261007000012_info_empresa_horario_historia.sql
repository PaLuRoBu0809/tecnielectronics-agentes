-- Horario, historia y dirección completa de la empresa (docs/PLAN_DE_MEJORAS.md, Fase 12).
--
-- Datos entregados y confirmados por el negocio el 2026-10-07.
--   - horario  (uso 'siempre'): va en el prompt del orquestador en cada mensaje.
--   - historia (uso 'bajo_demanda'): el orquestador la consulta con Info_empresa
--     solo cuando el cliente pregunta.
--   - ubicacion: se completa con la dirección exacta de la sede.
--
-- Re-aplicable: los inserts no pisan una fila que ya exista, y la dirección
-- solo se actualiza si sigue con el texto inicial de la migración 011 (si la
-- empresa ya la editó desde el Table Editor, no se toca).

insert into public.info_empresa (tema, titulo, contenido, uso) values
(
    'horario', 'Horario de atención',
    $$Atención al público en la sede física de Cartagena (Urb. La Gloria, Casa 1, Av. del Consulado):
- Lunes a viernes: 8:15 a.m. a 12:00 p.m. y de 2:00 p.m. a 5:45 p.m. (cerrado al mediodía).
- Sábados: 8:00 a.m. a 12:30 p.m.
- Domingos y festivos: cerrado.$$,
    'siempre'
),
(
    'historia', 'Historia de la empresa',
    $$1. Origen y fundación (1995)
La empresa nació oficialmente el 10 de julio de 1995 en Cartagena de Indias, Colombia. Fue fundada por el empresario Nolberto Antonio Redondo Iguarán, quien identificó una creciente necesidad en el mercado corporativo del Caribe colombiano: las empresas e instituciones locales carecían de un proveedor integral que unificara la tecnología informática con la infraestructura física y el mobiliario de oficina.

2. La era de la consolidación tecnológica y física
Durante sus primeros años, la compañía se enfocó en el suministro básico de hardware, software y consumibles de computación. Sin embargo, bajo la dirección de Nolberto Redondo, la empresa adoptó una visión de "solución llave en mano" para oficinas. En lugar de solo vender computadores, comenzaron a ofrecer servicios técnicos avanzados:
- Infraestructura de conectividad: diseño e instalación de redes de voz, datos y cableado estructurado eléctrico.
- Línea de mobiliario: montaje completo de puestos de trabajo, escritorios, estanterías pesadas y sillería ergonómica corporativa.

3. Diversificación y seguridad electrónica (década de 2010)
Con la llegada de las nuevas exigencias de seguridad corporativa, Tecnielectronis dio un paso estratégico al incorporar sistemas de seguridad electrónica. Esto expandió su negocio hacia el montaje de circuitos cerrados de televisión (CCTV), controles de acceso biométricos para personal y plantas telefónicas o eléctricas para garantizar la continuidad operativa de sus clientes. Además, se consolidaron como distribuidores mayoristas de marcas líderes de impresión y suministros, como Kyocera, HP y Ricoh.

4. Transformación digital y actualidad
En años recientes, Tecnielectronis evolucionó de ser un proveedor exclusivamente local a expandir sus operaciones a través del comercio electrónico mediante su portal oficial TecniElectronis. Implementaron alianzas con pasarelas de pago virtuales seguras (como GOU Pagos), lo que les permitió realizar envíos asegurados a nivel nacional, a toda Colombia.
A pesar de su crecimiento digital, la empresa mantiene su núcleo operativo en Cartagena, atendiendo directamente a entidades de salud, educación y empresas del sector industrial del norte del país, manteniéndose como una empresa familiar e institucional sólida con más de tres décadas de vigencia.$$,
    'bajo_demanda'
)
on conflict (tema) do nothing;

update public.info_empresa
set contenido = 'Cartagena, Bolívar. Urb. La Gloria, Casa 1, Av. del Consulado.',
    actualizado_en = now()
where tema = 'ubicacion' and contenido = 'Cartagena, Bolívar. Avenida El Consulado.';
