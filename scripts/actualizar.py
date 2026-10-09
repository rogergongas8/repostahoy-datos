#!/usr/bin/env python3
"""Actualiza los datos de este repositorio a partir de las páginas públicas de https://www.repostahoy.es.

Qué hace, en orden:
  1. Lee las series de precios que cada página lleva incrustadas (atributo data-series): medias mensuales
     (páginas /precio/{combustible}/historico), y medias diarias y semanales de España (/informe-semanal)
     y de cada provincia (/gasolineras/{provincia}).
  2. Las valida (rango de precios, semanas y días completos) y las FUSIONA con lo que ya hay en data/:
     las series diarias que la web solo muestra a 90 días se van acumulando aquí.
  3. Reescribe los CSV, dibuja los gráficos de docs/ y actualiza los bloques automáticos del README.

Solo usa la biblioteca estándar de Python (3.9 o superior). Licencia del código: MIT. Datos: CC BY 4.0.
Uso:  python scripts/actualizar.py            (actualiza)
      python scripts/actualizar.py --offline  (no descarga: regenera README y gráficos con lo que hay)
"""
import csv
import html
import json
import re
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = "https://www.repostahoy.es"
RAIZ = Path(__file__).resolve().parent.parent
DATOS = RAIZ / "data"
DOCS = RAIZ / "docs"
MADRID = ZoneInfo("Europe/Madrid")
USER_AGENT = "repostahoy-datos/1.0 (+https://github.com/rogergongas8/repostahoy-datos)"
PAUSA = 0.6  # segundos entre peticiones: son pocas (unas 60) y la web las sirve desde caché

# id de la web (en la URL) -> id del dato, nombre, unidad
COMBUSTIBLES = {
    "diesel": ("diesel", "Diésel", "EUR/litro", "Gasóleo A (el diésel normal)"),
    "diesel-premium": ("diesel_premium", "Diésel Premium", "EUR/litro", "Gasóleo premium (aditivado)"),
    "gasolina-95": ("gasolina_95", "Gasolina 95", "EUR/litro", "Gasolina 95 E5"),
    "gasolina-98": ("gasolina_98", "Gasolina 98", "EUR/litro", "Gasolina 98 E5"),
    "glp": ("glp", "GLP", "EUR/litro", "Gases licuados del petróleo (autogás)"),
    "gnc": ("gnc", "GNC", "EUR/kg", "Gas natural comprimido (se vende por kilo)"),
}
LIQUIDOS = ["diesel", "diesel_premium", "gasolina_95", "gasolina_98"]  # los que tienen serie diaria y semanal
NOMBRE = {v[0]: v[1] for v in COMBUSTIBLES.values()}
ESPECIALES = {"las-palmas", "santa-cruz-de-tenerife", "ceuta", "melilla"}  # impuestos propios sobre carburantes
PRECIO_MIN, PRECIO_MAX = 0.30, 3.50  # fuera de este rango, el dato se descarta
HOY = datetime.now(MADRID).date()

CAMPOS = {
    "mensual_espana.csv": ["mes", "combustible", "precio_medio", "mes_completo"],
    "semanal_espana.csv": ["semana_inicio", "combustible", "precio_medio"],
    "diario_espana.csv": ["fecha", "combustible", "precio_medio"],
    "semanal_provincias.csv": ["semana_inicio", "provincia", "combustible", "precio_medio"],
    "diario_provincias.csv": ["fecha", "provincia", "combustible", "precio_medio"],
}


def descargar(ruta):
    peticion = urllib.request.Request(BASE + ruta, headers={"User-Agent": USER_AGENT})
    for intento in range(3):
        try:
            with urllib.request.urlopen(peticion, timeout=45) as r:
                return r.read().decode("utf-8")
        except Exception as e:  # red o servidor: reintenta
            if intento == 2:
                raise RuntimeError("No se pudo descargar %s: %s" % (ruta, e))
            time.sleep(3 * (intento + 1))


