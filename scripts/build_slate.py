#!/usr/bin/env python3
"""
MLB Hit Prop Enrichment Pipeline -- build_slate.py
Produces data/latest-enriched-slate.json matching the V7.1 dashboard schema.

SECRETS (via GitHub Secrets / env vars):
  ODDS_API_KEY         -- The Odds API key for batter_hits player props
  PERPLEXITY_API_KEY   -- Perplexity API key (optional, for lineup parsing)

DATA SOURCES (V1 -- manual CSV inputs):
  data/candidates.csv  -- hitter_name,team,opp,pitcher_name,...
  data/lineups.csv     -- team,slot,hitter_name
  data/weather.csv     -- home_team,away_team,condition,tempF,wind
  data/odds.csv        -- hitter_name,team,overOdds,underOdds,booksAgreeing,sourceBook
  (odds pulled live from The Odds API if ODDS_API_KEY is set)
"""

import os
import json
import csv
import re
import logging
import requests
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data"
OUTPUT_FILE = DATA_DIR / "latest-enriched-slate.json"

ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
ODDS_API_BASE = "https://api.the-odds-api.com/v4"
PREFERRED_BOOK = "fanduel"


# ──────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────

def normalize(name: str) -> str:
    """Lowercase + remove non-alphanumeric for fuzzy matching."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _parse_int(val, default=None):
    try:
        return int(str(val).strip())
    except (ValueError, TypeError):
        return default


def _parse_float(val, default=None):
    try:
        return float(str(val).strip())
    except (ValueError, TypeError):
        return default


def _parse_odds(val):
    """Parse American odds string like '-230' or '+180' to int."""
    try:
        s = str(val).strip().replace("+", "")
        return int(s)
    except (ValueError, TypeError):
        return None


# ──────────────────────────────────────────────
# SECTION 1: DATA FETCHERS
# ──────────────────────────────────────────────

def fetch_candidate_rows() -> list:
    """Returns list of dicts from candidates.csv."""
    path = DATA_DIR / "candidates.csv"
    if not path.exists():
        log.warning("candidates.csv not found -- returning empty list")
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return [row for row in csv.DictReader(f) if any(row.values())]


def fetch_confirmed_lineups() -> list:
    """Returns list of {team, slot, hitter_name} from lineups.csv."""
    path = DATA_DIR / "lineups.csv"
    if not path.exists():
        log.warning("lineups.csv not found -- all players will be unconfirmed")
        return []
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                slot = int(row.get("slot", ""))
                if 1 <= slot <= 9:
                    rows.append({**row, "slot": slot})
            except (ValueError, TypeError):
                log.warning(
                    f"Lineups: invalid slot '{row.get('slot')}' for "
                    f"{row.get('hitter_name')} -- skipping"
                )
    return rows


def fetch_weather() -> list:
    """Returns list of weather rows from weather.csv."""
    path = DATA_DIR / "weather.csv"
    if not path.exists():
        log.warning("weather.csv not found -- context will be empty")
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return [row for row in csv.DictReader(f) if any(row.values())]


def fetch_batter_hits_odds() -> list:
    """
    Returns list of odds dicts.
    Uses The Odds API if ODDS_API_KEY is set, else reads odds.csv.
    Only accepts marketKey=batter_hits, line=0.5.
    """
    if ODDS_API_KEY:
        return _fetch_odds_api()
    path = DATA_DIR / "odds.csv"
    if not path.exists():
        log.warning("odds.csv not found and no ODDS_API_KEY -- market will be empty")
        return []
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                over = _parse_odds(row.get("overOdds", ""))
                under = _parse_odds(row.get("underOdds", ""))
                if over is None or under is None:
                    continue
                rows.append({
                    "player": row.get("hitter_name", "").strip(),
                    "team": row.get("team", "").strip().upper(),
                    "overOdds": over,
                    "underOdds": under,
                    "booksAgreeing": _parse_int(row.get("booksAgreeing", "1")) or 1,
                    "book": row.get("sourceBook", "manual").strip(),
                })
            except Exception as e:
                log.warning(f"Odds CSV row error: {e}")
    return rows


def _fetch_odds_api() -> list:
    """Pull batter_hits 0.5 odds from The Odds API."""
    try:
        resp = requests.get(
            f"{ODDS_API_BASE}/sports/baseball_mlb/events",
            params={"apiKey": ODDS_API_KEY, "dateFormat": "iso"},
            timeout=15,
        )
        resp.raise_for_status()
        events = resp.json()
        log.info(f"Odds API: {len(events)} MLB events found")
    except Exception as e:
        log.error(f"Odds API events fetch failed: {e}")
        return []

    results = []
    for event in events[:10]:
        event_id = event.get("id")
        try:
            resp = requests.get(
                f"{ODDS_API_BASE}/sports/baseball_mlb/events/{event_id}/odds",
                params={
                    "apiKey": ODDS_API_KEY,
                    "regions": "us",
                    "markets": "batter_hits",
                    "oddsFormat": "american",
                    "dateFormat": "iso",
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.warning(f"Odds API: failed for event {event_id}: {e}")
            continue

        for book in data.get("bookmakers", []):
            for mkt in book.get("markets", []):
                if mkt.get("key") != "batter_hits":
                    continue
                player_map = {}
                for outcome in mkt.get("outcomes", []):
                    if outcome.get("point") != 0.5:
                        continue
                    desc = outcome.get("description", "")
                    side = outcome.get("name", "").lower()
                    if side not in ("over", "under"):
                        continue
                    if desc not in player_map:
                        player_map[desc] = {"over": None, "under": None}
                    player_map[desc][side] = outcome.get("price")

                book_name = book.get("key", "unknown")
                for player_name, sides in player_map.items():
                    if sides["over"] is None or sides["under"] is None:
                        continue
                    key = normalize(player_name)
                    existing = next(
                        (r for r in results if normalize(r["player"]) == key), None
                    )
                    if existing:
                        existing["booksAgreeing"] += 1
                        if book_name == PREFERRED_BOOK:
                            existing["overOdds"] = sides["over"]
                            existing["underOdds"] = sides["under"]
                            existing["book"] = PREFERRED_BOOK
                    else:
                        results.append({
                            "player": player_name,
                            "team": "",
                            "overOdds": sides["over"],
                            "underOdds": sides["under"],
                            "booksAgreeing": 1,
                            "book": book_name,
                        })
    return results


# ──────────────────────────────────────────────
# SECTION 2: WEATHER PARSER
# ──────────────────────────────────────────────

def parse_weather_context(row: dict) -> dict:
    """
    Convert a weather CSV row to {tempF, windOut, windIn, humidity, dome}.
    Rules:
    - 'dome' or 'indoors' in condition -> dome=True, no wind
    - wind containing 'out' -> windOut=True
    - wind containing 'in' -> windIn=True
    - crosswind (L-R, R-L) -> both False
    - N/A wind -> both False
    """
    condition = (row.get("condition") or "").lower()
    temp_raw = row.get("tempF") or row.get("temp") or ""
    wind_raw = (row.get("wind") or "").lower()

    is_dome = "dome" in condition or "indoor" in condition

    if is_dome:
        return {
            "tempF": None,
            "windOut": False,
            "windIn": False,
            "humidity": False,
            "dome": True,
        }

    temp_f = _parse_float(temp_raw)

    wind_out = False
    wind_in = False
    if "n/a" not in wind_raw and wind_raw.strip():
        if "out" in wind_raw:
            wind_out = True
        elif "in" in wind_raw:
            wind_in = True
        # crosswind: neither

    humidity = "humid" in condition or "fog" in condition

    return {
        "tempF": temp_f,
        "windOut": wind_out,
        "windIn": wind_in,
        "humidity": humidity,
        "dome": False,
    }


# ──────────────────────────────────────────────
# SECTION 3: ENRICHMENT ENGINE
# ──────────────────────────────────────────────

def build_enriched_slate() -> list:
    """
    Main enrichment loop:
    1. Load all data sources
    2. For each candidate, match lineups, weather, odds
    3. Build V7.1 JSON object
    4. Return the slate array
    """
    source_ts = datetime.now(timezone.utc).isoformat()

    candidates = fetch_candidate_rows()
    if not candidates:
        log.info("No candidates found -- slate will be empty")
        return []

    lineups = fetch_confirmed_lineups()
    weather_rows = fetch_weather()
    odds_rows = fetch_batter_hits_odds()

    # Index for fast lookup
    lineup_index = {}
    for lr in lineups:
        key = (lr.get("team", "").upper(), normalize(lr.get("hitter_name", "")))
        lineup_index[key] = lr

    weather_index = {}
    for wr in weather_rows:
        home = (wr.get("home_team") or "").upper()
        away = (wr.get("away_team") or "").upper()
        ctx = parse_weather_context(wr)
        if home:
            weather_index[home] = ctx
        if away:
            weather_index[away] = ctx

    odds_index = {}
    for od in odds_rows:
        key = (od.get("team", "").upper(), normalize(od.get("player", "")))
        odds_index[key] = od
        # Also index by player name alone (for cross-team matching)
        odds_index[("", normalize(od.get("player", "")))] = od

    slate = []
    for c in candidates:
        hitter_name = (c.get("hitter_name") or "").strip()
        team = (c.get("team") or "").strip().upper()
        opp = (c.get("opp") or "").strip().upper()
        pitcher_name = (c.get("pitcher_name") or "").strip()

        if not hitter_name or not team:
            log.warning(f"Skipping row with missing hitter/team: {c}")
            continue

        # --- Lineup slot ---
        lu_key = (team, normalize(hitter_name))
        lu_row = lineup_index.get(lu_key)
        slot = None
        confirmed = False
        if lu_row:
            slot = lu_row.get("slot")
            confirmed = True

        # --- Weather context ---
        ctx = weather_index.get(team) or weather_index.get(opp) or {
            "tempF": None,
            "windOut": False,
            "windIn": False,
            "humidity": False,
            "dome": False,
        }

        # --- Odds ---
        odds_row = odds_index.get((team, normalize(hitter_name))) or                    odds_index.get(("", normalize(hitter_name)))
        market = {}
        if odds_row:
            market = {
                "overOdds": odds_row.get("overOdds"),
                "underOdds": odds_row.get("underOdds"),
                "booksAgreeing": odds_row.get("booksAgreeing", 1),
                "marketKey": "batter_hits",
                "line": 0.5,
                "sourceBook": odds_row.get("book", "unknown"),
            }

        # --- Hitter stats from CSV (optional columns) ---
        def _f(key):
            return _parse_float(c.get(key))

        hitter_obj = {
            "name": hitter_name,
            "team": team,
            "opp": opp,
            "slot": slot,
            "confirmed": confirmed,
        }
        stat_map = {
            "seasonBA": "seasonBA", "seasonwOBA": "seasonwOBA",
            "xBA": "xBA", "xwOBA": "xwOBA",
            "contact": "contact", "k": "k",
            "l30PA": "l30PA", "l7BA": "l7BA", "l14BA": "l14BA",
            "l21BA": "l21BA", "l7wOBA": "l7wOBA", "l14xwOBA": "l14xwOBA",
            "l14Contact": "l14Contact", "l30xBA": "l30xBA",
        }
        for csv_col, json_key in stat_map.items():
            val = _f(csv_col)
            if val is not None:
                hitter_obj[json_key] = val

        # --- Pitcher stats from CSV (optional columns) ---
        pitcher_obj = {"name": pitcher_name}
        pitcher_stat_map = {
            "xERA": "xERA", "stuffPlus": "stuffPlus",
            "csw": "csw", "bb9": "bb9", "k9": "k9",
        }
        for csv_col, json_key in pitcher_stat_map.items():
            val = _f(csv_col)
            if val is not None:
                pitcher_obj[json_key] = val

        # --- Build ID ---
        slot_str = str(slot) if slot is not None else "x"
        team_lower = team.lower()
        hitter_slug = re.sub(r"[^a-z0-9]", "_", hitter_name.lower())
        record_id = f"{hitter_slug}_{team_lower}_{opp.lower()}_{slot_str}"

        record = {
            "id": record_id,
            "hitter": hitter_obj,
            "pitcher": pitcher_obj,
            "context": ctx,
        }
        if market:
            record["market"] = market

        slate.append(record)

    log.info(f"Built {len(slate)} enriched records")
    return slate


# ──────────────────────────────────────────────
# SECTION 4: OUTPUT
# ──────────────────────────────────────────────

def write_output(slate: list):
    """Write the enriched slate JSON to data/latest-enriched-slate.json."""
    enrichment_ts = datetime.now(timezone.utc).isoformat()
    payload = {
        "_meta": {
            "source_ts": enrichment_ts,
            "enrichment_ts": enrichment_ts,
            "row_count": len(slate),
            "schema": "V7.1",
            "note": "Auto-generated by GitHub Action. Do not edit manually.",
        },
        "slate": slate,
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    log.info(f"Wrote {len(slate)} records to {OUTPUT_FILE}")


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────

if __name__ == "__main__":
    log.info("=== MLB Hit Prop Enrichment Pipeline starting ===")
    slate = build_enriched_slate()
    write_output(slate)
    log.info("=== Pipeline complete ===")
