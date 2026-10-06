#!/usr/bin/env python3
"""
Migra eventos.csv (ex Excel) a la base SQLite del esquema Prisma:
Evento + EventoFecha + EventoFechaEspacio. Sólo usa la librería estándar.

  python migrar_eventos.py eventos.csv prisma/dev.db              -> simulacro (no escribe)
  python migrar_eventos.py eventos.csv prisma/dev.db --apply      -> escribe
  python migrar_eventos.py eventos.csv prisma/dev.db --sin-agrupar -> un Evento por fila

  TIPO_EVENTO_DEFAULT=OTRO  (código existente en la tabla TipoEvento)

HACÉ UNA COPIA de la base antes de usar --apply.
"""
import csv
import os
import sqlite3
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone

args = [a for a in sys.argv[1:] if not a.startswith("--")]
APPLY = True
AGRUPAR = "--sin-agrupar" not in sys.argv
CSV_PATH = args[0] if len(args) > 0 else "eventos.csv"
DB_PATH = args[1] if len(args) > 1 else "dev_db"
TIPO_EVENTO = os.environ.get("TIPO_EVENTO_DEFAULT", "OTRO")

# 'NOMBRE EN EL EXCEL': 'codigo_UF'  (para espacios que no coinciden por nombre)
MAPA_ESPACIOS = {
    "CAPACITACIÓN 008": "008",
    "CAPACITACIÓN 009": "009",
    "CAPACITACIÓN 010": "010",
    "CAPACITACIÓN TELECOM": "104",
    "SALA DE DIVULGACIÓN URDIMBRE": "011",
    "MIRADOR DEL PARANÁ": "318",
    "TERRAZA": "319",
    "CATERING MIRADOR": "336",
    "QUINCHO": "234",
    "MIRADORTEC LAB": "MLAB",
    "PALIER CHURQUI": "105",  # ⚠ supuesto: el palier junto a la Sala Estar "Churquí"
    # "COCINA MIRADOR": "336",  # ⚠ dudoso: ¿es parte de "Catering Mirador y Auxiliar"? Descomentá si sí
    # "HALL DE ENTRADA": no tiene UF en tu lista (probablemente sea un ítem del tarifario)
}

PARTICIPACION = {"PUBLICO", "PRIVADO"}
NIVEL = {"GENERAL", "RELACIONADO", "AVANZADO"}


def norm(s):
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return " ".join(s.split()).upper()


