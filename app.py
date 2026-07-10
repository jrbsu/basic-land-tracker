from __future__ import annotations

import csv
import datetime as dt
import gzip
import io
import json
import os
import re
import sqlite3
from collections import defaultdict, OrderedDict
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

import urllib.error
import urllib.parse
import urllib.request

import ijson
from flask import (
    Flask,
    Response,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "basic_lands.sqlite3"

LAND_ORDER = ["Plains", "Island", "Swamp", "Mountain", "Forest"]
EXTRA_LANDS = [
    "Wastes",
    "Snow-Covered Plains",
    "Snow-Covered Island",
    "Snow-Covered Swamp",
    "Snow-Covered Mountain",
    "Snow-Covered Forest",
]
ALL_LANDS = LAND_ORDER + EXTRA_LANDS
FINISH_ORDER = ["nonfoil", "foil", "etched"]

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "local-dev-basic-land-tracker")
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_UPLOAD_MB", "2048")) * 1024 * 1024


# -----------------------------------------------------------------------------
# Database
# -----------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    if "db" not in g:
        DATA_DIR.mkdir(exist_ok=True)
        db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        g.db = db
    return g.db


@app.teardown_appcontext
def close_db(error: Optional[BaseException] = None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    UPLOAD_DIR.mkdir(exist_ok=True)
    with closing(sqlite3.connect(DB_PATH)) as db:
        db.execute("PRAGMA foreign_keys = ON")
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS cards (
                scryfall_id TEXT PRIMARY KEY,
                oracle_id TEXT,
                name TEXT NOT NULL,
                land_type TEXT NOT NULL,
                set_code TEXT NOT NULL,
                set_name TEXT NOT NULL,
                set_type TEXT,
                released_at TEXT,
                collector_number TEXT NOT NULL,
                collector_sort INTEGER,
                lang TEXT,
                layout TEXT,
                image_small TEXT,
                image_normal TEXT,
                scryfall_uri TEXT,
                finishes_json TEXT NOT NULL DEFAULT '[]',
                nonfoil INTEGER NOT NULL DEFAULT 0,
                foil INTEGER NOT NULL DEFAULT 0,
                etched INTEGER NOT NULL DEFAULT 0,
                full_art INTEGER NOT NULL DEFAULT 0,
                promo INTEGER NOT NULL DEFAULT 0,
                variation INTEGER NOT NULL DEFAULT 0,
                digital INTEGER NOT NULL DEFAULT 0,
                border_color TEXT,
                frame TEXT,
                rarity TEXT,
                artist TEXT,
                treatment TEXT NOT NULL DEFAULT 'Regular',
                imported_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_cards_set ON cards(set_code, collector_sort, collector_number);
            CREATE INDEX IF NOT EXISTS idx_cards_land ON cards(land_type);
            CREATE INDEX IF NOT EXISTS idx_cards_treatment ON cards(treatment);
            CREATE INDEX IF NOT EXISTS idx_cards_set_name ON cards(set_name);

            CREATE TABLE IF NOT EXISTS owned (
                scryfall_id TEXT NOT NULL,
                finish TEXT NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 1,
                source TEXT NOT NULL DEFAULT 'manual',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (scryfall_id, finish),
                FOREIGN KEY (scryfall_id) REFERENCES cards(scryfall_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_owned_finish ON owned(finish);

            CREATE TABLE IF NOT EXISTS import_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                import_type TEXT NOT NULL,
                filename TEXT,
                rows_seen INTEGER NOT NULL DEFAULT 0,
                rows_imported INTEGER NOT NULL DEFAULT 0,
                rows_matched INTEGER NOT NULL DEFAULT 0,
                rows_unmatched INTEGER NOT NULL DEFAULT 0,
                message TEXT,
                created_at TEXT NOT NULL
            );
            """
        )
        db.commit()


@app.cli.command("init-db")
def init_db_command() -> None:
    init_db()
    print(f"Initialized {DB_PATH}")


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def bool_int(value: Any) -> int:
    return 1 if bool(value) else 0


def collector_sort_value(collector_number: str) -> int:
    """Return a useful numeric-ish sort value for collector numbers like 280, 280a, A-23."""
    if not collector_number:
        return 999999
    match = re.search(r"\d+", collector_number)
    return int(match.group(0)) if match else 999999


def normalize_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def first_present(row: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        if key in row and row[key] is not None and str(row[key]).strip() != "":
            return str(row[key]).strip()
    return ""


def parse_quantity(value: str) -> int:
    if not value:
        return 1
    value = str(value).strip()
    match = re.search(r"\d+", value)
    if not match:
        return 1
    return max(0, int(match.group(0)))


def normalize_finish(value: str, fallback: str = "nonfoil") -> str:
    if value is None:
        return fallback
    raw = str(value).strip().lower()
    if raw in {"", "normal", "regular", "nonfoil", "non-foil", "nf", "false", "no", "n", "0"}:
        return "nonfoil"
    if raw in {"foil", "f", "true", "yes", "y", "1"}:
        return "foil"
    if raw in {"etched", "foil-etched", "etched foil", "e"}:
        return "etched"
    if "etched" in raw:
        return "etched"
    if "foil" in raw:
        return "foil"
    return fallback


def derive_treatment(card: Dict[str, Any]) -> str:
    name = card.get("name", "")
    frame_effects = card.get("frame_effects") or []
    promo_types = card.get("promo_types") or []
    finishes = card.get("finishes") or []

    if name.startswith("Snow-Covered"):
        return "Snow"
    if name == "Wastes":
        return "Wastes"
    if "etched" in finishes:
        return "Etched available"
    if "showcase" in frame_effects:
        return "Showcase"
    if "extendedart" in frame_effects:
        return "Extended art"
    if card.get("full_art"):
        return "Full art"
    if card.get("promo") or promo_types:
        return "Promo"
    if card.get("border_color") == "borderless":
        return "Borderless"
    return "Regular"


class ScryfallError(RuntimeError):
    """Raised when the Scryfall API cannot be reached or returns an unexpected error."""


class ScryfallNotFound(ScryfallError):
    """Raised when Scryfall has no card for the requested set/collector number."""


SCRYFALL_HEADERS = {
    "User-Agent": "BasicLandTracker/1.0 (local desktop app)",
    "Accept": "application/json;q=0.9,*/*;q=0.8",
}


def fetch_scryfall_card(set_code: str, collector_number: str) -> Dict[str, Any]:
    """Look up a single printing directly from the Scryfall API."""
    url = "https://api.scryfall.com/cards/{}/{}".format(
        urllib.parse.quote(set_code, safe=""),
        urllib.parse.quote(collector_number, safe=""),
    )
    req = urllib.request.Request(url, headers=SCRYFALL_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ScryfallNotFound(f"No Scryfall card found for set '{set_code}' #{collector_number}.") from exc
        raise ScryfallError(f"Scryfall returned HTTP {exc.code}.") from exc
    except urllib.error.URLError as exc:
        raise ScryfallError(f"Could not reach Scryfall: {exc.reason}") from exc
    except TimeoutError as exc:
        raise ScryfallError("Scryfall request timed out.") from exc


def get_image_uris(card: Dict[str, Any]) -> Tuple[str, str]:
    image_uris = card.get("image_uris") or {}
    if not image_uris and card.get("card_faces"):
        image_uris = card["card_faces"][0].get("image_uris") or {}
    return image_uris.get("small", ""), image_uris.get("normal", "")


def is_basic_land_card(card: Dict[str, Any], include_extras: bool = True) -> bool:
    if card.get("lang") != "en":
        return False
    if card.get("layout") in {"art_series", "token", "emblem", "planar", "scheme", "vanguard"}:
        return False
    name = card.get("name", "")
    accepted = ALL_LANDS if include_extras else LAND_ORDER
    if name not in accepted:
        return False
    type_line = card.get("type_line", "")
    return "Basic" in type_line and "Land" in type_line


def open_text_file(path: Path) -> io.TextIOBase:
    if path.suffix.lower() == ".gz":
        return io.TextIOWrapper(gzip.open(path, "rb"), encoding="utf-8")
    return path.open("r", encoding="utf-8")


def iter_scryfall_cards(path: Path) -> Iterator[Dict[str, Any]]:
    """Stream either Scryfall bulk JSON array or newline-delimited JSON."""
    with open_text_file(path) as f:
        # Peek the first non-whitespace character without loading the whole file.
        first = ""
        while True:
            char = f.read(1)
            if char == "":
                return
            if not char.isspace():
                first = char
                break
        f.seek(0)
        if first == "[":
            yield from ijson.items(f, "item")
        else:
            for line in f:
                line = line.strip().rstrip(",")
                if not line:
                    continue
                yield json.loads(line)


def save_uploaded_file(field_name: str) -> Optional[Path]:
    uploaded = request.files.get(field_name)
    if not uploaded or uploaded.filename == "":
        return None
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    filename = secure_filename(uploaded.filename)
    path = UPLOAD_DIR / filename
    uploaded.save(path)
    return path


def path_from_form_or_upload(field_name: str, path_field: str) -> Optional[Path]:
    path_text = request.form.get(path_field, "").strip()
    if path_text:
        path = Path(path_text).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"File does not exist: {path}")
        return path
    return save_uploaded_file(field_name)


def row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    d["available_finishes"] = json.loads(d.pop("finishes_json") or "[]")
    d["owned_finishes"] = set()
    return d


def log_import(
    import_type: str,
    filename: Optional[str],
    rows_seen: int,
    rows_imported: int,
    rows_matched: int = 0,
    rows_unmatched: int = 0,
    message: str = "",
) -> None:
    db = get_db()
    db.execute(
        """
        INSERT INTO import_log
            (import_type, filename, rows_seen, rows_imported, rows_matched, rows_unmatched, message, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (import_type, filename, rows_seen, rows_imported, rows_matched, rows_unmatched, message, now_iso()),
    )
    db.commit()


# -----------------------------------------------------------------------------
# Importers
# -----------------------------------------------------------------------------

CARD_UPSERT_SQL = """
    INSERT INTO cards (
        scryfall_id, oracle_id, name, land_type, set_code, set_name, set_type, released_at,
        collector_number, collector_sort, lang, layout, image_small, image_normal, scryfall_uri,
        finishes_json, nonfoil, foil, etched, full_art, promo, variation, digital,
        border_color, frame, rarity, artist, treatment, imported_at
    ) VALUES (
        :scryfall_id, :oracle_id, :name, :land_type, :set_code, :set_name, :set_type, :released_at,
        :collector_number, :collector_sort, :lang, :layout, :image_small, :image_normal, :scryfall_uri,
        :finishes_json, :nonfoil, :foil, :etched, :full_art, :promo, :variation, :digital,
        :border_color, :frame, :rarity, :artist, :treatment, :imported_at
    )
    ON CONFLICT(scryfall_id) DO UPDATE SET
        oracle_id=excluded.oracle_id,
        name=excluded.name,
        land_type=excluded.land_type,
        set_code=excluded.set_code,
        set_name=excluded.set_name,
        set_type=excluded.set_type,
        released_at=excluded.released_at,
        collector_number=excluded.collector_number,
        collector_sort=excluded.collector_sort,
        lang=excluded.lang,
        layout=excluded.layout,
        image_small=excluded.image_small,
        image_normal=excluded.image_normal,
        scryfall_uri=excluded.scryfall_uri,
        finishes_json=excluded.finishes_json,
        nonfoil=excluded.nonfoil,
        foil=excluded.foil,
        etched=excluded.etched,
        full_art=excluded.full_art,
        promo=excluded.promo,
        variation=excluded.variation,
        digital=excluded.digital,
        border_color=excluded.border_color,
        frame=excluded.frame,
        rarity=excluded.rarity,
        artist=excluded.artist,
        treatment=excluded.treatment,
        imported_at=excluded.imported_at
"""


def build_card_row(card: Dict[str, Any], imported_at: str) -> Optional[Dict[str, Any]]:
    """Turn a raw Scryfall card object into a `cards` table row, or None if it has no finishes."""
    small, normal = get_image_uris(card)
    finishes = [f for f in (card.get("finishes") or []) if f in FINISH_ORDER]
    if not finishes:
        return None

    return {
        "scryfall_id": card.get("id"),
        "oracle_id": card.get("oracle_id"),
        "name": card.get("name"),
        "land_type": card.get("name"),
        "set_code": (card.get("set") or "").lower(),
        "set_name": card.get("set_name") or "Unknown set",
        "set_type": card.get("set_type"),
        "released_at": card.get("released_at"),
        "collector_number": str(card.get("collector_number") or ""),
        "collector_sort": collector_sort_value(str(card.get("collector_number") or "")),
        "lang": card.get("lang"),
        "layout": card.get("layout"),
        "image_small": small,
        "image_normal": normal,
        "scryfall_uri": card.get("scryfall_uri"),
        "finishes_json": json.dumps(finishes),
        "nonfoil": bool_int("nonfoil" in finishes),
        "foil": bool_int("foil" in finishes),
        "etched": bool_int("etched" in finishes),
        "full_art": bool_int(card.get("full_art")),
        "promo": bool_int(card.get("promo")),
        "variation": bool_int(card.get("variation")),
        "digital": bool_int(card.get("digital")),
        "border_color": card.get("border_color"),
        "frame": card.get("frame"),
        "rarity": card.get("rarity"),
        "artist": card.get("artist"),
        "treatment": derive_treatment(card),
        "imported_at": imported_at,
    }


def import_scryfall_bulk(path: Path, replace_existing: bool = True, paper_only: bool = True) -> Dict[str, Any]:
    db = get_db()
    rows_seen = 0
    rows_imported = 0
    timestamp = now_iso()

    # Do not wipe ownership before importing. Scryfall IDs are stable, so after the
    # import finishes we can remove stale catalogue rows while preserving ownership
    # for cards that still exist in the refreshed data.

    batch: List[Dict[str, Any]] = []
    for card in iter_scryfall_cards(path):
        rows_seen += 1
        if rows_seen % 50000 == 0:
            db.executemany(CARD_UPSERT_SQL, batch)
            db.commit()
            batch.clear()

        if paper_only and "paper" not in (card.get("games") or []):
            continue
        if not is_basic_land_card(card, include_extras=True):
            continue

        row = build_card_row(card, timestamp)
        if row is None:
            continue

        batch.append(row)
        rows_imported += 1

    if batch:
        db.executemany(CARD_UPSERT_SQL, batch)
        db.commit()

    if replace_existing:
        db.execute(
            "DELETE FROM owned WHERE scryfall_id IN (SELECT scryfall_id FROM cards WHERE imported_at != ?)",
            (timestamp,),
        )
        db.execute("DELETE FROM cards WHERE imported_at != ?", (timestamp,))
        db.commit()

    log_import("scryfall", str(path), rows_seen, rows_imported, message="Scryfall bulk import completed")
    return {"rows_seen": rows_seen, "rows_imported": rows_imported}


def normalized_csv_rows(path: Path) -> Iterator[Dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            return
        normalized = {field: normalize_header(field) for field in reader.fieldnames}
        for raw_row in reader:
            yield {normalized[key]: (value or "").strip() for key, value in raw_row.items() if key is not None}


def match_collection_row(db: sqlite3.Connection, row: Dict[str, str]) -> Optional[str]:
    scryfall_id = first_present(
        row,
        "scryfall_id",
        "scryfallid",
        "scryfall_uuid",
        "card_id",
        "id",
        "uuid",
    )
    if scryfall_id:
        found = db.execute("SELECT scryfall_id FROM cards WHERE scryfall_id = ?", (scryfall_id,)).fetchone()
        if found:
            return found["scryfall_id"]

    name = first_present(row, "name", "card_name", "card")
    collector_number = first_present(
        row,
        "collector_number",
        "collector_no",
        "number",
        "cn",
        "collector",
    )
    set_value = first_present(
        row,
        "set_code",
        "set",
        "edition_code",
        "edition",
        "set_name",
        "expansion",
    )
    if not name:
        return None

    params: List[Any] = [name]
    where = ["LOWER(name) = LOWER(?)"]

    if collector_number:
        where.append("collector_number = ?")
        params.append(collector_number)

    if set_value:
        # If it looks like a set code, match code. Otherwise also try set name.
        if re.fullmatch(r"[a-zA-Z0-9]{2,6}", set_value):
            where.append("LOWER(set_code) = LOWER(?)")
            params.append(set_value)
        else:
            where.append("LOWER(set_name) = LOWER(?)")
            params.append(set_value)

    found = db.execute(
        f"SELECT scryfall_id FROM cards WHERE {' AND '.join(where)} LIMIT 1", params
    ).fetchone()
    if found:
        return found["scryfall_id"]

    # Fallback: some exports use set name in 'edition' but short codes in 'set'.
    if name and set_value and collector_number:
        found = db.execute(
            """
            SELECT scryfall_id FROM cards
            WHERE LOWER(name) = LOWER(?)
              AND collector_number = ?
              AND (LOWER(set_code) = LOWER(?) OR LOWER(set_name) = LOWER(?))
            LIMIT 1
            """,
            (name, collector_number, set_value, set_value),
        ).fetchone()
        if found:
            return found["scryfall_id"]

    return None


def import_collection_csv(path: Path, clear_existing: bool = True, source: str = "moxfield") -> Dict[str, Any]:
    db = get_db()
    rows_seen = 0
    rows_matched = 0
    rows_unmatched = 0
    unmatched_examples: List[Dict[str, str]] = []

    if clear_existing:
        db.execute("DELETE FROM owned")
        db.commit()

    for row in normalized_csv_rows(path):
        rows_seen += 1
        scryfall_id = match_collection_row(db, row)
        if not scryfall_id:
            rows_unmatched += 1
            if len(unmatched_examples) < 10:
                unmatched_examples.append(row)
            continue

        quantity = parse_quantity(first_present(row, "quantity", "count", "owned", "total_quantity"))
        if quantity <= 0:
            continue

        finish_raw = first_present(
            row,
            "finish",
            "foil",
            "printing",
            "is_foil",
            "isfoil",
            "variant",
        )
        finish = normalize_finish(finish_raw)
        available = db.execute("SELECT finishes_json FROM cards WHERE scryfall_id = ?", (scryfall_id,)).fetchone()
        available_finishes = json.loads(available["finishes_json"] or "[]") if available else []
        if finish not in available_finishes:
            # Do not drop the card: fall back to the first available finish.
            finish = available_finishes[0] if available_finishes else "nonfoil"

        db.execute(
            """
            INSERT INTO owned (scryfall_id, finish, quantity, source, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(scryfall_id, finish) DO UPDATE SET
                quantity = owned.quantity + excluded.quantity,
                source = excluded.source,
                updated_at = excluded.updated_at
            """,
            (scryfall_id, finish, quantity, source, now_iso()),
        )
        rows_matched += 1

    db.commit()
    message = "Collection import completed"
    if unmatched_examples:
        message += "; first unmatched examples: " + json.dumps(unmatched_examples[:3])
    log_import(
        "collection",
        str(path),
        rows_seen,
        rows_imported=rows_matched,
        rows_matched=rows_matched,
        rows_unmatched=rows_unmatched,
        message=message,
    )
    return {
        "rows_seen": rows_seen,
        "rows_matched": rows_matched,
        "rows_unmatched": rows_unmatched,
        "unmatched_examples": unmatched_examples,
    }


# -----------------------------------------------------------------------------
# Query builders
# -----------------------------------------------------------------------------

def set_summaries(search: str = "", include_extras: bool = False, hide_complete: bool = False) -> List[Dict[str, Any]]:
    db = get_db()
    where = []
    params: List[Any] = []
    if not include_extras:
        where.append("c.land_type IN ({})".format(",".join("?" for _ in LAND_ORDER)))
        params.extend(LAND_ORDER)
    if search:
        where.append("(LOWER(c.set_code) LIKE LOWER(?) OR LOWER(c.set_name) LIKE LOWER(?))")
        params.extend([f"%{search}%", f"%{search}%"])
    where_sql = "WHERE " + " AND ".join(where) if where else ""

    rows = db.execute(
        f"""
        WITH filtered_cards AS (
            SELECT c.*
            FROM cards c
            {where_sql}
        ),
        card_summary AS (
            SELECT
                set_code,
                set_name,
                MAX(released_at) AS released_at,
                COUNT(*) AS card_count,
                SUM(nonfoil + foil + etched) AS needed_finishes
            FROM filtered_cards
            GROUP BY set_code, set_name
        ),
        owned_summary AS (
            SELECT
                fc.set_code,
                fc.set_name,
                COUNT(*) AS owned_finishes
            FROM filtered_cards fc
            JOIN owned o ON o.scryfall_id = fc.scryfall_id
            WHERE o.quantity > 0
            GROUP BY fc.set_code, fc.set_name
        )
        SELECT
            cs.set_code,
            cs.set_name,
            cs.released_at,
            cs.card_count,
            cs.needed_finishes,
            COALESCE(os.owned_finishes, 0) AS owned_finishes
        FROM card_summary cs
        LEFT JOIN owned_summary os
            ON os.set_code = cs.set_code AND os.set_name = cs.set_name
        ORDER BY cs.released_at DESC, cs.set_name ASC
        """,
        params,
    ).fetchall()

    summaries = []
    for row in rows:
        needed = row["needed_finishes"] or 0
        owned = row["owned_finishes"] or 0
        complete = needed > 0 and owned >= needed
        if hide_complete and complete:
            continue
        summaries.append(
            {
                **dict(row),
                "needed_finishes": needed,
                "owned_finishes": owned,
                "percent": round((owned / needed) * 100, 1) if needed else 0,
                "complete": complete,
            }
        )
    return summaries


def build_set_grid(
    set_code: Optional[str] = None,
    search: str = "",
    include_extras: bool = False,
    hide_complete: bool = False,
) -> List[Dict[str, Any]]:
    db = get_db()
    where = []
    params: List[Any] = []
    if set_code:
        where.append("LOWER(c.set_code) = LOWER(?)")
        params.append(set_code)
    if not include_extras:
        where.append("c.land_type IN ({})".format(",".join("?" for _ in LAND_ORDER)))
        params.extend(LAND_ORDER)
    if search:
        where.append("(LOWER(c.set_code) LIKE LOWER(?) OR LOWER(c.set_name) LIKE LOWER(?))")
        params.extend([f"%{search}%", f"%{search}%"])
    where_sql = "WHERE " + " AND ".join(where) if where else ""

    card_rows = db.execute(
        f"""
        SELECT c.*
        FROM cards c
        {where_sql}
        ORDER BY c.released_at DESC, c.set_name ASC, c.treatment ASC,
                 c.collector_sort ASC, c.collector_number ASC
        """,
        params,
    ).fetchall()

    owned_rows = db.execute("SELECT scryfall_id, finish, quantity FROM owned WHERE quantity > 0").fetchall()
    owned_map: Dict[str, set] = defaultdict(set)
    for row in owned_rows:
        owned_map[row["scryfall_id"]].add(row["finish"])

    set_map: "OrderedDict[Tuple[str, str], Dict[str, Any]]" = OrderedDict()
    land_order = ALL_LANDS if include_extras else LAND_ORDER

    for row in card_rows:
        card = row_to_dict(row)
        card["owned_finishes"] = owned_map.get(card["scryfall_id"], set())
        card["is_owned"] = bool(card["owned_finishes"])
        card["missing_finishes"] = [f for f in card["available_finishes"] if f not in card["owned_finishes"]]
        key = (card["set_code"], card["set_name"])
        if key not in set_map:
            set_map[key] = {
                "set_code": card["set_code"],
                "set_name": card["set_name"],
                "released_at": card["released_at"],
                "treatments": OrderedDict(),
                "needed_finishes": 0,
                "owned_finishes": 0,
                "card_count": 0,
                "land_order": land_order,
            }
        set_obj = set_map[key]
        set_obj["card_count"] += 1
        set_obj["needed_finishes"] += len(card["available_finishes"])
        set_obj["owned_finishes"] += len(card["owned_finishes"])

        treatment = card["treatment"] or "Regular"
        if treatment not in set_obj["treatments"]:
            set_obj["treatments"][treatment] = {land: [] for land in land_order}
        if card["land_type"] in set_obj["treatments"][treatment]:
            set_obj["treatments"][treatment][card["land_type"]].append(card)

    sets = []
    for set_obj in set_map.values():
        needed = set_obj["needed_finishes"]
        owned = set_obj["owned_finishes"]
        set_obj["percent"] = round((owned / needed) * 100, 1) if needed else 0
        set_obj["complete"] = needed > 0 and owned >= needed
        if hide_complete and set_obj["complete"]:
            continue

        # Turn treatment columns into row matrices for template simplicity.
        treatment_sections = []
        for treatment, by_land in set_obj["treatments"].items():
            max_len = max((len(cards) for cards in by_land.values()), default=0)
            rows = []
            for i in range(max_len):
                rows.append([by_land[land][i] if i < len(by_land[land]) else None for land in land_order])
            treatment_sections.append({"name": treatment, "rows": rows})
        set_obj["treatment_sections"] = treatment_sections
        sets.append(set_obj)
    return sets


def global_stats() -> Dict[str, Any]:
    db = get_db()
    card_count = db.execute("SELECT COUNT(*) AS n FROM cards").fetchone()["n"]
    set_count = db.execute("SELECT COUNT(DISTINCT set_code) AS n FROM cards").fetchone()["n"]
    needed = db.execute("SELECT COALESCE(SUM(nonfoil + foil + etched), 0) AS n FROM cards").fetchone()["n"]
    owned = db.execute("SELECT COUNT(*) AS n FROM owned WHERE quantity > 0").fetchone()["n"]
    last_imports = db.execute(
        "SELECT * FROM import_log ORDER BY created_at DESC, id DESC LIMIT 5"
    ).fetchall()
    return {
        "card_count": card_count,
        "set_count": set_count,
        "needed_finishes": needed,
        "owned_finishes": owned,
        "percent": round((owned / needed) * 100, 1) if needed else 0,
        "last_imports": last_imports,
    }


# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------

@app.before_request
def ensure_db() -> None:
    init_db()


@app.route("/")
def index() -> str:
    search = request.args.get("q", "").strip()
    include_extras = request.args.get("extras") == "1"
    hide_complete = request.args.get("hide_complete") == "1"
    view = request.args.get("view", "grid")
    stats = global_stats()
    sets = build_set_grid(search=search, include_extras=include_extras, hide_complete=hide_complete)
    summaries = set_summaries(search=search, include_extras=include_extras, hide_complete=hide_complete)
    return render_template(
        "index.html",
        stats=stats,
        sets=sets,
        summaries=summaries,
        search=search,
        include_extras=include_extras,
        hide_complete=hide_complete,
        view=view,
        land_order=ALL_LANDS if include_extras else LAND_ORDER,
        finish_order=FINISH_ORDER,
    )


@app.route("/set/<set_code>")
def set_detail(set_code: str) -> str:
    include_extras = request.args.get("extras") == "1"
    sets = build_set_grid(set_code=set_code, include_extras=include_extras)
    if not sets:
        flash(f"No imported basic lands found for set code {set_code!r}.", "warning")
        return redirect(url_for("index"))
    return render_template(
        "set.html",
        set_obj=sets[0],
        include_extras=include_extras,
        land_order=ALL_LANDS if include_extras else LAND_ORDER,
        finish_order=FINISH_ORDER,
    )


@app.route("/imports", methods=["GET", "POST"])
def imports() -> str:
    if request.method == "POST":
        action = request.form.get("action")
        try:
            if action == "scryfall":
                path = path_from_form_or_upload("scryfall_file", "scryfall_path")
                if not path:
                    flash("Choose a Scryfall JSON file or enter a local path.", "error")
                    return redirect(url_for("imports"))
                replace_existing = request.form.get("replace_existing") == "1"
                paper_only = request.form.get("paper_only") == "1"
                result = import_scryfall_bulk(path, replace_existing=replace_existing, paper_only=paper_only)
                flash(
                    f"Imported {result['rows_imported']:,} basic-land printings from {result['rows_seen']:,} Scryfall rows.",
                    "success",
                )
                return redirect(url_for("index"))

            if action == "collection":
                path = path_from_form_or_upload("collection_file", "collection_path")
                if not path:
                    flash("Choose a collection CSV file or enter a local path.", "error")
                    return redirect(url_for("imports"))
                clear_existing = request.form.get("clear_existing") == "1"
                source = request.form.get("source", "moxfield").strip() or "moxfield"
                result = import_collection_csv(path, clear_existing=clear_existing, source=source)
                level = "success" if result["rows_matched"] else "warning"
                flash(
                    f"Matched {result['rows_matched']:,} collection rows; {result['rows_unmatched']:,} unmatched out of {result['rows_seen']:,} rows.",
                    level,
                )
                if result["unmatched_examples"]:
                    flash("First unmatched row keys: " + ", ".join(result["unmatched_examples"][0].keys()), "info")
                return redirect(url_for("index"))
        except Exception as exc:  # Local app: show practical errors.
            flash(f"Import failed: {exc}", "error")
            return redirect(url_for("imports"))

    stats = global_stats()
    return render_template("imports.html", stats=stats)


def owned_collection_rows() -> List[Dict[str, Any]]:
    db = get_db()
    rows = db.execute(
        """
        SELECT c.*, o.finish AS owned_finish, o.quantity, o.source, o.updated_at
        FROM owned o
        JOIN cards c ON c.scryfall_id = o.scryfall_id
        WHERE o.quantity > 0
        ORDER BY o.updated_at DESC, c.name ASC, c.set_code ASC
        """
    ).fetchall()
    return [dict(row) for row in rows]


@app.route("/collection")
def collection() -> str:
    return render_template(
        "collection.html",
        entries=owned_collection_rows(),
        finish_order=FINISH_ORDER,
        stats=global_stats(),
    )


# Column order matches the aliases `match_collection_row` checks first, so a file
# exported here re-imports as an exact scryfall_id match with no fuzzy fallback.
COLLECTION_EXPORT_HEADERS = [
    "scryfall_id",
    "name",
    "set_code",
    "set_name",
    "collector_number",
    "finish",
    "quantity",
]


@app.get("/collection/export.csv")
def collection_export_csv():
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(COLLECTION_EXPORT_HEADERS)
    for entry in owned_collection_rows():
        writer.writerow(
            [
                entry["scryfall_id"],
                entry["name"],
                entry["set_code"],
                entry["set_name"],
                entry["collector_number"],
                entry["owned_finish"],
                entry["quantity"],
            ]
        )

    filename = f"basic-land-collection-{now_iso()[:10]}.csv"
    return Response(
        buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/card-lookup")
def api_card_lookup():
    set_code = request.args.get("set", "").strip().lower()
    collector_number = request.args.get("cn", "").strip()
    if not set_code or not collector_number:
        return jsonify(ok=False, error="Enter a set code and collector number."), 400

    try:
        card = fetch_scryfall_card(set_code, collector_number)
    except ScryfallNotFound as exc:
        return jsonify(ok=False, error=str(exc)), 404
    except ScryfallError as exc:
        return jsonify(ok=False, error=str(exc)), 502

    if not is_basic_land_card(card, include_extras=True):
        return (
            jsonify(
                ok=False,
                error=f"{card.get('name', 'That card')} isn't a supported basic land "
                "(this tracker only handles English basic lands, Wastes, and snow basics).",
            ),
            422,
        )

    finishes = [f for f in (card.get("finishes") or []) if f in FINISH_ORDER]
    if not finishes:
        return jsonify(ok=False, error=f"{card.get('name')} has no trackable finishes on Scryfall."), 422

    small, _normal = get_image_uris(card)
    return jsonify(
        ok=True,
        scryfall_id=card.get("id"),
        name=card.get("name"),
        set_code=(card.get("set") or "").lower(),
        set_name=card.get("set_name"),
        collector_number=str(card.get("collector_number") or ""),
        image_small=small,
        scryfall_uri=card.get("scryfall_uri"),
        finishes=finishes,
    )


@app.post("/collection/add")
def collection_add():
    set_code = request.form.get("set_code", "").strip().lower()
    collector_number = request.form.get("collector_number", "").strip()
    finish = request.form.get("finish", "").strip().lower()
    quantity = parse_quantity(request.form.get("quantity", "1"))

    if not set_code or not collector_number:
        flash("Enter a set code and collector number.", "error")
        return redirect(url_for("collection"))
    if finish not in FINISH_ORDER:
        flash(f"Unknown finish: {finish}", "error")
        return redirect(url_for("collection"))
    if quantity <= 0:
        flash("Quantity must be at least 1.", "error")
        return redirect(url_for("collection"))

    try:
        card = fetch_scryfall_card(set_code, collector_number)
    except ScryfallError as exc:
        flash(str(exc), "error")
        return redirect(url_for("collection"))

    if not is_basic_land_card(card, include_extras=True):
        flash(
            f"{card.get('name', 'That card')} isn't a supported basic land "
            "(this tracker only handles English basic lands, Wastes, and snow basics).",
            "error",
        )
        return redirect(url_for("collection"))

    finishes = [f for f in (card.get("finishes") or []) if f in FINISH_ORDER]
    if finish not in finishes:
        flash(
            f"{card.get('name')} ({set_code.upper()} #{collector_number}) has no {finish} finish on Scryfall.",
            "error",
        )
        return redirect(url_for("collection"))

    timestamp = now_iso()
    db = get_db()
    row = build_card_row(card, timestamp)
    db.execute(CARD_UPSERT_SQL, row)
    db.execute(
        """
        INSERT INTO owned (scryfall_id, finish, quantity, source, updated_at)
        VALUES (:scryfall_id, :finish, :quantity, 'manual-entry', :updated_at)
        ON CONFLICT(scryfall_id, finish) DO UPDATE SET
            quantity = owned.quantity + excluded.quantity,
            source = excluded.source,
            updated_at = excluded.updated_at
        """,
        {"scryfall_id": card.get("id"), "finish": finish, "quantity": quantity, "updated_at": timestamp},
    )
    db.commit()
    flash(
        f"Added {quantity} × {card.get('name')} ({set_code.upper()} #{collector_number}, {finish}) to your collection.",
        "success",
    )
    return redirect(url_for("collection"))


@app.post("/collection/delete/<scryfall_id>/<finish>")
def collection_delete(scryfall_id: str, finish: str):
    db = get_db()
    db.execute("DELETE FROM owned WHERE scryfall_id = ? AND finish = ?", (scryfall_id, finish))
    db.commit()
    flash("Removed from your collection.", "success")
    return redirect(url_for("collection"))


@app.post("/toggle/<scryfall_id>/<finish>")
def toggle_owned(scryfall_id: str, finish: str):
    if finish not in FINISH_ORDER:
        flash(f"Unknown finish: {finish}", "error")
        return redirect(request.referrer or url_for("index"))
    db = get_db()
    card = db.execute("SELECT finishes_json FROM cards WHERE scryfall_id = ?", (scryfall_id,)).fetchone()
    if not card:
        flash("Card not found.", "error")
        return redirect(request.referrer or url_for("index"))
    available = json.loads(card["finishes_json"] or "[]")
    if finish not in available:
        flash(f"That printing does not have a {finish} finish.", "warning")
        return redirect(request.referrer or url_for("index"))

    existing = db.execute(
        "SELECT quantity FROM owned WHERE scryfall_id = ? AND finish = ?",
        (scryfall_id, finish),
    ).fetchone()
    if existing and existing["quantity"] > 0:
        db.execute("DELETE FROM owned WHERE scryfall_id = ? AND finish = ?", (scryfall_id, finish))
    else:
        db.execute(
            """
            INSERT INTO owned (scryfall_id, finish, quantity, source, updated_at)
            VALUES (?, ?, 1, 'manual', ?)
            ON CONFLICT(scryfall_id, finish) DO UPDATE SET
                quantity = 1,
                source = 'manual',
                updated_at = excluded.updated_at
            """,
            (scryfall_id, finish, now_iso()),
        )
    db.commit()
    return redirect(request.form.get("return_to") or request.referrer or url_for("index"))


@app.post("/clear-owned")
def clear_owned():
    db = get_db()
    db.execute("DELETE FROM owned")
    db.commit()
    flash("Cleared ownership data. Card catalogue was kept.", "success")
    return redirect(url_for("index"))


if __name__ == "__main__":
    init_db()
    app.run(debug=True)
