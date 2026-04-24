"""
api/main.py — FastAPI backend for UMD Dining Nutrition App.

Run with:
    uvicorn api.main:app --reload

Endpoints:
    GET  /menu                          → foods for a hall/date/meal
    POST /analyze                       → totals, violations, swap suggestions
    POST /meal-plan                     → save a meal plan to the DB
"""

import json
import datetime
import sys
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

sys.path.append(str(Path(__file__).parent.parent))
from db.db import init_db, get_connection, save_meal_plan

app = FastAPI(title="UMD Dining Nutrition API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],  # Next.js dev server
    allow_methods=["*"],
    allow_headers=["*"],
)

init_db()


# ── Models ────────────────────────────────────────────────────────────────────

class AnalyzeRequest(BaseModel):
    hall_id: int
    date: str                   # "M/D/YYYY"
    meal_type: str              # "breakfast" | "lunch" | "dinner"
    selected_rec_nums: list[str]
    calorie_goal: float
    fat_goal: float
    carb_goal: float
    allergies: list[str] = []   # e.g. ["dairy", "gluten"]


class SavePlanRequest(BaseModel):
    user_id: int
    hall_id: int
    date: str
    meal_type: str
    food_ids: list[int]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_date(date_str: str) -> datetime.date:
    try:
        return datetime.datetime.strptime(date_str, "%m/%d/%Y").date()
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid date format: {date_str}. Use M/D/YYYY.")


def _day_name(d: datetime.date) -> str:
    return d.strftime("%A").lower()


def _allergens_list(raw: str) -> list[str]:
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return [a.strip().lower() for a in raw.split(",") if a.strip()]


def _check_violations(totals: dict, goals: dict) -> list[str]:
    violations = []
    if totals["calories"] > goals["calorie_goal"]:
        violations.append("calories")
    if totals["fat_g"] > goals["fat_goal"]:
        violations.append("fat")
    if totals["carbs_g"] > goals["carb_goal"]:
        violations.append("carbs")
    return violations


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/menu")
def get_menu(
    hall_id: int = Query(...),
    date: str = Query(..., description="M/D/YYYY"),
    meal_type: str = Query(...),
):
    """Return all foods for a given hall, date, and meal."""
    d = _parse_date(date)
    day = _day_name(d)

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT food_id, food_name, rec_num, calories, fat_g, carbs_g,
                   protein_g, serving_size, allergens
            FROM foods
            WHERE hall_id = ? AND day_of_week = ? AND meal_type = ?
            """,
            (hall_id, day, meal_type.lower()),
        ).fetchall()

    return [
        {
            "food_id": r["food_id"],
            "food_name": r["food_name"],
            "rec_num": r["rec_num"],
            "calories": r["calories"],
            "fat_g": r["fat_g"],
            "carbs_g": r["carbs_g"],
            "protein_g": r["protein_g"],
            "serving_size": r["serving_size"],
            "allergens": _allergens_list(r["allergens"]),
        }
        for r in rows
    ]


@app.post("/analyze")
def analyze_meal(req: AnalyzeRequest):
    """
    Given selected foods + nutrition goals + allergies, return:
    - running totals
    - which goals are violated and which foods pushed them over
    - swap suggestions for each violating food
    """
    d = _parse_date(req.date)
    day = _day_name(d)

    # Fetch selected foods
    if not req.selected_rec_nums:
        raise HTTPException(status_code=400, detail="No foods selected.")

    placeholders = ",".join("?" * len(req.selected_rec_nums))
    with get_connection() as conn:
        selected = conn.execute(
            f"""
            SELECT food_id, food_name, rec_num, calories, fat_g, carbs_g,
                   protein_g, serving_size, allergens
            FROM foods WHERE rec_num IN ({placeholders})
            """,
            req.selected_rec_nums,
        ).fetchall()

    selected = [dict(r) for r in selected]
    for food in selected:
        food["allergens"] = _allergens_list(food["allergens"])

    # Running totals
    totals = {
        "calories": sum(f["calories"] for f in selected),
        "fat_g":    sum(f["fat_g"]    for f in selected),
        "carbs_g":  sum(f["carbs_g"]  for f in selected),
    }
    goals = {
        "calorie_goal": req.calorie_goal,
        "fat_goal":     req.fat_goal,
        "carb_goal":    req.carb_goal,
    }
    violations = _check_violations(totals, goals)

    if not violations:
        return {"totals": totals, "violations": [], "flagged_foods": [], "swaps": []}

    # Find which foods pushed totals over — work backwards through selected list
    flagged_food_ids = set()
    running = {"calories": 0.0, "fat_g": 0.0, "carbs_g": 0.0}
    for food in selected:
        was_ok = _check_violations(running, goals) == []
        running["calories"] += food["calories"]
        running["fat_g"]    += food["fat_g"]
        running["carbs_g"]  += food["carbs_g"]
        now_violated = _check_violations(running, goals)
        if was_ok and now_violated:
            flagged_food_ids.add(food["food_id"])
        elif not was_ok:
            # Already over — every subsequent food is also flagged
            flagged_food_ids.add(food["food_id"])

    flagged_foods = [f for f in selected if f["food_id"] in flagged_food_ids]

    # Fetch swap candidates: same hall, day, meal — not already selected — allergen-safe
    with get_connection() as conn:
        candidates = conn.execute(
            """
            SELECT food_id, food_name, rec_num, calories, fat_g, carbs_g,
                   protein_g, serving_size, allergens
            FROM foods
            WHERE hall_id = ? AND day_of_week = ? AND meal_type = ?
              AND rec_num NOT IN ({})
            """.format(placeholders),
            [req.hall_id, day, req.meal_type.lower()] + req.selected_rec_nums,
        ).fetchall()

    candidates = [dict(r) for r in candidates]
    for c in candidates:
        c["allergens"] = _allergens_list(c["allergens"])

    # Filter out allergen conflicts
    user_allergies = set(a.lower() for a in req.allergies)
    safe_candidates = [
        c for c in candidates
        if not user_allergies.intersection(set(c["allergens"]))
    ]

    # For each flagged food, find the best swap (one that reduces total violations most)
    swaps = []
    for flagged in flagged_foods:
        totals_without = {
            "calories": totals["calories"] - flagged["calories"],
            "fat_g":    totals["fat_g"]    - flagged["fat_g"],
            "carbs_g":  totals["carbs_g"]  - flagged["carbs_g"],
        }

        ranked = []
        for swap in safe_candidates:
            hypothetical = {
                "calories": totals_without["calories"] + swap["calories"],
                "fat_g":    totals_without["fat_g"]    + swap["fat_g"],
                "carbs_g":  totals_without["carbs_g"]  + swap["carbs_g"],
            }
            remaining_violations = len(_check_violations(hypothetical, goals))
            ranked.append((remaining_violations, swap))

        ranked.sort(key=lambda x: x[0])
        top = [s for _, s in ranked[:3]]

        swaps.append({
            "flagged_food": flagged,
            "suggestions": top if top else None,
            "no_swap_available": len(top) == 0,
        })

    return {
        "totals": totals,
        "goals": goals,
        "violations": violations,
        "flagged_foods": flagged_foods,
        "swaps": swaps,
    }


@app.post("/meal-plan")
def save_plan(req: SavePlanRequest):
    """Save a finalized meal plan to the database."""
    d = _parse_date(req.date)
    day = _day_name(d)
    save_meal_plan(req.user_id, req.food_ids, day, req.meal_type, req.date)
    return {"saved": True, "count": len(req.food_ids)}
