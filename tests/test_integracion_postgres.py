"""
tests/test_integracion_postgres.py

Fase 10.3 del plan de mejoras (`docs/PLAN_DE_MEJORAS.md`): aplica TODAS las
migraciones de `supabase/migrations/` sobre un PostgreSQL real y verifica el
constraint anti-choque por técnico y el CHECK de la migración 004. Los demás
tests mockean Supabase; este es el único que prueba el SQL de verdad.

De dónde sale el Postgres (en este orden):
1. `TEST_DATABASE_URL` (ej. el servicio `postgres` del CI): se usa esa base,
   que debe estar VACÍA.
2. Si no, un clúster desechable creado con `initdb` en una carpeta temporal
   (busca los binarios en `PG_BIN` o en las rutas habituales de Windows y
   Linux) y un puerto propio. No toca ningún Postgres existente.
3. Si no hay Postgres ni el paquete `psycopg`, el test se OMITE (sale con
   código 0 y lo dice), para no romper `correr_todos.py` en máquinas sin
   Postgres.

Corre con:
    python tests/test_integracion_postgres.py
"""
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
from tools.inventario_repository import expandir_termino  # noqa: E402
MIGRACIONES = sorted((RAIZ / "supabase" / "migrations").glob("*.sql"))
ESQUEMA_BASE = Path(__file__).resolve().parent / "integracion" / "esquema_base_prueba.sql"

try:
    import psycopg
    from psycopg import errors
except ImportError:
    print("⏭️  OMITIDO: falta el paquete psycopg (pip install 'psycopg[binary]').")
    sys.exit(0)


def _buscar_bin_postgres():
    candidatos = [os.environ.get("PG_BIN", "")]
    candidatos += sorted(glob.glob(r"C:\Program Files\PostgreSQL\*\bin"), reverse=True)
    candidatos += sorted(glob.glob("/usr/lib/postgresql/*/bin"), reverse=True)
    for carpeta in candidatos:
        if carpeta and (Path(carpeta) / ("initdb.exe" if os.name == "nt" else "initdb")).exists():
            return Path(carpeta)
    initdb = shutil.which("initdb")
    return Path(initdb).parent if initdb else None


