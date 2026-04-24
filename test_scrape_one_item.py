"""
test_scrape_one_item.py
-----------------------
Scrapes a single food item from the UMD Nutrition site and prints
its name, calories, fat, and carbs.

Run with:
    pip install requests beautifulsoup4
    python test_scrape_one_item.py
"""

import re
import json
import requests
from bs4 import BeautifulSoup

# ── Config ────────────────────────────────────────────────────────────────────
TEST_ITEM = {
    "name": "French Toast",
    "rec_num": "119370*1",
}

BASE_URL = "https://nutrition.umd.edu"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": BASE_URL,
}

# ── Scraper ───────────────────────────────────────────────────────────────────
def scrape_label(rec_num: str) -> dict:
    """
    Fetches a single label.aspx page and extracts:
      - food_name
      - calories
      - fat_g
      - carbs_g
      - protein_g
      - serving_size
      - allergens (list)
    """
    url = f"{BASE_URL}/label.aspx?RecNumAndPort={rec_num}"
    print(f"Fetching: {url}")

    session = requests.Session()
    response = session.get(url, headers=HEADERS, timeout=10)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

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

    # ── Food name ─────────────────────────────────────────────────────────────
    # Title format: "Nutrition | Label - French Toast"
    title_tag = soup.find("title")
    if title_tag and "Label -" in title_tag.text:
        result["food_name"] = title_tag.text.split("Label -", 1)[1].strip()

    # ── Nutrition facts ───────────────────────────────────────────────────────
    full_text = soup.get_text(separator="\n")
    lines = [line.strip() for line in full_text.splitlines() if line.strip()]

    for i, line in enumerate(lines):
        line_lower = line.lower()

        # "Serving size" appears alone; the value is the very next line ("1 ea")
        if line_lower == "serving size" and result["serving_size"] is None:
            if i + 1 < len(lines):
                result["serving_size"] = lines[i + 1]

        # "Calories per serving" appears alone; the value is the next line ("251")
        if line_lower == "calories per serving" and result["calories"] is None:
            if i + 1 < len(lines) and re.match(r"^\d+$", lines[i + 1]):
                result["calories"] = int(lines[i + 1])

        # "Total Fat" and "Total Carbohydrate." have the value on the next line ("10.5g")
        if "total fat" in line_lower and result["fat_g"] is None:
            match = re.search(r"(\d+\.?\d*)\s*g", line)
            if match:
                result["fat_g"] = float(match.group(1))
            elif i + 1 < len(lines):
                match = re.search(r"(\d+\.?\d*)\s*g", lines[i + 1])
                if match:
                    result["fat_g"] = float(match.group(1))

        if "total carbohydrate" in line_lower and result["carbs_g"] is None:
            match = re.search(r"(\d+\.?\d*)\s*g", line)
            if match:
                result["carbs_g"] = float(match.group(1))
            elif i + 1 < len(lines):
                match = re.search(r"(\d+\.?\d*)\s*g", lines[i + 1])
                if match:
                    result["carbs_g"] = float(match.group(1))

        # "Protein" alone, value on next line ("10.9g")
        if line_lower == "protein" and result["protein_g"] is None:
            if i + 1 < len(lines):
                match = re.search(r"(\d+\.?\d*)\s*g", lines[i + 1])
                if match:
                    result["protein_g"] = float(match.group(1))

    # ── Allergens ─────────────────────────────────────────────────────────────
    # Allergens are plain text after an "ALLERGENS:" line, comma-separated.
    for i, line in enumerate(lines):
        if line.strip().upper() == "ALLERGENS:" and i + 1 < len(lines):
            raw = lines[i + 1]
            # Skip if next line is the disclaimer (very long) rather than allergen names
            if len(raw) < 200:
                result["allergens"] = [a.strip().lower() for a in raw.split(",") if a.strip()]
            break

    return result


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 50)
    print(f"Testing scrape for: {TEST_ITEM['name']}")
    print("=" * 50)

    try:
        data = scrape_label(TEST_ITEM["rec_num"])

        print("\n[OK] Scrape successful!\n")
        print(json.dumps(data, indent=2))

        print("\n-- Validation --")
        for field in ["food_name", "calories", "fat_g", "carbs_g"]:
            status = "OK" if data[field] is not None else "MISSING"
            print(f"  {field}: {data[field]}  [{status}]")

    except requests.exceptions.ConnectionError:
        print("[ERROR] Could not connect -- check your internet connection.")
    except requests.exceptions.HTTPError as e:
        print(f"[ERROR] HTTP error: {e}")
    except Exception as e:
        print(f"[ERROR] Unexpected error: {e}")
        raise