def series_de(pagina):
    """Todas las series incrustadas en una página: lista de (combustible, [(fecha_madrid, precio), ...])."""
    salida = []
    for m in re.finditer(r'data-series="([^"]+)"', pagina):
        for combustible, puntos in json.loads(html.unescape(m.group(1))).items():
            salida.append((combustible, [
                (datetime.fromtimestamp(t, timezone.utc).astimezone(MADRID).date(), float(p)) for t, p in puntos
            ]))
    return salida


def valido(precio):
    return PRECIO_MIN <= precio <= PRECIO_MAX


def paso_dias(puntos):
    """Días entre puntos, en mediana (el primer y el último punto de una serie pueden ser parciales)."""
    fechas = [f for f, _ in puntos]
    if len(fechas) < 2:
        return None
    pasos = sorted((b - a).days for a, b in zip(fechas, fechas[1:]))
    return pasos[len(pasos) // 2]


def clasificar(series):
    """Separa las series de una página por granularidad: diaria (paso 1 día, la de 90 días), semanal (paso ~7 días) y mensual."""
    diaria, semanal, mensual = {}, {}, {}
    for combustible, puntos in series:
        paso = paso_dias(puntos)
        if paso == 1:
            if len(puntos) >= len(diaria.get(combustible, [])):
                diaria[combustible] = puntos  # nos quedamos con la más larga (90 días)
        elif paso is not None and 6 <= paso <= 8:
            if len(puntos) >= len(semanal.get(combustible, [])):
                semanal[combustible] = puntos  # la más larga (5 años)
        elif paso is not None and paso > 20:
            mensual[combustible] = puntos
    return diaria, semanal, mensual


def escribir(nombre, filas):
    DATOS.mkdir(exist_ok=True)
    campos = CAMPOS[nombre]
    clave = campos[:-1] if nombre != "mensual_espana.csv" else campos[:2]
    filas = sorted(filas.values(), key=lambda f: tuple(f[c] for c in clave))
    with (DATOS / nombre).open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=campos, lineterminator="\n")
        w.writeheader()
        w.writerows(filas)
    return len(filas)


def fusionar(nombre, nuevas):
    """Une las filas nuevas con las que ya hay; para la misma clave gana la nueva."""
    campos = CAMPOS[nombre]
    clave = campos[:2] if nombre == "mensual_espana.csv" else campos[:-1]
    if nombre == "mensual_espana.csv":
        clave = ["mes", "combustible"]
    existentes = {}
    ruta = DATOS / nombre
    if ruta.exists():
        with ruta.open(encoding="utf-8", newline="") as f:
            for fila in csv.DictReader(f):
                existentes[tuple(fila[c] for c in clave)] = fila
    for fila in nuevas:
        existentes[tuple(fila[c] for c in clave)] = fila
    return existentes


def fmt(precio):
    """Las series de la web vienen redondeadas a la milésima de euro: no se prometen más decimales."""
    return "%.3f" % precio


def filas_diarias(puntos_por_combustible, extra=None):
    filas = []
    for combustible, puntos in puntos_por_combustible.items():
        for dia, precio in puntos:
            if dia >= HOY or not valido(precio):  # hoy aún no ha terminado: no es un dato definitivo
                continue
            fila = {"fecha": dia.isoformat(), "combustible": combustible, "precio_medio": fmt(precio)}
            fila.update(extra or {})
            filas.append(fila)
    return filas


def filas_semanales(puntos_por_combustible, extra=None):
    """Solo semanas completas: empiezan en lunes y ya han terminado (domingo anterior a hoy)."""
    filas = []
    for combustible, puntos in puntos_por_combustible.items():
        for dia, precio in puntos:
            if dia.weekday() != 0 or dia + timedelta(days=7) > HOY or not valido(precio):
                continue
            fila = {"semana_inicio": dia.isoformat(), "combustible": combustible, "precio_medio": fmt(precio)}
            fila.update(extra or {})
            filas.append(fila)
    return filas


