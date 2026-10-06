import sqlite3
c = sqlite3.connect("dev_db")
for t in ("Evento", "EventoFecha", "EventoFechaEspacio"):
    print(t, c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])