"""
Dietary Matrix
==============
A Flask app that recreates the category x macro calorie matrix from
Dietary_Matrix.xlsx and fills it in from what you log, so you can see which
cells you are hitting, missing, or overshooting for a meal, a day, or a week.

Foods come in two kinds:
  * Ingredients: basic foods with macros per serving, plus optional weight,
    volume and count equivalences for that serving.
  * Recipes: built from ingredients and other recipes. Nutrition, and how it
    splits across the matrix categories, is calculated from the parts.

Run:  pip install -r requirements.txt  &&  python app.py
Then open http://127.0.0.1:5000
"""

import copy
import json
import os
import re
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta

from flask import Flask, flash, g, redirect, render_template, request, url_for

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DIET_DB", os.path.join(BASE_DIR, "dietary_matrix.db"))

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-me-for-anything-public")

# --------------------------------------------------------------------------
# Model constants (mirrors the spreadsheet)
# --------------------------------------------------------------------------
CATEGORIES = ["Meats & Beans", "Grains", "Fruits", "Vegetables"]
EXTRA_CATEGORY = "Oil & Dairy"  # tracked as a separate allowance, like the sheet
ALL_CATEGORIES = CATEGORIES + [EXTRA_CATEGORY]
MACROS = ["carbs", "fat", "protein"]
MACRO_LABELS = {"carbs": "Carbs", "fat": "Fats", "protein": "Protein"}
KCAL_PER_G = {"carbs": 4, "fat": 9, "protein": 4}
MEALS = ["Breakfast", "Lunch", "Dinner", "Snack"]

# unit key -> (dimension, factor to base unit, display label). Base: g, ml.
UNITS = {
    "serving": ("serving", 1.0, "serving"),
    "each": ("each", 1.0, "each"),
    "g": ("weight", 1.0, "g"),
    "kg": ("weight", 1000.0, "kg"),
    "oz": ("weight", 28.349523125, "oz"),
    "lb": ("weight", 453.59237, "lb"),
    "ml": ("volume", 1.0, "ml"),
    "l": ("volume", 1000.0, "L"),
    "tsp": ("volume", 4.92892159375, "tsp"),
    "tbsp": ("volume", 14.78676478125, "tbsp"),
    "floz": ("volume", 29.5735295625, "fl oz"),
    "cup": ("volume", 236.5882365, "cup"),
    "pint": ("volume", 473.176473, "pint"),
    "quart": ("volume", 946.352946, "quart"),
}
WEIGHT_UNITS = [k for k, v in UNITS.items() if v[0] == "weight"]
VOLUME_UNITS = [k for k, v in UNITS.items() if v[0] == "volume"]
PLURAL_UNITS = {"serving", "cup", "pint", "quart"}

# Defaults taken straight from Dietary_Matrix.xlsx
DEFAULT_SETTINGS = {
    "daily_calories": 1900,
    # Oil & Dairy share of the whole day. Sheet: ROUND((5/95)*C2, -1) -> 100 kcal
    "extra_pct": 0.05,
    # Which number the matrix percentages multiply.
    #   "daily": daily calories only (what the sheet's calorie table does: =C5*$C$2)
    #   "total": daily calories + Oil & Dairy allowance (95% grid + 5% = 100%)
    "pct_base": "daily",
    "matrix": {
        "Meats & Beans": {"carbs": 0.02, "fat": 0.20, "protein": 0.13},
        "Grains": {"carbs": 0.28, "fat": 0.03, "protein": 0.05},
        "Fruits": {"carbs": 0.12, "fat": 0.01, "protein": 0.00},
        "Vegetables": {"carbs": 0.08, "fat": 0.01, "protein": 0.02},
    },
    # Not in the sheet: how a day's plan is split across meals (for Meal view)
    "meal_split": {"Breakfast": 0.25, "Lunch": 0.30, "Dinner": 0.35, "Snack": 0.10},
    # A cell counts as "on target" within +/- this fraction of its target...
    "tolerance": 0.10,
    # ...or within this many kcal per day, whichever is larger
    "min_band_kcal": 10,
}

