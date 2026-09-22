import os
import sqlite3
import json
from contextlib import contextmanager
from typing import Optional, List

import httpx
from fastapi import FastAPI, HTTPException, Request
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

DEFAULT_PANEL_ORDER = ["overview", "new-spool", "units", "spools", "shopping"]


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
        conn.execute(
            """CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
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


class LayoutUpdate(BaseModel):
    order: List[str]


@app.get("/layout")
def get_layout():
    with db() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key=?", ("panel_order",)).fetchone()
    if row:
        try:
            order = json.loads(row["value"])
            if isinstance(order, list) and all(isinstance(x, str) for x in order):
                return {"order": order}
        except Exception:
            pass
    return {"order": DEFAULT_PANEL_ORDER}


@app.put("/layout")
def set_layout(payload: LayoutUpdate):
    order = payload.order
    if not isinstance(order, list) or not order:
        raise HTTPException(400, "order darf nicht leer sein")
    with db() as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("panel_order", json.dumps(order)),
        )
    return {"ok": True, "order": order}



# --- Label-Designer: gespeicherte Vorlagen (Vorder-/Rueckseite, Kartenmasse, Elemente) ---
with db() as conn:
    conn.execute(
            "CREATE TABLE IF NOT EXISTS label_templates ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "name TEXT NOT NULL, "
            "side TEXT NOT NULL, "
            "width_mm REAL NOT NULL DEFAULT 85, "
            "height_mm REAL NOT NULL DEFAULT 55, "
            "elements TEXT NOT NULL, "
            "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
            ")"
    )


class LabelTemplateCreate(BaseModel):
    name: str
    side: str = "front"
    width_mm: float = 85
    height_mm: float = 55
    elements: list = []


class LabelTemplateUpdate(BaseModel):
    name: Optional[str] = None
    side: Optional[str] = None
    width_mm: Optional[float] = None
    height_mm: Optional[float] = None
    elements: Optional[list] = None


def template_to_dict(row):
    return {
            "id": row["id"],
            "name": row["name"],
            "side": row["side"],
            "width_mm": row["width_mm"],
            "height_mm": row["height_mm"],
            "elements": json.loads(row["elements"]) if row["elements"] else [],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
    }


@app.get("/label-templates")
def list_label_templates():
    with db() as conn:
        rows = conn.execute("SELECT * FROM label_templates ORDER BY id").fetchall()
        return [template_to_dict(r) for r in rows]


@app.get("/label-templates/{template_id}")
def get_label_template(template_id: int):
    with db() as conn:
        row = conn.execute("SELECT * FROM label_templates WHERE id=?", (template_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Vorlage nicht gefunden")
        return template_to_dict(row)


@app.post("/label-templates")
def create_label_template(t: LabelTemplateCreate):
    with db() as conn:
        cur = conn.execute(
                    "INSERT INTO label_templates (name, side, width_mm, height_mm, elements) VALUES (?, ?, ?, ?, ?)",
                    (t.name, t.side, t.width_mm, t.height_mm, json.dumps(t.elements)),
        )
        row = conn.execute("SELECT * FROM label_templates WHERE id=?", (cur.lastrowid,)).fetchone()
        return template_to_dict(row)


@app.put("/label-templates/{template_id}")
def update_label_template(template_id: int, t: LabelTemplateUpdate):
    with db() as conn:
        row = conn.execute("SELECT * FROM label_templates WHERE id=?", (template_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Vorlage nicht gefunden")
        fields = []
        values = []
        if t.name is not None:
            fields.append("name=?")
            values.append(t.name)
        if t.side is not None:
            fields.append("side=?")
            values.append(t.side)
        if t.width_mm is not None:
            fields.append("width_mm=?")
            values.append(t.width_mm)
        if t.height_mm is not None:
            fields.append("height_mm=?")
            values.append(t.height_mm)
        if t.elements is not None:
            fields.append("elements=?")
            values.append(json.dumps(t.elements))
        if fields:
            sql = "UPDATE label_templates SET " + ", ".join(fields) + ", updated_at=CURRENT_TIMESTAMP WHERE id=?"
            values.append(template_id)
            conn.execute(sql, tuple(values))
        row = conn.execute("SELECT * FROM label_templates WHERE id=?", (template_id,)).fetchone()
        return template_to_dict(row)


@app.delete("/label-templates/{template_id}")
def delete_label_template(template_id: int):
    with db() as conn:
        conn.execute("DELETE FROM label_templates WHERE id=?", (template_id,))
        return {"ok": True}


@app.post("/label-templates/{template_id}/duplicate")
def duplicate_label_template(template_id: int):
    with db() as conn:
        row = conn.execute("SELECT * FROM label_templates WHERE id=?", (template_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Vorlage nicht gefunden")
        d = template_to_dict(row)
        cur = conn.execute(
                    "INSERT INTO label_templates (name, side, width_mm, height_mm, elements) VALUES (?, ?, ?, ?, ?)",
                    (d["name"] + " (Kopie)", d["side"], d["width_mm"], d["height_mm"], json.dumps(d["elements"])),
        )
        newrow = conn.execute("SELECT * FROM label_templates WHERE id=?", (cur.lastrowid,)).fetchone()
        return template_to_dict(newrow)


DEFAULT_LABEL_FIELDS = [
    {"key": "spool.id", "label": "Spulen-ID", "entity": "spool"},
    {"key": "spool.remaining_weight", "label": "Restgewicht (g)", "entity": "spool"},
    {"key": "spool.location", "label": "Standort", "entity": "spool"},
    {"key": "filament.name", "label": "Filament-Name", "entity": "filament"},
    {"key": "filament.material", "label": "Material", "entity": "filament"},
    {"key": "filament.color_hex", "label": "Farbe", "entity": "filament"},
    {"key": "filament.diameter", "label": "Durchmesser (mm)", "entity": "filament"},
    {"key": "filament.density", "label": "Dichte (g/cm3)", "entity": "filament"},
    {"key": "filament.settings_extruder_temp", "label": "Duesentemperatur (C)", "entity": "filament"},
    {"key": "filament.settings_bed_temp", "label": "Betttemperatur (C)", "entity": "filament"},
    {"key": "filament.weight", "label": "Spulengewicht (g)", "entity": "filament"},
    {"key": "filament.article_number", "label": "Artikelnummer", "entity": "filament"},
    {"key": "vendor.name", "label": "Hersteller", "entity": "vendor"},
]


@app.get("/label-fields")
async def list_label_fields():
    fields = list(DEFAULT_LABEL_FIELDS)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            for entity in ("spool", "filament", "vendor"):
                resp = await client.get(SPOOLMAN_URL + "/field/" + entity)
                resp.raise_for_status()
                for cf in resp.json():
                    k = cf["key"]
                    fields.append({
                                            "key": entity + ".extra." + k,
                                            "label": cf.get("name") or k,
                                            "entity": entity,
                                            "custom": True,
                    })
    except Exception:
        pass
    return fields

# ---------- Label Export (PNG/PDF via Headless Chromium) ----------
import base64
import io
import zipfile
import qrcode
from qrcode.constants import ERROR_CORRECT_H
from PIL import Image, ImageDraw
from playwright.async_api import async_playwright
from pypdf import PdfReader, PdfWriter
from fastapi import Response


def js_str(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        if v == int(v):
            return str(int(v))
        return str(v)
    return str(v)


def ld_resolve_field(key, spool):
    if not key or key == "static":
        return ""
    parts = key.split(".")
    if parts and parts[0] == "spool":
        parts = parts[1:]
    elif parts and parts[0] == "vendor":
        parts = ["filament"] + parts
    if not parts:
        return ""
    obj = spool
    for i, p in enumerate(parts):
        if p == "extra":
            extra = (obj or {}).get("extra") or {}
            rest = ".".join(parts[i + 1:])
            v = extra.get(rest)
            if isinstance(v, str):
                try:
                    v = json.loads(v)
                except Exception:
                    pass
            return "" if v is None else v
        if obj is None:
            return ""
        obj = obj.get(p) if isinstance(obj, dict) else None
    return "" if obj is None else obj


def ld_get_color(spool):
    filament = (spool or {}).get("filament") or {}
    hexval = filament.get("color_hex") or (str(filament.get("multi_color_hexes") or "").split(",")[0].strip() or "888888")
    return "#" + str(hexval).replace("#", "").split(",")[0]


def ld_get_colors(spool):
    filament = (spool or {}).get("filament") or {}
    multi = filament.get("multi_color_hexes")
    if multi:
        raw = [c.strip().lstrip("#") for c in str(multi).split(",")]
        raw = [c for c in raw if c]
        if raw:
            return raw
    single = filament.get("color_hex")
    if single:
        return [str(single).replace("#", "").split(",")[0]]
    return ["888888"]


def ld_get_color_css(spool):
    colors = ld_get_colors(spool)
    if len(colors) <= 1:
        return "#" + colors[0]
    n = len(colors)
    stops = []
    for i, c in enumerate(colors):
        pct = round(i / (n - 1) * 100, 1) if n > 1 else 0
        stops.append("#" + c + " " + str(pct) + "%")
    return "linear-gradient(90deg, " + ", ".join(stops) + ")"


def ld_spool_icon_svg(color, multi_colors=None, direction=None):
    cx, cy, r = 50.0, 50.0, 46.0
    rx = r
    ry = r * 0.82
    bx = rx * 0.8
    by = ry * 0.8
    hub_rx = r * 0.34
    hub_ry = ry * 0.34
    hole_rx = r * 0.13
    hole_ry = ry * 0.13
    body = "<ellipse cx=\"" + str(cx) + "\" cy=\"" + str(cy) + "\" rx=\"" + str(rx) + "\" ry=\"" + str(ry) + "\" fill=\"#e2e8f0\"/>"
    if multi_colors and len(multi_colors) >= 2:
        n = len(multi_colors)
        if direction == "coaxial":
            parts = []
            for i, c in enumerate(multi_colors):
                f = 1 - i / n
                parts.append("<ellipse cx=\"" + str(cx) + "\" cy=\"" + str(cy) + "\" rx=\"" + str(bx * f) + "\" ry=\"" + str(by * f) + "\" fill=\"#" + c + "\"/>")
            fills = "".join(parts)
        else:
            stops2 = []
            for i, c in enumerate(multi_colors):
                pct2 = round(i / (n - 1) * 100, 1) if n > 1 else 0
                stops2.append("<stop offset=\"" + str(pct2) + "%\" stop-color=\"#" + c + "\"/>")
            fills = "<linearGradient id=\"spoolgrad\" x1=\"0\" y1=\"0\" x2=\"0\" y2=\"1\">" + "".join(stops2) + "</linearGradient>" + "<rect x=\"" + str(cx - bx) + "\" y=\"" + str(cy - by) + "\" width=\"" + str(bx * 2) + "\" height=\"" + str(by * 2) + "\" fill=\"url(#spoolgrad)\"/>"
        body += "<clipPath id=\"spoolclip\"><ellipse cx=\"" + str(cx) + "\" cy=\"" + str(cy) + "\" rx=\"" + str(bx) + "\" ry=\"" + str(by) + "\"/></clipPath>" + "<g clip-path=\"url(#spoolclip)\">" + fills + "</g>" + "<ellipse cx=\"" + str(cx) + "\" cy=\"" + str(cy) + "\" rx=\"" + str(bx) + "\" ry=\"" + str(by) + "\" fill=\"none\" stroke=\"rgba(0,0,0,.35)\" stroke-width=\"1\"/>"
    else:
        body += "<ellipse cx=\"" + str(cx) + "\" cy=\"" + str(cy) + "\" rx=\"" + str(bx) + "\" ry=\"" + str(by) + "\" fill=\"" + color + "\" stroke=\"rgba(0,0,0,.35)\" stroke-width=\"1\"/>"
    tail = "<ellipse cx=\"" + str(cx - bx * 0.22) + "\" cy=\"" + str(cy - by * 0.3) + "\" rx=\"" + str(bx * 0.42) + "\" ry=\"" + str(by * 0.3) + "\" fill=\"rgba(255,255,255,.22)\"/>" + "<ellipse cx=\"" + str(cx) + "\" cy=\"" + str(cy) + "\" rx=\"" + str(hub_rx) + "\" ry=\"" + str(hub_ry) + "\" fill=\"#5f5e5a\" stroke=\"rgba(0,0,0,.3)\" stroke-width=\"0.75\"/>" + "<ellipse cx=\"" + str(cx) + "\" cy=\"" + str(cy) + "\" rx=\"" + str(hole_rx) + "\" ry=\"" + str(hole_ry) + "\" fill=\"#20242c\"/>"
    return "<svg viewBox=\"0 0 100 100\" xmlns=\"http://www.w3.org/2000/svg\" style=\"width:100%;height:100%\">" + body + tail + "</svg>"

def ld_html_escape(s):
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\"", "&quot;")
    )


def _hex_to_rgb(h):
    h = str(h).lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _lerp_rgb(c1, c2, t):
    return tuple(int(round(c1[i] + (c2[i] - c1[i]) * t)) for i in range(3))


def _multicolor_fill_image(size, colors, direction):
    rgb_colors = [_hex_to_rgb(c) for c in colors]
    n = len(rgb_colors)
    img = Image.new("RGB", (size, size))
    px = img.load()
    if direction == "coaxial":
        cx = cy = (size - 1) / 2.0
        maxr = size / 2.0 if size > 0 else 1.0
        for y in range(size):
            for x in range(size):
                d = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
                fd = min(d / maxr, 1.0)
                idx = min(int(n * (1 - fd)), n - 1)
                if idx < 0:
                    idx = 0
                px[x, y] = rgb_colors[idx]
        return img
    row_colors = []
    for y in range(size):
        t = y / (size - 1) if size > 1 else 0
        seg = t * (n - 1) if n > 1 else 0
        i = min(int(seg), n - 2) if n > 1 else 0
        local_t = seg - i
        col = _lerp_rgb(rgb_colors[i], rgb_colors[min(i + 1, n - 1)], local_t) if n > 1 else rgb_colors[0]
        row_colors.append(col)
    for y in range(size):
        for x in range(size):
            px[x, y] = row_colors[y]
    return img


def _draw_brand_mark(icon, box):
    x0, y0, x1, y1 = box
    cx = (x0 + x1) / 2.0
    cy = (y0 + y1) / 2.0
    half = (x1 - x0) / 2.0
    draw = ImageDraw.Draw(icon, "RGBA")
    def bbox(rr):
        return [cx - rr, cy - rr, cx + rr, cy + rr]
    r_outer = half * 0.90
    r_mid = half * 0.60
    r_gray = half * 0.30
    r_dot = half * 0.11
    w_outer = max(1, int(half * 0.14))
    w_mid = max(1, int(half * 0.10))
    draw.arc(bbox(r_outer), 225, 45, fill=(56, 189, 248, 255), width=w_outer)
    draw.arc(bbox(r_outer), 45, 225, fill=(167, 139, 250, 255), width=w_outer)
    draw.arc(bbox(r_mid), 225, 45, fill=(56, 189, 248, 160), width=w_mid)
    draw.arc(bbox(r_mid), 45, 225, fill=(167, 139, 250, 160), width=w_mid)
    draw.ellipse(bbox(r_gray), fill=(95, 94, 90, 255))
    draw.ellipse(bbox(r_dot), fill=(15, 23, 42, 255))


def make_qr_png_b64(data, color_hex, multi_colors=None, direction=None):
    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_H, box_size=10, border=4)
    qr.add_data(data or "")
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGBA")
    w, h = img.size
    icon_size = int(w * 0.26)
    pad_size = int(icon_size * 1.18)
    pad = Image.new("RGBA", (pad_size, pad_size), (255, 255, 255, 255))
    icon = Image.new("RGBA", (icon_size, icon_size), (0, 0, 0, 0))
    if multi_colors and len(multi_colors) >= 2:
        mask = Image.new("L", (icon_size, icon_size), 0)
        mdraw = ImageDraw.Draw(mask)
        mdraw.ellipse([0, 0, icon_size - 1, icon_size - 1], fill=255)
        fill_img = _multicolor_fill_image(icon_size, multi_colors, direction).convert("RGBA")
        icon.paste(fill_img, (0, 0), mask)
        draw = ImageDraw.Draw(icon)
        draw.ellipse([0, 0, icon_size - 1, icon_size - 1], outline=(0, 0, 0, 90))
    else:
        draw = ImageDraw.Draw(icon)
        draw.ellipse([0, 0, icon_size - 1, icon_size - 1], fill=color_hex, outline=(0, 0, 0, 90))
    inner = int(icon_size * 0.34)
    off = (icon_size - inner) // 2
    draw = ImageDraw.Draw(icon)
    draw.ellipse([off, off, off + inner, off + inner], fill=(255, 255, 255, 255), outline=(0, 0, 0, 90))
    _draw_brand_mark(icon, [off, off, off + inner, off + inner])
    pad.paste(icon, ((pad_size - icon_size) // 2, (pad_size - icon_size) // 2), icon)
    img.paste(pad, ((w - pad_size) // 2, (h - pad_size) // 2), pad)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def build_label_html(tpl, spool):
    w = tpl["width_mm"]
    h = tpl["height_mm"]
    color = ld_get_color(spool)
    parts = []
    for el in tpl["elements"]:
        x = el.get("x", 0)
        y = el.get("y", 0)
        ew = el.get("w", 10)
        eh = el.get("h", 10)
        style = "position:absolute;left:" + js_str(x) + "mm;top:" + js_str(y) + "mm;width:" + js_str(ew) + "mm;height:" + js_str(eh) + "mm;box-sizing:border-box;"
        t = el.get("type")
        if t == "text":
            field = el.get("field")
            if not field or field == "static":
                text = el.get("staticText") or ""
            else:
                v = ld_resolve_field(field, spool)
                text = "" if v is None else js_str(v)
            align_map = {"left": "flex-start", "center": "center", "right": "flex-end"}
            align = align_map.get(el.get("align", "left"), "flex-start")
            weight = "700" if el.get("bold") else "400"
            fs = el.get("fontSize", 3.2)
            fcolor = el.get("color") or "#111111"
            parts.append(
                "<div style='" + style + "display:flex;align-items:center;justify-content:" + align +
                ";font-size:" + js_str(fs) + "mm;font-weight:" + weight + ";color:" + fcolor +
                ";font-family:Arial,Helvetica,sans-serif;overflow:hidden;white-space:nowrap;line-height:1.1;'>" +
                ld_html_escape(text) + "</div>"
            )
        elif t == "qr":
            field = el.get("field")
            v = ld_resolve_field(field, spool) if field else ""
            if field == "spool.id" and v not in ("", None):
                data = "WEB+SPOOLMAN:S-" + js_str(v)
            else:
                data = js_str(v) if v != "" else ""
            qr_b64 = make_qr_png_b64(data, color, (ld_get_colors(spool) if len(ld_get_colors(spool)) > 1 else None), ((spool or {}).get("filament") or {}).get("multi_color_direction"))
            parts.append(
                "<div style='" + style + "background:#ffffff;display:flex;align-items:center;justify-content:center;'><img src='data:image/png;base64," + qr_b64 +
                "' style='width:100%;height:100%;object-fit:contain;object-position:center;display:block;'/></div>"
            )
        elif t == "color":
            parts.append("<div style='" + style + "background:" + ld_get_color_css(spool) + ";'></div>")
        elif t == "rect":
            fill = el.get("fill") or "#38bdf8"
            border = "border:0.3mm solid #111111;" if el.get("border") else ""
            parts.append("<div style='" + style + "background:" + fill + ";" + border + "'></div>")
        elif t == "icon":
            parts.append("<div style='" + style + "'>" + ld_spool_icon_svg(color, (ld_get_colors(spool) if len(ld_get_colors(spool)) > 1 else None), ((spool or {}).get("filament") or {}).get("multi_color_direction")) + "</div>")
    body = "".join(parts)
    html = (
        "<!doctype html><html><head><meta charset='utf-8'><style>"
        "*{margin:0;padding:0;box-sizing:border-box;}"
        "html,body{width:" + js_str(w) + "mm;height:" + js_str(h) + "mm;background:#ffffff;}"
        ".label{position:relative;width:" + js_str(w) + "mm;height:" + js_str(h) + "mm;overflow:hidden;background:#ffffff;}"
        "</style></head><body><div class='label'>" + body + "</div></body></html>"
    )
    return html


class LabelExportRequest(BaseModel):
    spool_ids: List[int]
    format: str = "png"
    dpi: int = 300


async def fetch_spool(client, spool_id):
    resp = await client.get(SPOOLMAN_URL + "/spool/" + str(spool_id))
    resp.raise_for_status()
    return resp.json()


async def render_label(browser, html, width_mm, height_mm, dpi, fmt):
    css_w = round(width_mm / 25.4 * 96)
    css_h = round(height_mm / 25.4 * 96)
    page = await browser.new_page(viewport={"width": css_w, "height": css_h}, device_scale_factor=dpi / 96)
    try:
        await page.set_content(html, wait_until="load")
        if fmt == "png":
            return await page.screenshot(type="png")
        return await page.pdf(
            width=str(width_mm) + "mm",
            height=str(height_mm) + "mm",
            print_background=True,
            margin={"top": "0mm", "bottom": "0mm", "left": "0mm", "right": "0mm"},
        )
    finally:
        await page.close()


@app.post("/label-templates/{template_id}/export")
async def export_label_template(template_id: int, req: LabelExportRequest):
    with db() as conn:
        row = conn.execute("SELECT * FROM label_templates WHERE id=?", (template_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Vorlage nicht gefunden")
    tpl = template_to_dict(row)
    if not req.spool_ids:
        raise HTTPException(400, "Keine Spulen ausgewaehlt")
    if req.format not in ("png", "pdf"):
        raise HTTPException(400, "Ungueltiges Format (png oder pdf)")
    dpi = max(72, min(req.dpi or 300, 600))

    spools = []
    async with httpx.AsyncClient(timeout=15) as client:
        for sid in req.spool_ids:
            try:
                spools.append(await fetch_spool(client, sid))
            except Exception:
                raise HTTPException(400, "Spule " + str(sid) + " nicht gefunden")

    outputs = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(args=["--no-sandbox"])
        try:
            for spool in spools:
                html = build_label_html(tpl, spool)
                data = await render_label(browser, html, tpl["width_mm"], tpl["height_mm"], dpi, req.format)
                outputs.append(data)
        finally:
            await browser.close()

    if req.format == "png":
        if len(outputs) == 1:
            return Response(content=outputs[0], media_type="image/png", headers={
                "Content-Disposition": "attachment; filename=\"label_" + str(template_id) + "_" + str(req.spool_ids[0]) + ".png\""
            })
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for sid, data in zip(req.spool_ids, outputs):
                zf.writestr("label_" + str(sid) + ".png", data)
        return Response(content=buf.getvalue(), media_type="application/zip", headers={
            "Content-Disposition": "attachment; filename=\"labels_" + str(template_id) + ".zip\""
        })
    else:
        if len(outputs) == 1:
            return Response(content=outputs[0], media_type="application/pdf", headers={
                "Content-Disposition": "attachment; filename=\"label_" + str(template_id) + ".pdf\""
            })
        writer = PdfWriter()
        for data in outputs:
            reader = PdfReader(io.BytesIO(data))
            for pg in reader.pages:
                writer.add_page(pg)
        out = io.BytesIO()
        writer.write(out)
        return Response(content=out.getvalue(), media_type="application/pdf", headers={
            "Content-Disposition": "attachment; filename=\"labels_" + str(template_id) + ".pdf\""
        })


# --- Stufe 1: Slot-Label-Generierung & Druckansicht (2026-09-20) ---
from fastapi.responses import HTMLResponse
from PIL import ImageFont as _ImageFont


def _slot_label_png(slot_id):
    with db() as conn:
        slot = conn.execute("SELECT * FROM slots WHERE id=?", (slot_id,)).fetchone()
        if not slot:
            raise HTTPException(404, "slot not found")
        unit = conn.execute(
            "SELECT * FROM storage_units WHERE id=?", (slot["unit_id"],)
        ).fetchone()
    data = f"WEB+LAGER:SLOT-{slot_id}"
    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_H, box_size=8, border=4)
    qr.add_data(data)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    W = 420
    qr_size = 230
    card = Image.new("RGB", (W, W), "white")
    qr_img = qr_img.resize((qr_size, qr_size))
    qr_x = (W - qr_size) // 2
    card.paste(qr_img, (qr_x, 16))
    draw = ImageDraw.Draw(card)
    try:
        font_name = _ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 38
        )
        font_slot = _ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 34
        )
        font_small = _ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 26
        )
    except Exception:
        font_name = font_slot = font_small = _ImageFont.load_default()

    unit_name = unit["name"] if unit else "?"
    y = qr_size + 30

    def centered(text, font, y, fill="black"):
        bbox = draw.textbbox((0, 0), text, font=font)
        w = bbox[2] - bbox[0]
        draw.text(((W - w) / 2, y), text, fill=fill, font=font)

    centered(unit_name, font_name, y)
    y += 48
    centered(f"Ebene {slot['level'] + 1} / Platz {slot['position'] + 1}", font_slot, y)
    y += 42
    centered(f"Slot #{slot_id}", font_small, y, fill="black")

    buf = io.BytesIO()
    card.save(buf, format="PNG")
    return buf.getvalue()


@app.get("/slots/{slot_id}/label.png")
def slot_label_png(slot_id: int):
    png = _slot_label_png(slot_id)
    return Response(content=png, media_type="image/png")


@app.get("/units/{unit_id}/labels-print", response_class=HTMLResponse)
def unit_labels_print(unit_id: int, cols: int = 4):
    cols = max(2, min(cols, 6))
    with db() as conn:
        unit = conn.execute(
            "SELECT * FROM storage_units WHERE id=?", (unit_id,)
        ).fetchone()
        if not unit:
            raise HTTPException(404, "unit not found")
        slots = conn.execute(
            "SELECT * FROM slots WHERE unit_id=? ORDER BY level, position",
            (unit_id,),
        ).fetchall()
    imgs = "".join(
        f'<div class="label"><img src="/api/storage/slots/{s["id"]}/label.png"></div>'
        for s in slots
    )
    opts = "".join(
        f'<option value="{n}"{" selected" if n == cols else ""}>{n} pro Reihe</option>'
        for n in (3, 4, 5, 6)
    )
    html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Labels {unit["name"]}</title>
<style>
  body {{ font-family: sans-serif; margin:0; padding: 16px; }}
  .toolbar {{ margin-bottom: 16px; display:flex; gap:12px; align-items:center; }}
  .toolbar button {{ padding:8px 16px; font-size:16px; }}
  .toolbar select {{ padding:6px 10px; font-size:15px; }}
  .grid {{ display:grid; grid-template-columns: repeat({cols}, 1fr); gap: 6mm; }}
  .label img {{ width: 100%; display:block; border: 1px dashed #ccc; }}
  @media print {{
    .toolbar {{ display:none; }}
    .label {{ break-inside: avoid; }}
  }}
</style></head>
<body>
<div class="toolbar">
  <button onclick="window.print()">Drucken</button>
  <label>Spalten:
    <select onchange="location.href='?cols=' + this.value">{opts}</select>
  </label>
</div>
<h2>{unit["name"]} \u2013 Slot-Labels</h2>
<div class="grid">{imgs}</div>
</body></html>"""
    return html
