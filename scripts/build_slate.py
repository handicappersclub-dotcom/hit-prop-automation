#!/usr/bin/env python3
"""
MLB Hit Prop Enrichment Pipeline — build_slate.py
Produces data/latest-enriched-slate.json matching the V7.1 dashboard schema.

SECRETS (via GitHub Secrets / env vars):
  ODDS_API_KEY       — The Odds API key for batter_hits player props
    PERPLEXITY_API_KEY — Perplexity API key (optional, for lineup parsing)

    DATA SOURCES (V1 — manual CSV inputs, swap-in-place for live APIs):
      data/candidates.csv  — hitter,team,opp,pitcher
        data/lineups.csv     — team,slot,hitter
          data/weather.csv     — team,opp,conditions,tempF,wind
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

DATA_DIR   = Path(__file__).parent.parent / "data"
OUTPUT_FILE = DATA_DIR / "latest-enriched-slate.json"

ODDS_API_KEY       = os.environ.get("ODDS_API_KEY", "")
ODDS_API_BASE      = "https://api.the-odds-api.com/v4"
PREFERRED_BOOK     = "fanduel"

# ──────────────────────────────────────────────
# SECTION 1: DATA FETCHERS
# ──────────────────────────────────────────────

def fetch_candidate_rows() -> list[dict]:
      """
          Returns list of {hitter, team, opp, pitcher}.
              V1: reads data/candidates.csv
                  Future: swap in MLB Stats API or Google Sheets API call.
                      """
      path = DATA_DIR / "candidates.csv"
      if not path.exists():
                log.warning("candidates.csv not found — returning empty list")
                return []
            with open(path, newline="", encoding="utf-8") as f:
                      return [row for row in csv.DictReader(f) if any(row.values())]


def fetch_confirmed_lineups() -> list[dict]:
      """
          Returns list of {team, slot, hitter}.
              Only include rows where slot is 1–9 and source confirmed it.
                  V1: reads data/lineups.csv
                      Future: swap in MLB Stats API /lineup endpoint or Perplexity parser.
                          """
    path = DATA_DIR / "lineups.csv"
    if not path.exists():
              log.warning("lineups.csv not found — all players will be unconfirmed")
              return []
          rows = []
    with open(path, newline="", encoding="utf-8") as f:
              for row in csv.DictReader(f):
                            try:
                                              slot = int(row.get("slot", ""))
                                              if 1 <= slot <= 9:
                                                                    rows.append({**row, "slot": slot})
                            except (ValueError, TypeError):
                                              log.warning(f"Lineups: invalid slot '{row.get('slot')}' for {row.get('hitter')} — skipping")
                                  return rows


def fetch_weather() -> list[dict]:
      """
          Returns list of {team, opp, conditions, tempF, wind}.
              V1: reads data/weather.csv
                  Future: swap in Open-Meteo API call using stadium coordinates.
                      """
    path = DATA_DIR / "weather.csv"
    if not path.exists():
              log.warning("weather.csv not found — context will be empty")
              return []
          with open(path, newline="", encoding="utf-8") as f:
                    return [row for row in csv.DictReader(f) if any(row.values())]


def fetch_batter_hits_odds() -> list[dict]:
      """
          Returns list of {player, team, overOdds, underOdds, booksAgreeing, book}.
              Rules:
                    - marketKey must be batter_hits
                          - line must be 0.5
                                - side over/under only
                                      - no total bases, RBI, runs, SGP, parlay-leg, alternate hits
                                          V1: uses The Odds API if ODDS_API_KEY is set; else reads data/odds.csv
                                              """
      if ODDS_API_KEY:
                return _fetch_odds_api()
            path = DATA_DIR / "odds.csv"
    if not path.exists():
              log.warning("odds.csv not found and no ODDS_API_KEY — market will be empty")
              return []
          rows = []
    with open(path, newline="", encoding="utf-8") as f:
              for row in csv.DictReader(f):
                            try:
                                              over  = _parse_odds(row.get("overOdds",  ""))
                                              under = _parse_odds(row.get("underOdds", ""))
                                              if over is None or under is None:
                                                                    log.warning(f"Odds CSV: incomplete odds for {row.get('player')} — skipping")
                                                                    continue
                                                                rows.append({
                                                  "player":        row.get("player", "").strip(),
                                                  "team":          row.get("team",   "").strip().upper(),
                                                  "overOdds":      over,
                                                  "underOdds":     under,
                                                  "booksAgreeing": _parse_int(row.get("booksAgreeing", "1")) or 1,
                                                  "book":          row.get("book", "manual").strip(),
                                              })
except Exception as e:
                log.warning(f"Odds CSV row error: {e}")
    return rows


def _fetch_odds_api() -> list[dict]:
      """Pull batter_hits 0.5 odds from The Odds API."""
    try:
              # Get upcoming MLB events
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
    for event in events[:10]:  # limit to avoid API quota burn
              event_id = event.get("id")
              try:
                            resp = requests.get(
                                              f"{ODDS_API_BASE}/sports/baseball_mlb/events/{event_id}/odds",
                                              params={
                                                                    "apiKey":   ODDS_API_KEY,
                                                                    "regions":  "us",
                                                                    "markets":  "batter_hits",
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
                                                          # Group outcomes by description (player name)
                                                          player_map = {}
                                        for outcome in mkt.get("outcomes", []):
                                                              pt = outcome.get("point")
                                                              if pt != 0.5:
                                                                                        continue  # only 0.5 line
                    desc = outcome.get("description", "")
                    side = outcome.get("name", "").lower()
                    if side not in ("over", "under"):
                                              continue
                                          if desc not in player_map:
                                                                    player_map[desc] = {}
                                                                player_map[desc][side] = outcome.get("price")

                for player_name, sides in player_map.items():
                                      if "over" not in sides or "under" not in sides:
                                                                continue
                                                            # Determine team from event home/away
                                                            home_team = event.get("home_team", "")
                    away_team = event.get("away_team", "")
                    team_abbr = _guess_team_abbr(player_name, home_team, away_team)

                    key = _norm(player_name) + "|" + team_abbr
                    if key not in {_norm(r["player"]) + "|" + r["team"] for r in results}:
                                              # Count books agreeing
                                              books_count = sum(
                                                                            1 for b in data.get("bookmakers", [])
                                                                            for m in b.get("markets", [])
                                                                            if m.get("key") == "batter_hits"
                                                                            for o in m.get("outcomes", [])
                                                                            if o.get("point") == 0.5 and _norm(o.get("description","")) == _norm(player_name)
                                              ) // 2  # divide by 2 because each player has over+under
                        results.append({
                                                      "player":        player_name,
                                                      "team":          team_abbr,
                                                      "overOdds":      sides["over"],
                                                      "underOdds":     sides["under"],
                                                      "booksAgreeing": max(books_count, 1),
                                                      "book":          book.get("title", "unknown"),
                        })

    log.info(f"Odds API: {len(results)} batter_hits 0.5 lines collected")
    return results


# ──────────────────────────────────────────────
# SECTION 2: ENRICHMENT ENGINE
# ──────────────────────────────────────────────

def build_enriched_slate(
      candidates: list[dict],
      lineups:    list[dict],
      weather:    list[dict],
      odds:       list[dict],
) -> list[dict]:
      """
          Merges the four data sources into the V7.1 dashboard JSON schema.
              Never guesses missing slots or odds — leaves them null/empty.
                  """
    # Build lookup maps
    lineup_map = {}
    for r in lineups:
              key = _norm(r.get("hitter", "")) + "|" + str(r.get("team", "")).upper()
        lineup_map[key] = int(r["slot"])

    weather_map = {}
    for r in weather:
              team = str(r.get("team", "")).upper()
        opp  = str(r.get("opp",  "")).upper()
        game_key = "-".join(sorted([team, opp]))
        cond = str(r.get("conditions", "")).lower()
        wind = str(r.get("wind", "")).lower()
        is_dome = "dome" in cond or "indoor" in cond or wind == "n/a"
        temp_raw = r.get("tempF", "")
        weather_map[game_key] = {
                      "tempF":    None if is_dome else _parse_float(temp_raw),
                      "windOut":  not is_dome and "out" in wind,
                      "windIn":   not is_dome and "in"  in wind,
                      "humidity": False,
                      "dome":     is_dome,
        }

    odds_map = {}
    for r in odds:
              key = _norm(r.get("player", "")) + "|" + str(r.get("team", "")).upper()
        odds_map[key] = {
                      "overOdds":      r["overOdds"],
                      "underOdds":     r["underOdds"],
                      "booksAgreeing": r["booksAgreeing"],
                      "marketKey":     "batter_hits",
                      "line":          0.5,
                      "sourceBook":    r.get("book", "manual"),
        }

    enriched = []
    for r in candidates:
              hitter = str(r.get("hitter", "")).strip()
        team   = str(r.get("team",   "")).upper().strip()
        opp    = str(r.get("opp",    "")).upper().strip()
        pitcher = str(r.get("pitcher","")).strip()

        lookup_key = _norm(hitter) + "|" + team
        game_key   = "-".join(sorted([team, opp]))

        slot      = lineup_map.get(lookup_key)
        confirmed = slot is not None
        mkt       = odds_map.get(lookup_key)
        wx        = weather_map.get(game_key)

        if not confirmed:
                      log.info(f"No lineup match: {hitter} ({team}) — slot=null")
        if mkt is None:
                      log.info(f"No odds found: {hitter} ({team})")
        if wx is None:
                      log.info(f"No weather found: {team} vs {opp}")

        row_id = re.sub(r"\s+", "_", _norm(hitter)) + "_" + team + "_" + opp

        enriched.append({
                      "id":      row_id,
                      "hitter":  {
                                        "name":      hitter  or None,
                                        "team":      team    or None,
                                        "opp":       opp     or None,
                                        "slot":      slot,
                                        "confirmed": confirmed,
                      },
                      "pitcher": {"name": pitcher or None},
                      "context": wx  or {},
                      "market":  mkt or {},
        })

    return enriched


# ──────────────────────────────────────────────
# SECTION 3: UTILITIES
# ──────────────────────────────────────────────

def _norm(s: str) -> str:
      return re.sub(r"[^a-z0-9 ]", "", str(s or "").lower()).strip()

def _parse_odds(v) -> int | None:
      if v is None or str(v).strip() == "":
                return None
    try:
              return int(float(re.sub(r"[^-0-9.]", "", str(v))))
except (ValueError, TypeError):
        return None

def _parse_float(v) -> float | None:
      if v is None or str(v).strip() == "":
                return None
    try:
              return float(v)
except (ValueError, TypeError):
        return None

def _parse_int(v) -> int | None:
      if v is None or str(v).strip() == "":
                return None
    try:
              return int(float(v))
except (ValueError, TypeError):
        return None

def _guess_team_abbr(player_name: str, home_team: str, away_team: str) -> str:
      """Placeholder — in a real impl, look up the player's team from a roster map."""
    return "MLB"  # Will be replaced when roster API integration is added


# ──────────────────────────────────────────────
# SECTION 4: MAIN
# ──────────────────────────────────────────────

def main():
      source_ts     = datetime.now(timezone.utc).isoformat()
    enrichment_ts = source_ts  # same pass for now

    log.info("=== MLB Hit Prop Enrichment Pipeline ===")
    log.info(f"Source timestamp:     {source_ts}")

    candidates = fetch_candidate_rows()
    lineups    = fetch_confirmed_lineups()
    weather    = fetch_weather()
    odds       = fetch_batter_hits_odds()

    log.info(f"Candidates: {len(candidates)}, Lineups: {len(lineups)}, "
                          f"Weather: {len(weather)}, Odds: {len(odds)}")

    enriched = build_enriched_slate(candidates, lineups, weather, odds)

    output = {
              "_meta": {
                            "source_ts":     source_ts,
                            "enrichment_ts": enrichment_ts,
                            "row_count":     len(enriched),
                            "schema":        "V7.1",
              },
              "slate": enriched,
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
              json.dump(output, f, indent=2)

    log.info(f"Wrote {len(enriched)} enriched rows → {OUTPUT_FILE}")


if __name__ == "__main__":
      main()
