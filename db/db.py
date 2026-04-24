"""
db.py — SQLite connection and helpers.

Using SQLite for local dev; swap the connect() call for mysql.connector
or pymysql when moving to MySQL in prod.
"""

import sqlite3
import json
from pathlib import Path

DB_PATH = Path(__file__).parent / "dining.db"
SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """Create tables if they don't exist yet."""
    import re as _re
    raw = SCHEMA_PATH.read_text()
    # SQLite doesn't support ENUM — replace any ENUM(...) with TEXT.
    ddl = _re.sub(r"ENUM\([^)]+\)", "TEXT", raw, flags=_re.DOTALL)
    ddl = ddl.replace("AUTO_INCREMENT", "").replace("INT PRIMARY KEY", "INTEGER PRIMARY KEY")

    with get_connection() as conn:
        conn.executescript(ddl)


def upsert_food(hall_id: int, food: dict) -> int:
    """Insert a food row; skip if rec_num already cached. Returns food_id."""
    sql_select = "SELECT food_id FROM foods WHERE rec_num = ?"
    sql_insert = """
        INSERT INTO foods
            (hall_id, food_name, meal_type, day_of_week, rec_num,
             calories, fat_g, carbs_g, protein_g, serving_size, allergens)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """
    allergens_str = json.dumps(food.get("allergens", []))
    with get_connection() as conn:
        row = conn.execute(sql_select, (food["rec_num"],)).fetchone()
        if row:
            return row["food_id"]
        cur = conn.execute(sql_insert, (
            hall_id,
            food["food_name"],
            food["meal_type"],
            food["day_of_week"],
            food["rec_num"],
            food["calories"],
            food["fat_g"],
            food["carbs_g"],
            food.get("protein_g"),
            food.get("serving_size"),
            allergens_str,
        ))
        return cur.lastrowid


def get_foods_for_meal(hall_id: int, day: str, meal_type: str) -> list[dict]:
    sql = """
        SELECT * FROM foods
        WHERE hall_id = ? AND day_of_week = ? AND meal_type = ?
    """
    with get_connection() as conn:
        rows = conn.execute(sql, (hall_id, day.lower(), meal_type.lower())).fetchall()
    return [dict(r) for r in rows]


def save_meal_plan(user_id: int, food_ids: list[int], day: str,
                   meal_type: str, plan_date: str) -> None:
    sql = """
        INSERT INTO meal_plan (user_id, food_id, day_of_week, meal_type, plan_date)
        VALUES (?,?,?,?,?)
    """
    with get_connection() as conn:
        conn.executemany(sql, [
            (user_id, fid, day.lower(), meal_type.lower(), plan_date)
            for fid in food_ids
        ])
