"""
scraper.py — Weekly full-menu scraper for all three UMD dining halls.

Cron: run once per week (menu updates Monday).
Only fetches label.aspx for items not already in the DB (rec_num cache).

Menu endpoint: longmenu.aspx?locationNum={id}&dtdate={M/D/YYYY}&mealName={Breakfast|Lunch|Dinner}
Hall IDs: South Campus=16, Yahentamitsi=19, 251 North=51
"""

import re
import time
import random
import datetime
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup

sys.path.append(str(Path(__file__).parent.parent))
from db.db import init_db, upsert_food, get_connection

BASE_URL = "https://nutrition.umd.edu"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": BASE_URL,
}

DINING_HALLS = [
    {"name": "South Campus Dining Hall", "location_num": "16"},
    {"name": "Yahentamitsi Dining Hall",  "location_num": "19"},
    {"name": "251 North",                 "location_num": "51"},
]

# mealName param values the site accepts
MEAL_PARAMS = ["Breakfast", "Lunch", "Dinner"]

DAYS_AHEAD = 7  # scrape the next 7 days from today


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sleep() -> None:
    time.sleep(random.uniform(1.0, 3.0))


def _get(session: requests.Session, url: str) -> BeautifulSoup:
    resp = session.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


def _rec_num_cached(rec_num: str) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM foods WHERE rec_num = ?", (rec_num,)
        ).fetchone()
    return row is not None


def _date_str(d: datetime.date) -> str:
    return f"{d.month}/{d.day}/{d.year}"


def _day_name(d: datetime.date) -> str:
    return d.strftime("%A").lower()  # "monday", "tuesday", ...


# ── Menu page scraper ─────────────────────────────────────────────────────────

def scrape_menu_page(session: requests.Session, location_num: str,
                     date: datetime.date, meal: str) -> list[dict]:
    """
    Returns a list of { rec_num, food_name, allergens } from one
    longmenu.aspx page (one hall / one date / one meal).

    Structure: <tr> rows where food rows contain an <a href="label.aspx?...RecNumAndPort=...">
    and allergen icons are <img alt="Contains dairy"> siblings in the same <td>.
    Station rows have <strong> text but no food link.
    """
    date_str = _date_str(date)
    url = (f"{BASE_URL}/longmenu.aspx"
           f"?locationNum={location_num}&dtdate={date_str}&mealName={meal}")
    try:
        soup = _get(session, url)
    except requests.RequestException as e:
        print(f"  [warn] {url}: {e}")
        return []

    items = []
    for a in soup.find_all("a", href=re.compile(r"RecNumAndPort")):
        m = re.search(r"RecNumAndPort=([^&]+)", a["href"])
        if not m:
            continue
        rec_num = m.group(1)
        food_name = a.get_text(strip=True)

        # Allergen icons are <img alt="Contains X"> siblings in the same <td>
        td = a.find_parent("td")
        allergens = []
        if td:
            for img in td.find_all("img", alt=re.compile(r"contains", re.I)):
                allergen = img["alt"].lower().replace("contains", "").strip()
                if allergen and allergen not in allergens:
                    allergens.append(allergen)

        items.append({
            "rec_num": rec_num,
            "food_name": food_name,
            "allergens_menu": allergens,  # fast path; label page has the authoritative list
        })

    return items


# ── Label page parser ─────────────────────────────────────────────────────────