class ClusterTemporal:
    PUERTO = 55432

    def __init__(self, bin_dir: Path):
        self.bin = bin_dir
        self.datos = Path(tempfile.mkdtemp(prefix="pg_prueba_"))

    def _ejecutar(self, programa, *args):
        # Sin capturar la salida (DEVNULL): el servidor que arranca `pg_ctl
        # start` hereda los descriptores del proceso, y si fueran tuberías
        # capturadas, subprocess esperaría para siempre un fin de archivo que
        # nunca llega mientras el servidor siga vivo. El log va a log.txt.
        subprocess.run([str(self.bin / programa), *args], check=True,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)

    def __enter__(self):
        self._ejecutar("initdb", "-D", str(self.datos), "-U", "postgres", "-A", "trust", "-E", "UTF8", "--no-locale")
        self._ejecutar("pg_ctl", "-D", str(self.datos), "-l", str(self.datos / "log.txt"),
                       "-o", f"-p {self.PUERTO}", "-w", "start")
        return f"postgresql://postgres@localhost:{self.PUERTO}/postgres"

    def __exit__(self, *exc):
        subprocess.run([str(self.bin / "pg_ctl"), "-D", str(self.datos), "-m", "fast", "-w", "stop"],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
        shutil.rmtree(self.datos, ignore_errors=True)


def _debe_fallar(conn, sql, error_esperado, mensaje):
    try:
        with conn.transaction():
            conn.execute(sql)
    except error_esperado:
        return
    raise AssertionError(mensaje)


def _cita(tecnico, inicio, fin, estado="confirmado", servicio=1):
    tecnico_sql = "null" if tecnico is None else str(tecnico)
    return (
        "insert into servicios_agendados (servicio_id, tecnico_id, fecha_hora_inicio, fecha_hora_fin, estado) "
        f"values ({servicio}, {tecnico_sql}, '2026-10-05 {inicio}-05', '2026-10-05 {fin}-05', '{estado}')"
    )


def _debe_fallar_con(conn, sql, codigo, mensaje):
    """Las funciones de ventas señalan errores de negocio con un código en
    el mensaje de la excepción (ej. 'STOCK_INSUFICIENTE')."""
    try:
        with conn.transaction():
            conn.execute(sql)
    except errors.RaiseException as exc:
        assert codigo in str(exc), f"{mensaje}: se esperaba {codigo}, llegó {exc}"
        return
    raise AssertionError(mensaje)


CATEGORIA = "00000000-0000-0000-0000-0000000000c1"
OTRA_CATEGORIA = "00000000-0000-0000-0000-0000000000c2"
PORTATIL = "00000000-0000-0000-0000-000000000001"  # stock 5
MOUSE = "00000000-0000-0000-0000-000000000002"  # stock 10, con % en el nombre
AGOTADO = "00000000-0000-0000-0000-000000000003"  # stock 0
INACTIVO = "00000000-0000-0000-0000-000000000004"
OTRA_CAT = "00000000-0000-0000-0000-000000000005"
DISCO = "00000000-0000-0000-0000-000000000006"


def _sembrar_ventas_antiguas(conn) -> None:
    """Datos con los defectos reales que dejó n8n, ANTES de la migración 006."""
    conn.execute(
        f"insert into product_categories (id, code, name) values "
        f"('{CATEGORIA}', 'LAP', 'Portátiles'), ('{OTRA_CATEGORIA}', 'AUD', 'Audio')"
    )
    conn.execute(
        "insert into products (id, code, name, description, category_id, cost_price, sale_price, stock, is_active) "
        "values "
        f"('{PORTATIL}', 'P1', 'Portátil HP Pavilion', 'Equipo de 16GB con programa de oficina', "
        f"'{CATEGORIA}', 2000000, 3000000, 5, true), "
        f"('{MOUSE}', 'P2', 'Mouse 100% inalámbrico', 'Mouse óptico', '{CATEGORIA}', 20000, 50000, 10, true), "
        f"('{AGOTADO}', 'P3', 'Portátil HP Agotado', null, '{CATEGORIA}', 1, 2, 0, true), "
        f"('{INACTIVO}', 'P4', 'Portátil HP Viejo', null, '{CATEGORIA}', 1, 2, 3, false), "
        f"('{OTRA_CAT}', 'P5', 'Audífonos HP', null, '{OTRA_CATEGORIA}', 1, 2, 3, true), "
        # La marca solo viene en la descripción, como en el catálogo actual (009 la extrae).
        f"('{DISCO}', 'P6', 'Disco Externo 1TB', 'Producto informático marca Western Digital, modelo X.', "
        f"'{OTRA_CATEGORIA}', 1, 2, 3, true)"
    )
    conn.execute(
        "insert into orders (order_number, customer_name, customer_phone, customer_address, city, items, "
        "total_amount, shipping_status, \"Metodo_pago\", payment_status, session_id) values "
        # items como TEXTO con JSON adentro, como desde la orden 27 de n8n.
        f"(27, 'Ana', '3001', 'Calle 1', 'Medellín', "
        f"to_jsonb('[{{\"producto_id\": \"{PORTATIL}\", \"cantidad\": 2}}]'::text), "
        "6000000, 'PENDIENTE_DESPACHO', 'contra_entrega', 'PAGA EN CASA', 'antiguo'), "
        "(28, 'Ana', '3001', 'Calle 1', 'Medellín', '[]', 1, 'CANCELADO', 'en linea', 'CANCELADO', 'antiguo'), "
        "(29, 'Ana', '3001', 'Calle 1', 'Medellín', '[]', 1, 'CANCELADO', 'contra_entrega', 'CANCELADO', 'antiguo'), "
        "(10, 'Ana', '3001', 'Calle 1', 'Medellín', '[]', 1, 'PENDIENTE_DESPACHO', null, null, null)"
    )
    conn.execute("select setval('orders_contraentrega_order_number_seq', 100)")
    # Sin clave primaria, n8n pudo dejar el mismo producto repetido.
    conn.execute(
        f"insert into carrito_compras (session_id, producto_id, cantidad) values "
        f"('dup', '{MOUSE}', 1), ('dup', '{MOUSE}', 2)"
    )


def _crear_orden(sesion, metodo="contra_entrega", total="null", expira="null", referencia="null", link="null"):
    return (
        f"select order_number from crear_orden_desde_carrito('{sesion}', 'Juan', '3001234567', 'Cra 1', 'Medellín', "
        f"'{metodo}', {total}, {referencia}, {link}, {expira})"
    )


def _stock(conn, producto):
    return conn.execute(f"select stock from products where id = '{producto}'").fetchone()[0]


def _probar_info_empresa(conn, migraciones) -> None:
    """Migraciones 011 y 012: información de la empresa."""
    filas = dict(conn.execute("select tema, uso from info_empresa").fetchall())
    assert filas["contacto"] == "siempre" and filas["quienes_somos"] == "bajo_demanda", filas
    assert filas["horario"] == "siempre" and filas["historia"] == "bajo_demanda", "La 012 agrega horario e historia"
    assert len(filas) == 11, filas
    ubicacion = conn.execute("select contenido from info_empresa where tema = 'ubicacion'").fetchone()[0]
    assert ubicacion == "Cartagena, Bolívar. Urb. La Gloria, Casa 1, Av. del Consulado.", ubicacion
    historia = conn.execute("select contenido from info_empresa where tema = 'historia'").fetchone()[0]
    assert "10 de julio de 1995" in historia and "Te gustaría" not in historia, "Sin la línea final del chatbot"

    conn.execute("update info_empresa set contenido = 'Nueva sede' where tema = 'ubicacion'")
    conn.execute(next(m for m in migraciones if "horario_historia" in m.name).read_text(encoding="utf-8"))
    assert conn.execute("select contenido from info_empresa where tema = 'ubicacion'").fetchone()[0] == "Nueva sede", (
        "Re-aplicar la 012 no debe pisar una dirección que la empresa ya editó"
    )
    _debe_fallar(conn, "insert into info_empresa (tema, titulo, contenido, uso) values ('x', 'X', 'algo', 'a veces')",
                 errors.CheckViolation, "uso solo acepta 'siempre' o 'bajo_demanda'")
    _debe_fallar(conn, "insert into info_empresa (tema, titulo, contenido) values ('Mi Tema', 'X', 'algo')",
                 errors.CheckViolation, "el tema va en minúsculas y sin espacios")
    _debe_fallar(conn, "insert into info_empresa (tema, titulo, contenido) values ('vacio', 'X', '   ')",
                 errors.CheckViolation, "el contenido no puede estar vacío")

    # La empresa edita un dato desde el Table Editor; re-aplicar la migración no lo pisa.
    conn.execute("update info_empresa set contenido = 'Línea: 300 000 0000' where tema = 'contacto'")
    conn.execute(next(m for m in migraciones if "info_empresa" in m.name).read_text(encoding="utf-8"))
    contenido = conn.execute("select contenido from info_empresa where tema = 'contacto'").fetchone()[0]
    assert contenido == "Línea: 300 000 0000", "Re-aplicar la 011 no debe pisar lo que editó la empresa"
    print("✅ 011 y 012: info_empresa con 11 temas (horario e historia), valida datos y no pisa ediciones.")


def _crear_orden_servicio(sesion, dias="1"):
    return (
        f"select numero from crear_orden_servicio('{sesion}', 'Ana Pérez', '3001112222', 1, 'Portátil Lenovo', "
        f"'No enciende', hoy_colombia() + {dias}, '09:30')"
    )


def _probar_ordenes_servicio(conn) -> None:
    """Migración 013: órdenes de servicio por día + seguimiento con nota y responsable."""
    _debe_fallar_con(conn, _crear_orden_servicio("s1", dias="-1"), "FECHA_PASADA", "No se agenda en el pasado")
    _debe_fallar_con(
        conn,
        "select crear_orden_servicio('s1', 'Ana', '300', 999, 'PC', 'x', hoy_colombia() + 1)",
        "SERVICIO_NO_EXISTE", "El servicio debe existir en el catálogo",
    )
    numero = conn.execute(_crear_orden_servicio("s1")).fetchone()[0]
    estado, hora = conn.execute(
        f"select estado::text, to_char(hora_aproximada, 'HH24:MI') from ordenes_servicio where numero = {numero}"
    ).fetchone()
    assert (estado, hora) == ("PENDIENTE_RECEPCION", "09:30"), (estado, hora)
    notas = conn.execute(f"select responsable from seguimiento_orden_servicio where numero = {numero}").fetchall()
    assert notas == [("Asistente virtual",)], "Crear deja la primera nota del seguimiento"

    _debe_fallar_con(conn, f"select modificar_orden_servicio({numero}, 'otro', hoy_colombia() + 2)",
                     "ORDEN_SERVICIO_NO_ENCONTRADA", "Un cliente no modifica la orden de otro")
    conn.execute(f"select modificar_orden_servicio({numero}, 's1', hoy_colombia() + 3, p_equipo => 'Portátil HP')")
    nota = conn.execute(
        f"select nota from seguimiento_orden_servicio where numero = {numero} order by id desc limit 1"
    ).fetchone()[0]
    assert "día de entrega" in nota and "equipo Portátil Lenovo -> Portátil HP" in nota, nota
    assert "hora aproximada" not in nota, "Solo se anota lo que cambió"

    # --- Dashboard ---
    _debe_fallar_con(conn, f"select cambiar_estado_orden_servicio({numero}, 'RECIBIDO', '  ', 'Laura')",
                     "NOTA_REQUERIDA", "Cambiar el estado exige una nota")
    _debe_fallar_con(conn, f"select cambiar_estado_orden_servicio({numero}, 'RECIBIDO', 'Llegó', '')",
                     "RESPONSABLE_REQUERIDO", "Cambiar el estado exige el responsable")
    _debe_fallar_con(conn, f"select cambiar_estado_orden_servicio({numero}, 'PENDIENTE_RECEPCION', 'x', 'Laura')",
                     "MISMO_ESTADO", "Cambiar al mismo estado no tiene sentido (para eso está Agregar nota)")
    conn.execute(f"select cambiar_estado_orden_servicio({numero}, 'RECIBIDO', 'Llegó con cargador', 'Laura')")
    _debe_fallar_con(conn, f"select modificar_orden_servicio({numero}, 's1', hoy_colombia() + 4)",
                     "ORDEN_SERVICIO_NO_MODIFICABLE", "Recibido el equipo, el cliente ya no cambia el día")
    _debe_fallar_con(conn, f"select cancelar_orden_servicio({numero}, 's1')",
                     "ORDEN_SERVICIO_NO_MODIFICABLE", "Recibido el equipo, el cliente ya no cancela por el chat")
    id_nota = conn.execute(f"select (agregar_nota_orden_servicio({numero}, 'Falla en la fuente', 'Pedro')).id"
                           ).fetchone()[0]
    conn.execute(f"select editar_nota_orden_servicio({id_nota}, 'Falla en la fuente de poder', 'Laura')")
    fila = conn.execute(
        f"select estado::text, nota, responsable, editado_por, editado_en is not null "
        f"from seguimiento_orden_servicio where id = {id_nota}"
    ).fetchone()
    assert fila == ("RECIBIDO", "Falla en la fuente de poder", "Pedro", "Laura", True), fila
    _debe_fallar_con(conn, "select editar_nota_orden_servicio(999999, 'x', 'y')", "NOTA_NO_ENCONTRADA",
                     "Editar una nota inexistente falla")

    otra = conn.execute(_crear_orden_servicio("s1")).fetchone()[0]
    conn.execute(f"select cancelar_orden_servicio({otra}, 's1')")
    assert conn.execute(f"select estado::text from ordenes_servicio where numero = {otra}").fetchone()[0] == \
        "CANCELADO"
    _debe_fallar(conn, "insert into seguimiento_orden_servicio (numero, estado, nota, responsable) "
                       f"values ({otra}, 'CANCELADO', 'x', ' ')",
                 errors.CheckViolation, "Ni por fuera de las funciones se guarda una nota sin responsable")
    print("✅ 013: órdenes de servicio por día; el cliente cambia o cancela solo las suyas y antes de llevar el "
          "equipo; el dashboard exige nota y responsable; las notas se editan dejando quién lo hizo.")


def _probar_seguimiento_pedidos(conn) -> None:
    """Migración 014: estado de envío con nota y responsable, y el trigger de seguimiento."""
    conn.execute(f"select agregar_al_carrito('p1', '{MOUSE}', 2)")
    pedido = conn.execute(_crear_orden("p1")).fetchone()[0]
    stock = _stock(conn, MOUSE)

    _debe_fallar_con(conn, f"select cambiar_estado_pedido({pedido}, 'DESPACHADO', '', 'Laura')",
                     "NOTA_REQUERIDA", "Cambiar el estado del pedido exige una nota")
    conn.execute(f"select cambiar_estado_pedido({pedido}, 'DESPACHADO', 'Guía Servientrega 123', 'Laura')")
    conn.execute(f"select agregar_nota_pedido({pedido}, 'Llega el viernes', 'Laura')")
    conn.execute(f"select cambiar_estado_pedido({pedido}, 'CANCELADO', 'Devuelto por la transportadora', 'Pedro')")
    assert _stock(conn, MOUSE) == stock + 2, "Cancelar desde el dashboard devuelve el stock reservado"
    _debe_fallar_con(conn, f"select cambiar_estado_pedido({pedido}, 'DESPACHADO', 'x', 'Pedro')",
                     "PEDIDO_CANCELADO", "Un pedido cancelado no se reactiva")
    historial = conn.execute(
        f"select estado::text, nota, responsable from seguimiento_pedido where order_number = {pedido} order by id"
    ).fetchall()
    assert historial == [
        ("DESPACHADO", "Guía Servientrega 123", "Laura"),
        ("DESPACHADO", "Llega el viernes", "Laura"),
        ("CANCELADO", "Devuelto por la transportadora", "Pedro"),
    ], historial

    # Cambios que no vienen del dashboard también quedan, con nota del sistema.
    conn.execute(f"select agregar_al_carrito('p2', '{MOUSE}', 1)")
    otro = conn.execute(_crear_orden("p2")).fetchone()[0]
    conn.execute(f"select cancelar_orden({otro}, 'p2')")
    assert conn.execute(f"select nota, responsable from seguimiento_pedido where order_number = {otro}").fetchall() \
        == [("Cancelado por el cliente desde el chat.", "Sistema")]
    print("✅ 014: pedidos con estado + nota + responsable desde el dashboard; cancelar devuelve el stock y los "
          "cambios del cliente quedan también en el seguimiento.")


def _probar_ventas(conn) -> None:
    # --- Normalización de los datos de n8n ---
    filas = dict(
        (f[0], f[1:]) for f in conn.execute(
            "select order_number, \"Metodo_pago\"::text, payment_status::text, jsonb_typeof(items), stock_reservado "
            "from orders"
        ).fetchall()
    )
    assert filas[27] == ("contra_entrega", "CONTRAENTREGA", "array", False), filas[27]
    assert filas[28][:2] == ("en_linea", "PENDIENTE"), filas[28]
    assert filas[29][:2] == ("contra_entrega", "CONTRAENTREGA"), filas[29]
    assert filas[10][:2] == (None, None), filas[10]
    _debe_fallar(conn, "update orders set \"Metodo_pago\" = 'en linea' where order_number = 27",
                 errors.InvalidTextRepresentation, "El ENUM debe rechazar 'en linea'")
    print("✅ 006: datos de n8n normalizados (ENUM, items como JSON real) y el ENUM rechaza valores mal escritos.")

    carrito = conn.execute("select cantidad from carrito_compras where session_id = 'dup'").fetchall()
    assert carrito == [(3,)], carrito
    print("✅ 006: el carrito duplicado se consolidó sumando cantidades y ya tiene clave primaria.")

    # --- Búsqueda (008 + 009): grupos armados por el código real de Python
    #     con los sinónimos que siembra la migración ---
    sinonimos = [f[0] for f in conn.execute("select palabras from sinonimos_busqueda").fetchall()]
    assert sinonimos, "La 008 debe sembrar grupos de sinónimos"

    def resumen(termino, categoria=CATEGORIA, precio_max=None, orden=None):
        grupos = json.dumps(expandir_termino(termino, sinonimos))
        return conn.execute(
            "select buscar_productos_venta(%s::jsonb, %s, %s, 5, %s)", (grupos, categoria, precio_max, orden)
        ).fetchone()[0]

    def buscar(termino, categoria=CATEGORIA, **opciones):
        return [p["name"] for p in resumen(termino, categoria, **opciones)["productos"]]

    assert buscar("portatil hp") == ["Portátil HP Pavilion"], buscar("portatil hp")
    assert buscar("HP PORTÁTILES") == ["Portátil HP Pavilion"], "Orden, tildes, mayúsculas y plural no importan"
    assert buscar("laptop hp") == ["Portátil HP Pavilion"], "Sinónimo: laptop encuentra Portátil"
    assert buscar("portátil lenovo") == [], "Entre palabras distintas deben coincidir TODAS"
    assert buscar("un mouse de 100%") == ["Mouse 100% inalámbrico"], "Ignora palabras vacías y símbolos"
    grupos_con_simbolos = json.dumps([["1%0"], ["(.*)"]])
    assert conn.execute("select buscar_productos_venta(%s::jsonb) ->> 'total'",
                        (grupos_con_simbolos,)).fetchone()[0] == "0", "%, paréntesis y .* se buscan literalmente"
    assert buscar("16gb") == ["Portátil HP Pavilion"], "Busca también en la descripción"
    assert buscar("ram") == [], "Inicio de palabra: 'ram' no coincide dentro de otra palabra"
    assert buscar("hp", OTRA_CATEGORIA) == ["Audífonos HP"], "Filtra por categoría"
    assert buscar("auriculares", None) == ["Audífonos HP"], "Sin categoría = todo el catálogo; sinónimo + plural"
    assert "cost_price" not in json.dumps(resumen("", None)), "Nunca expone cost_price"
    print("✅ Búsqueda: sinónimos, plurales, sin tildes ni orden, todas las palabras, inicio de palabra, "
          "% literal, categoría opcional, sin agotados/inactivos ni cost_price.")

    # --- Resumen para afinar (009) ---
    marca = conn.execute(f"select brand from products where id = '{DISCO}'").fetchone()[0]
    assert marca == "Western Digital", f"La 009 debe sacar la marca de la descripción: {marca}"
    conn.execute(
        "insert into products (code, name, category_id, sale_price, stock, brand) "
        f"select 'A' || i, 'Auriculares Sony ' || i, '{OTRA_CATEGORIA}'::uuid, i * 100000, 5, 'Sony' "
        "from generate_series(1, 4) as i "
        f"union all select 'B' || i, 'Auriculares Acer ' || i, '{OTRA_CATEGORIA}'::uuid, i * 50000, 5, 'Acer' "
        "from generate_series(1, 3) as i"
    )
    muchos = resumen("audífonos", OTRA_CATEGORIA)
    assert muchos["total"] == 8 and muchos["productos"] == [], "Más de 5: resumen sin productos (hay que afinar)"
    assert (muchos["precio_min"], muchos["precio_max"]) == (2, 400000), muchos
    assert muchos["marcas"] == [
        {"marca": "Sony", "cantidad": 4, "precio_min": 100000, "precio_max": 400000},
        {"marca": "Acer", "cantidad": 3, "precio_min": 50000, "precio_max": 150000},
        {"marca": "Otras marcas", "cantidad": 1, "precio_min": 2, "precio_max": 2},
    ], muchos["marcas"]
    assert buscar("audífonos sony", OTRA_CATEGORIA) == [f"Auriculares Sony {i}" for i in (1, 2, 3, 4)], (
        "Con la marca en el término ya son pocos: se muestran, del más barato al más caro"
    )
    assert buscar("audífonos", OTRA_CATEGORIA, precio_max=150000) == [
        "Auriculares Acer 3", "Auriculares Acer 2", "Auriculares Sony 1", "Auriculares Acer 1", "Audífonos HP",
    ], "Con presupuesto: solo lo que alcanza, de lo mejor a lo más barato"
    baratos = resumen("audífonos", OTRA_CATEGORIA, orden="mas_baratos")
    assert [p["name"] for p in baratos["productos"]] == [
        "Audífonos HP", "Auriculares Acer 1", "Auriculares Acer 2", "Auriculares Sony 1", "Auriculares Acer 3",
    ], "Aunque sean muchos, 'mas_baratos' los muestra del más barato al más caro"
    assert baratos["total"] == 8 and len(baratos["marcas"]) == 3, "El resumen viene igual"
    caros = buscar("audífonos", OTRA_CATEGORIA, orden="mas_caros")
    # Acer 2 y Sony 1 cuestan lo mismo ($100.000): el empate se resuelve por nombre.
    assert caros == ["Auriculares Sony 4", "Auriculares Sony 3", "Auriculares Sony 2", "Auriculares Acer 3",
                     "Auriculares Acer 2"], f"'mas_caros': el más caro de TODOS primero: {caros}"
    assert buscar("audífonos", OTRA_CATEGORIA, precio_max=150000, orden="mas_baratos")[0] == "Audífonos HP", (
        "El orden pedido prevalece sobre el orden por presupuesto"
    )
    print("✅ Resumen para afinar: total, rango de precios y opciones por marca; presupuesto, marca y orden por "
          "precio afinan.")

    # --- Carrito ---
    assert conn.execute(f"select agregar_al_carrito('c1', '{PORTATIL}', 2)").fetchone()[0] == 2
    cantidad = conn.execute(f"select agregar_al_carrito('c1', '{PORTATIL}', 1)").fetchone()[0]
    assert cantidad == 3, "Añadir dos veces suma"
    _debe_fallar_con(conn, f"select agregar_al_carrito('c1', '{PORTATIL}', 3)", "STOCK_INSUFICIENTE",
                     "3 en el carrito + 3 supera el stock de 5")
    _debe_fallar_con(conn, f"select agregar_al_carrito('c1', '{AGOTADO}', 1)", "STOCK_INSUFICIENTE",
                     "Un producto sin stock no se puede añadir")
    _debe_fallar_con(conn, f"select agregar_al_carrito('c1', '{INACTIVO}', 1)", "PRODUCTO_NO_DISPONIBLE",
                     "Un producto inactivo no se puede añadir")
    assert conn.execute(f"select fijar_cantidad_carrito('c1', '{PORTATIL}', 1)").fetchone()[0] == 1
    _debe_fallar_con(conn, f"select fijar_cantidad_carrito('c1', '{PORTATIL}', 6)", "STOCK_INSUFICIENTE",
                     "Fijar más que el stock debe fallar")
    _debe_fallar_con(conn, f"select fijar_cantidad_carrito('c1', '{MOUSE}', 1)", "NO_ESTA_EN_CARRITO",
                     "Fijar la cantidad de algo que no está en el carrito debe fallar")
    print("✅ Carrito: añadir suma, valida stock y activo; fijar cantidad valida stock y existencia.")

    # --- Crear orden (contra entrega) ---
    conn.execute(f"select agregar_al_carrito('c1', '{MOUSE}', 2)")
    _debe_fallar_con(conn, _crear_orden("c1", total="1"), "TOTAL_CAMBIO",
                     "Si el total esperado no coincide, no se crea la orden")
    _debe_fallar_con(conn, _crear_orden("c1", metodo="en_linea"), "DATOS_PAGO_EN_LINEA_REQUERIDOS",
                     "En línea exige referencia, link y vencimiento")
    orden1 = conn.execute(_crear_orden("c1")).fetchone()[0]
    fila = conn.execute(
        f"select payment_status::text, total_amount, stock_reservado, jsonb_array_length(items) "
        f"from orders where order_number = {orden1}"
    ).fetchone()
    assert fila == ("CONTRAENTREGA", 3100000, True, 2), fila
    assert (_stock(conn, PORTATIL), _stock(conn, MOUSE)) == (4, 8), "La orden descuenta stock"
    assert conn.execute("select count(*) from carrito_compras where session_id = 'c1'").fetchone()[0] == 0
    _debe_fallar_con(conn, _crear_orden("c1"), "CARRITO_VACIO", "Un segundo Crear_orden con el carrito vacío falla")
    print("✅ Crear orden: atómica (stock descontado, carrito vaciado), valida total y carrito vacío.")

    # Otro cliente se lleva el stock entre el añadir y el crear orden.
    conn.execute(f"select agregar_al_carrito('c2', '{PORTATIL}', 4)")
    conn.execute(f"update products set stock = 3 where id = '{PORTATIL}'")
    _debe_fallar_con(conn, _crear_orden("c2"), "STOCK_INSUFICIENTE", "Se revalida el stock al crear la orden")
    conn.execute(f"update products set stock = 4 where id = '{PORTATIL}'")
    conn.execute("delete from carrito_compras where session_id = 'c2'")
    _debe_fallar(conn, f"update products set stock = -1 where id = '{PORTATIL}'",
                 errors.CheckViolation, "El stock no puede quedar negativo")
    print("✅ Stock: se revalida al crear la orden y nunca queda negativo.")

    # --- Modificar y cancelar: solo la última y en PENDIENTE_DESPACHO ---
    conn.execute(f"select agregar_al_carrito('c1', '{MOUSE}', 1)")
    orden2 = conn.execute(_crear_orden("c1")).fetchone()[0]
    _debe_fallar_con(conn, f"select cancelar_orden({orden1}, 'c1')", "NO_ES_ULTIMA_ORDEN",
                     "Solo se cancela la última orden")
    _debe_fallar_con(conn, f"select cancelar_orden({orden2}, 'otro')", "ORDEN_NO_ENCONTRADA",
                     "Un cliente no puede cancelar la orden de otro")
    conn.execute(f"select modificar_datos_orden({orden2}, 'c1', p_city => 'Bogotá')")
    assert conn.execute(f"select city, customer_name from orders where order_number = {orden2}").fetchone() == (
        "Bogotá", "Juan"), "Modificar cambia solo lo enviado"
    conn.execute(f"select cancelar_orden({orden2}, 'c1')")
    assert _stock(conn, MOUSE) == 8, "Cancelar devuelve el stock reservado"
    _debe_fallar_con(conn, f"select modificar_datos_orden({orden2}, 'c1', p_city => 'Cali')", "ORDEN_NO_PENDIENTE",
                     "Una orden cancelada ya no se modifica")
    conn.execute("update orders set session_id = 'antiguo2' where order_number = 27")
    conn.execute("select cancelar_orden(27, 'antiguo2')")
    assert _stock(conn, PORTATIL) == 4, "Cancelar una orden de n8n NO devuelve stock (nunca lo descontó)"
    print("✅ Modificar/cancelar: solo la última, del mismo cliente, en PENDIENTE_DESPACHO; "
          "stock devuelto solo si se reservó.")

    # --- Pago en línea ---
    ref = "'00000000-0000-0000-0000-0000000000a1'"
    conn.execute(f"select agregar_al_carrito('c3', '{PORTATIL}', 1)")
    orden3 = conn.execute(_crear_orden("c3", "en_linea", "3000000", "now() + interval '1 hour'", ref, "'https://mp/1'")
                          ).fetchone()[0]

    def aplicar(estado):
        return conn.execute(f"select order_number from aplicar_estado_pago({ref}, '{estado}')").fetchall()

    assert aplicar("RECHAZADO") == [(orden3,)], "El primer cambio devuelve la fila (se notifica)"
    assert aplicar("RECHAZADO") == [], "Un aviso repetido no devuelve nada (no se notifica dos veces)"
    assert aplicar("APROBADO") == [(orden3,)], "Reintento aprobado"
    assert aplicar("PENDIENTE") == [], "Un aviso viejo no retrocede una orden APROBADA"
    assert conn.execute(f"select reserva_expira_en from orders where order_number = {orden3}").fetchone()[0] is None
    _debe_fallar_con(conn, f"select cancelar_orden({orden3}, 'c3')", "PAGO_APROBADO",
                     "Una orden pagada no se cancela desde el agente")
    print("✅ Pagos: una notificación por cambio real, sin retroceder desde APROBADO, y lo pagado no se cancela.")

    # --- Liberación automática ---
    ref_vencida = "'00000000-0000-0000-0000-0000000000a2'"
    conn.execute(f"select agregar_al_carrito('c4', '{PORTATIL}', 2)")
    orden4 = conn.execute(_crear_orden("c4", "en_linea", "6000000", "now() + interval '1 hour'", ref_vencida,
                                       "'https://mp/2'")).fetchone()[0]
    assert _stock(conn, PORTATIL) == 1
    conn.execute(f"update orders set reserva_expira_en = now() - interval '1 minute' where order_number = {orden4}")
    conn.execute(f"select agregar_al_carrito('viejo', '{MOUSE}', 1)")
    conn.execute("update carrito_compras set actualizado_en = now() - interval '25 hours' where session_id = 'viejo'")
    resultado = conn.execute("select liberar_reservas_vencidas(24)").fetchone()[0]
    assert resultado == {"ordenes_canceladas": 1, "items_carrito_borrados": 1}, resultado
    assert _stock(conn, PORTATIL) == 3, "La reserva vencida devuelve el stock"
    assert conn.execute(f"select shipping_status::text from orders where order_number = {orden4}").fetchone()[0] == \
        "CANCELADO"
    assert conn.execute("select liberar_reservas_vencidas(24)").fetchone()[0]["ordenes_canceladas"] == 0
    print("✅ Liberación: órdenes en línea vencidas se cancelan con su stock de vuelta; carritos viejos se borran.")


def probar(url: str) -> None:
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(ESQUEMA_BASE.read_text(encoding="utf-8"))
        conn.execute("insert into servicios_tecnicos (nombre, duracion_minutos) values ('Diagnóstico', 60)")

        # Migraciones hasta la 003 (las posteriores se aplican después de
        # sembrar una cita "antigua" sin técnico, para probar el backfill).
        hasta_003 = [m for m in MIGRACIONES if m.name[:14] <= "20260928000003"]
        posteriores = [m for m in MIGRACIONES if m.name[:14] > "20260928000003"]
        for migracion in hasta_003:
            conn.execute(migracion.read_text(encoding="utf-8"))
        tecnicos = [f[0] for f in conn.execute("select id from tecnicos order by id").fetchall()]
        assert len(tecnicos) == 2, "La migración 003 debe crear 2 técnicos de ejemplo"
        t1, t2 = tecnicos

        # Una conversación con el formato anterior a la Fase 11 (columnas por agente).
        conn.execute(
            "insert into conversaciones (session_id, historial_orquestador, historial_servicio_tecnico) values "
            "('3001', '[{\"role\": \"user\", \"content\": \"hola\"}]', "
            "'[{\"role\": \"user\", \"content\": \"mouse\"}]')"
        )

        # El hueco que cierra la 004: con tecnico_id NULL no hay protección.
        conn.execute(_cita(None, "08:00", "09:00"))
        print("✅ (antes de la 004) una cita confirmada sin técnico se acepta: el hueco existe.")

        _sembrar_ventas_antiguas(conn)

        for migracion in posteriores:
            conn.execute(migracion.read_text(encoding="utf-8"))
        # Aplicar todo de nuevo no debe fallar (migraciones idempotentes).
        for migracion in MIGRACIONES:
            conn.execute(migracion.read_text(encoding="utf-8"))
        print(f"✅ Las {len(MIGRACIONES)} migraciones se aplican sobre Postgres real, y son re-aplicables.")

        backfill = conn.execute(
            "select tecnico_id from servicios_agendados where fecha_hora_inicio = '2026-10-05 08:00-05'"
        )
        assert backfill.fetchone()[0] == t1, "La 004 debe completar tecnico_id con el técnico del servicio"
        print("✅ La migración 004 completa el técnico de las citas antiguas.")

        historiales = conn.execute("select historiales from conversaciones where session_id = '3001'").fetchone()[0]
        assert historiales == {
            "orquestador": [{"role": "user", "content": "hola"}],
            "servicio_tecnico": [{"role": "user", "content": "mouse"}],
        }, historiales
        print("✅ La migración 005 copia los historiales de las columnas viejas a `historiales`.")

        conn.execute(_cita(t1, "10:00", "11:00"))
        _debe_fallar(conn, _cita(t1, "10:30", "11:30"), errors.ExclusionViolation,
                     "Dos citas confirmadas del MISMO técnico que se solapan deben rechazarse (23P01)")
        conn.execute(_cita(t1, "11:00", "12:00"))  # contigua: el rango es [inicio, fin)
        conn.execute(_cita(t2, "10:00", "11:00"))  # otro técnico, misma hora
        conn.execute(_cita(t1, "10:30", "11:30", estado="cancelado por cliente"))
        print("✅ Constraint por técnico: rechaza el choque; acepta contiguas, otro técnico y canceladas.")

        _debe_fallar(conn, _cita(None, "15:00", "16:00"), errors.CheckViolation,
                     "Una cita confirmada sin técnico debe rechazarse tras la migración 004")
        conn.execute(_cita(None, "15:00", "16:00", estado="cancelado por cliente"))
        print("✅ Tras la 004, toda cita confirmada exige técnico (las canceladas no).")

        _probar_ventas(conn)
        _probar_info_empresa(conn, MIGRACIONES)
        _probar_ordenes_servicio(conn)
        _probar_seguimiento_pedidos(conn)


if __name__ == "__main__":
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        probar(url)
    else:
        bin_dir = _buscar_bin_postgres()
        if bin_dir is None:
            print("⏭️  OMITIDO: no hay TEST_DATABASE_URL ni binarios de PostgreSQL (initdb) disponibles.")
            sys.exit(0)
        with ClusterTemporal(bin_dir) as url_temporal:
            probar(url_temporal)
    print("\n✅ Todos los tests de integración con PostgreSQL pasaron.")