def parse_fecha(txt):
    try:
        d, m, y = (int(x) for x in (txt or "").strip().split("/"))
        return datetime(y, m, d, tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def main():
    # ── CSV ────────────────────────────────────────────────────────────────
    with open(CSV_PATH, newline="", encoding="utf-8-sig") as f:
        rd = csv.DictReader(f)
        rd.fieldnames = [h.strip() for h in rd.fieldnames]
        filas = []
        for r in rd:
            r.pop(None, None)  # columnas sobrantes (fila E0140)
            filas.append({k: (v or "").strip() for k, v in r.items()})
    print(f"Filas leídas: {len(filas)}")

    if not os.path.isfile(DB_PATH):
        candidatos = []
        for raiz, dirs, archivos in os.walk("."):
            dirs[:] = [d for d in dirs if d not in ("node_modules", ".git")]
            candidatos += [os.path.join(raiz, a) for a in archivos if a.endswith((".db", ".sqlite", ".sqlite3"))]
        msg = f'No encuentro la base de datos en "{DB_PATH}".'
        if candidatos:
            msg += "\nArchivos .db encontrados desde esta carpeta:\n  " + "\n  ".join(candidatos)
            msg += "\nVolvé a correr indicando la ruta, por ejemplo:\n  python migrar_eventos.py eventos.csv " + candidatos[0]
        else:
            msg += "\nNo hay ningún .db en esta carpeta ni en sus subcarpetas. Revisá DATABASE_URL en tu archivo .env."
        sys.exit(msg)

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    if not cur.execute("SELECT 1 FROM TipoEvento WHERE codigo = ?", (TIPO_EVENTO,)).fetchone():
        sys.exit(f'No existe TipoEvento con codigo "{TIPO_EVENTO}". Usá TIPO_EVENTO_DEFAULT=<codigo>.')

    # Prisma guarda DateTime en SQLite como entero (ms) o como texto ISO según la
    # versión: copio el formato de una fila existente; si no hay, uso ms.
    fila_ej = cur.execute("SELECT typeof(creado_en) t FROM Evento LIMIT 1").fetchone()
    fecha_como_texto = bool(fila_ej and fila_ej["t"] == "text")

    def dt(d):
        return d.isoformat(timespec="milliseconds").replace("+00:00", "Z") if fecha_como_texto else int(d.timestamp() * 1000)

    ahora = datetime.now(timezone.utc)

    # ── Unidades funcionales ───────────────────────────────────────────────
    ufs = cur.execute("SELECT id, codigo_UF, nombre FROM UnidadFuncional").fetchall()
    uf_nombre = {norm(u["nombre"]): u["id"] for u in ufs}
    uf_codigo = {u["codigo_UF"]: u["id"] for u in ufs}
    sin_match = Counter()

    def resolver_uf(nombre):
        if not norm(nombre):
            return None
        if nombre in MAPA_ESPACIOS and MAPA_ESPACIOS[nombre] in uf_codigo:
            return uf_codigo[MAPA_ESPACIOS[nombre]]
        if norm(nombre) in uf_nombre:
            return uf_nombre[norm(nombre)]
        sin_match[nombre] += 1
        return None

    # ── Normalizar filas ───────────────────────────────────────────────────
    omitidas, items = [], []
    for r in filas:
        fecha = parse_fecha(r.get("Fecha"))
        titulo = " ".join(r.get("NombreActividad", "").split())
        if not titulo or not fecha:
            motivo = "sin título" if not titulo else f'fecha inválida o vacía ("{r.get("Fecha")}")'
            omitidas.append(f'{r.get("IDEvento")}: {motivo}')
            continue
        uf_ids = []
        for col in ("Espacio1", "Espacio2", "Espacio3"):
            uid = resolver_uf(r.get(col, ""))
            if uid and uid not in uf_ids:
                uf_ids.append(uid)
        items.append({"r": r, "titulo": titulo, "fecha": fecha, "uf_ids": uf_ids})

    anio_tipico = Counter(i["fecha"].year for i in items).most_common(1)[0][0]
    sospechosas = [i for i in items if i["fecha"].year != anio_tipico]

    # ── Agrupar ────────────────────────────────────────────────────────────
    grupos = defaultdict(list)
    for it in items:
        grupos[norm(it["titulo"]) if AGRUPAR else it["r"]["IDEvento"]].append(it)

    # ── Insertar ───────────────────────────────────────────────────────────
    creados = ya_existian = 0
    for its in grupos.values():
        its.sort(key=lambda i: i["fecha"])
        p, u = its[0], its[-1]
        r = p["r"]

        if cur.execute(
            "SELECT 1 FROM Evento WHERE titulo = ? AND fecha_inicio = ?", (p["titulo"], dt(p["fecha"]))
        ).fetchone():
            ya_existian += 1
            continue

        notas = "\n".join(
            [
                f'Migrado desde Excel ({", ".join(i["r"]["IDEvento"] for i in its)}).',
                f'Sector responsable: {r.get("SectorResponsable") or "s/d"}.',
                f'Tipo de público: {r.get("TipoPublico") or "s/d"}'
                + (f' / {r["SubtipoPublico"]}' if r.get("SubtipoPublico") else "")
                + ".",
            ]
            + [
                f'· {i["r"]["IDEvento"]} {i["fecha"].date()} — {i["r"].get("Horario") or "s/d"}, '
                f'{i["r"].get("Duracion_Hs") or "?"} h ({i["r"].get("duracion_enum") or "s/d"}), '
                f'{i["r"].get("Asistentes") or "?"} asistentes ({i["r"].get("rango_asistentes_enum") or "s/d"})'
                for i in its
            ]
        )
        participacion = r.get("tipo_participacion_enum") if r.get("tipo_participacion_enum") in PARTICIPACION else "PUBLICO"
        nivel = r.get("nivel_publico_enum") if r.get("nivel_publico_enum") in NIVEL else "GENERAL"

        cur.execute(
            """INSERT INTO Evento
               (uf_id, nombre_organizador, titulo, tipo_evento, fecha_inicio, fecha_fin,
                estado_evento, participacion, nivel_publico, visible_en_app,
                notas_internas, creado_en, updatedAt)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                p["uf_ids"][0] if p["uf_ids"] else None,
                r.get("SectorResponsable") or "Sin definir",  # ⚠ el Excel no tiene organizador real
                p["titulo"],
                TIPO_EVENTO,  # ⚠ el Excel no tiene tipo
                dt(p["fecha"]),
                dt(u["fecha"]),
                "FINALIZADO" if u["fecha"] < ahora else "PUBLICADO",
                participacion,
                nivel,
                0,  # histórico: que no aparezca en la app sin decidirlo
                notas,
                dt(ahora),
                dt(ahora),
            ),
        )
        evento_id = cur.lastrowid
        for orden, i in enumerate(its):
            cur.execute(
                "INSERT INTO EventoFecha (evento_id, fecha, orden, uf_id, creado_en) VALUES (?,?,?,?,?)",
                (evento_id, dt(i["fecha"]), orden, i["uf_ids"][0] if i["uf_ids"] else None, dt(ahora)),
            )
            fecha_id = cur.lastrowid
            for uid in i["uf_ids"]:
                cur.execute(
                    "INSERT INTO EventoFechaEspacio (evento_fecha_id, uf_id) VALUES (?,?)", (fecha_id, uid)
                )
        creados += 1

    if APPLY:
        con.commit()
    else:
        con.rollback()
    con.close()

    # ── Informe ────────────────────────────────────────────────────────────
    print(f"\n{'ESCRITO' if APPLY else 'SIMULACRO (nada se escribió)'}")
    print(f"Eventos a crear: {creados} | ya existían: {ya_existian} | fechas totales: {len(items)}")
    print(f"Formato de fecha usado: {'texto ISO' if fecha_como_texto else 'entero (ms)'}")
    if omitidas:
        print(f"\nFilas OMITIDAS ({len(omitidas)}):\n  " + "\n  ".join(omitidas))
    if sospechosas:
        print(f"\nFechas fuera del año típico ({anio_tipico}) — revisar:\n  "
              + "\n  ".join(f'{i["r"]["IDEvento"]} {i["fecha"].date()}' for i in sospechosas))
    if sin_match:
        print("\nEspacios SIN match en UnidadFuncional (agregalos a MAPA_ESPACIOS o creá la UF):\n  "
              + "\n  ".join(f'"{n}" ({c} filas)' for n, c in sin_match.items()))


if __name__ == "__main__":
    main()