def scrape_label(session: requests.Session, rec_num: str) -> dict | None:
    url = f"{BASE_URL}/label.aspx?RecNumAndPort={rec_num}"
    try:
        soup = _get(session, url)
    except requests.RequestException as e:
        print(f"  [warn] {url}: {e}")
        return None

    result = {
        "rec_num": rec_num,
        "food_name": None,
        "serving_size": None,
        "calories": None,
        "fat_g": None,
        "carbs_g": None,
        "protein_g": None,
        "allergens": [],
    }

    title_tag = soup.find("title")
    if title_tag and "Label -" in title_tag.text:
        result["food_name"] = title_tag.text.split("Label -", 1)[1].strip()

    lines = [l.strip() for l in soup.get_text(separator="\n").splitlines() if l.strip()]
    for i, line in enumerate(lines):
        ll = line.lower()

        if ll == "serving size" and result["serving_size"] is None:
            if i + 1 < len(lines):
                result["serving_size"] = lines[i + 1]

        if ll == "calories per serving" and result["calories"] is None:
            if i + 1 < len(lines) and re.match(r"^\d+$", lines[i + 1]):
                result["calories"] = int(lines[i + 1])

        if "total fat" in ll and result["fat_g"] is None:
            m = re.search(r"(\d+\.?\d*)\s*g", line) or (
                re.search(r"(\d+\.?\d*)\s*g", lines[i + 1]) if i + 1 < len(lines) else None
            )
            if m:
                result["fat_g"] = float(m.group(1))

        if "total carbohydrate" in ll and result["carbs_g"] is None:
            m = re.search(r"(\d+\.?\d*)\s*g", line) or (
                re.search(r"(\d+\.?\d*)\s*g", lines[i + 1]) if i + 1 < len(lines) else None
            )
            if m:
                result["carbs_g"] = float(m.group(1))

        if ll == "protein" and result["protein_g"] is None:
            if i + 1 < len(lines):
                m = re.search(r"(\d+\.?\d*)\s*g", lines[i + 1])
                if m:
                    result["protein_g"] = float(m.group(1))

    for i, line in enumerate(lines):
        if line.strip().upper() == "ALLERGENS:" and i + 1 < len(lines):
            raw = lines[i + 1]
            if len(raw) < 200:
                result["allergens"] = [a.strip().lower() for a in raw.split(",") if a.strip()]
            break

    return result


# ── Orchestrator ──────────────────────────────────────────────────────────────

def run_weekly_scrape() -> None:
    init_db()
    session = requests.Session()
    today = datetime.date.today()

    # Seed dining halls table.
    with get_connection() as conn:
        for hall in DINING_HALLS:
            exists = conn.execute(
                "SELECT hall_id FROM dining_halls WHERE hall_name = ?", (hall["name"],)
            ).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO dining_halls (hall_name, nutrition_url) VALUES (?,?)",
                    (hall["name"], f"{BASE_URL}/longmenu.aspx?locationNum={hall['location_num']}"),
                )

    for hall in DINING_HALLS:
        with get_connection() as conn:
            hall_id = conn.execute(
                "SELECT hall_id FROM dining_halls WHERE hall_name = ?", (hall["name"],)
            ).fetchone()["hall_id"]

        print(f"\n[{hall['name']}]")
        new_count = 0

        for day_offset in range(DAYS_AHEAD):
            date = today + datetime.timedelta(days=day_offset)
            day_name = _day_name(date)

            for meal in MEAL_PARAMS:
                menu_items = scrape_menu_page(session, hall["location_num"], date, meal)
                if not menu_items:
                    continue
                print(f"  {day_name} {meal}: {len(menu_items)} items")

                for item in menu_items:
                    if _rec_num_cached(item["rec_num"]):
                        continue

                    _sleep()
                    label = scrape_label(session, item["rec_num"])
                    if label is None:
                        continue

                    label["meal_type"] = meal.lower()
                    label["day_of_week"] = day_name
                    # Fall back to menu-page food name if label parse missed it
                    if not label["food_name"]:
                        label["food_name"] = item["food_name"]
                    # Use label allergens (authoritative); fall back to menu icons
                    if not label["allergens"]:
                        label["allergens"] = item["allergens_menu"]

                    if any(label[f] is None for f in ("food_name", "calories", "fat_g", "carbs_g")):
                        print(f"  [skip] incomplete: {item['rec_num']} {label}")
                        continue

                    upsert_food(hall_id, label)
                    new_count += 1
                    print(f"    [new] {label['food_name']}")

        print(f"  Total new items: {new_count}")

    print("\nWeekly scrape complete.")


if __name__ == "__main__":
    run_weekly_scrape()