# Starter ingredients (approximate USDA values). Edit or delete freely.
# name, category, serving label, carbs g, fat g, protein g, grams, ml, count, count label
SEED_INGREDIENTS = [
    ("Chicken breast, cooked", "Meats & Beans", "100 g", 0, 3.6, 31, 100, None, None, None),
    ("Salmon, cooked", "Meats & Beans", "100 g", 0, 12, 22, 100, None, None, None),
    ("Egg, large", "Meats & Beans", "1 egg", 0.4, 4.8, 6.3, 50, None, 1, "egg"),
    ("Black beans, cooked", "Meats & Beans", "1/2 cup", 20, 0.5, 7.6, 86, 118.29, None, None),
    ("Brown rice, cooked", "Grains", "1 cup", 45, 1.8, 5, 195, 236.59, None, None),
    ("Rolled oats, dry", "Grains", "1/2 cup", 27, 2.5, 5, 40, 118.29, None, None),
    ("Whole wheat bread", "Grains", "1 slice", 12, 1, 4, 32, None, 1, "slice"),
    ("Banana", "Fruits", "1 medium", 27, 0.4, 1.3, 118, None, 1, "banana"),
    ("Apple", "Fruits", "1 medium", 25, 0.3, 0.5, 182, None, 1, "apple"),
    ("Broccoli", "Vegetables", "1 cup chopped", 6, 0.3, 2.6, 91, 236.59, None, None),
    ("Spinach, raw", "Vegetables", "1 cup", 1.1, 0.1, 0.9, 30, 236.59, None, None),
    ("Olive oil", "Oil & Dairy", "1 tbsp", 0, 14, 0, 13.5, 14.79, None, None),
    ("Milk, 2%", "Oil & Dairy", "1 cup", 12, 5, 8, 244, 236.59, None, None),
    ("Greek yogurt, plain nonfat", "Oil & Dairy", "1 container", 6, 0.7, 17, 170, None, 1, "container"),
]
# name, serving label, yield servings, batch g, batch ml, batch count, count label, [(child, qty, unit)]
SEED_RECIPES = [
    ("Overnight oats", "1 jar", 1, None, None, 1, "jar",
     [("Rolled oats, dry", 0.5, "cup"), ("Milk, 2%", 0.75, "cup"), ("Banana", 1, "each")]),
    ("Chicken rice bowl", "1 bowl", 2, None, None, 2, "bowl",
     [("Chicken breast, cooked", 250, "g"), ("Brown rice, cooked", 1.5, "cup"),
      ("Broccoli", 2, "cup"), ("Olive oil", 2, "tsp")]),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS foods (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL UNIQUE COLLATE NOCASE,
    kind           TEXT NOT NULL DEFAULT 'ingredient',   -- 'ingredient' | 'recipe'
    category       TEXT NOT NULL DEFAULT '',             -- ingredients only
    serving        TEXT NOT NULL DEFAULT '1 serving',    -- what one serving is called
    carbs_g        REAL NOT NULL DEFAULT 0,              -- per serving (ingredients only)
    fat_g          REAL NOT NULL DEFAULT 0,
    protein_g      REAL NOT NULL DEFAULT 0,
    serving_g      REAL,                                 -- one serving weighs...
    serving_ml     REAL,                                 -- one serving measures...
    serving_each   REAL,                                 -- one serving is N pieces
    each_label     TEXT,                                 -- what a piece is called
    yield_servings REAL,                                 -- recipes: batch makes N servings
    batch_g        REAL,                                 -- recipes: whole batch weighs...
    batch_ml       REAL,
    batch_each     REAL
);
CREATE TABLE IF NOT EXISTS recipe_components (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    recipe_id INTEGER NOT NULL REFERENCES foods(id) ON DELETE CASCADE,
    child_id  INTEGER NOT NULL REFERENCES foods(id),
    quantity  REAL NOT NULL,
    unit      TEXT NOT NULL,
    position  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    day        TEXT NOT NULL,              -- ISO date, YYYY-MM-DD
    meal       TEXT NOT NULL,
    food_id    INTEGER NOT NULL REFERENCES foods(id),
    servings   REAL NOT NULL DEFAULT 1,    -- servings at the time it was logged (fallback)
    quantity   REAL,
    unit       TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_log_day ON log(day);
CREATE INDEX IF NOT EXISTS idx_comp_recipe ON recipe_components(recipe_id);
"""

# Columns added after the first version, for upgrading an existing database
MIGRATIONS = {
    "foods": [("kind", "TEXT NOT NULL DEFAULT 'ingredient'"), ("serving_g", "REAL"),
              ("serving_ml", "REAL"), ("serving_each", "REAL"), ("each_label", "TEXT"),
              ("yield_servings", "REAL"), ("batch_g", "REAL"), ("batch_ml", "REAL"),
              ("batch_each", "REAL")],
    "log": [("quantity", "REAL"), ("unit", "TEXT")],
}


# --------------------------------------------------------------------------
# Database helpers
# --------------------------------------------------------------------------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)

    # Upgrade older databases in place
    for table, cols in MIGRATIONS.items():
        have = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols:
            if name not in have:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    db.execute("UPDATE log SET quantity = servings, unit = 'serving' WHERE quantity IS NULL")
    # Give the original starter foods their weight/volume/count equivalences
    for name, _c, _s, _cb, _f, _p, grams, ml, each, label in SEED_INGREDIENTS:
        db.execute("""UPDATE foods SET serving_g = ?, serving_ml = ?, serving_each = ?, each_label = ?
                      WHERE name = ? AND kind = 'ingredient' AND serving_g IS NULL
                        AND serving_ml IS NULL AND serving_each IS NULL""",
                   (grams, ml, each, label, name))

    if not db.execute("SELECT 1 FROM settings WHERE key='config'").fetchone():
        db.execute("INSERT INTO settings (key, value) VALUES ('config', ?)",
                   (json.dumps(DEFAULT_SETTINGS),))

    no_foods = db.execute("SELECT COUNT(*) FROM foods").fetchone()[0] == 0
    if no_foods and os.environ.get("DIET_SEED", "1") != "0":
        db.executemany("""
            INSERT INTO foods (name, kind, category, serving, carbs_g, fat_g, protein_g,
                               serving_g, serving_ml, serving_each, each_label)
            VALUES (?, 'ingredient', ?, ?, ?, ?, ?, ?, ?, ?, ?)""", SEED_INGREDIENTS)
        ids = {r[1]: r[0] for r in db.execute("SELECT id, name FROM foods")}
        for name, serving, yld, bg, bml, beach, label, parts in SEED_RECIPES:
            cur = db.execute("""
                INSERT INTO foods (name, kind, category, serving, yield_servings, batch_g,
                                   batch_ml, batch_each, each_label)
                VALUES (?, 'recipe', '', ?, ?, ?, ?, ?, ?)""", (name, serving, yld, bg, bml, beach, label))
            for pos, (child, qty, unit) in enumerate(parts):
                db.execute("INSERT INTO recipe_components (recipe_id, child_id, quantity, unit, position) "
                           "VALUES (?, ?, ?, ?, ?)", (cur.lastrowid, ids[child], qty, unit, pos))
    db.commit()
    db.close()


def _merge(base, override):
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def load_config():
    row = get_db().execute("SELECT value FROM settings WHERE key='config'").fetchone()
    cfg = copy.deepcopy(DEFAULT_SETTINGS)
    if row:
        _merge(cfg, json.loads(row["value"]))
    return cfg


def save_config(cfg):
    db = get_db()
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('config', ?)",
               (json.dumps(cfg),))
    db.commit()


def extra_allowance(cfg):
    """Oil & Dairy kcal/day. Same formula as the sheet: ROUND(p/(1-p) * daily, -1)."""
    p = cfg["extra_pct"]
    if p <= 0 or p >= 1:
        return 0.0
    return float(round(p / (1 - p) * cfg["daily_calories"], -1))


def pct_base_kcal(cfg):
    if cfg.get("pct_base") == "total":
        return cfg["daily_calories"] + extra_allowance(cfg)
    return cfg["daily_calories"]


# --------------------------------------------------------------------------
# Quantities and units
# --------------------------------------------------------------------------
UNICODE_FRACTIONS = {"½": " 1/2", "⅓": " 1/3", "⅔": " 2/3", "¼": " 1/4", "¾": " 3/4",
                     "⅛": " 1/8", "⅜": " 3/8", "⅝": " 5/8", "⅞": " 7/8"}
_QTY_RE = re.compile(r"^\s*(?:(\d+(?:\.\d+)?)\s+)?(\d+(?:\.\d+)?)(?:\s*/\s*(\d+(?:\.\d+)?))?\s*$")


def parse_qty(text):
    """Parse '2', '1.5', '3/4', '1 1/2' or '1½'. Returns a float > 0, or None."""
    if text is None:
        return None
    s = str(text).strip().replace(",", ".")
    for ch, rep in UNICODE_FRACTIONS.items():
        s = s.replace(ch, rep)
    m = _QTY_RE.match(s)
    if not m:
        return None
    whole, num, den = m.groups()
    if den is not None:
        if float(den) == 0:
            return None
        value = float(num) / float(den) + (float(whole) if whole else 0)
    elif whole:  # "1 2" is not a quantity
        return None
    else:
        value = float(num)
    return value if value > 0 else None


_NICE_FRACTIONS = [(1 / 8, "1/8"), (1 / 4, "1/4"), (1 / 3, "1/3"), (3 / 8, "3/8"), (1 / 2, "1/2"),
                   (5 / 8, "5/8"), (2 / 3, "2/3"), (3 / 4, "3/4"), (7 / 8, "7/8")]


def fmt_qty(q):
    """1.5 -> '1 1/2', 0.25 -> '1/4', 150 -> '150', 0.4 -> '0.4'."""
    if q is None:
        return ""
    whole = int(q)
    frac = q - whole
    if frac < 0.005:
        return f"{whole:,}"
    if frac > 0.995:
        return f"{whole + 1:,}"
    if q < 20:
        for value, label in _NICE_FRACTIONS:
            if abs(frac - value) < 0.005:
                return f"{whole} {label}" if whole else label
    return f"{q:,.2f}".rstrip("0").rstrip(".")


def pluralize(word, q):
    if q <= 1 + 1e-9 or not word or word.endswith("s"):
        return word
    if word.endswith(("ch", "sh", "x")):
        return word + "es"
    return word + "s"


def unit_label(unit, q=1, each_label=None):
    if unit == "each":
        return pluralize(each_label or "piece", q)
    label = UNITS.get(unit, (None, None, unit))[2]
    return pluralize(label, q) if unit in PLURAL_UNITS else label


def fmt_amount(q, unit, each_label=None):
    return f"{fmt_qty(q)} {unit_label(unit, q, each_label)}"


def nice_volume(ml):
    """Show a volume in the friendliest kitchen unit: (qty, unit)."""
    for unit, step in (("cup", 8), ("tbsp", 2), ("tsp", 4)):
        q = ml / UNITS[unit][1]
        if q >= (0.25 if unit == "cup" else 0.25) and abs(q * step - round(q * step)) < 0.03 and q < 50:
            return round(q * step) / step, unit
    return round(ml, 1), "ml"


def to_base(value, unit, dim):
    """Convert a measure to g or ml. Returns None for an unknown or mismatched unit."""
    spec = UNITS.get(unit)
    if not spec or spec[0] != dim:
        return None
    return value * spec[1]


# --------------------------------------------------------------------------
# Food library: nutrition for ingredients and (nested) recipes
# --------------------------------------------------------------------------
class Library:
    """Loads every food and recipe component once per request and answers
    nutrition and unit-conversion questions, recursing through recipes."""

    def __init__(self, db):
        self.foods = {r["id"]: dict(r) for r in db.execute("SELECT * FROM foods")}
        self.components = defaultdict(list)
        for r in db.execute("SELECT * FROM recipe_components ORDER BY recipe_id, position, id"):
            self.components[r["recipe_id"]].append(dict(r))
        self._eq, self._ps, self._busy = {}, {}, set()

    # ---- conversions -----------------------------------------------------
    def equiv(self, fid):
        """What one serving equals: {'g', 'ml', 'each', 'each_label', 'g_estimated'}."""
        if fid in self._eq:
            return self._eq[fid]
        f = self.foods[fid]
        if f["kind"] == "recipe":
            y = f["yield_servings"] or 1
            grams, estimated = (f["batch_g"] / y if f["batch_g"] else None), False
            if grams is None:
                est = self.estimated_batch_g(fid)
                if est:
                    grams, estimated = est / y, True
            eq = {"g": grams, "ml": f["batch_ml"] / y if f["batch_ml"] else None,
                  "each": f["batch_each"] / y if f["batch_each"] else None,
                  "each_label": f["each_label"] or "piece", "g_estimated": estimated}
        else:
            eq = {"g": f["serving_g"] or None, "ml": f["serving_ml"] or None,
                  "each": f["serving_each"] or None, "each_label": f["each_label"] or "piece",
                  "g_estimated": False}
        self._eq[fid] = eq
        return eq

    def estimated_batch_g(self, fid):
        """Sum of the parts' weights, if every part has a known weight."""
        if fid in self._busy:
            return None
        self._busy.add(fid)
        try:
            total = 0.0
            for c in self.components.get(fid, []):
                if c["child_id"] not in self.foods:
                    return None
                s = self.to_servings(c["child_id"], c["quantity"], c["unit"])
                cg = self.equiv(c["child_id"])["g"]
                if s is None or not cg:
                    return None
                total += s * cg
            return total or None
        finally:
            self._busy.discard(fid)

    def to_servings(self, fid, qty, unit):
        spec = UNITS.get(unit)
        if fid not in self.foods or spec is None or qty is None:
            return None
        if unit == "serving":
            return qty
        eq = self.equiv(fid)
        if unit == "each":
            return qty / eq["each"] if eq["each"] else None
        base = qty * spec[1]
        if spec[0] == "weight":
            return base / eq["g"] if eq["g"] else None
        if spec[0] == "volume":
            return base / eq["ml"] if eq["ml"] else None
        return None

    def units_for(self, fid):
        eq = self.equiv(fid)
        units = ["serving"]
        if eq["each"]:
            units.append("each")
        if eq["g"]:
            units += WEIGHT_UNITS
        if eq["ml"]:
            units += VOLUME_UNITS
        return units

    # ---- nutrition -------------------------------------------------------
    def per_serving(self, fid):
        """Macro grams in one serving, split by matrix category, plus any problems."""
        if fid in self._ps:
            return self._ps[fid]
        f = self.foods[fid]
        by_cat = {c: {m: 0.0 for m in MACROS} for c in ALL_CATEGORIES}
        issues = []
        if f["kind"] != "recipe":
            cat = f["category"] if f["category"] in ALL_CATEGORIES else EXTRA_CATEGORY
            for m in MACROS:
                by_cat[cat][m] = f[f"{m}_g"]
        elif fid in self._busy:
            issues.append(f"{f['name']} contains itself.")
        else:
            self._busy.add(fid)
            try:
                for c in self.components.get(fid, []):
                    child = self.foods.get(c["child_id"])
                    if not child:
                        continue
                    s = self.to_servings(child["id"], c["quantity"], c["unit"])
                    if s is None:
                        issues.append(f"{fmt_amount(c['quantity'], c['unit'], 'piece')} of {child['name']} "
                                      f"can't be converted, so it isn't counted.")
                        continue
                    sub = self.per_serving(child["id"])
                    issues += sub["issues"]
                    for cat in ALL_CATEGORIES:
                        for m in MACROS:
                            by_cat[cat][m] += sub["by_cat"][cat][m] * s
            finally:
                self._busy.discard(fid)
            y = f["yield_servings"] or 1
            for cat in ALL_CATEGORIES:
                for m in MACROS:
                    by_cat[cat][m] /= y
        result = {"by_cat": by_cat, "issues": issues}
        self._ps[fid] = result
        return result

    def summary(self, fid):
        """Everything the templates need to describe a food's serving."""
        ps = self.per_serving(fid)["by_cat"]
        macros = {m: sum(ps[c][m] for c in ALL_CATEGORIES) for m in MACROS}
        cat_kcal = {c: sum(ps[c][m] * KCAL_PER_G[m] for m in MACROS) for c in ALL_CATEGORIES}
        kcal = sum(cat_kcal.values())
        split = [(c, v / kcal) for c, v in cat_kcal.items() if kcal and v / kcal >= 0.005]
        split.sort(key=lambda x: -x[1])
        return {"macros": macros, "kcal": kcal, "split": split, "equiv_text": self.equiv_text(fid)}

    def equiv_text(self, fid):
        eq = self.equiv(fid)
        parts = []
        if eq["g"]:
            parts.append(("about " if eq["g_estimated"] else "") + f"{eq['g']:,.0f} g")
        if eq["ml"]:
            q, u = nice_volume(eq["ml"])
            parts.append(fmt_amount(q, u))
        if eq["each"]:
            parts.append(fmt_amount(eq["each"], "each", eq["each_label"]))
        return ", ".join(parts)

    def contains(self, fid, target):
        """True if food fid is target, or is a recipe that includes target at any depth."""
        stack, seen = [fid], set()
        while stack:
            cur = stack.pop()
            if cur == target:
                return True
            if cur in seen:
                continue
            seen.add(cur)
            stack += [c["child_id"] for c in self.components.get(cur, [])]
        return False

    def used_in(self, fid):
        return sorted({self.foods[r]["name"] for r, comps in self.components.items()
                       for c in comps if c["child_id"] == fid and r in self.foods})

    def client_data(self):
        """Compact data the browser uses for unit menus and live previews."""
        out = {}
        for fid, f in self.foods.items():
            eq = self.equiv(fid)
            ps = self.per_serving(fid)["by_cat"]
            out[fid] = {
                "name": f["name"], "kind": f["kind"], "serving": f["serving"],
                "g": eq["g"], "ml": eq["ml"], "each": eq["each"], "each_label": eq["each_label"],
                "macros": {m: sum(ps[c][m] for c in ALL_CATEGORIES) for m in MACROS},
                "cats": {c: sum(ps[c][m] * KCAL_PER_G[m] for m in MACROS) for c in ALL_CATEGORIES},
            }
        return out


def get_library():
    if "lib" not in g:
        g.lib = Library(get_db())
    return g.lib


# --------------------------------------------------------------------------
# Small parsing helpers
# --------------------------------------------------------------------------
def parse_float(value, default=None, minimum=None):
    try:
        v = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN
        return default
    if minimum is not None and v < minimum:
        return default
    return v


def parse_day(value, default=None):
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return default if default is not None else date.today()


def guess_meal(now=None):
    hour = (now or datetime.now()).hour
    if hour < 11:
        return "Breakfast"
    if hour < 15:
        return "Lunch"
    if hour < 21:
        return "Dinner"
    return "Snack"


def fmt_day(d):
    if isinstance(d, str):
        d = date.fromisoformat(d)
    return f"{d:%A}, {d:%B} {d.day}"


# --------------------------------------------------------------------------
# Period + matrix math
# --------------------------------------------------------------------------
def resolve_period(args, cfg):
    scope = args.get("scope", "day")
    if scope not in ("meal", "day", "week"):
        scope = "day"
    d = parse_day(args.get("day"))
    meal = args.get("meal") if args.get("meal") in MEALS else guess_meal()
    today = date.today()

    if scope == "meal":
        start = end = d
        factor = cfg["meal_split"].get(meal, 0)
        step = 1
        label = f"{meal}, {d:%a %b} {d.day}"
        note = f"Targets are {factor * 100:g}% of your daily plan"
    elif scope == "week":
        start = d - timedelta(days=d.weekday())
        end = start + timedelta(days=6)
        step = 7
        if start <= today <= end:
            factor = (today - start).days + 1
            note = f"Targets cover Monday through today (day {factor} of 7)"
        else:
            factor = 7
            note = "Targets cover the full 7 days"
        label = f"Week of {start:%B} {start.day}"
    else:
        start = end = d
        factor = 1
        step = 1
        label = fmt_day(d)
        note = "Targets are your full daily plan"

    return {
        "scope": scope, "day": d, "start": start, "end": end, "meal": meal,
        "factor": factor, "label": label, "note": note,
        "prev": (d - timedelta(days=step)).isoformat(),
        "next": (d + timedelta(days=step)).isoformat(),
        "is_today": start <= today <= end,
    }


def fetch_entries(period, lib):
    sql = "SELECT id, day, meal, food_id, servings, quantity, unit FROM log WHERE day BETWEEN ? AND ?"
    params = [period["start"].isoformat(), period["end"].isoformat()]
    if period["scope"] == "meal":
        sql += " AND meal = ?"
        params.append(period["meal"])
    entries = []
    for row in get_db().execute(sql, params).fetchall():
        e = dict(row)
        food = lib.foods.get(e["food_id"])
        if not food:
            continue
        qty = e["quantity"] if e["quantity"] is not None else e["servings"]
        unit = e["unit"] or "serving"
        servings = lib.to_servings(food["id"], qty, unit)
        if servings is None:  # food's equivalences changed since logging
            servings = e["servings"]
        ps = lib.per_serving(food["id"])["by_cat"]
        e["by_cat"] = {c: {m: ps[c][m] * servings * KCAL_PER_G[m] for m in MACROS}
                       for c in ALL_CATEGORIES}
        e["total"] = sum(v for row_ in e["by_cat"].values() for v in row_.values())
        e["name"] = food["name"]
        e["kind"] = food["kind"]
        e["tag"] = "Recipe" if food["kind"] == "recipe" else food["category"]
        e["amount"] = fmt_amount(qty, unit, lib.equiv(food["id"])["each_label"])
        if unit != "serving" and abs(servings - qty) > 0.005:
            e["amount"] += f" ({fmt_qty(servings)} {pluralize('serving', servings)})"
        entries.append(e)
    meal_order = {m: i for i, m in enumerate(MEALS)}
    entries.sort(key=lambda e: (e["day"], meal_order.get(e["meal"], 99), e["id"]))
    return entries


def group_entries(entries):
    days = []
    for e in entries:
        if not days or days[-1]["day"] != e["day"]:
            days.append({"day": e["day"], "label": fmt_day(e["day"]), "meals": [], "total": 0.0})
        day = days[-1]
        if not day["meals"] or day["meals"][-1]["meal"] != e["meal"]:
            day["meals"].append({"meal": e["meal"], "foods": [], "total": 0.0})
        day["meals"][-1]["foods"].append(e)
        day["meals"][-1]["total"] += e["total"]
        day["total"] += e["total"]
    return days


def make_cell(target, actual, cfg, factor, macro=None):
    band = max(target * cfg["tolerance"], cfg["min_band_kcal"] * factor)
    if actual < target - band:
        status = "under"
    elif actual > target + band:
        status = "over"
    else:
        status = "on"

    if target > 0:
        fill = min(actual / target, 1.0) * 100
        pct = actual / target
    else:
        fill = 100.0 if actual > 0 else 0.0
        pct = None

    delta = actual - target
    if status == "under":
        verdict = f"{-delta:,.0f} to go"
    elif status == "over":
        verdict = f"{delta:,.0f} over"
    elif target == 0 and actual == 0:
        verdict = "None planned"
    else:
        verdict = "On target"

    cell = {"target": target, "actual": actual, "status": status, "fill": round(fill, 1),
            "pct": pct, "delta": delta, "verdict": verdict}
    if macro:
        cell["target_g"] = target / KCAL_PER_G[macro]
        cell["actual_g"] = actual / KCAL_PER_G[macro]
    return cell


def build_matrix(cfg, entries, factor):
    base = pct_base_kcal(cfg) * factor
    actual = {cat: {m: 0.0 for m in MACROS} for cat in ALL_CATEGORIES}
    for e in entries:
        for cat in ALL_CATEGORIES:
            for m in MACROS:
                actual[cat][m] += e["by_cat"][cat][m]

    cells, row_totals = {}, {}
    col_target = {m: 0.0 for m in MACROS}
    col_actual = {m: 0.0 for m in MACROS}
    for cat in CATEGORIES:
        cells[cat] = {}
        for m in MACROS:
            t = cfg["matrix"][cat][m] * base
            a = actual[cat][m]
            cells[cat][m] = make_cell(t, a, cfg, factor, m)
            col_target[m] += t
            col_actual[m] += a
        row_totals[cat] = make_cell(sum(c["target"] for c in cells[cat].values()),
                                    sum(c["actual"] for c in cells[cat].values()), cfg, factor)

    col_totals = {m: make_cell(col_target[m], col_actual[m], cfg, factor, m) for m in MACROS}
    core_t, core_a = sum(col_target.values()), sum(col_actual.values())
    core_total = make_cell(core_t, core_a, cfg, factor)

    extra_t = extra_allowance(cfg) * factor
    extra_a = sum(actual[EXTRA_CATEGORY].values())
    extra = make_cell(extra_t, extra_a, cfg, factor)

    grand = make_cell(core_t + extra_t, core_a + extra_a, cfg, factor)

    flagged = [(f"{cat} {MACRO_LABELS[m].lower()}", cells[cat][m])
               for cat in CATEGORIES for m in MACROS]
    flagged.append((EXTRA_CATEGORY, extra))
    gaps = sorted([(n, c) for n, c in flagged if c["status"] == "under"],
                  key=lambda x: x[1]["delta"])[:3]
    excess = sorted([(n, c) for n, c in flagged if c["status"] == "over"],
                    key=lambda x: -x[1]["delta"])[:3]

    return {
        "cells": cells, "row_totals": row_totals, "col_totals": col_totals,
        "core_total": core_total, "extra": extra, "extra_macros": actual[EXTRA_CATEGORY],
        "grand": grand, "gaps": gaps, "excess": excess,
        "plan_rows": {cat: sum(cfg["matrix"][cat].values()) for cat in CATEGORIES},
        "plan_cols": {m: sum(cfg["matrix"][cat][m] for cat in CATEGORIES) for m in MACROS},
    }


# --------------------------------------------------------------------------
# Suggestions: what to eat next to fill the gaps without overfilling
# --------------------------------------------------------------------------
OVER_WEIGHT = 2.0          # a calorie pushed past a target costs twice a calorie of gap filled
MIN_IMPROVEMENT = 10.0     # ignore suggestions that help by less than this many kcal
SERVING_STEPS = [0.25, 0.5, 0.75, 1, 1.25, 1.5, 2, 2.5, 3]
MAX_SERVINGS = 3.0


def _slots(mx, cfg, factor):
    """The 12 matrix cells plus the Oil & Dairy allowance, as parallel lists.
    bands: how far past target still counts as on target (same rule as the matrix colors)."""
    slots = [(f"{cat} {MACRO_LABELS[m].lower()}", cat, m) for cat in CATEGORIES for m in MACROS]
    slots.append((EXTRA_CATEGORY, EXTRA_CATEGORY, None))
    targets = [mx["cells"][c][m]["target"] if m else mx["extra"]["target"] for _, c, m in slots]
    actuals = [mx["cells"][c][m]["actual"] if m else mx["extra"]["actual"] for _, c, m in slots]
    bands = [max(t * cfg["tolerance"], cfg["min_band_kcal"] * factor) for t in targets]
    return slots, targets, bands, actuals


def _food_vector(lib, fid, slots):
    ps = lib.per_serving(fid)["by_cat"]
    return [ps[c][m] * KCAL_PER_G[m] if m else sum(ps[c][mm] * KCAL_PER_G[mm] for mm in MACROS)
            for _, c, m in slots]


def _loss(targets, bands, actuals):
    return sum(max(0.0, t - a) + OVER_WEIGHT * max(0.0, a - t - b)
               for t, b, a in zip(targets, bands, actuals))


def _kitchen_volume(ml):
    cups = ml / UNITS["cup"][1]
    if cups >= 0.245:
        return round(cups * 4) / 4, "cup"
    tbsp = ml / UNITS["tbsp"][1]
    if tbsp >= 1:
        return round(tbsp * 2) / 2, "tbsp"
    return max(0.5, round(ml / UNITS["tsp"][1] * 2) / 2), "tsp"


def natural_amount(lib, fid, servings):
    """Express a number of servings the way you'd actually measure it."""
    eq = lib.equiv(fid)
    if eq["each"]:
        pieces = round(servings * eq["each"] * 2) / 2
        return (pieces, "each") if pieces > 0 else None
    if lib.foods[fid]["kind"] == "recipe":
        q = round(servings * 2) / 2
        return (q, "serving") if q > 0 else None
    if eq["ml"]:
        return _kitchen_volume(servings * eq["ml"])
    if eq["g"]:
        grams = servings * eq["g"]
        step = 5 if grams < 100 else 10
        q = round(grams / step) * step
        return (q, "g") if q > 0 else None
    q = round(servings * 4) / 4
    return (q, "serving") if q > 0 else None


def _candidates(lib, fid):
    seen, out = set(), []
    for s in SERVING_STEPS:
        amt = natural_amount(lib, fid, s)
        if not amt or amt in seen:
            continue
        seen.add(amt)
        servings = lib.to_servings(fid, *amt)
        if servings and servings <= MAX_SERVINGS + 0.01:
            out.append((amt[0], amt[1], servings))
    return out


def _best_amount(vec, targets, bands, actuals, cands):
    base = _loss(targets, bands, actuals)
    best = None
    for qty, unit, servings in cands:
        after = [a + v * servings for a, v in zip(actuals, vec)]
        gain = base - _loss(targets, bands, after)
        if best is None or gain > best[0] + 1e-9:
            best = (gain, qty, unit, servings, after)
    return best


def _describe(slots, targets, bands, before, after):
    fills, overs = [], []
    for (name, _c, _m), t, b, a0, a1 in zip(slots, targets, bands, before, after):
        filled = max(0.0, t - a0) - max(0.0, t - a1)
        pushed = max(0.0, a1 - t - b) - max(0.0, a0 - t - b)
        if filled >= 5:
            fills.append((name, filled))
        if pushed >= 5:
            overs.append((name, pushed))
    fills.sort(key=lambda x: -x[1])
    overs.sort(key=lambda x: -x[1])
    return fills, overs


def suggest(cfg, mx, lib, factor, uses, n=5, plan_size=4):
    slots, targets, bands, actuals = _slots(mx, cfg, factor)
    remaining = sum(max(0.0, t - a) for t, a in zip(targets, actuals))
    if remaining < MIN_IMPROVEMENT:
        return {"picks": [], "plan": [], "remaining": remaining}

    foods = [f for f in lib.foods.values() if not lib.per_serving(f["id"])["issues"]]
    vectors = {f["id"]: _food_vector(lib, f["id"], slots) for f in foods}
    cands = {f["id"]: _candidates(lib, f["id"]) for f in foods}
    vectors = {k: v for k, v in vectors.items() if sum(v) > 0 and cands[k]}

    def pick(current, exclude=()):
        scored = []
        for fid, vec in vectors.items():
            if fid in exclude:
                continue
            best = _best_amount(vec, targets, bands, current, cands[fid])
            if best and best[0] >= MIN_IMPROVEMENT:
                # foods you actually eat win close calls
                scored.append((round(best[0] / 5), uses.get(fid, 0), fid, best))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return scored

    def as_item(fid, best, before):
        gain, qty, unit, servings, after = best
        fills, overs = _describe(slots, targets, bands, before, after)
        eq = lib.equiv(fid)
        return {"food_id": fid, "name": lib.foods[fid]["name"], "kind": lib.foods[fid]["kind"],
                "tag": "Recipe" if lib.foods[fid]["kind"] == "recipe" else lib.foods[fid]["category"],
                "qty": qty, "unit": unit, "amount": fmt_amount(qty, unit, eq["each_label"]),
                "kcal": sum(after) - sum(before), "gain": gain, "fills": fills[:3], "overs": overs}

    picks = [as_item(fid, best, actuals) for _, _, fid, best in pick(actuals)[:n]]

    plan, current, used = [], list(actuals), set()
    for _ in range(plan_size):
        ranked = pick(current, used)
        if not ranked:
            break
        _, _, fid, best = ranked[0]
        plan.append(as_item(fid, best, current))
        current = best[4]
        used.add(fid)

    def on_count(vals):
        return sum(1 for t, a in zip(targets, vals) if make_cell(t, a, cfg, factor)["status"] == "on")

    return {
        "picks": picks, "plan": plan, "remaining": remaining,
        "plan_total": sum(current) - sum(actuals),
        "plan_after": sum(current), "plan_target": sum(targets),
        "on_before": on_count(actuals), "on_after": on_count(current), "slot_count": len(slots),
        "plan_overs": [(name, a - t) for (name, _c, _m), t, a in zip(slots, targets, current)
                       if make_cell(t, a, cfg, factor)["status"] == "over"],
    }


# --------------------------------------------------------------------------
# Build from what's on hand: amounts of chosen ingredients that fit the gaps
# --------------------------------------------------------------------------
MAX_BUILD_STEPS = 600


def build_step(lib, fid):
    """The smallest sensible kitchen increment for a food: (qty, unit)."""
    eq = lib.equiv(fid)
    kcal = lib.summary(fid)["kcal"]
    if eq["each"]:
        piece_kcal = kcal / eq["each"]
        return (0.5 if piece_kcal > 120 else 1.0), "each"
    if lib.foods[fid]["kind"] == "recipe":
        return 0.25, "serving"
    if eq["ml"]:
        if eq["ml"] >= 59:
            return 0.25, "cup"
        return (1.0, "tsp") if eq["ml"] >= 14 else (0.5, "tsp")
    if eq["g"]:
        return (10.0, "g") if eq["g"] >= 30 else (5.0, "g")
    return 0.25, "serving"


def tidy_amount(qty, unit):
    """4 tsp stays, 3 tsp -> 1 tbsp, 1000 g -> 1 kg."""
    if unit == "tsp" and qty >= 3 and abs(qty / 3 - round(qty / 3)) < 1e-9:
        return qty / 3, "tbsp"
    if unit == "g" and qty >= 1000 and qty % 100 == 0:
        return qty / 1000, "kg"
    return qty, unit


def build_from_pantry(cfg, mx, lib, factor, pantry, exclude_hint=()):
    """pantry: list of (food_id, max_servings or None). Returns the best combination found."""
    slots, targets, bands, actuals = _slots(mx, cfg, factor)
    items = []
    for fid, cap in pantry:
        if fid not in lib.foods or lib.per_serving(fid)["issues"]:
            continue
        sq, su = build_step(lib, fid)
        sps = lib.to_servings(fid, sq, su)
        vec = _food_vector(lib, fid, slots)
        if not sps or sum(vec) <= 0:
            continue
        max_steps = int(cap / sps + 1e-9) if cap else None
        items.append({"fid": fid, "sq": sq, "su": su, "sps": sps,
                      "vec": [v * sps for v in vec], "max": max_steps, "n": 0})

    current = list(actuals)
    loss = _loss(targets, bands, current)

    def shifted(cur, item, k):
        return [a + v * k for a, v in zip(cur, item["vec"])]

    def can(item, k):
        n = item["n"] + k
        return n >= 0 and (item["max"] is None or n <= item["max"])

    # 1. Greedy: keep adding the single step that helps most
    for _ in range(MAX_BUILD_STEPS):
        best = None
        for it in items:
            if can(it, 1):
                nxt = shifted(current, it, 1)
                gain = loss - _loss(targets, bands, nxt)
                if best is None or gain > best[0]:
                    best = (gain, it, nxt)
        if not best or best[0] <= 0.5:
            break
        best[1]["n"] += 1
        current, loss = best[2], loss - best[0]

    # 2. Polish: single removals and pairwise swaps until nothing helps
    for _ in range(MAX_BUILD_STEPS):
        best = None
        moves = [((it, -1),) for it in items] + \
                [((a, 1), (b, -1)) for a in items for b in items if a is not b]
        for move in moves:
            if not all(can(it, k) for it, k in move):
                continue
            nxt = current
            for it, k in move:
                nxt = shifted(nxt, it, k)
            gain = loss - _loss(targets, bands, nxt)
            if gain > 0.5 and (best is None or gain > best[0]):
                best = (gain, move, nxt)
        if not best:
            break
        for it, k in best[1]:
            it["n"] += k
        current, loss = best[2], loss - best[0]

    used, left_out = [], []
    for it in items:
        f = lib.foods[it["fid"]]
        tag = "Recipe" if f["kind"] == "recipe" else f["category"]
        if it["n"] == 0:
            _f, overs = _describe(slots, targets, bands, current, shifted(current, it, 1))
            reason = ("it would push " + " and ".join(n for n, _ in overs[:2]) + " over") if overs \
                else "it doesn't help with what's left"
            if it["max"] == 0:
                reason = "your limit is less than one step"
            left_out.append({"name": f["name"], "reason": reason})
            continue
        qty, unit = tidy_amount(it["n"] * it["sq"], it["su"])
        used.append({"food_id": it["fid"], "name": f["name"], "tag": tag, "qty": qty, "unit": unit,
                     "amount": fmt_amount(qty, unit, lib.equiv(it["fid"])["each_label"]),
                     "kcal": sum(it["vec"]) * it["n"],
                     "at_limit": it["max"] is not None and it["n"] >= it["max"]})
    used.sort(key=lambda u: -u["kcal"])

    fills, overs = _describe(slots, targets, bands, actuals, current)

    def status(t, a):
        return make_cell(t, a, cfg, factor)["status"]

    after_cells = {}
    for (name, c, m), t, a in zip(slots, targets, current):
        after_cells[(c, m)] = {"target": t, "actual": a, "status": status(t, a)}
    still_open = sorted([(name, t - a) for (name, _c, _m), t, a in zip(slots, targets, current)
                         if status(t, a) == "under"], key=lambda x: -x[1])

    # One library food (not on hand) that would close the most of what's left
    hint = None
    if still_open:
        taken = {it["fid"] for it in items} | set(exclude_hint)
        best = None
        for fid in lib.foods:
            if fid in taken or lib.per_serving(fid)["issues"]:
                continue
            vec = _food_vector(lib, fid, slots)
            cands = _candidates(lib, fid)
            if not cands or sum(vec) <= 0:
                continue
            b = _best_amount(vec, targets, bands, current, cands)
            if b and b[0] >= MIN_IMPROVEMENT and (best is None or b[0] > best[0][0]):
                best = (b, fid)
        if best:
            (gain, qty, unit, _s, after), fid = best
            hf, _ho = _describe(slots, targets, bands, current, after)
            hint = {"name": lib.foods[fid]["name"],
                    "amount": fmt_amount(qty, unit, lib.equiv(fid)["each_label"]),
                    "fills": hf[:2]}

    on = lambda vals: sum(1 for t, a in zip(targets, vals) if status(t, a) == "on")
    return {"used": used, "left_out": left_out, "total": sum(current) - sum(actuals),
            "fills": fills[:4], "overs": overs, "after": after_cells,
            "after_total": sum(current), "target_total": sum(targets),
            "on_before": on(actuals), "on_after": on(current), "slot_count": len(slots),
            "still_open": still_open[:3], "hint": hint}


# --------------------------------------------------------------------------
# Template helpers
# --------------------------------------------------------------------------
@app.template_filter("num")
def num_filter(value, digits=0):
    if value is None:
        return "–"
    return f"{value:,.{digits}f}"


@app.template_filter("pct")
def pct_filter(value):
    if value is None:
        return "–"
    return f"{round(value * 100, 1):g}%"


@app.template_filter("qty")
def qty_filter(value):
    return fmt_qty(value)


@app.context_processor
def inject_globals():
    return {"CATEGORIES": CATEGORIES, "ALL_CATEGORIES": ALL_CATEGORIES,
            "EXTRA_CATEGORY": EXTRA_CATEGORY, "MACROS": MACROS,
            "MACRO_LABELS": MACRO_LABELS, "MEALS": MEALS, "UNITS": UNITS,
            "WEIGHT_UNITS": WEIGHT_UNITS, "VOLUME_UNITS": VOLUME_UNITS}


def view_params(form):
    return {k: form.get(f"v_{k}") for k in ("scope", "day", "meal") if form.get(f"v_{k}")}


def back_to_view(form, **extra):
    """Redirect to the matrix view the user was looking at."""
    return redirect(url_for("matrix", **view_params(form), **extra))


# --------------------------------------------------------------------------
# Routes: matrix + log
# --------------------------------------------------------------------------
@app.route("/")
def matrix():
    cfg = load_config()
    lib = get_library()
    period = resolve_period(request.args, cfg)
    entries = fetch_entries(period, lib)
    mx = build_matrix(cfg, entries, period["factor"])

    foods = sorted(lib.foods.values(), key=lambda f: f["name"].lower())
    groups = [("Recipes", [f for f in foods if f["kind"] == "recipe"])]
    groups += [(c, [f for f in foods if f["kind"] != "recipe" and f["category"] == c])
               for c in ALL_CATEGORIES]
    summaries = {f["id"]: lib.summary(f["id"]) for f in foods}

    if period["scope"] == "week":
        log_day = date.today() if period["is_today"] else period["start"]
    else:
        log_day = period["day"]
    log_meal = period["meal"] if period["scope"] == "meal" else guess_meal()
    selected = request.args.get("food", type=int)
    uses = dict(get_db().execute("SELECT food_id, COUNT(*) FROM log GROUP BY food_id").fetchall())
    tips = suggest(cfg, mx, lib, period["factor"], uses)

    return render_template(
        "matrix.html", cfg=cfg, p=period, mx=mx, days=group_entries(entries),
        groups=[gr for gr in groups if gr[1]], summaries=summaries, has_foods=bool(foods),
        log_day=log_day.isoformat(), log_meal=log_meal, selected=selected,
        food_data=lib.client_data(), tips=tips,
    )


def insert_log(lib, food_id, qty_text, unit, day, meal):
    """Validate and add one log entry. Returns (message, error)."""
    food = lib.foods.get(food_id)
    if not food:
        return None, "Choose a food from the list, or add a new one."
    qty = parse_qty(qty_text)
    if qty is None:
        return None, "Enter an amount like 1, 0.5, 3/4 or 1 1/2."
    servings = lib.to_servings(food["id"], qty, unit)
    if servings is None:
        dim = UNITS.get(unit, ("that unit",))[0]
        return None, (f"{food['name']} has no {dim} set for its serving, so it can't be logged in "
                      f"{unit_label(unit, 2)}. Pick another unit, or add a {dim} on its edit page.")
    get_db().execute("INSERT INTO log (day, meal, food_id, servings, quantity, unit) VALUES (?, ?, ?, ?, ?, ?)",
                     (day.isoformat(), meal, food["id"], servings, qty, unit))
    return f"{fmt_amount(qty, unit, lib.equiv(food['id'])['each_label'])} of {food['name']}", None


def _log_target(form):
    day = parse_day(form.get("day"))
    meal = form.get("meal") if form.get("meal") in MEALS else guess_meal()
    return day, meal


@app.post("/log")
def add_log():
    form = request.form
    lib = get_library()
    day, meal = _log_target(form)
    food_id = form.get("food_id", type=int)
    msg, err = insert_log(lib, food_id, form.get("quantity"), form.get("unit", "serving"), day, meal)
    if err:
        flash(err, "error")
        return back_to_view(form, **({"food": food_id} if food_id in lib.foods else {}))
    get_db().commit()
    flash(f"Logged {msg} to {meal.lower()} on {fmt_day(day)}.", "ok")
    return back_to_view(form)


@app.post("/log/batch")
def add_log_batch():
    """Log several suggested foods at once; all or nothing."""
    form = request.form
    lib = get_library()
    day, meal = _log_target(form)
    done = []
    for fid, qty, unit in zip(form.getlist("food_id"), form.getlist("quantity"), form.getlist("unit")):
        msg, err = insert_log(lib, int(fid) if fid.isdigit() else None, qty, unit, day, meal)
        if err:
            get_db().rollback()
            flash(err, "error")
            return back_to_view(form)
        done.append(msg)
    get_db().commit()
    flash(f"Logged {len(done)} foods to {meal.lower()} on {fmt_day(day)}: {', '.join(done)}.", "ok")
    return back_to_view(form)


@app.post("/log/<int:entry_id>/delete")
def delete_log(entry_id):
    db = get_db()
    db.execute("DELETE FROM log WHERE id = ?", (entry_id,))
    db.commit()
    flash("Removed from the log.", "ok")
    return back_to_view(request.form)


# --------------------------------------------------------------------------
# Routes: food library
# --------------------------------------------------------------------------
@app.route("/foods")
def foods():
    lib = get_library()
    uses = dict(get_db().execute("SELECT food_id, COUNT(*) FROM log GROUP BY food_id").fetchall())
    items = sorted(lib.foods.values(), key=lambda f: f["name"].lower())
    rows = [{**f, **lib.summary(f["id"]), "uses": uses.get(f["id"], 0),
             "used_in": lib.used_in(f["id"]), "issues": lib.per_serving(f["id"])["issues"]}
            for f in items]
    ingredients = {c: [r for r in rows if r["kind"] != "recipe" and r["category"] == c]
                   for c in ALL_CATEGORIES}
    recipes = [r for r in rows if r["kind"] == "recipe"]
    return render_template("foods.html", ingredients=ingredients, recipes=recipes,
                           n_ingredients=len(rows) - len(recipes))


def blank_ingredient():
    return {"name": "", "category": CATEGORIES[0], "serving": "", "carbs": "", "fat": "",
            "protein": "", "weight": "", "weight_unit": "g", "volume": "", "volume_unit": "cup",
            "count": "", "each_label": ""}


def ingredient_values(f):
    v = blank_ingredient()
    v.update(name=f["name"], category=f["category"], serving=f["serving"],
             **{m: f"{f[m + '_g']:g}" for m in MACROS})
    if f["serving_g"]:
        v["weight"] = f"{round(f['serving_g'], 2):g}"
    if f["serving_ml"]:
        q, u = nice_volume(f["serving_ml"])
        v["volume"], v["volume_unit"] = f"{q:g}", u
    if f["serving_each"]:
        v["count"] = f"{f['serving_each']:g}"
    v["each_label"] = f["each_label"] or ""
    return v


def blank_recipe():
    return {"name": "", "serving": "1 serving", "yield": "1", "weight": "", "weight_unit": "g",
            "volume": "", "volume_unit": "cup", "count": "", "each_label": "", "rows": []}


def recipe_values(f, lib):
    v = blank_recipe()
    v.update(name=f["name"], serving=f["serving"], each_label=f["each_label"] or "",
             **{"yield": f"{f['yield_servings'] or 1:g}"})
    if f["batch_g"]:
        v["weight"] = f"{round(f['batch_g'], 1):g}"
    if f["batch_ml"]:
        q, u = nice_volume(f["batch_ml"])
        v["volume"], v["volume_unit"] = f"{q:g}", u
    if f["batch_each"]:
        v["count"] = f"{f['batch_each']:g}"
    v["rows"] = [{"food_id": c["child_id"], "qty": fmt_qty(c["quantity"]), "unit": c["unit"]}
                 for c in lib.components.get(f["id"], [])]
    return v


def read_measure(form, field, dim, label):
    """Optional measure + unit -> base amount, or raises ValueError."""
    raw = (form.get(field) or "").strip()
    if not raw:
        return None
    q = parse_qty(raw)
    base = to_base(q, form.get(f"{field}_unit", "g" if dim == "weight" else "ml"), dim) if q else None
    if base is None:
        raise ValueError(f"{label} must be an amount above 0, like 150 or 1 1/2.")
    return base


def save_ingredient(form, food_id=None):
    """Validate and save. Returns (id, error)."""
    name = (form.get("name") or "").strip()
    category = form.get("category")
    if not name:
        return None, "Give the ingredient a name."
    if category not in ALL_CATEGORIES:
        return None, "Pick a category for the ingredient."
    data = {"name": name, "category": category,
            "serving": (form.get("serving") or "").strip() or "1 serving"}
    for m in MACROS:
        raw = (form.get(m) or "").strip()
        v = parse_float(raw, default=None, minimum=0) if raw else 0.0
        if v is None:
            return None, f"{MACRO_LABELS[m]} must be a number of grams, 0 or more."
        data[f"{m}_g"] = v
    try:
        data["serving_g"] = read_measure(form, "weight", "weight", "Weight")
        data["serving_ml"] = read_measure(form, "volume", "volume", "Volume")
    except ValueError as e:
        return None, str(e)
    raw = (form.get("count") or "").strip()
    data["serving_each"] = parse_qty(raw) if raw else None
    if raw and data["serving_each"] is None:
        return None, "Count must be an amount above 0, like 1 or 2."
    data["each_label"] = (form.get("each_label") or "").strip() or None
    if data["serving_each"] and not data["each_label"]:
        data["each_label"] = "piece"

    db = get_db()
    try:
        if food_id:
            db.execute("""UPDATE foods SET name=:name, category=:category, serving=:serving,
                          carbs_g=:carbs_g, fat_g=:fat_g, protein_g=:protein_g, serving_g=:serving_g,
                          serving_ml=:serving_ml, serving_each=:serving_each, each_label=:each_label
                          WHERE id=:id""", {**data, "id": food_id})
        else:
            cur = db.execute("""INSERT INTO foods (name, kind, category, serving, carbs_g, fat_g,
                                protein_g, serving_g, serving_ml, serving_each, each_label)
                                VALUES (:name, 'ingredient', :category, :serving, :carbs_g, :fat_g,
                                :protein_g, :serving_g, :serving_ml, :serving_each, :each_label)""", data)
            food_id = cur.lastrowid
        db.commit()
    except sqlite3.IntegrityError as e:
        db.rollback()
        return None, integrity_message(e, name)
    return food_id, None


def integrity_message(err, name):
    if "UNIQUE" in str(err):
        return f"Something called “{name}” is already in your foods."
    app.logger.error("Could not save %s: %s", name, err)
    return f"“{name}” couldn't be saved ({err}). Nothing was changed."


def read_recipe_rows(form):
    rows = []
    for fid, qty, unit in zip(form.getlist("comp_food"), form.getlist("comp_qty"),
                              form.getlist("comp_unit")):
        if not fid and not qty.strip():
            continue
        rows.append({"food_id": int(fid) if fid.isdigit() else None, "qty": qty.strip(), "unit": unit})
    return rows


def save_recipe(form, lib, recipe_id=None):
    """Validate and save. Returns (id, error)."""
    name = (form.get("name") or "").strip()
    if not name:
        return None, "Give the recipe a name."
    yld = parse_qty(form.get("yield"))
    if not yld:
        return None, "Enter how many servings the recipe makes, like 4 or 1 1/2."
    try:
        batch_g = read_measure(form, "weight", "weight", "Batch weight")
        batch_ml = read_measure(form, "volume", "volume", "Batch volume")
    except ValueError as e:
        return None, str(e)
    raw = (form.get("count") or "").strip()
    batch_each = parse_qty(raw) if raw else None
    if raw and batch_each is None:
        return None, "Pieces must be an amount above 0, like 12."
    each_label = (form.get("each_label") or "").strip() or None
    if batch_each and not each_label:
        each_label = "piece"

    rows = read_recipe_rows(form)
    if not rows:
        return None, "Add at least one ingredient or recipe."
    comps = []
    for i, r in enumerate(rows, 1):
        child = lib.foods.get(r["food_id"])
        if not child:
            return None, f"Line {i}: choose an ingredient or recipe."
        if recipe_id and lib.contains(child["id"], recipe_id):
            return None, f"Line {i}: {child['name']} already uses this recipe, so it can't go inside it."
        q = parse_qty(r["qty"])
        if q is None:
            return None, f"Line {i}: enter an amount for {child['name']}, like 2, 3/4 or 150."
        if lib.to_servings(child["id"], q, r["unit"]) is None:
            dim = UNITS.get(r["unit"], ("that unit",))[0]
            return None, (f"Line {i}: {child['name']} has no {dim} set, so it can't be measured in "
                          f"{unit_label(r['unit'], 2)}. Choose another unit.")
        comps.append((child["id"], q, r["unit"], i))

    data = {"name": name, "serving": (form.get("serving") or "").strip() or "1 serving",
            "yield_servings": yld, "batch_g": batch_g, "batch_ml": batch_ml,
            "batch_each": batch_each, "each_label": each_label}
    db = get_db()
    try:
        if recipe_id:
            db.execute("""UPDATE foods SET name=:name, serving=:serving, yield_servings=:yield_servings,
                          batch_g=:batch_g, batch_ml=:batch_ml, batch_each=:batch_each,
                          each_label=:each_label WHERE id=:id""", {**data, "id": recipe_id})
            db.execute("DELETE FROM recipe_components WHERE recipe_id = ?", (recipe_id,))
        else:
            cur = db.execute("""INSERT INTO foods (name, kind, category, serving, yield_servings, batch_g,
                                batch_ml, batch_each, each_label)
                                VALUES (:name, 'recipe', '', :serving, :yield_servings, :batch_g,
                                :batch_ml, :batch_each, :each_label)""", data)
            recipe_id = cur.lastrowid
        db.executemany("INSERT INTO recipe_components (recipe_id, child_id, quantity, unit, position) "
                       "VALUES (?, ?, ?, ?, ?)", [(recipe_id, *c) for c in comps])
        db.commit()
    except sqlite3.IntegrityError as e:
        db.rollback()
        return None, integrity_message(e, name)
    return recipe_id, None


def after_save(form, food_id, message):
    flash(message, "ok")
    if form.get("back") == "matrix":
        return back_to_view(form, food=food_id)
    return redirect(url_for("foods"))


def render_food_form(kind, values, food=None):
    lib = get_library()
    ctx = {"values": values, "food": food, "back": request.values.get("back", ""),
           "view": {k: request.values.get(k) or request.values.get(f"v_{k}") or ""
                    for k in ("scope", "day", "meal")}}
    if food:
        ctx["uses"] = get_db().execute("SELECT COUNT(*) FROM log WHERE food_id = ?",
                                       (food["id"],)).fetchone()[0]
        ctx["used_in"] = lib.used_in(food["id"])
    if kind == "recipe":
        choices = [f for f in lib.foods.values()
                   if not (food and lib.contains(f["id"], food["id"]))]
        choices.sort(key=lambda f: f["name"].lower())
        ctx["choice_groups"] = [("Recipes", [f for f in choices if f["kind"] == "recipe"])] + \
            [(c, [f for f in choices if f["kind"] != "recipe" and f["category"] == c])
             for c in ALL_CATEGORIES]
        ctx["food_data"] = lib.client_data()
        ctx["issues"] = lib.per_serving(food["id"])["issues"] if food else []
        return render_template("recipe_form.html", **ctx)
    return render_template("ingredient_form.html", **ctx)


@app.route("/foods/new/<kind>", methods=["GET", "POST"])
def new_food(kind):
    if kind not in ("ingredient", "recipe"):
        return redirect(url_for("foods"))
    if request.method == "POST":
        form = request.form
        if kind == "recipe":
            fid, err = save_recipe(form, get_library())
            values = {**blank_recipe(), **form.to_dict(), "rows": read_recipe_rows(form)}
        else:
            fid, err = save_ingredient(form)
            values = {**blank_ingredient(), **form.to_dict()}
        if not err:
            return after_save(form, fid, f"Added {form.get('name').strip()}.")
        flash(err, "error")
        return render_food_form(kind, values)
    if kind == "recipe" and request.args.getlist("comp_food"):
        values = {**blank_recipe(), "rows": read_recipe_rows(request.args),
                  **{k: request.args[k] for k in ("name", "serving", "yield") if request.args.get(k)}}
        return render_food_form(kind, values)
    return render_food_form(kind, blank_recipe() if kind == "recipe" else blank_ingredient())


@app.route("/foods/<int:food_id>/edit", methods=["GET", "POST"])
def edit_food(food_id):
    lib = get_library()
    food = lib.foods.get(food_id)
    if not food:
        flash("That food no longer exists.", "error")
        return redirect(url_for("foods"))
    kind = food["kind"]
    if request.method == "POST":
        form = request.form
        if kind == "recipe":
            fid, err = save_recipe(form, lib, food_id)
            values = {**blank_recipe(), **form.to_dict(), "rows": read_recipe_rows(form)}
        else:
            fid, err = save_ingredient(form, food_id)
            values = {**blank_ingredient(), **form.to_dict()}
        if not err:
            return after_save(form, fid, f"Saved {form.get('name').strip()}. "
                                         "Past log entries and recipes that use it are updated.")
        flash(err, "error")
        return render_food_form(kind, values, food)
    values = recipe_values(food, lib) if kind == "recipe" else ingredient_values(food)
    return render_food_form(kind, values, food)


@app.post("/foods/<int:food_id>/delete")
def delete_food(food_id):
    lib = get_library()
    food = lib.foods.get(food_id)
    if not food:
        return redirect(url_for("foods"))
    used_in = lib.used_in(food_id)
    if used_in:
        flash(f"{food['name']} is part of {', '.join(used_in)}. Remove it from "
              f"{'that recipe' if len(used_in) == 1 else 'those recipes'} first.", "error")
        return redirect(url_for("edit_food", food_id=food_id))
    db = get_db()
    uses = db.execute("SELECT COUNT(*) FROM log WHERE food_id = ?", (food_id,)).fetchone()[0]
    if uses and request.form.get("with_log") != "1":
        flash("That food is in your log. Delete it from its edit page to remove its log entries too.", "error")
        return redirect(url_for("foods"))
    db.execute("DELETE FROM log WHERE food_id = ?", (food_id,))
    db.execute("DELETE FROM recipe_components WHERE recipe_id = ?", (food_id,))
    db.execute("DELETE FROM foods WHERE id = ?", (food_id,))
    db.commit()
    flash(f"Deleted {food['name']}.", "ok")
    return redirect(url_for("foods"))


# --------------------------------------------------------------------------
# Routes: build from what's on hand
# --------------------------------------------------------------------------
def load_pantry():
    row = get_db().execute("SELECT value FROM settings WHERE key='pantry'").fetchone()
    return json.loads(row["value"]) if row else {"have": [], "limits": {}}


def save_pantry(pantry):
    db = get_db()
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('pantry', ?)", (json.dumps(pantry),))
    db.commit()


@app.route("/build")
def build():
    cfg = load_config()
    lib = get_library()
    period = resolve_period(request.args, cfg)
    mx = build_matrix(cfg, fetch_entries(period, lib), period["factor"])

    if "go" in request.args:  # the form was submitted: remember what's on hand
        have = [int(x) for x in request.args.getlist("have") if x.isdigit() and int(x) in lib.foods]
        limits = {}
        for fid in lib.foods:
            raw = (request.args.get(f"max_{fid}") or "").strip()
            if raw:
                limits[str(fid)] = [raw, request.args.get(f"maxu_{fid}", "serving")]
        pantry = {"have": have, "limits": limits}
        save_pantry(pantry)
    else:
        pantry = load_pantry()
        pantry["have"] = [f for f in pantry.get("have", []) if f in lib.foods]

    errors, chosen = [], []
    for fid in pantry["have"]:
        cap = None
        lim = pantry.get("limits", {}).get(str(fid))
        if lim:
            q = parse_qty(lim[0])
            cap = lib.to_servings(fid, q, lim[1]) if q else None
            if cap is None:
                errors.append(f"Couldn't read the limit for {lib.foods[fid]['name']}, so it's treated as unlimited.")
        chosen.append((fid, cap))

    result = build_from_pantry(cfg, mx, lib, period["factor"], chosen) if chosen and "go" in request.args else None

    foods = sorted(lib.foods.values(), key=lambda f: f["name"].lower())
    groups = [(c, [f for f in foods if f["kind"] != "recipe" and f["category"] == c]) for c in ALL_CATEGORIES]
    groups.append(("Recipes and leftovers", [f for f in foods if f["kind"] == "recipe"]))
    natural = {f["id"]: build_step(lib, f["id"])[1] for f in foods}
    log_day = (date.today() if period["is_today"] else period["start"]) if period["scope"] == "week" else period["day"]
    log_meal = period["meal"] if period["scope"] == "meal" else guess_meal()
    return render_template(
        "build.html", p=period, mx=mx, cfg=cfg, groups=[gr for gr in groups if gr[1]],
        pantry=pantry, natural=natural, summaries={f["id"]: lib.summary(f["id"]) for f in foods},
        result=result, errors=errors, food_data=lib.client_data(),
        log_day=log_day.isoformat(), log_meal=log_meal,
    )


# --------------------------------------------------------------------------
# Routes: plan (settings)
# --------------------------------------------------------------------------
@app.route("/plan", methods=["GET", "POST"])
def plan():
    cfg = load_config()
    if request.method == "POST":
        f = request.form
        daily = parse_float(f.get("daily_calories"), default=None, minimum=1)
        if daily is None:
            flash("Daily calories must be a number above 0.", "error")
            return redirect(url_for("plan"))
        cfg["daily_calories"] = daily
        cfg["extra_pct"] = min(parse_float(f.get("extra_pct"), cfg["extra_pct"] * 100, 0), 99) / 100
        cfg["pct_base"] = "total" if f.get("pct_base") == "total" else "daily"
        for i, cat in enumerate(CATEGORIES):
            for m in MACROS:
                current = cfg["matrix"][cat][m] * 100
                cfg["matrix"][cat][m] = parse_float(f.get(f"m_{i}_{m}"), current, 0) / 100
        for meal in MEALS:
            current = cfg["meal_split"][meal] * 100
            cfg["meal_split"][meal] = parse_float(f.get(f"meal_{meal}"), current, 0) / 100
        cfg["tolerance"] = parse_float(f.get("tolerance"), cfg["tolerance"] * 100, 0) / 100
        cfg["min_band_kcal"] = parse_float(f.get("min_band_kcal"), cfg["min_band_kcal"], 0)
        save_config(cfg)
        flash("Plan saved. The matrix now uses these targets.", "ok")
        return redirect(url_for("plan"))

    base = pct_base_kcal(cfg)
    matrix_sum = sum(v for row in cfg["matrix"].values() for v in row.values())
    targets = {cat: {m: cfg["matrix"][cat][m] * base for m in MACROS} for cat in CATEGORIES}
    core = sum(v for row in targets.values() for v in row.values())
    allowance = extra_allowance(cfg)
    return render_template(
        "plan.html", cfg=cfg, base=base, matrix_sum=matrix_sum, targets=targets,
        core=core, allowance=allowance, meal_sum=sum(cfg["meal_split"].values()),
    )


@app.post("/plan/reset")
def reset_plan():
    save_config(copy.deepcopy(DEFAULT_SETTINGS))
    flash("Plan reset to the spreadsheet values.", "ok")
    return redirect(url_for("plan"))


init_db()

if __name__ == "__main__":
    app.run(debug=True)