def filas_mensuales(puntos_por_combustible):
    filas = []
    for combustible, puntos in puntos_por_combustible.items():
        for dia, precio in puntos:
            if not valido(precio):
                continue
            mes = dia.strftime("%Y-%m")
            completo = (dia.replace(day=1) + timedelta(days=32)).replace(day=1) <= HOY
            filas.append({"mes": mes, "combustible": combustible, "precio_medio": fmt(precio),
                          "mes_completo": "true" if completo else "false"})
    return filas


def provincias():
    texto = descargar("/llms.txt")
    lista = re.findall(r"\[Gasolineras en ([^\]]+)\]\(" + re.escape(BASE) + r"/gasolineras/([a-z0-9-]+)\)", texto)
    if len(lista) < 50:
        raise RuntimeError("Se esperaban 52 provincias y hay %d" % len(lista))
    return [(slug, nombre) for nombre, slug in lista]


def actualizar():
    DATOS.mkdir(exist_ok=True)
    nuevas = {n: [] for n in CAMPOS}

    # Mensual, por combustible (la serie de /historico es la más larga: desde que haya datos en el archivo).
    for ruta, (id_dato, *_resto) in COMBUSTIBLES.items():
        _d, _s, mensual = clasificar(series_de(descargar("/precio/%s/historico" % ruta)))
        nuevas["mensual_espana.csv"] += filas_mensuales(mensual)
        time.sleep(PAUSA)

    # España: diario (90 días) y semanal (5 años) de los cuatro combustibles líquidos.
    diaria, semanal, _m = clasificar(series_de(descargar("/informe-semanal")))
    nuevas["diario_espana.csv"] += filas_diarias(diaria)
    nuevas["semanal_espana.csv"] += filas_semanales(semanal)
    time.sleep(PAUSA)

    # Cada provincia: lo mismo.
    lista = provincias()
    for slug, _nombre in lista:
        diaria, semanal, _m = clasificar(series_de(descargar("/gasolineras/%s" % slug)))
        nuevas["diario_provincias.csv"] += filas_diarias(diaria, {"provincia": slug})
        nuevas["semanal_provincias.csv"] += filas_semanales(semanal, {"provincia": slug})
        time.sleep(PAUSA)

    # Controles mínimos antes de escribir nada.
    if len(nuevas["mensual_espana.csv"]) < 6 * 100 or len(nuevas["diario_provincias.csv"]) < 52 * 4 * 80:
        raise RuntimeError("Las series descargadas son demasiado cortas: la web habrá cambiado; no se escribe nada.")

    total = {}
    for nombre, filas in nuevas.items():
        total[nombre] = escribir(nombre, fusionar(nombre, filas))

    with (DATOS / "provincias.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["provincia", "nombre", "impuestos_propios"])
        for slug, nombre in sorted(lista):
            w.writerow([slug, nombre, "true" if slug in ESPECIALES else "false"])
    with (DATOS / "combustibles.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["combustible", "nombre", "unidad", "descripcion", "series_diaria_y_semanal"])
        for id_dato, nombre, unidad, descripcion in COMBUSTIBLES.values():
            w.writerow([id_dato, nombre, unidad, descripcion, "true" if id_dato in LIQUIDOS else "false"])
    return total


def semanal_desde_diario():
    """Semanas exactas (lunes a domingo, hora de Madrid) calculadas con los días acumulados aquí.

    Una semana solo cuenta si están sus 7 días. Estas filas mandan sobre las que traiga la web para la misma semana.
    """
    for diario, semanal, con_provincia in (("diario_espana.csv", "semanal_espana.csv", False),
                                           ("diario_provincias.csv", "semanal_provincias.csv", True)):
        grupos = {}
        for f in cargar(diario):
            d = date.fromisoformat(f["fecha"])
            lunes = d - timedelta(days=d.weekday())
            grupos.setdefault((lunes, f.get("provincia"), f["combustible"]), {})[d] = float(f["precio_medio"])
        filas = []
        for (lunes, provincia, combustible), dias in grupos.items():
            if len(dias) == 7 and lunes + timedelta(days=7) <= HOY:
                fila = {"semana_inicio": lunes.isoformat(), "combustible": combustible, "precio_medio": fmt(sum(dias.values()) / 7)}
                if con_provincia:
                    fila["provincia"] = provincia
                filas.append(fila)
        if filas:
            escribir(semanal, fusionar(semanal, filas))


# ───────────────────────── resúmenes, gráficos y README ─────────────────────────

def cargar(nombre):
    ruta = DATOS / nombre
    if not ruta.exists():
        return []
    with ruta.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def eur(v, decimales=3):
    return ("%.*f" % (decimales, v)).replace(".", ",") + " €"


def mes_texto(mes):
    meses = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
    anio, m = mes.split("-")
    return "%s de %s" % (meses[int(m) - 1], anio)


def fecha_texto(iso):
    meses = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
    d = date.fromisoformat(iso)
    return "%d de %s de %d" % (d.day, meses[d.month - 1], d.year)


def resumen_cobertura():
    ficheros = [
        ("mensual_espana.csv", "mes", "Media mensual de España (los 6 combustibles)"),
        ("semanal_espana.csv", "semana_inicio", "Media semanal de España (4 combustibles líquidos)"),
        ("diario_espana.csv", "fecha", "Media diaria de España (4 combustibles líquidos)"),
        ("semanal_provincias.csv", "semana_inicio", "Media semanal de cada provincia (4 combustibles líquidos)"),
        ("diario_provincias.csv", "fecha", "Media diaria de cada provincia (4 combustibles líquidos)"),
    ]
    lineas = ["| Archivo | Qué contiene | Filas | Desde | Hasta |", "|---|---|---:|---|---|"]
    for nombre, campo, descripcion in ficheros:
        filas = cargar(nombre)
        if not filas:
            continue
        fechas = sorted(f[campo] for f in filas)
        formato = (lambda x: mes_texto(x)) if campo == "mes" else fecha_texto
        lineas.append("| [`data/%s`](data/%s) | %s | %s | %s | %s |" % (
            nombre, nombre, descripcion, format(len(filas), ",").replace(",", "."), formato(fechas[0]), formato(fechas[-1])))
    return "\n".join(lineas)


def resumen_cifras():
    mensual = cargar("mensual_espana.csv")
    if not mensual:
        return "_Aún no hay datos._"
    por = {}
    for f in mensual:
        por.setdefault(f["combustible"], {})[f["mes"]] = (float(f["precio_medio"]), f["mes_completo"] == "true")
    lineas = []
    for combustible in ["diesel", "gasolina_95"]:
        meses = por.get(combustible)
        if not meses:
            continue
        completos = {m: v[0] for m, v in meses.items() if v[1]}
        ultimo = max(completos)
        precio = completos[ultimo]
        pico = max(completos, key=completos.get)
        valle = min(completos, key=completos.get)
        partes = ["**%s**: %s de media en %s" % (NOMBRE[combustible], eur(precio), mes_texto(ultimo))]
        for anios in (1, 5, 10):
            antes = "%04d-%s" % (int(ultimo[:4]) - anios, ultimo[5:])
            if antes in completos:
                variacion = (precio / completos[antes] - 1) * 100
                partes.append("%s hace %d %s (%s%s %%)" % (eur(completos[antes]), anios, "año" if anios == 1 else "años",
                                                          "+" if variacion >= 0 else "−", ("%.1f" % abs(variacion)).replace(".", ",")))
        lineas.append("- " + "; ".join(partes) + ".")
        lineas.append("  El mes más caro de la serie fue %s (%s) y el más barato, %s (%s)." % (
            mes_texto(pico), eur(completos[pico]), mes_texto(valle), eur(completos[valle])))
    lineas += extremos_provinciales()
    return "\n".join(lineas)


def extremos_provinciales():
    """Provincia más barata y más cara de la última semana completa (sin Canarias, Ceuta ni Melilla, que tienen otros impuestos)."""
    semanal = cargar("semanal_provincias.csv")
    if not semanal:
        return []
    nombres = {f["provincia"]: f["nombre"] for f in cargar("provincias.csv")}
    ultima = max(f["semana_inicio"] for f in semanal)
    inicio = date.fromisoformat(ultima)
    fin = inicio + timedelta(days=6)
    lineas = ["", "Última semana completa (del %d de %s al %s), sin Canarias, Ceuta ni Melilla:" % (
        inicio.day, fecha_texto(inicio.isoformat()).split(" de ")[1] if inicio.month != fin.month else fecha_texto(fin.isoformat()).split(" de ")[1], fecha_texto(fin.isoformat()))]
    for combustible in ("diesel", "gasolina_95"):
        valores = {f["provincia"]: float(f["precio_medio"]) for f in semanal
                   if f["semana_inicio"] == ultima and f["combustible"] == combustible and f["provincia"] not in ESPECIALES}
        if len(valores) < 40:
            continue
        barata, cara = min(valores, key=valores.get), max(valores, key=valores.get)
        lineas.append("- %s: la provincia más barata fue %s (%s) y la más cara, %s (%s); diferencia de %s céntimos por litro." % (
            NOMBRE[combustible], nombres[barata], eur(valores[barata]), nombres[cara], eur(valores[cara]),
            ("%.1f" % ((valores[cara] - valores[barata]) * 100)).replace(".", ",")))
    return lineas


COLORES = {"diesel": "#1f3a5f", "diesel_premium": "#5b8def", "gasolina_95": "#d9822b", "gasolina_98": "#a23b72"}


def grafico(nombre_fichero, titulo, series, formato_x):
    """SVG sencillo (fondo blanco, para que se lea igual en modo claro y oscuro)."""
    ancho, alto, izq, der, arriba, abajo = 900, 400, 62, 24, 56, 64
    todos = [p for puntos in series.values() for p in puntos]
    if not todos:
        return
    x0, x1 = min(p[0] for p in todos), max(p[0] for p in todos)
    ys = [p[1] for p in todos]
    y0 = int(min(ys) * 10) / 10 - 0.1
    y1 = int(max(ys) * 10) / 10 + 0.2
    X = lambda d: izq + (d - x0).days / max((x1 - x0).days, 1) * (ancho - izq - der)
    Y = lambda v: arriba + (1 - (v - y0) / (y1 - y0)) * (alto - arriba - abajo)
    s = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" role="img" aria-label="%s" font-family="system-ui,Segoe UI,Helvetica,Arial,sans-serif">' % (ancho, alto, html.escape(titulo)),
         '<rect width="%d" height="%d" rx="10" fill="#ffffff"/>' % (ancho, alto),
         '<text x="%d" y="30" font-size="17" font-weight="600" fill="#1b1a17">%s</text>' % (izq, html.escape(titulo))]
    paso = 0.2 if y1 - y0 <= 1.6 else 0.5
    v = (int(y0 / paso) + 1) * paso
    while v < y1:
        s.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#e5e7e5"/>' % (izq, Y(v), ancho - der, Y(v)))
        s.append('<text x="%d" y="%.1f" font-size="12" fill="#5b5a55" text-anchor="end">%s</text>' % (izq - 8, Y(v) + 4, ("%.2f" % v).replace(".", ",")))
        v += paso
    for etiqueta, d in formato_x(x0, x1):
        s.append('<text x="%.1f" y="%d" font-size="12" fill="#5b5a55" text-anchor="middle">%s</text>' % (X(d), alto - abajo + 22, etiqueta))
    lx = izq
    for combustible, puntos in series.items():
        if not puntos:
            continue
        trazo = " ".join("%.1f,%.1f" % (X(d), Y(v)) for d, v in puntos)
        s.append('<polyline fill="none" stroke="%s" stroke-width="2.2" stroke-linejoin="round" points="%s"/>' % (COLORES[combustible], trazo))
        s.append('<rect x="%d" y="%d" width="12" height="12" rx="2" fill="%s"/>' % (lx, alto - 26, COLORES[combustible]))
        s.append('<text x="%d" y="%d" font-size="12.5" fill="#1b1a17">%s</text>' % (lx + 18, alto - 16, NOMBRE[combustible]))
        lx += 150
    s.append('<text x="%d" y="%d" font-size="11.5" fill="#5b5a55" text-anchor="end">€ por litro, impuestos incluidos · repostahoy.es</text>' % (ancho - der, alto - 6))
    s.append("</svg>")
    DOCS.mkdir(exist_ok=True)
    (DOCS / nombre_fichero).write_text("\n".join(s) + "\n", encoding="utf-8")


def graficos():
    mensual = cargar("mensual_espana.csv")
    por = {c: [] for c in LIQUIDOS}
    for f in mensual:
        if f["combustible"] in por and f["mes_completo"] == "true":
            anio, m = map(int, f["mes"].split("-"))
            por[f["combustible"]].append((date(anio, m, 15), float(f["precio_medio"])))
    for puntos in por.values():
        puntos.sort()

    def anios(x0, x1):
        paso = 1 if (x1.year - x0.year) <= 10 else 2
        return [(str(a), date(a, 1, 1)) for a in range(x0.year + 1, x1.year + 1, paso)]
    grafico("mensual.svg", "Precio medio mensual de los carburantes en España", por, anios)

    diario = cargar("diario_espana.csv")
    por = {c: [] for c in LIQUIDOS}
    for f in diario:
        if f["combustible"] in por:
            por[f["combustible"]].append((date.fromisoformat(f["fecha"]), float(f["precio_medio"])))
    for puntos in por.values():
        puntos.sort()

    def semanas(x0, x1):
        etiquetas, d = [], x0
        salto = max(((x1 - x0).days // 6), 1)
        while d <= x1:
            etiquetas.append(("%d/%d" % (d.day, d.month), d))
            d += timedelta(days=salto)
        return etiquetas
    if any(len(v) > 7 for v in por.values()):
        # Hasta los últimos 120 días para que el gráfico siga siendo legible aunque el archivo crezca.
        corte = max(p[0] for v in por.values() for p in v) - timedelta(days=120)
        recorte = {c: [p for p in v if p[0] >= corte] for c, v in por.items()}
        grafico("diario.svg", "Precio medio diario en España (últimos 120 días)", recorte, semanas)


def reemplazar_bloque(texto, marca, contenido):
    patron = re.compile(r"(<!-- AUTO:%s -->)(.*?)(<!-- /AUTO:%s -->)" % (marca, marca), re.S)
    if not patron.search(texto):
        raise RuntimeError("Falta el bloque automático %s en el README" % marca)
    return patron.sub(lambda m: "%s\n%s\n%s" % (m.group(1), contenido, m.group(3)), texto)


def readme():
    ruta = RAIZ / "README.md"
    texto = ruta.read_text(encoding="utf-8")
    texto = reemplazar_bloque(texto, "CIFRAS", resumen_cifras())
    texto = reemplazar_bloque(texto, "COBERTURA", resumen_cobertura())
    ruta.write_text(texto, encoding="utf-8")
    cff = RAIZ / "CITATION.cff"
    if cff.exists():
        t = cff.read_text(encoding="utf-8")
        t = re.sub(r"(?m)^date-released: .*$", "date-released: %s" % HOY.isoformat(), t)
        cff.write_text(t, encoding="utf-8")


if __name__ == "__main__":
    if "--offline" not in sys.argv:
        resultado = actualizar()
        print("Filas escritas:", json.dumps(resultado, indent=2))
    semanal_desde_diario()
    graficos()
    readme()
    print("Listo.")
