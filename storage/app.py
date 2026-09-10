import os
import sqlite3
from contextlib import contextmanager
from typing import Optional, List

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

DB_PATH = os.environ.get("DB_PATH", "/data/storage.db")
SPOOLMAN_URL = os.environ.get("SPOOLMAN_URL", "http://lagersystem_spoolman:8000/api/v1")

app = FastAPI(title="Lagersystem Storage Overlay")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

VALID_LAYOUTS = ("row", "depth")


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS storage_units (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                type TEXT NOT NULL,
                levels INTEGER NOT NULL,
                slots_per_level INTEGER NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                layout TEXT NOT NULL DEFAULT 'row',
                ace_feed_slot INTEGER,
                rows_per_level INTEGER NOT NULL DEFAULT 1
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS slots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                unit_id INTEGER NOT NULL REFERENCES storage_units(id) ON DELETE CASCADE,
                level INTEGER NOT NULL,
                position INTEGER NOT NULL,
                spool_id INTEGER
            )"""
        )
        # Einkaufsliste: eigenstaendige Tabelle, kein Bezug zu Spoolman-IDs (Spule kann
        # beim Anlegen des Eintrags bereits archiviert sein).
        conn.execute(
            """CREATE TABLE IF NOT EXISTS shopping_list (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                material TEXT,
                color_name TEXT,
                color_hex TEXT,
                vendor TEXT,
                note TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )"""
        )
        # Migration fuer bestehende Datenbanken (Spalten kamen nach dem ersten Release dazu)
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(storage_units)").fetchall()]
        if "layout" not in cols:
            conn.execute("ALTER TABLE storage_units ADD COLUMN layout TEXT NOT NULL DEFAULT 'row'")
        if "ace_feed_slot" not in cols:
            conn.execute("ALTER TABLE storage_units ADD COLUMN ace_feed_slot INTEGER")
        if "rows_per_level" not in cols:
            conn.execute("ALTER TABLE storage_units ADD COLUMN rows_per_level INTEGER NOT NULL DEFAULT 1")


init_db()


class UnitCreate(BaseModel):
    name: str
    type: str
    levels: int
    slots_per_level: int
    layout: str = "row"
    ace_feed_slot: Optional[int] = None
    rows_per_level: int = 1


class UnitUpdate(BaseModel):
    name: Optional[str] = None
    layout: Optional[str] = None
    ace_feed_slot: Optional[int] = None
    clear_ace_feed_slot: Optional[bool] = None
    rows_per_level: Optional[int] = None


class SlotAssign(BaseModel):
    spool_id: Optional[int] = None


class ShoppingItemCreate(BaseModel):
    material: Optional[str] = None
    color_name: Optional[str] = None
    color_hex: Optional[str] = None
    vendor: Optional[str] = None
    note: Optional[str] = None


def unit_to_dict(u, slots):
    return {
        "id": u["id"],
        "name": u["name"],
        "type": u["type"],
        "levels": u["levels"],
        "slots_per_level": u["slots_per_level"],
        "layout": u["layout"] if u["layout"] else "row",
        "ace_feed_slot": u["ace_feed_slot"],
        "rows_per_level": u["rows_per_level"] if u["rows_per_level"] else 1,
        "slots": [dict(s) for s in slots],
    }


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/units")
def list_units():
    with db() as conn:
        units = conn.execute(
            "SELECT * FROM storage_units ORDER BY sort_order, id"
        ).fetchall()
        result = []
        for u in units:
            slots = conn.execute(
                "SELECT * FROM slots WHERE unit_id=? ORDER BY level, position",
                (u["id"],),
            ).fetchall()
            result.append(unit_to_dict(u, slots))
        return result


@app.post("/units")
def create_unit(u: UnitCreate):
    if u.levels < 1 or u.slots_per_level < 1:
        raise HTTPException(400, "Ebenen und Plätze pro Ebene müssen mindestens 1 sein")
    if u.layout not in VALID_LAYOUTS:
        raise HTTPException(400, "Ungültiges Layout")
    if u.ace_feed_slot is not None and (u.ace_feed_slot < 1 or u.ace_feed_slot > u.slots_per_level):
        raise HTTPException(400, "ACE-Zuführungsplatz liegt außerhalb der Plätze pro Ebene")
    if u.rows_per_level < 1:
        raise HTTPException(400, "Reihen pro Ebene müssen mindestens 1 sein")
    if u.slots_per_level % u.rows_per_level != 0:
        raise HTTPException(400, "Plätze pro Ebene müssen durch Reihen pro Ebene teilbar sein")
    with db() as conn:
        cur = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM storage_units"
        )
        sort_order = cur.fetchone()["n"]
        cur = conn.execute(
            "INSERT INTO storage_units (name, type, levels, slots_per_level, sort_order, layout, ace_feed_slot, rows_per_level) VALUES (?,?,?,?,?,?,?,?)",
            (u.name, u.type, u.levels, u.slots_per_level, sort_order, u.layout, u.ace_feed_slot, u.rows_per_level),
        )
        unit_id = cur.lastrowid
        for level in range(u.levels):
            for pos in range(u.slots_per_level):
                conn.execute(
                    "INSERT INTO slots (unit_id, level, position, spool_id) VALUES (?,?,?,NULL)",
                    (unit_id, level, pos),
                )
        return {"id": unit_id}


@app.patch("/units/{unit_id}")
def update_unit(unit_id: int, u: UnitUpdate):
    with db() as conn:
        existing = conn.execute(
            "SELECT * FROM storage_units WHERE id=?", (unit_id,)
        ).fetchone()
        if not existing:
            raise HTTPException(404, "Gerät nicht gefunden")
        if u.name is not None:
            conn.execute(
                "UPDATE storage_units SET name=? WHERE id=?", (u.name, unit_id)
            )
        if u.layout is not None:
            if u.layout not in VALID_LAYOUTS:
                raise HTTPException(400, "Ungültiges Layout")
            conn.execute(
                "UPDATE storage_units SET layout=? WHERE id=?", (u.layout, unit_id)
            )
        if u.clear_ace_feed_slot:
            conn.execute(
                "UPDATE storage_units SET ace_feed_slot=NULL WHERE id=?", (unit_id,)
            )
        elif u.ace_feed_slot is not None:
            if u.ace_feed_slot < 1 or u.ace_feed_slot > existing["slots_per_level"]:
                raise HTTPException(400, "ACE-Zuführungsplatz liegt außerhalb der Plätze pro Ebene")
            conn.execute(
                "UPDATE storage_units SET ace_feed_slot=? WHERE id=?",
                (u.ace_feed_slot, unit_id),
            )
        if u.rows_per_level is not None:
            if u.rows_per_level < 1:
                raise HTTPException(400, "Reihen pro Ebene müssen mindestens 1 sein")
            if existing["slots_per_level"] % u.rows_per_level != 0:
                raise HTTPException(400, "Plätze pro Ebene müssen durch Reihen pro Ebene teilbar sein")
            conn.execute(
                "UPDATE storage_units SET rows_per_level=? WHERE id=?",
                (u.rows_per_level, unit_id),
            )
        return {"ok": True}


@app.delete("/units/{unit_id}")
def delete_unit(unit_id: int):
    with db() as conn:
        conn.execute("DELETE FROM slots WHERE unit_id=?", (unit_id,))
        conn.execute("DELETE FROM storage_units WHERE id=?", (unit_id,))
        return {"ok": True}


async def sync_spoolman_location(spool_id: int, location: str):
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            await client.patch(
                f"{SPOOLMAN_URL}/spool/{spool_id}", json={"location": location}
            )
        except Exception:
            # Best-effort Sync -- Slot-Zuweisung soll nicht an einem Spoolman-Hänger scheitern.
            pass


@app.put("/slots/{slot_id}")
async def assign_slot(slot_id: int, body: SlotAssign):
    with db() as conn:
        slot = conn.execute("SELECT * FROM slots WHERE id=?", (slot_id,)).fetchone()
        if not slot:
            raise HTTPException(404, "Platz nicht gefunden")
        unit = conn.execute(
            "SELECT * FROM storage_units WHERE id=?", (slot["unit_id"],)
        ).fetchone()

        if (
            body.spool_id is not None
            and unit["ace_feed_slot"]
            and (slot["position"] + 1) == unit["ace_feed_slot"]
        ):
            raise HTTPException(
                400, "Dieser Platz ist für die ACE-Zuführung reserviert und kann nicht belegt werden"
            )

        previous_spool_id = slot["spool_id"]

        if body.spool_id is not None:
            # Falls diese Spule bereits woanders liegt: dort freiräumen.
            conn.execute(
                "UPDATE slots SET spool_id=NULL WHERE spool_id=? AND id!=?",
                (body.spool_id, slot_id),
            )

        conn.execute(
            "UPDATE slots SET spool_id=? WHERE id=?", (body.spool_id, slot_id)
        )

        if body.spool_id is not None:
            if unit["levels"] > 1:
                label = f'{unit["name"]} · Ebene {slot["level"] + 1} · Platz {slot["position"] + 1}'
            else:
                label = f'{unit["name"]} · Platz {slot["position"] + 1}'
            await sync_spoolman_location(body.spool_id, label)
        elif previous_spool_id is not None:
            await sync_spoolman_location(previous_spool_id, "")

    return {"ok": True}


# ---------- Einkaufsliste ----------
# Eigenstaendige, simple Liste (kein Bezug zu Spoolman/Grocy). Wird hauptsaechlich vom
# Frontend befuellt, wenn eine leere Spule aus dem System genommen wird ("Rolle leer"),
# kann aber auch manuell gepflegt werden.

@app.get("/shopping-list")
def list_shopping_items():
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM shopping_list ORDER BY created_at DESC, id DESC"
        ).fetchall()
        return [dict(r) for r in rows]


@app.post("/shopping-list")
def create_shopping_item(item: ShoppingItemCreate):
    if not any([item.material, item.color_name, item.note]):
        raise HTTPException(400, "Bitte mindestens Material, Farbe oder Notiz angeben")
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO shopping_list (material, color_name, color_hex, vendor, note) VALUES (?,?,?,?,?)",
            (item.material, item.color_name, item.color_hex, item.vendor, item.note),
        )
        return {"id": cur.lastrowid}


@app.delete("/shopping-list/{item_id}")
def delete_shopping_item(item_id: int):
    with db() as conn:
        conn.execute("DELETE FROM shopping_list WHERE id=?", (item_id,))
        return {"ok": True}
