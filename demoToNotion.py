#!/usr/bin/env python3
"""
Point this at a CS2 demo (.dem) and it will parse it, compute stats, and
create respective pages in Notion.

Usage:
    python demoToNotion.py \
        --demo match.dem \
        --gegner "Example Team" \
        --liga "DACH S6 Spk5 - Phase 1" \
        --typ Official \
        --dry-run

    (drop --dry-run to actually write to Notion)

Requirements:
    pip install demoparser2 requests
    NOTION_TOKEN="secret_xxx"   # Internal Integration Token set as env var
    Optional: Go, for demos demoparser2 cannot read (see GoDemoParser)
    The integration needs access to Matches/Maps/Performance/Gegner/Spieler
    (Notion: "..." menu on each DB -> Connections -> add your integration)

The script writes these properties, matched by exact name:

  Performance   Name, Spieler, Match (mandatory), then Rating, Swing, KAST,
                ADR, Kills/R, First Kills/R, First Death/R,
                Untradable Deaths/R, Utility Damage/R, Last Man Standing/R,
                Enemies Flashed/R, Flash Leading to Kills/R,
                1v1..1v5 WR/SR%, Wasted Utility/R ($) and Date.
                "Map" is a rollup off Match and is never written.

  Maps          Titel, Match-Room / Demo-Link (mandatory), then Map, Typ,
                Ergebnis, Liga/Turnier, Datum, Gegner, VOD-Link and the team
                rates CT%, T%, Pistol WR, Eco WR, Half-Buy WR, Buy WR,
                5v4 WR, 4v5 WR, Conv WR, KAST.

  Gegner        Name -- only when the opponent is new; see below.

  Matches       Name, Gegner, Datum, one of 1. Map / 2. Map / Decider, and
                Resultat. The n. Ergebnis columns are rollups off those
                relations, so Notion fills them in by itself.

Nothing has to exist before the first run, everything is created. 
Use "--check" reads all three schemas and reports exactly what is missing or wrongly typed. 
Anything missing is left out of the write; only the titles and relations are mandatory.

DATABASE IDS
    NOTION_DATABASES holds DATABASE ids -- the id of the database itself.
    These are NOT the data-source / collection ids that appear in a
    "collection://..." URI, which I learned the hard way.
    There is a preflight that looks the database up by title: if it finds one, 
    it reports the correct id and uses it for that run; if it finds nothing, 
    the integration genuinely has no access.

    Override any id without editing this file:
        NOTION_DB_MAPS, NOTION_DB_PERFORMANCE, NOTION_DB_SPIELER,
        NOTION_DB_GEGNER
    A full Notion URL, a dashed uuid or a bare 32-hex id are all accepted.

OPPONENTS AND SERIES
    The Opponents relation on the Maps row is filled in from --gegner: the
    opponent is looked up in the Gegner database by title, case-insensitively
    ("EXAMPLE TEAM" matches "Example Team"), and created if they are new.
    --gegner-page skips the lookup and uses the page name.

    The map is then put into a Match. A Match for the same opponent on the
    same date is reused, and the map goes into the first free slot --
    1. Map, then 2. Map, then Decider. A map that is already linked keeps the
    slot it has, so re-importing never shuffles a series around, and a series
    that already holds three maps is left alone rather than overwritten.
    ==> This means one should run this script on the maps IN THE ORDER THEY WERE PLAYED.
    thx :)

WHERE THE NUMBERS COME FROM
    Kills, deaths, assists, headshots, total damage, utility damage and
    enemies flashed are read from CS2's own per-player match counters
    (m_iKills / m_iDamage / m_iUtilityDamage / m_iEnemiesFlashed ...), i.e.
    exactly the numbers the in-game scoreboard shows. That means:
      * team kills and suicides do NOT count as kills,
      * team damage, self damage and overkill damage do NOT count towards ADR,
      * warmup and the knife round are already excluded by the game.
    The round count comes from the team score (CCSTeam.m_iScore), so the
    knife round and the synthetic warmup round_end are not counted.

    Everything that has no scoreboard counter -- opening duels, untradable
    deaths, KAST, clutches, flash-assists -- is derived from the event
    timeline, restricted to the rounds of the real match.

LIMITATIONS AND NOTES:
    1. "Rating" is a community-reverse-engineered approximation of HLTV
       Rating 2.0. HLTV never officially published the exact formula, so
       this is an approximation, not the exact HLTV value. Unlucky...

    2. Player matching runs on SteamID64 (stable, unlike in-game names),
       against the Spieler database. These need to always be strings, because
       the number is so large that floats lose precision. (Notion "weakness")

    3. The round count is read from the demo itself (team score), so MR12
       and Overtimes need no configuration and overtime are handled automatically.

    4. "Untradable death", "last man standing" and "1vX clutch" all use a
       5-second trade/flash window (FIVE_SEC_WINDOW below).

    5. A clutch that ends in a save (the clutcher survives but the team
       loses the round) counts as a won clutch.

    6. "Swing" is round swing: how much a player moved their team chance of
       winning the round, averaged over the map. Neither HLTV nor Leetify nor 
       anyone else publish their win-probability tables, so the model here (the SWING_*
       constants) is a logistic fit calibrated against HLTV demos. It reproduces these
       pretty well to around 95% accuracy. Hence I deem it good enough.
"""

import argparse, bisect, hashlib, json, math, os, shutil, subprocess, sys, time, requests
from datetime import date
from collections import defaultdict
from dotenv import load_dotenv
import pandas as pd
from demoparser2 import DemoParser

load_dotenv()

# --------------------------------------------------------------------------
# Notion configuration
# --------------------------------------------------------------------------

NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_VERSION = "2022-06-28"
NOTION_API = "https://api.notion.com/v1"

def normalize_notion_id(raw):
    """
    Accepts a bare id, a dashed uuid, or any Notion URL and returns a dashed
    uuid. Returns "" if there is no 32-hex-digit id in there at all.
    """
    text = str(raw or "").strip().lower().split("?")[0].rstrip("/")
    text = text.rsplit("/", 1)[-1]
    digits = "".join(ch for ch in text if ch in "0123456789abcdef")
    if len(digits) < 32:
        return ""
    h = digits[-32:]
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


# These are DATABASE ids -- the id of the database itself, which is what the
# REST API wants. They are NOT the data-source / collection ids that appear
# in a "collection://..." URI or that some tools copy...
NOTION_DATABASES = {
    "Maps": os.getenv("NOTION_DB_MAPS"),
    "Performance": os.getenv("NOTION_DB_PERFORMANCE"),
    "Spieler": os.getenv("NOTION_DB_SPIELER"),
    "Gegner": os.getenv("NOTION_DB_GEGNER"),
    "Matches": os.getenv("NOTION_DB_MATCHES"),
}
NOTION_DATABASES = {k: normalize_notion_id(v) for k, v in NOTION_DATABASES.items()}


def db(label):
    return NOTION_DATABASES[label]

# --------------------------------------------------------------------------
# Tunable constants for the new event-window metrics
# --------------------------------------------------------------------------

FIVE_SEC_WINDOW = 5.0            # trade window + flash-to-kill window, in seconds
FLASH_EFFECTIVE_THRESHOLD = 0.7  # minimum added blind duration (s) to count a flash
DEFAULT_TICK_RATE = 64.0         # CS2 demos are recorded at 64 tick

# --------------------------------------------------------------------------
# Round swing model
# --------------------------------------------------------------------------
# Win probability is logistic in the man advantage:
#     logit P(CT wins) = SWING_CT_BASE + SWING_PER_MAN * (ct_alive - t_alive)
#                        - SWING_BOMB * bomb_planted
# These three were calibrated against several demos whose per-player round swing was
# known and the fit reproduces them within 0.95 percentage points. They imply 5v5 51.5%
# CT, 5v4 72.0%, a 1v2 clutch at 28-31% depending on side, matching published man-advantage win rates.

SWING_CT_BASE = 0.060      # logit of CT win probability in a 5v5, no bomb
SWING_PER_MAN = 0.885      # logit shift per man of advantage
SWING_BOMB = 0.294         # logit shift towards T once the bomb is planted

# How the win-probability gain of a kill is split (Leetify published split).
SWING_CREDIT_KILLER = 0.35
SWING_CREDIT_DAMAGE = 0.30   # shared by damage dealt to the victim
SWING_CREDIT_FLASH = 0.15    # flash assist within FIVE_SEC_WINDOW
SWING_CREDIT_TRADE = 0.20    # to the teammate whose death is being traded

# --------------------------------------------------------------------------
# NaN-safe helpers
# --------------------------------------------------------------------------
# demoparser2 returns pandas DataFrames. Missing values show up as NaN
# (a float), and NaN is truthy in Python (bool(float("nan")) is True).
# Any `row.get(x) or default` pattern silently breaks on NaN: it keeps
# the NaN instead of falling back, and str(NaN) == "nan" instead of "".
# These helpers make that safe everywhere.

def safe_str(val) -> str:
    if val is None:
        return ""
    if isinstance(val, float) and math.isnan(val):
        return ""
    return str(val)


def safe_num(val, default=0.0):
    if val is None:
        return default
    if isinstance(val, float) and math.isnan(val):
        return default
    return val


def normalize_side(raw):
    """Maps assorted CT/T spellings (and team_num codes) to CT / T / None."""
    s = safe_str(raw).strip().upper()
    if s.endswith(".0"):
        s = s[:-2]
    if s in ("3", "CT", "COUNTER-TERRORIST", "COUNTERTERRORIST"):
        return "CT"
    if s in ("2", "T", "TERRORIST"):
        return "T"
    return None


# --------------------------------------------------------------------------
# Demo parsing & stat computation
# --------------------------------------------------------------------------
# Grenade prices
UTILITY_PRICE = {
    "high explosive grenade": 300,
    "flashbang": 200,
    "smoke grenade": 300,
    "molotov": 400,
    "incendiary grenade": 600,
    "decoy grenade": 50,
}

# CS2 scoreboard counters
SCOREBOARD_PROPS = [
    "kills_total",
    "deaths_total",
    "assists_total",
    "damage_total",
    "utility_damage_total",
    "enemies_flashed_total",
    "headshot_kills_total",
    "team_num",
    "team_rounds_total",
]


class MapStats:
    def __init__(self):
        self.map_name = "unknown"
        self.rounds_total = 0
        self.round_winner = {}     # round_num -> "CT"/"T"
        self.roster = {}           # round_num -> steamid -> "CT"/"T"
        self.tick_rate = DEFAULT_TICK_RATE
        self.kill_events = []
        self.flash_events = []
        self.damage_share = defaultdict(lambda: defaultdict(float))
        self.event_damage = defaultdict(float)
        self.event_util_damage = defaultdict(float)
        self.plant_tick = {}       # round_num -> tick the bomb was planted
        self.equip = defaultdict(lambda: defaultdict(int))          # rnd -> side -> $
        self.equip_players = defaultdict(lambda: defaultdict(int))  # rnd -> side -> n
        self.pistol_rounds = set()
        self.has_scoreboard = False
        self.final_team_num = {}   # steamid -> 2/3 at match end
        self.team_score = {}       # 2/3 -> final score
        self.players = defaultdict(
            lambda: {
                "name": None,
                "kills": 0,
                "deaths": 0,
                "assists": 0,
                "headshots": 0,
                "first_kills": 0,
                "first_deaths": 0,
                "dmg_dealt": 0.0,
                "util_dmg_dealt": 0.0,
                "enemies_flashed": 0,
                "rounds_played": 0,
                "rounds_survived": 0,
                "rounds_with_kast": 0,
                "untradable_deaths": 0,
                "wasted_util_value": 0,
                "swing": 0.0,
                "ef_count": 0,
                "fltk_count": 0,
                "lms_events": [],   # (round_num, enemies_alive, side, tick)
            }
        )

    def player(self, steamid: str, name=None):
        p = self.players[steamid]
        clean_name = safe_str(name)
        if clean_name:
            p["name"] = clean_name
        return p


# --------------------------------------------------------------------------
# Round table: which round_end events belong to the *actual* match
# --------------------------------------------------------------------------

def build_round_table(parser):
    """
    Returns (rounds, seg_start_tick).

    `rounds` is a list of {"tick", "winner", "trp"} for the real match rounds,
    in order; round number = index + 1.

    Two things are filtered out here, which the old code counted as rounds:
      * the synthetic tick-0 `round_end` row (round 0, winner NaN),
      * anything before a match restart -- i.e. the knife round, after which
        the server does mp_restartgame and `total_rounds_played` resets to 0.
    """
    try:
        ends = parser.parse_event(
            "round_end", other=["total_rounds_played", "is_warmup_period"]
        )
    except Exception as exc:
        print(f"WARNING: could not parse round_end ({exc})")
        return [], -1

    rows = []
    for _, r in ends.iterrows():
        winner = normalize_side(r.get("winner"))
        if winner is None:
            continue  # synthetic / aborted round_end carries no winner
        if bool(safe_num(r.get("is_warmup_period"), False)):
            continue
        rows.append(
            {
                "tick": int(r["tick"]),
                "trp": int(safe_num(r.get("total_rounds_played"), 0)),
                "winner": winner,
            }
        )
    rows.sort(key=lambda x: x["tick"])

    # Split on total_rounds_played resets -> the last segment is the real match.
    segments, cur, prev = [], [], None
    for r in rows:
        if prev is not None and r["trp"] <= prev:
            segments.append(cur)
            cur = []
        cur.append(r)
        prev = r["trp"]
    if cur:
        segments.append(cur)
    if not segments:
        return [], -1

    final = segments[-1]
    dropped = sum(len(s) for s in segments[:-1])
    if dropped:
        print(
            f"NOTE: ignored {dropped} round(s) before the last match restart "
            f"(knife round / restarted match)."
        )
    seg_start = segments[-2][-1]["tick"] if len(segments) > 1 else -1
    return final, seg_start


def make_round_lookup(rounds, seg_start):
    """tick -> 1-based round number within the real match, or None."""
    end_ticks = [r["tick"] for r in rounds]

    def lookup(tick):
        if tick <= seg_start:
            return None
        idx = bisect.bisect_left(end_ticks, tick)
        if idx >= len(end_ticks):
            return None  # after the final round_end: post-match
        return idx + 1

    return lookup


# --------------------------------------------------------------------------
# Scoreboard snapshot (authoritative kills/damage/UD/EF)
# --------------------------------------------------------------------------

def read_scoreboard(parser, rounds, stats):
    """
    Reads CS2's own per-player counters. Sampled at every round end, keeping 
    each player's latest row, so players who disconnect to quickly still get 
    their final numbers.
    """
    if not rounds:
        return
    ticks = [r["tick"] for r in rounds]
    try:
        df = parser.parse_ticks(SCOREBOARD_PROPS, ticks=ticks)
    except Exception as exc:
        print(f"WARNING: could not read scoreboard counters ({exc}); reconstructing from events.")
        return

    stats.has_scoreboard = True
    latest = {}
    for _, row in df.iterrows():
        sid = safe_str(row.get("steamid"))
        if not sid:
            continue
        tick = safe_num(row.get("tick"), 0)
        if sid not in latest or tick >= latest[sid][0]:
            latest[sid] = (tick, row)

    for sid, (_, row) in latest.items():
        p = stats.player(sid, row.get("name"))
        p["kills"] = int(safe_num(row.get("kills_total"), 0))
        p["deaths"] = int(safe_num(row.get("deaths_total"), 0))
        p["assists"] = int(safe_num(row.get("assists_total"), 0))
        p["headshots"] = int(safe_num(row.get("headshot_kills_total"), 0))
        p["dmg_dealt"] = float(safe_num(row.get("damage_total"), 0))
        p["util_dmg_dealt"] = float(safe_num(row.get("utility_damage_total"), 0))
        p["enemies_flashed"] = int(safe_num(row.get("enemies_flashed_total"), 0))
        tn = safe_num(row.get("team_num"), None)
        if tn is not None:
            stats.final_team_num[sid] = int(tn)
            stats.team_score[int(tn)] = int(safe_num(row.get("team_rounds_total"), 0))


# --------------------------------------------------------------------------
# Per-round roster (who was alive on which side at freeze-time end)
# --------------------------------------------------------------------------

def build_roster(parser, round_of_tick, stats):
    roster = defaultdict(dict)
    try:
        freeze = parser.parse_event("round_freeze_end")
        ticks = sorted({int(t) for t in freeze["tick"].tolist()})
    except Exception as exc:
        print(f"WARNING: no round_freeze_end ({exc}); clutch/LMS metrics will be skipped.")
        return roster
    ticks = [t for t in ticks if round_of_tick(t) is not None]
    if not ticks:
        return roster
    try:
        df = parser.parse_ticks(["team_num", "is_alive"], ticks=ticks)
    except Exception as exc:
        print(f"WARNING: could not read per-round sides ({exc}); clutch/LMS skipped.")
        return roster

    for _, row in df.iterrows():
        rnd = round_of_tick(safe_num(row.get("tick"), -1))
        sid = safe_str(row.get("steamid"))
        if rnd is None or not sid:
            continue
        if row.get("is_alive") is False:
            continue
        side = normalize_side(safe_num(row.get("team_num"), None))
        if side:
            roster[rnd][sid] = side
            stats.player(sid, row.get("name"))
    return roster


# --------------------------------------------------------------------------
# Fallback parser (demoinfocs-golang)
# --------------------------------------------------------------------------

GO_PARSER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "godemo")


class GoDemoParser:
    """
    Stand-in for DemoParser on demos demoparser2 cannot read: some fail every
    event/tick call with EntityNotFound or MalformedMessage
    (LaihoE/demoparser#339). godemo/ reads them with demoinfocs-golang and
    exports exactly the events and tick values this script asks for, under
    demoparser2's column names, so nothing downstream has to know.
    Needs Go installed (https://go.dev/dl/).
    """

    def __init__(self, path, header):
        if not shutil.which("go"):
            raise RuntimeError(
                "demoparser2 cannot read this demo and the Go fallback needs Go "
                "installed (https://go.dev/dl/)."
            )
        print("Parsing with the Go fallback (demoinfocs-golang) ...")
        proc = subprocess.run(
            ["go", "run", ".", os.path.abspath(path)],
            cwd=GO_PARSER_DIR, capture_output=True, text=True, encoding="utf-8",
        )
        if proc.returncode != 0:
            raise RuntimeError(f"Go fallback failed:\n{proc.stderr.strip()}")
        data = json.loads(proc.stdout)
        self._header = {**data["header"], **(header or {})}
        self._events = data["events"]
        self._ticks = pd.DataFrame(data["ticks"] or [])

    def parse_header(self):
        return self._header

    def parse_event(self, name, player=None, other=None):
        if name not in self._events:
            raise KeyError(f"event {name} is not exported by the Go fallback")
        rows = self._events[name]
        return pd.DataFrame(rows) if rows else pd.DataFrame(columns=["tick"])

    def parse_ticks(self, props, ticks=None):
        cols = ["tick", "steamid", "name", *props]
        if self._ticks.empty:
            return pd.DataFrame(columns=cols)
        df = self._ticks
        if ticks is not None:
            df = df[df["tick"].isin(set(ticks))]
        return df[[c for c in cols if c in df.columns]].reset_index(drop=True)


def open_parser(path):
    """demoparser2, or the Go fallback when demoparser2 cannot read the demo."""
    parser = DemoParser(path)
    try:
        header = parser.parse_header()
    except Exception:
        header = None
    try:
        parser.parse_event("round_end")
        return parser
    except Exception as exc:
        print(f"NOTE: demoparser2 cannot read this demo ({exc}); "
              f"see LaihoE/demoparser#339.")
    return GoDemoParser(path, header)


# --------------------------------------------------------------------------
# Main parse
# --------------------------------------------------------------------------

def parse_demo(path: str) -> MapStats:
    parser = open_parser(path)
    stats = MapStats()

    header = parser.parse_header()
    stats.map_name = header.get("map_name", "unknown")
    stats.tick_rate = safe_num(header.get("tick_rate"), DEFAULT_TICK_RATE) or DEFAULT_TICK_RATE

    rounds, seg_start = build_round_table(parser)
    round_of_tick = make_round_lookup(rounds, seg_start)
    for i, r in enumerate(rounds):
        stats.round_winner[i + 1] = r["winner"]

    read_scoreboard(parser, rounds, stats)

    # Round count: prefer the scoreboard team scores (exactly the score line
    # onesee in game); fall back to the number of decided rounds.
    if len(stats.team_score) == 2:
        stats.rounds_total = sum(stats.team_score.values())
        if stats.rounds_total != len(rounds):
            print(
                f"NOTE: team score sums to {stats.rounds_total} but "
                f"{len(rounds)} decided rounds were found; using the score."
            )
    else:
        stats.rounds_total = len(rounds)

    stats.roster = build_roster(parser, round_of_tick, stats)

    # Kills: opening duels + event timeline for trades/clutch/KAST
    try:
        kills = parser.parse_event(
            "player_death",
            player=["team_num"],
            other=["total_rounds_played", "is_warmup_period"],
        )
    except Exception:
        kills = parser.parse_event("player_death")

    first_kill_done = defaultdict(bool)
    for _, row in kills.iterrows():
        tick = int(safe_num(row.get("tick"), 0))
        rnd = round_of_tick(tick)
        if rnd is None:
            continue  # warmup, knife round, or post-match
        if bool(safe_num(row.get("is_warmup_period"), False)):
            continue

        attacker = safe_str(row.get("attacker_steamid"))
        victim = safe_str(row.get("user_steamid"))
        if not victim:
            continue

        a_side = normalize_side(safe_num(row.get("attacker_team_num"), None)) \
            or stats.roster.get(rnd, {}).get(attacker)
        v_side = normalize_side(safe_num(row.get("user_team_num"), None)) \
            or stats.roster.get(rnd, {}).get(victim)

        stats.player(victim, row.get("user_name"))
        if attacker:
            stats.player(attacker, row.get("attacker_name"))

        # A teamkill or a suicide is not an opening kill.
        real_kill = bool(attacker) and attacker != victim and not (
            a_side is not None and a_side == v_side
        )

        if real_kill and not first_kill_done[rnd]:
            stats.player(attacker)["first_kills"] += 1
            stats.player(victim)["first_deaths"] += 1
            first_kill_done[rnd] = True

        stats.kill_events.append(
            {
                "tick": tick,
                "round": rnd,
                "attacker": attacker,
                "victim": victim,
                "attacker_side": a_side,
                "victim_side": v_side,
                "is_teamkill": bool(attacker) and attacker != victim
                and a_side is not None and a_side == v_side,
                "assister": safe_str(row.get("assister_steamid")),
            }
        )

    # Wasted utility is read from inventories, see collect_wasted_utility
    collect_damage(parser, round_of_tick, stats)
    collect_bomb_plants(parser, round_of_tick, stats)
    collect_equipment(parser, round_of_tick, stats)

    if not stats.has_scoreboard:
        fallback_from_events(stats)

    collect_flash_events(parser, round_of_tick, stats)
    process_derived(stats)
    collect_wasted_utility(parser, stats)
    compute_round_swing(stats)
    mark_pistol_rounds(stats)
    return stats



# --------------------------------------------------------------------------
# Event-based fallback (only used if the scoreboard counters are unavailable)
# --------------------------------------------------------------------------

# player_hurt reports fire damage under "inferno"; "molotov"/"incgrenade"
# only show up for the rare direct-impact hit.
FALLBACK_UTILITY_WEAPONS = {
    "hegrenade", "flashbang", "smokegrenade", "molotov", "incgrenade", "inferno",
}


def fallback_from_events(stats):
    """
    Reconstructs kills/deaths/assists/damage from the event timeline when
    CS2 own per-player counters could not be read. Damage comes from
    collect_damage(), which already excludes team/self damage and caps each
    hit at the victim remaining HP.
    """
    for k in stats.kill_events:
        if k["victim"]:
            stats.player(k["victim"])["deaths"] += 1
        if k["attacker"] and k["attacker"] != k["victim"] and not k["is_teamkill"]:
            stats.player(k["attacker"])["kills"] += 1
        if k.get("assister"):
            stats.player(k["assister"])["assists"] += 1

    for sid, dealt in stats.event_damage.items():
        stats.player(sid)["dmg_dealt"] = dealt
    for sid, dealt in stats.event_util_damage.items():
        stats.player(sid)["util_dmg_dealt"] = dealt


# --------------------------------------------------------------------------
# Flashes
# --------------------------------------------------------------------------

def collect_flash_events(parser, round_of_tick, stats):
    """
    CS2 demos carry no player_blind game event. So... every flashbang_detonate 
    is matched against the flash_duration property of each player one tick before 
    and at the detonation tick. A player whose flash_duration jumps was blinded by 
    that flashbang.

    Caveat: If two flashes detonate on the same tick, we may misattribute the blind to the wrong thrower.
    This should be so rare though that I can not get myself to care about it.
    """
    try:
        det = parser.parse_event("flashbang_detonate")
    except Exception as exc:
        print(f"WARNING: flashbang_detonate unavailable ({exc}); FLTK/R will be 0.")
        return
    if not hasattr(det, "iterrows") or len(det) == 0:
        return

    det_rows = []
    for _, row in det.iterrows():
        tick = int(safe_num(row.get("tick"), 0))
        rnd = round_of_tick(tick)
        thrower = safe_str(row.get("user_steamid"))
        if rnd is not None and thrower:
            det_rows.append((tick, thrower, rnd))
    if not det_rows:
        return

    wanted = sorted({t for tick, _, _ in det_rows for t in (tick - 1, tick)})
    try:
        df = parser.parse_ticks(["flash_duration", "team_num"], ticks=wanted)
    except Exception as exc:
        print(f"WARNING: could not read flash_duration ({exc}); FLTK/R will be 0.")
        return

    by_tick = defaultdict(dict)
    for _, row in df.iterrows():
        sid = safe_str(row.get("steamid"))
        if not sid:
            continue
        by_tick[int(safe_num(row.get("tick"), 0))][sid] = (
            float(safe_num(row.get("flash_duration"), 0.0)),
            normalize_side(safe_num(row.get("team_num"), None)),
        )

    for tick, thrower, rnd in det_rows:
        before = by_tick.get(tick - 1, {})
        after = by_tick.get(tick, {})
        thrower_side = (after.get(thrower) or before.get(thrower) or (0.0, None))[1]
        for sid, (dur_after, side) in after.items():
            if sid == thrower:
                continue
            gain = dur_after - before.get(sid, (0.0, None))[0]
            if gain < FLASH_EFFECTIVE_THRESHOLD:
                continue
            stats.flash_events.append(
                {
                    "tick": tick,
                    "round": rnd,
                    "attacker": thrower,
                    "victim": sid,
                    "attacker_side": thrower_side,
                    "victim_side": side,
                    "duration": gain,
                    "same_team": thrower_side is not None and thrower_side == side,
                }
            )


# --------------------------------------------------------------------------
# Timeline post-processing
# -------------------------------------------------------------------------

def process_derived(stats):
    """Needs the whole kill/flash timeline at once: clutch/LMS detection,
    trade detection (-> untradable deaths + KAST), flash-leading-to-kill."""
    window_ticks = FIVE_SEC_WINDOW * stats.tick_rate

    kills_by_round = defaultdict(list)
    for k in stats.kill_events:
        kills_by_round[k["round"]].append(k)

    # Last man standing / clutch situations
    for rnd, klist in kills_by_round.items():
        klist.sort(key=lambda k: k["tick"])
        roster_this_round = stats.roster.get(rnd, {})
        if not roster_this_round:
            for k in klist:
                k["victim_was_lms_before_death"] = False
            continue
        alive = {
            "T": {sid for sid, s in roster_this_round.items() if s == "T"},
            "CT": {sid for sid, s in roster_this_round.items() if s == "CT"},
        }
        clutch_seen = set()
        for k in klist:
            victim, vs = k["victim"], k["victim_side"]
            if not victim or vs not in alive:
                k["victim_was_lms_before_death"] = False
                continue
            k["victim_was_lms_before_death"] = len(alive[vs]) == 1
            alive[vs].discard(victim)
            if len(alive[vs]) == 1 and vs not in clutch_seen:
                survivor = next(iter(alive[vs]))
                enemies = len(alive["CT" if vs == "T" else "T"])
                if enemies >= 1:
                    clutch_seen.add(vs)
                    stats.player(survivor)["lms_events"].append(
                        (rnd, enemies, vs, k["tick"])
                    )

    # Trades -> untradable deaths (+ traded flag, reused for KAST)
    # which teammate death a kill avenges (used by the swing credit split)
    for klist in kills_by_round.values():
        for i, k in enumerate(klist):
            k["traded_victim"] = None
            for prev in reversed(klist[:i]):
                if k["tick"] - prev["tick"] > window_ticks:
                    break
                if prev["victim_side"] == k["attacker_side"] \
                        and prev["attacker"] == k["victim"]:
                    k["traded_victim"] = prev["victim"]
                    break

    ordered = sorted(stats.kill_events, key=lambda k: k["tick"])
    for i, k in enumerate(ordered):
        k["traded"] = False
        if not k["attacker"] or not k["victim"] or k["attacker"] == k["victim"]:
            continue
        for k2 in ordered[i + 1:]:
            if k2["tick"] - k["tick"] > window_ticks:
                break
            if k2["round"] != k["round"]:
                continue
            if k2["victim"] == k["attacker"] and k2["attacker_side"] == k["victim_side"]:
                k["traded"] = True
                break
        if not k["traded"] and not k.get("victim_was_lms_before_death"):
            stats.player(k["victim"])["untradable_deaths"] += 1

    compute_kast(stats, kills_by_round)

    # Flash leading to kill: a flashed enemy dies to the flasher team
    kills_by_victim = defaultdict(list)
    for k in stats.kill_events:
        if k["victim"] and not k["is_teamkill"]:
            kills_by_victim[k["victim"]].append(k)

    for f in stats.flash_events:
        if f["same_team"] or f["victim_side"] is None:
            continue
        stats.player(f["attacker"])["ef_count"] += 1
        for k in kills_by_victim.get(f["victim"], []):
            if k["round"] != f["round"]:
                continue
            if 0 <= k["tick"] - f["tick"] <= window_ticks and \
                    k["attacker_side"] == f["attacker_side"]:
                stats.player(f["attacker"])["fltk_count"] += 1
                break


def compute_kast(stats, kills_by_round):
    """KAST = share of rounds with a Kill, Assist, Survival or a traded death."""
    rounds_played = defaultdict(int)
    kast_rounds = defaultdict(int)
    survived = defaultdict(int)

    for rnd in range(1, stats.rounds_total + 1):
        roster_this_round = stats.roster.get(rnd, {})
        if not roster_this_round:
            continue
        killers, assisters, victims, traded = set(), set(), set(), set()
        for k in kills_by_round.get(rnd, []):
            if k["attacker"] and not k["is_teamkill"] and k["attacker"] != k["victim"]:
                killers.add(k["attacker"])
            if k.get("assister"):
                assisters.add(k["assister"])
            if k["victim"]:
                victims.add(k["victim"])
                if k.get("traded"):
                    traded.add(k["victim"])
        for sid in roster_this_round:
            rounds_played[sid] += 1
            alive_at_end = sid not in victims
            if alive_at_end:
                survived[sid] += 1
            if alive_at_end or sid in killers or sid in assisters or sid in traded:
                kast_rounds[sid] += 1

    for sid, p in stats.players.items():
        p["rounds_played"] = rounds_played.get(sid, 0)
        p["rounds_with_kast"] = kast_rounds.get(sid, 0)
        p["rounds_survived"] = survived.get(
            sid, max(stats.rounds_total - p["deaths"], 0)
        )


def compute_clutch_winrates(stats):
    # Note: A survived clutch (a save) counts as won
    deaths_by_round = defaultdict(set)
    for k in stats.kill_events:
        if k["victim"]:
            deaths_by_round[k["round"]].add(k["victim"])

    result = defaultdict(lambda: {n: [0, 0] for n in range(1, 6)})
    for steamid, p in stats.players.items():
        for rnd, enemies_alive, side, _tick in p.get("lms_events", []):
            if enemies_alive not in result[steamid]:
                continue
            bucket = result[steamid][enemies_alive]
            bucket[1] += 1
            saved = steamid not in deaths_by_round.get(rnd, set())
            if stats.round_winner.get(rnd) == side or saved:
                bucket[0] += 1
    return result



# --------------------------------------------------------------------------
# Damage / bomb / inventory collection (shared by ADR fallback, swing, unused)
# --------------------------------------------------------------------------

def collect_damage(parser, round_of_tick, stats):
    """
    Fills stats.damage_share[(round, victim)][attacker] with HP-capped enemy
    damage, and stats.event_damage / stats.event_util_damage with per-player
    totals (used only when the scoreboard counters are unavailable).

    player_hurt reports raw damage, so a 239-damage AWP hit on a 40 HP player
    would otherwise count 239. Capping at the victim remaining HP must be done
    """
    try:
        hurts = parser.parse_event(
            "player_hurt",
            player=["team_num"],
            other=["total_rounds_played", "is_warmup_period"],
        )
    except Exception as exc:
        print(f"WARNING: player_hurt unavailable ({exc}); damage share is empty.")
        return

    rows = sorted((r for _, r in hurts.iterrows()),
                  key=lambda r: int(safe_num(r.get("tick"), 0)))
    health_left = {}
    for row in rows:
        rnd = round_of_tick(int(safe_num(row.get("tick"), 0)))
        if rnd is None or bool(safe_num(row.get("is_warmup_period"), False)):
            continue
        victim = safe_str(row.get("user_steamid"))
        if not victim:
            continue

        key = (rnd, victim)
        before = health_left.get(key, 100.0)
        after = float(safe_num(row.get("health"), 0))
        dealt = max(0.0, before - after)
        health_left[key] = after

        attacker = safe_str(row.get("attacker_steamid"))
        if not attacker or attacker == victim:
            continue  # world damage (fall damage) or self damage
        a_side = normalize_side(safe_num(row.get("attacker_team_num"), None))
        v_side = normalize_side(safe_num(row.get("user_team_num"), None))
        if a_side is not None and a_side == v_side:
            continue  # team damage

        stats.damage_share[key][attacker] += dealt
        stats.event_damage[attacker] += dealt
        if safe_str(row.get("weapon")).lower() in FALLBACK_UTILITY_WEAPONS:
            stats.event_util_damage[attacker] += dealt


def collect_bomb_plants(parser, round_of_tick, stats):
    try:
        plants = parser.parse_event("bomb_planted")
    except Exception:
        return
    if not hasattr(plants, "iterrows"):
        return
    for _, row in plants.iterrows():
        tick = int(safe_num(row.get("tick"), 0))
        rnd = round_of_tick(tick)
        if rnd is not None and rnd not in stats.plant_tick:
            stats.plant_tick[rnd] = tick


def collect_wasted_utility(parser, stats):
    """
    Wasted utility = the dollar value of the grenades a player was still
    holding at the moment they died.

    Utility carried through a round the player survived is NOT counted: that
    is a deliberate save, not waste. Only nades that died with the player are
    charged, because those are the ones nobody can get any value from.
    """
    death_tick = {}   # (round, steamid) -> tick they died
    for k in stats.kill_events:
        if k["victim"] and (k["round"], k["victim"]) not in death_tick:
            death_tick[(k["round"], k["victim"])] = k["tick"]

    wanted = set()
    for (rnd, sid), tick in death_tick.items():
        if stats.roster.get(rnd, {}).get(sid):
            wanted.add((int(tick) - 1, sid))

    if not wanted:
        return
    try:
        df = parser.parse_ticks(["inventory"], ticks=sorted({t for t, _ in wanted}))
    except Exception as exc:
        print(f"WARNING: could not read inventories ({exc}); "
              f"'Unused Utility ($)' will be 0.")
        return

    for _, row in df.iterrows():
        tick = int(safe_num(row.get("tick"), -1))
        sid = safe_str(row.get("steamid"))
        if (tick, sid) not in wanted:
            continue
        items = row.get("inventory")
        if items is None or isinstance(items, float):
            continue
        value = sum(UTILITY_PRICE.get(safe_str(item).lower(), 0) for item in items)
        stats.player(sid)["wasted_util_value"] += value


# --------------------------------------------------------------------------
# Round swing
# --------------------------------------------------------------------------

def round_win_probability(ct_alive, t_alive, bomb_planted):
    """
    P(CT wins) from the alive counts and the bomb state, as a logistic model.

    The constants were calibrated against several demos whose per-player round swing
    were known (see SWING_* above). Thanks to openly available demos.
    """
    if ct_alive <= 0:
        return 0.0
    if t_alive <= 0 and not bomb_planted:
        return 1.0
    z = SWING_CT_BASE + SWING_PER_MAN * (ct_alive - t_alive)
    if bomb_planted:
        z -= SWING_BOMB
    return 1.0 / (1.0 + math.exp(-max(-40.0, min(40.0, z))))


def _kill_credit(stats, rnd, kill):
    """
    How the win-probability gain of one kill is split, following the split
    Leetify describes: ~35% to the killer, ~30% across everyone who damaged
    the victim, ~15% for a flash assist and ~20% to the teammate being traded.
    A share nobody earned (no flash assist, not a trade) falls back to the
    killer, so the whole gain is always attributed.
    """
    shares = defaultdict(float)
    killer = kill["attacker"]
    shares[killer] += SWING_CREDIT_KILLER

    damage = stats.damage_share.get((rnd, kill["victim"]), {})
    total_damage = sum(damage.values())
    if total_damage > 0:
        for sid, dealt in damage.items():
            shares[sid] += SWING_CREDIT_DAMAGE * (dealt / total_damage)
    else:
        shares[killer] += SWING_CREDIT_DAMAGE

    window = FIVE_SEC_WINDOW * stats.tick_rate
    flashers = [f["attacker"] for f in stats.flash_events
                if f["round"] == rnd and f["victim"] == kill["victim"]
                and not f["same_team"] and f["attacker"] != killer
                and 0 <= kill["tick"] - f["tick"] <= window]
    if flashers:
        for sid in flashers:
            shares[sid] += SWING_CREDIT_FLASH / len(flashers)
    else:
        shares[killer] += SWING_CREDIT_FLASH

    if kill.get("traded_victim"):
        shares[kill["traded_victim"]] += SWING_CREDIT_TRADE
    else:
        shares[killer] += SWING_CREDIT_TRADE

    return shares


def _side_members(roster, side):
    return {sid for sid, value in roster.items() if value == side}


def compute_round_swing(stats):
    """
    Round swing = how much a player moved their team chance of winning the
    round, averaged over the rounds of the map.

    Every change in win probability is attributed to somebody, so a team
    swings sum to (rounds won - sum of starting win probabilities) / rounds.
    Dying is charged to the player who died; whatever the round result adds
    on top of the last modelled state is split across that side survivors.
    """
    kills_by_round = defaultdict(list)
    for k in stats.kill_events:
        kills_by_round[k["round"]].append(k)

    totals = defaultdict(float)
    for rnd in range(1, stats.rounds_total + 1):
        roster = stats.roster.get(rnd, {})
        if not roster:
            continue
        alive = {"CT": {s for s, v in roster.items() if v == "CT"},
                 "T": {s for s, v in roster.items() if v == "T"}}
        planted = False
        p_ct = round_win_probability(len(alive["CT"]), len(alive["T"]), planted)

        timeline = [(k["tick"], 0, k) for k in kills_by_round.get(rnd, [])]
        if rnd in stats.plant_tick:
            timeline.append((stats.plant_tick[rnd], 1, None))
        timeline.sort(key=lambda e: (e[0], e[1]))

        for _tick, kind, kill in timeline:
            if kind == 1:  # bomb planted: T gains, CT pays
                planted = True
                new_ct = round_win_probability(len(alive["CT"]), len(alive["T"]), True)
                gain = p_ct - new_ct
                p_ct = new_ct
                for side, sign in (("T", 1.0), ("CT", -1.0)):
                    members = alive[side] or _side_members(roster, side)
                    if members:
                        for sid in members:
                            totals[sid] += sign * gain / len(members)
                continue

            victim, victim_side = kill["victim"], kill["victim_side"]
            if not victim or victim_side not in alive or victim not in alive[victim_side]:
                continue
            killing_side = "CT" if victim_side == "T" else "T"
            alive[victim_side].discard(victim)
            new_ct = round_win_probability(len(alive["CT"]), len(alive["T"]), planted)
            delta_ct = new_ct - p_ct
            p_ct = new_ct
            gain = delta_ct if killing_side == "CT" else -delta_ct

            totals[victim] -= gain      # the player who died carries the loss

            if kill["is_teamkill"] or not kill["attacker"] or kill["attacker"] == victim:
                # Nobody earned this one -- a suicide, a teamkill, or the bomb
                # going off and killing the last CTs when every T is already
                # dead. Spread it over the side that benefited (falling back to
                # its full roster) so no win probability is lost.
                members = alive[killing_side] or _side_members(roster, killing_side)
                if members:
                    for sid in members:
                        totals[sid] += gain / len(members)
                continue

            shares = _kill_credit(stats, rnd, kill)
            weight = sum(shares.values()) or 1.0
            for sid, share in shares.items():
                totals[sid] += gain * (share / weight)

        # whatever the actual result adds on top of the modelled state
        ct_won = stats.round_winner.get(rnd) == "CT"
        residual_ct = (1.0 if ct_won else 0.0) - p_ct
        for side, residual in (("CT", residual_ct), ("T", -residual_ct)):
            pool = alive[side] or _side_members(roster, side)
            if pool:
                for sid in pool:
                    totals[sid] += residual / len(pool)

    rounds = max(stats.rounds_total, 1)
    for sid, value in totals.items():
        stats.player(sid)["swing"] = value / rounds



# --------------------------------------------------------------------------
# Team-level stats for the Maps row
# --------------------------------------------------------------------------
# Buy classification uses the team equipment value at the end of freeze time,
# divided by the number of players on that side, so it is not thrown off by a
# 4-man side after a disconnect.
ECO_PER_PLAYER = 1700        # below this, the side is on an eco
BUY_PER_PLAYER = 3750        # at or above this, the side is on a full buy


def collect_equipment(parser, round_of_tick, stats):
    """stats.equip[round][side] = the side total equipment value at freeze end."""
    try:
        freeze = parser.parse_event("round_freeze_end")
        ticks = sorted({int(t) for t in freeze["tick"].tolist()})
    except Exception as exc:
        print(f"WARNING: no round_freeze_end ({exc}); buy types unavailable.")
        return
    # a round can report several freeze_end ticks; keep only the first
    first_tick = {}
    for t in ticks:
        rnd = round_of_tick(t)
        if rnd is not None and rnd not in first_tick:
            first_tick[rnd] = t
    if not first_tick:
        return
    try:
        df = parser.parse_ticks(["current_equip_value", "team_num"],
                                ticks=sorted(first_tick.values()))
    except Exception as exc:
        print(f"WARNING: could not read equipment values ({exc}); buy types unavailable.")
        return

    round_of = {tick: rnd for rnd, tick in first_tick.items()}
    for _, row in df.iterrows():
        rnd = round_of.get(int(safe_num(row.get("tick"), -1)))
        side = normalize_side(safe_num(row.get("team_num"), None))
        if rnd is None or side is None:
            continue
        stats.equip[rnd][side] += int(safe_num(row.get("current_equip_value"), 0))
        stats.equip_players[rnd][side] += 1


def buy_type(stats, rnd, side):
    """pistol / eco / half_buy / buy for one side in one round."""
    if rnd in stats.pistol_rounds:
        return "pistol"
    players = stats.equip_players.get(rnd, {}).get(side, 0)
    if not players:
        return None
    per_player = stats.equip.get(rnd, {}).get(side, 0) / players
    if per_player < ECO_PER_PLAYER:
        return "eco"
    if per_player < BUY_PER_PLAYER:
        return "half_buy"
    return "buy"


def mark_pistol_rounds(stats):
    """
    Honestly overengineered because it should always just be [1, 13].
    But who knows what weird server config or admin is trying to mess with me
    """
    stats.pistol_rounds = set()
    previous_ct = None
    for rnd in range(1, stats.rounds_total + 1):
        roster = stats.roster.get(rnd, {})
        if not roster:
            continue
        if previous_ct is None:
            stats.pistol_rounds.add(rnd)
        else:
            switched = sum(1 for sid in previous_ct if roster.get(sid) == "T")
            stayed = sum(1 for sid in previous_ct if roster.get(sid) == "CT")
            if switched > stayed:
                stats.pistol_rounds.add(rnd)
                return
        previous_ct = {sid for sid, side in roster.items() if side == "CT"}


def _round_shapes(stats, rnd, our_players):
    """Walks the round and reports the alive-count situations our side hit."""
    roster = stats.roster.get(rnd, {})
    our_side = next((side for sid, side in roster.items() if sid in our_players), None)
    if our_side is None:
        return None
    their_side = "CT" if our_side == "T" else "T"
    alive = {"CT": sum(1 for s in roster.values() if s == "CT"),
             "T": sum(1 for s in roster.values() if s == "T")}

    shapes = {"ahead": False, "five_four": False, "four_five": False}
    kills = sorted((k for k in stats.kill_events if k["round"] == rnd),
                   key=lambda k: k["tick"])
    def note():
        ours, theirs = alive[our_side], alive[their_side]
        if ours > theirs:
            shapes["ahead"] = True
        if (ours, theirs) == (5, 4):
            shapes["five_four"] = True
        if (ours, theirs) == (4, 5):
            shapes["four_five"] = True
    note()
    for k in kills:
        if k["victim_side"] in alive:
            alive[k["victim_side"]] -= 1
            note()
    return our_side, shapes


def compute_team_stats(stats, our_players):
    if not our_players:
        return {}
    mark_pistol_rounds(stats)

    buckets = defaultdict(lambda: [0, 0])     # name -> [wins, played]
    won_pistol = set()
    sides = {}

    for rnd in range(1, stats.rounds_total + 1):
        shaped = _round_shapes(stats, rnd, our_players)
        if shaped is None:
            continue
        our_side, shapes = shaped
        sides[rnd] = our_side
        won = stats.round_winner.get(rnd) == our_side

        def add(name):
            buckets[name][1] += 1
            if won:
                buckets[name][0] += 1

        add("overall")
        add("CT" if our_side == "CT" else "T")

        ours = buy_type(stats, rnd, our_side)
        theirs = buy_type(stats, rnd, "CT" if our_side == "T" else "T")
        if ours:
            add(ours)
        if ours == "pistol" and won:
            won_pistol.add(rnd)

        if shapes["five_four"]:
            add("5v4")
        if shapes["four_five"]:
            add("4v5")

        # A conversion chance: we were up a man at some point, or it is the
        # round right after we won a pistol, or the enemy was on an eco.
        after_pistol_win = (rnd - 1) in won_pistol
        anti_eco = theirs == "eco" and ours in ("half_buy", "buy")
        if shapes["ahead"] or after_pistol_win or anti_eco:
            add("conversion")

    def rate(name):
        wins, played = buckets[name]
        return (wins / played) if played else None

    kast_values = [stats.players[sid]["rounds_with_kast"] / max(stats.rounds_total, 1)
                   for sid in our_players if sid in stats.players]

    return {
        "rounds": buckets["overall"][1],
        "wins": buckets["overall"][0],
        "CT%": rate("CT"),
        "T%": rate("T"),
        "Pistol WR": rate("pistol"),
        "Eco WR": rate("eco"),
        "Half-Buy WR": rate("half_buy"),
        "Buy WR": rate("buy"),
        "5v4 WR": rate("5v4"),
        "4v5 WR": rate("4v5"),
        "Conv WR": rate("conversion"),
        "KAST": (sum(kast_values) / len(kast_values)) if kast_values else None,
        "_counts": {k: tuple(v) for k, v in buckets.items()},
    }


def teams_in_demo(stats):
    """team_num -> set of steamids, so both teams can be reported offline."""
    teams = defaultdict(set)
    for sid, p in stats.players.items():
        if p["name"] and stats.final_team_num.get(sid):
            teams[stats.final_team_num[sid]].add(sid)
    return teams


# --------------------------------------------------------------------------
# Derived metrics
# --------------------------------------------------------------------------

def compute_rating_2(p, rounds):
    """Community approximation of HLTV Rating 2.0. Not the official formula."""
    if rounds == 0:
        return 0.0
    kpr = p["kills"] / rounds
    dpr = p["deaths"] / rounds
    apr = p["assists"] / rounds
    adr = p["dmg_dealt"] / rounds
    kast = p["rounds_with_kast"] / rounds
    impact = 2.13 * kpr + 0.42 * apr - 0.41
    rating = (
        0.0073 * (kast * 100)
        + 0.3591 * kpr
        - 0.5329 * dpr
        + 0.2372 * impact
        + 0.0032 * adr
        + 0.1587
    )
    return round(rating, 2)


def enemies_flashed(p, stats=None):
    """Prefer CS2 own counter; only fall back when it was never read.
    A genuine scoreboard 0 must not be replaced by the reconstruction."""
    if stats is not None and stats.has_scoreboard:
        return p["enemies_flashed"]
    return p["enemies_flashed"] if p["enemies_flashed"] else p["ef_count"]


def compute_extra_rates(p, rounds, stats=None):
    r = max(rounds, 1)
    return {
        "K/R": round(p["kills"] / r, 3),
        "FK/R": round(p["first_kills"] / r, 3),
        "FD/R": round(p["first_deaths"] / r, 3),
        "UTD/R": round(p["untradable_deaths"] / r, 3),
        "UD/R": round(p["util_dmg_dealt"] / r, 2),
        "LMS/R": round(len(p.get("lms_events", [])) / r, 3),
        "EF/R": round(enemies_flashed(p, stats) / r, 3),
        "FLTK/R": round(p["fltk_count"] / r, 3),
    }


def team_result(stats, our_players):
    """Win / Loss / Draw, judged from our own side each round."""
    wins = losses = 0
    for rnd in range(1, stats.rounds_total + 1):
        roster = stats.roster.get(rnd, {})
        our_side = next((side for sid, side in roster.items()
                         if sid in our_players), None)
        winner = stats.round_winner.get(rnd)
        if our_side is None or winner is None:
            continue
        if winner == our_side:
            wins += 1
        else:
            losses += 1
    if not wins and not losses:
        return None
    return "Win" if wins > losses else ("Loss" if wins < losses else "Draw")


# --------------------------------------------------------------------------
# Notion writes
# --------------------------------------------------------------------------

NOTION_TIMEOUT = 30          # seconds per HTTP request
NOTION_MAX_ATTEMPTS = 5      # attempts per request, for 429 / 5xx / network errors
NOTION_MIN_INTERVAL = 0.34   # Notion allows roughly 3 requests per second
NOTION_MAX_BACKOFF = 30.0
NOTION_MAX_TEXT = 2000       # Notion rich-text hard limit


# Maps.Map is a select whose options are bare map names ("Ancient"), not the
# de_ancient the demo header reports.
MAP_DISPLAY_NAMES = {
    "de_ancient": "Ancient", "de_anubis": "Anubis", "de_cache": "Cache",
    "de_dust2": "Dust2", "de_inferno": "Inferno", "de_mirage": "Mirage",
    "de_nuke": "Nuke", "de_overpass": "Overpass", "de_train": "Train",
    "de_vertigo": "Vertigo",
}


def map_display_name(raw):
    name = safe_str(raw).strip()
    return MAP_DISPLAY_NAMES.get(name.lower(), name.removeprefix("de_").title())


MAP_PROPS_REQUIRED = {
    "Titel": "title",
    "Match-Room / Demo-Link": "url",
}
MAP_PROPS_OPTIONAL = {
    "Map": "select",
    "Typ": "select",
    "Ergebnis": "select",
    "Liga/Turnier": "select",
    "Datum": "date",
    "Gegner": "relation",
    "VOD-Link": "url",
    "CT%": "number",
    "T%": "number",
    "Pistol WR": "number",
    "Eco WR": "number",
    "Half-Buy WR": "number",
    "Buy WR": "number",
    "5v4 WR": "number",
    "4v5 WR": "number",
    "Conv WR": "number",
    "KAST": "number",
}

# Performance.Map is a rollup off the Match relation -- it is computed by
# Notion and must never be written.
PERF_PROPS_REQUIRED = {
    "Name": "title",
    "Spieler": "relation",
    "Match": "relation",
}
PERF_PROPS_OPTIONAL = {
    "Rating": "number",
    "Swing": "number",
    "KAST": "number",
    "ADR": "number",
    "Kills/R": "number",
    "First Kills/R": "number",
    "First Death/R": "number",
    "Untradable Deaths/R": "number",
    "Utility Damage/R": "number",
    "Last Man Standing/R": "number",
    "Enemies Flashed/R": "number",
    "Flash Leading to Kills/R": "number",
    "1v1 WR/SR%": "number",
    "1v2 WR/SR%": "number",
    "1v3 WR/SR%": "number",
    "1v4 WR/SR%": "number",
    "1v5 WR/SR%": "number",
    "Wasted Utility/R ($)": "number",
    "Date": "date",
}

# our metric key -> Notion property name
PERF_RATE_PROPS = {
    "K/R": "Kills/R",
    "FK/R": "First Kills/R",
    "FD/R": "First Death/R",
    "UTD/R": "Untradable Deaths/R",
    "UD/R": "Utility Damage/R",
    "LMS/R": "Last Man Standing/R",
    "EF/R": "Enemies Flashed/R",
    "FLTK/R": "Flash Leading to Kills/R",
}

TEAM_STAT_PROPS = ["CT%", "T%", "Pistol WR", "Eco WR", "Half-Buy WR", "Buy WR",
                   "5v4 WR", "4v5 WR", "Conv WR", "KAST"]


class NotionError(RuntimeError):
    """An API call that will not succeed by retrying, with Notion own reason."""

    def __init__(self, context, status, code, message):
        self.status = status
        self.code = code
        self.detail = message
        text = f"{context}: HTTP {status}"
        if code:
            text += f" [{code}]"
        if message:
            text += f" - {message}"
        super().__init__(text)


_SESSION = None
_LAST_CALL = 0.0


def notion_headers():
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def notion_request(method, path, body=None, context="Notion request"):
    """
    One Notion API call with a timeout, client-side rate limiting and
    retries with exponential backoff on 429 / 5xx / network errors.

    A 4xx that retrying cannot fix (bad property, missing permission,
    unknown page) is raised as NotionError carrying the reason Notion gave,
    instead of the bare "400 Client Error" that raise_for_status produces.
    """
    global _SESSION, _LAST_CALL
    if _SESSION is None:
        _SESSION = requests.Session()

    url = f"{NOTION_API}{path}"
    backoff = 1.0

    for attempt in range(1, NOTION_MAX_ATTEMPTS + 1):
        pause = NOTION_MIN_INTERVAL - (time.monotonic() - _LAST_CALL)
        if pause > 0:
            time.sleep(pause)

        try:
            resp = _SESSION.request(
                method, url, headers=notion_headers(), json=body,
                timeout=NOTION_TIMEOUT,
            )
        except requests.RequestException as exc:
            _LAST_CALL = time.monotonic()
            if attempt == NOTION_MAX_ATTEMPTS:
                raise NotionError(context, 0, "network", str(exc)) from exc
            print(f"  {context}: {type(exc).__name__}, retrying in {backoff:.0f}s "
                  f"({attempt}/{NOTION_MAX_ATTEMPTS})")
            time.sleep(backoff)
            backoff = min(backoff * 2, NOTION_MAX_BACKOFF)
            continue

        _LAST_CALL = time.monotonic()

        if resp.status_code < 300:
            return resp.json() if resp.content else {}

        payload = {}
        try:
            payload = resp.json()
        except ValueError:
            pass
        code = payload.get("code", "")
        message = payload.get("message") or resp.text[:300]

        if (resp.status_code == 429 or resp.status_code >= 500) \
                and attempt < NOTION_MAX_ATTEMPTS:
            wait = backoff
            if resp.status_code == 429:
                try:
                    wait = float(resp.headers.get("Retry-After", backoff))
                except (TypeError, ValueError):
                    wait = backoff
            print(f"  {context}: HTTP {resp.status_code}, retrying in {wait:.0f}s "
                  f"({attempt}/{NOTION_MAX_ATTEMPTS})")
            time.sleep(wait)
            backoff = min(backoff * 2, NOTION_MAX_BACKOFF)
            continue

        raise NotionError(context, resp.status_code, code, message)

    raise NotionError(context, 0, "retries_exhausted",
                      f"gave up after {NOTION_MAX_ATTEMPTS} attempts")


# --------------------------------------------------------------------------
# Value builders (never emit NaN/Infinity, never exceed Notion limits)
# --------------------------------------------------------------------------

def notion_number(value, digits=3):
    v = safe_num(value, None)
    if v is None:
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return {"number": round(v, digits)}


def notion_title(text):
    return {"title": [{"text": {"content": safe_str(text)[:NOTION_MAX_TEXT] or "untitled"}}]}


def notion_select(name):
    clean = safe_str(name).strip()
    return {"select": {"name": clean[:100]}} if clean else None


def put(props, writable, name, value):
    """Only send a property the database actually has, and never send None."""
    if name in writable and value is not None:
        props[name] = value




# --------------------------------------------------------------------------
# Resolving players against the Spieler database
# --------------------------------------------------------------------------

def fetch_player_index():
    """
    SteamID64 -> Notion page id, read from the Spieler database.

    SteamID64 must be a Text property. As a Number it is a float64, which
    only holds 53 bits of integer precision while a SteamID64 needs 57, so
    Notion silently rounds every id and none of them would ever match.

    Real fun to find that one out...
    """
    index, cursor = {}, None
    rounded = []
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        data = notion_request("POST", f"/databases/{db('Spieler')}/query",
                              body=body,
                              context="reading the Spieler database")
        for page in data.get("results") or []:
            props = page.get("properties") or {}
            prop = props.get("SteamID64") or {}
            raw = ""
            if prop.get("type") == "rich_text":
                raw = "".join(part.get("plain_text", "")
                              for part in prop.get("rich_text") or [])
            elif prop.get("type") == "number" and prop.get("number") is not None:
                raw = str(int(prop["number"]))
                rounded.append(raw)
            steamid = "".join(ch for ch in raw if ch.isdigit())
            if steamid:
                index[steamid] = page["id"]
        cursor = data.get("next_cursor")
        if not data.get("has_more") or not cursor:
            break

    if rounded:
        print("  WARNING: Spieler.SteamID64 is a Number property. Notion stores "
              "numbers as float64, which cannot hold a SteamID64 exactly, so "
              "these ids are rounded and will not match:")
        for value in rounded:
            print(f"    {value}")
        print("  -> change SteamID64 to Text and re-enter the ids.")
    return index


def resolve_players(stats, index):
    """Returns (mapped steamids in the demo, names with no Spieler row)."""
    named = {sid: p["name"] for sid, p in stats.players.items() if p["name"]}
    mapped = [sid for sid in named if sid in index]
    unmapped = sorted(named[sid] for sid in named if sid not in index)
    return mapped, unmapped


# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------

def search_database(title):
    """The id of a database with this exact title that the integration can
    actually see. Only returns things already shared with the integration,
    so "nothing found" means a missing connection."""
    try:
        data = notion_request(
            "POST", "/search",
            body={"query": title,
                  "filter": {"value": "database", "property": "object"}},
            context=f"searching for the {title} database")
    except NotionError:
        return None
    for result in data.get("results") or []:
        name = "".join(part.get("plain_text", "")
                       for part in result.get("title") or [])
        if name.strip().lower() == title.strip().lower():
            return result.get("id")
    return None


def fetch_schema(label):
    """
    Reads a database schema. If the configured id 404s -- almost always
    because it is a data-source id rather than a database id -- the database
    is looked up by title and the correct id is reported and used.
    """
    configured = db(label)
    if not configured:
        raise NotionError(f"reading the {label} database schema", 0,
                          "bad_id", f"NOTION_DB_{label.upper()} is not a Notion id")
    try:
        data = notion_request("GET", f"/databases/{configured}",
                              context=f"reading the {label} database schema")
    except NotionError as exc:
        if exc.status != 404:
            raise
        found = normalize_notion_id(search_database(label))
        if not found or found == configured:
            raise
        print(f"  NOTE: the configured {label} id ({configured}) is not a "
              f"database id -- most likely a data-source/collection id.")
        print(f"        Found {label!r} at {found}; using it for this run.")
        print(f"        Make it permanent with NOTION_DB_{label.upper()}={found}")
        NOTION_DATABASES[label] = found
        data = notion_request("GET", f"/databases/{found}",
                              context=f"reading the {label} database schema")
    return {name: prop.get("type")
            for name, prop in (data.get("properties") or {}).items()}


def check_props(schema, required, optional, label, problems):
    writable = set()
    for name, want in required.items():
        got = schema.get(name)
        if got is None:
            problems.append(f"{label}: required property {name!r} does not exist")
        elif got != want:
            problems.append(
                f"{label}: property {name!r} is of type {got!r}, expected {want!r}")
        else:
            writable.add(name)

    missing = []
    for name, want in optional.items():
        got = schema.get(name)
        if got is None:
            missing.append(name)
        elif got != want:
            print(f"  WARNING: {label} property {name!r} is {got!r}, "
                  f"expected {want!r} -- it will not be written")
        else:
            writable.add(name)
    if missing:
        print(f"  NOTE: {label} has no {', '.join(repr(m) for m in missing)} "
              f"-- those values will not be written")
    return writable


def preflight(stats, args=None):
    """
    Validates the token, every schema we touch and the player mapping before
    anything is written.
    Returns (map_writable, perf_writable, index, mapped, match_writable)
    or None.
    """
    want_matches = bool(args is None or not getattr(args, "no_match", False))
    if not NOTION_TOKEN:
        print("\nNOTION_TOKEN env var is not set -- cannot write to Notion.")
        return None

    problems = []
    print("\nChecking Notion databases ...")
    try:
        map_schema = fetch_schema("Maps")
        perf_schema = fetch_schema("Performance")
        gegner_schema = fetch_schema("Gegner")
        match_schema = fetch_schema("Matches") if want_matches else {}
        index = fetch_player_index()
    except NotionError as exc:
        print(f"  {exc}")
        if exc.status in (401, 403):
            print("  -> check NOTION_TOKEN, and that the integration is added under "
                  "'...' -> Connections on Maps, Performance, Spieler, "
                  "Gegner and Matches.")
        elif exc.status == 404:
            print("  -> Notion says 'not found'. That is either a wrong id or a "
                  "missing connection, and its message cannot tell them apart.")
            print("     A lookup by title found nothing, so the integration "
                  "most likely has no access: open the database, '...' -> "
                  "Connections -> add your integration. Sharing the parent "
                  "page does NOT always cascade to databases inside it.")
            print("     Current ids: " + ", ".join(
                f"{k}={v or '(unset)'}" for k, v in NOTION_DATABASES.items()))
        return None

    map_writable = check_props(map_schema, MAP_PROPS_REQUIRED, MAP_PROPS_OPTIONAL,
                               "Maps", problems)
    perf_writable = check_props(perf_schema, PERF_PROPS_REQUIRED, PERF_PROPS_OPTIONAL,
                                "Performance", problems)
    check_props(gegner_schema, GEGNER_PROPS_REQUIRED, {}, "Gegner", problems)
    match_writable = check_props(match_schema, MATCH_PROPS_REQUIRED,
                                 MATCH_PROPS_OPTIONAL, "Matches", problems) \
        if want_matches else set()

    mapped, unmapped = resolve_players(stats, index)
    if not index:
        problems.append("the Spieler database has no usable SteamID64 values")
    elif not mapped:
        demo_names = ", ".join(sorted(p["name"] for p in stats.players.values()
                                      if p["name"]))
        problems.append(f"no player in this demo matches a Spieler row "
                        f"(demo has: {demo_names})")

    if problems:
        print("\nNotion preflight failed -- nothing was written:")
        for problem in problems:
            print(f"  - {problem}")
        return None

    if unmapped:
        print(f"  NOTE: no Spieler row for {', '.join(unmapped)} "
              f"-- no Performance row for them")
    print(f"  OK: will write 1 Maps row and {len(mapped)} Performance row(s)"
          + (", and update the Matches database." if want_matches else "."))
    return map_writable, perf_writable, index, mapped, match_writable


# --------------------------------------------------------------------------
# Page creation
# --------------------------------------------------------------------------

def find_map_page(demo_key):
    """The existing Maps row for this demo, as a full page object, or None."""
    data = notion_request(
        "POST", f"/databases/{db('Maps')}/query",
        body={"filter": {"property": "Match-Room / Demo-Link",
                         "url": {"equals": demo_key}}},
        context="looking for an existing Maps row",
    )
    results = data.get("results") or []
    return results[0] if results else None


def find_performance_pages(map_page_id):
    """Existing Performance rows for this map, keyed by their Spieler page."""
    rows, cursor = {}, None
    while True:
        body = {"filter": {"property": "Match",
                           "relation": {"contains": map_page_id}},
                "page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        data = notion_request(
            "POST", f"/databases/{db('Performance')}/query", body=body,
            context="looking for existing Performance rows")
        for page in data.get("results") or []:
            relation = (page.get("properties") or {}).get("Spieler") or {}
            for ref in relation.get("relation") or []:
                key = normalize_notion_id(ref.get("id"))
                if key:
                    rows[key] = page
        cursor = data.get("next_cursor")
        if not data.get("has_more") or not cursor:
            break
    return rows


def snapshot_properties(page, names):
    """
    The current value of the properties we are about to overwrite, in the
    shape the API accepts back, so a failed run can put them the way they
    were. A property that was empty snapshots as empty, which restores as
    empty -- that is what we want.
    """
    current = (page.get("properties") or {})
    original = {}
    for name in names:
        value = current.get(name)
        if not isinstance(value, dict):
            continue
        kind = value.get("type")
        if kind and kind in value and kind not in ("rollup", "formula"):
            original[name] = {kind: value[kind]}
    return original


def upsert_page(existing, parent_db, props, label, ledger):
    """
    Updates `existing` if there is one, otherwise creates a row. Records
    enough in `ledger` to undo either kind of change.
    Returns (page_id, "created" | "updated").
    """
    if existing:
        page_id = existing["id"]
        ledger["restore"].append((page_id, snapshot_properties(existing, props)))
        notion_request("PATCH", f"/pages/{page_id}", body={"properties": props},
                       context=f"updating {label}")
        return page_id, "updated"

    data = notion_request(
        "POST", "/pages",
        body={"parent": {"database_id": parent_db}, "properties": props},
        context=f"creating {label}")
    ledger["created"].append(data["id"])
    return data["id"], "created"


def build_map_props(stats, args, demo_key, result, team, writable,
                    gegner_page_id=None):
    map_name = map_display_name(stats.map_name)
    props = {}
    put(props, writable, "Titel", notion_title(f"{map_name} - {args.gegner}"))
    put(props, writable, "Match-Room / Demo-Link", {"url": demo_key})
    put(props, writable, "Map", notion_select(map_name))
    put(props, writable, "Typ", notion_select(args.typ))
    put(props, writable, "Ergebnis", notion_select(result))
    put(props, writable, "Liga/Turnier", notion_select(args.liga))
    if args.datum:
        put(props, writable, "Datum", {"date": {"start": args.datum}})
    if gegner_page_id:
        put(props, writable, "Gegner", {"relation": [{"id": gegner_page_id}]})
    if args.vod:
        put(props, writable, "VOD-Link", {"url": args.vod})
    for name in TEAM_STAT_PROPS:
        put(props, writable, name, notion_number(team.get(name), 4))
    return props


def build_performance_props(steamid, stats, args, map_page_id, clutches,
                            index, writable):
    p = stats.players[steamid]
    rounds = stats.rounds_total
    rates = compute_extra_rates(p, rounds, stats)
    map_name = map_display_name(stats.map_name)
    r = max(rounds, 1)

    props = {}
    put(props, writable, "Name",
        notion_title(f"{p['name']} - {args.gegner} - {map_name}"))
    put(props, writable, "Spieler", {"relation": [{"id": index[steamid]}]})
    put(props, writable, "Match", {"relation": [{"id": map_page_id}]})
    put(props, writable, "Rating", notion_number(compute_rating_2(p, rounds), 2))
    put(props, writable, "Swing", notion_number(p["swing"], 4))
    put(props, writable, "KAST", notion_number(p["rounds_with_kast"] / r, 4))
    put(props, writable, "ADR", notion_number(p["dmg_dealt"] / r, 1))
    put(props, writable, "Wasted Utility/R ($)",
        notion_number(p["wasted_util_value"] / r, 2))
    if args.datum:
        put(props, writable, "Date", {"date": {"start": args.datum}})
    for key, prop_name in PERF_RATE_PROPS.items():
        put(props, writable, prop_name, notion_number(rates[key]))

    # Win/save rate, only for situations the player actually faced, so an
    # untouched property stays empty instead of a misleading 0.
    for bucket in range(1, 6):
        wins, total = clutches.get(steamid, {}).get(bucket, [0, 0])
        if total:
            put(props, writable, f"1v{bucket} WR/SR%",
                notion_number(wins / total, 4))
    return props


def archive_pages(page_ids):
    """Undo a partially written map so a re-run starts from a clean state."""
    if not page_ids:
        return
    print(f"\nRolling back {len(page_ids)} page(s) created in this run ...")
    stuck = []
    for page_id in reversed(page_ids):
        try:
            notion_request("PATCH", f"/pages/{page_id}", body={"archived": True},
                           context=f"archiving {page_id}")
            print(f"  archived {page_id}")
        except NotionError as exc:
            stuck.append(page_id)
            print(f"  could not archive {page_id}: {exc}")
    if stuck:
        print("  -> delete these pages by hand before re-running:")
        for page_id in stuck:
            print(f"     {page_id}")


def undo(ledger):
    """Puts the databases back: created rows are archived, updated rows get
    their previous values written back."""
    for page_id, original in reversed(ledger["restore"]):
        if not original:
            continue
        try:
            notion_request("PATCH", f"/pages/{page_id}",
                           body={"properties": original},
                           context=f"restoring {page_id}")
            print(f"  restored {page_id}")
        except NotionError as exc:
            print(f"  could not restore {page_id}: {exc}")
            print("   -> Notion keeps page history; use it to revert that row.")
    archive_pages(ledger["created"])



# --------------------------------------------------------------------------
# Gegner + Matches
# --------------------------------------------------------------------------

GEGNER_PROPS_REQUIRED = {"Name": "title"}

MATCH_PROPS_REQUIRED = {"Name": "title"}
MATCH_PROPS_OPTIONAL = {
    "Gegner": "relation",
    "Datum": "date",
    "1. Map": "relation",
    "2. Map": "relation",
    "Decider": "relation",
    "Resultat": "select",
}

# A series holds three maps
MATCH_SLOTS = ("1. Map", "2. Map", "Decider")

def query_all(database, body, context):
    """Every row of a query, following pagination."""
    rows, cursor = [], None
    while True:
        page_body = dict(body, page_size=100)
        if cursor:
            page_body["start_cursor"] = cursor
        data = notion_request("POST", f"/databases/{database}/query",
                              body=page_body, context=context)
        rows.extend(data.get("results") or [])
        cursor = data.get("next_cursor")
        if not data.get("has_more") or not cursor:
            break
    return rows


def page_title(page):
    for value in (page.get("properties") or {}).values():
        if value.get("type") == "title":
            return "".join(part.get("plain_text", "")
                           for part in value.get("title") or [])
    return ""


def find_gegner_page(name):
    """
    The Gegner row for this opponent, matched on the title case-insensitively.
    DB might have "EXAMPLE TEAM" while one might type "Example Team".
    """
    wanted = safe_str(name).strip().casefold()
    if not wanted:
        return None
    for page in query_all(db("Gegner"), {}, "reading the Gegner database"):
        if page_title(page).strip().casefold() == wanted:
            return page
    return None


def resolve_gegner(args, ledger):
    """
    The Gegner page to link, creating the opponent if they are new.
    Returns (page_id, note) where note describes what happened.
    """
    if args.gegner_page:
        return normalize_notion_id(args.gegner_page), "given on the command line"

    existing = find_gegner_page(args.gegner)
    if existing:
        return existing["id"], f"matched {page_title(existing)!r}"

    data = notion_request(
        "POST", "/pages",
        body={"parent": {"database_id": db("Gegner")},
              "properties": {"Name": notion_title(args.gegner)}},
        context=f"creating the Gegner row for {args.gegner}")
    ledger["created"].append(data["id"])
    return data["id"], "created, this opponent was not in the database yet"


def match_title(args):
    """
    "<series> - <opponent>", where the series is --liga 
    unless --serie wants to overwrite it
    """
    if args.match_name:
        return args.match_name
    series = args.serie or args.liga
    if series:
        return f"{series} - {args.gegner}"
    return f"{args.gegner} - {args.datum}" if args.datum else args.gegner


def find_match_page(args, gegner_page_id):
    """An existing Match for this opponent on this date."""
    if args.match_page:
        page_id = normalize_notion_id(args.match_page)
        return notion_request("GET", f"/pages/{page_id}",
                              context="reading the Match given on the command line")
    if not gegner_page_id:
        return None
    conditions = [{"property": "Gegner",
                   "relation": {"contains": gegner_page_id}}]
    if args.datum:
        conditions.append({"property": "Datum", "date": {"equals": args.datum}})
    rows = query_all(db("Matches"), {"filter": {"and": conditions}},
                     "looking for an existing Match")
    return rows[0] if rows else None


def slot_occupant(match_page, slot):
    relation = ((match_page or {}).get("properties") or {}).get(slot) or {}
    refs = relation.get("relation") or []
    return normalize_notion_id(refs[0].get("id")) if refs else None


def choose_slot(match_page, map_page_id, forced=None):
    """
    Which "n. Map" slot this map belongs in. A map already linked keeps its
    slot, so re-importing does not shuffle a series around.
    """
    target = normalize_notion_id(map_page_id)
    for slot in MATCH_SLOTS:
        if slot_occupant(match_page, slot) == target:
            return slot, "already linked"
    if forced:
        return MATCH_SLOTS[forced - 1], "forced with --map-nr"
    for slot in MATCH_SLOTS:
        if slot_occupant(match_page, slot) is None:
            return slot, "next free slot"
    return None, "the series already has three maps"


def map_result_of(page_id, cache):
    """The Ergebnis of a Maps row, read once and remembered."""
    key = normalize_notion_id(page_id)
    if key not in cache:
        data = notion_request("GET", f"/pages/{page_id}",
                              context="reading a linked map result")
        selected = ((data.get("properties") or {}).get("Ergebnis") or {}).get("select")
        cache[key] = (selected or {}).get("name")
    return cache[key]


def series_result(match_page, slot, this_result, cache):
    """
    The standing of the whole series once this map is in it, counted over
    every slot: "1:0", "1:1", "2:1" and so on. None, only when no map in the
    series has a decided result yet, in which case Resultat is cleared rather
    than left showing an older, now wrong, standing.
    """
    results = []
    for other in MATCH_SLOTS:
        if other == slot:
            results.append(this_result)
            continue
        occupant = slot_occupant(match_page, other)
        if occupant:
            results.append(map_result_of(occupant, cache))
    wins = sum(1 for r in results if r == "Win")
    losses = sum(1 for r in results if r == "Loss")
    return f"{wins}:{losses}" if (wins or losses) else None


def link_map_to_match(args, map_page_id, result, gegner_page_id,
                      writable, ledger):
    """
    Puts this map into a Match, creating the Match if this is the first map
    of the series. Returns a short line describing what happened.
    """
    match_page = find_match_page(args, gegner_page_id)
    slot, why = choose_slot(match_page, map_page_id, args.map_nr)
    if slot is None:
        return (f"Match left alone: {why}. Pass --match-page/--map-nr to "
                f"place this map yourself.")
    if slot not in writable:
        return f"Match left alone: the Matches database has no {slot!r} property."

    props = {}
    put(props, writable, slot, {"relation": [{"id": map_page_id}]})
    if match_page is None:
        put(props, writable, "Name", notion_title(match_title(args)))
        put(props, writable, "Gegner",
            {"relation": [{"id": gegner_page_id}]} if gegner_page_id else None)
        if args.datum:
            put(props, writable, "Datum", {"date": {"start": args.datum}})

    # Always send Resultat, so a series that moves from 1:0 to 1:1 is not
    # left showing the old standing.
    standing = series_result(match_page, slot, result, {})
    if "Resultat" in writable:
        props["Resultat"] = ({"select": {"name": standing}} if standing
                             else {"select": None})

    page_id, how = upsert_page(match_page, db("Matches"), props,
                               "the Match row", ledger)
    detail = f"{slot} ({why})"
    detail += f", Resultat {standing}" if standing else ", Resultat cleared"
    return f"Match {how}: {page_id} -- {detail}"


def write_to_notion(stats, clutches, args, demo_key):
    """
    Imports the map, replacing what is already there for this demo rather
    than making a second copy of it: just in case anything ever changes here

    Either the whole map lands or none of it does, a failure archives rows
    created in this run and writes back the previous values of rows it had
    already updated.
    """
    checked = preflight(stats, args)
    if checked is None:
        return 1
    map_writable, perf_writable, index, mapped, match_writable = checked

    if args.gegner_page and not normalize_notion_id(args.gegner_page):
        print(f"\n--gegner-page is not a Notion page id: {args.gegner_page!r}")
        print("  Pass the opponent's page id or its Notion URL.")
        return 1

    try:
        existing_map = find_map_page(demo_key)
        existing_perf = (find_performance_pages(existing_map["id"])
                         if existing_map else {})
    except NotionError as exc:
        print(f"\nERROR: {exc}")
        return 1

    our_players = set(mapped)
    team = compute_team_stats(stats, our_players)
    result = team_result(stats, our_players)

    if existing_map:
        print(f"\nThis demo is already in Notion ({existing_map['id']}) -- "
              f"updating it in place ({len(existing_perf)} Performance row(s) found).")

    ledger = {"created": [], "restore": []}
    print("\nWriting to Notion ...")
    try:
        gegner_page_id, note = resolve_gegner(args, ledger)
        print(f"  Gegner: {gegner_page_id} -- {note}")

        props = build_map_props(stats, args, demo_key, result, team, map_writable,
                                gegner_page_id)
        map_page_id, how = upsert_page(existing_map, db("Maps"), props,
                                       "the Maps row", ledger)
        print(f"  Maps row {how}: {map_page_id}")

        for steamid in mapped:
            name = stats.players[steamid]["name"]
            props = build_performance_props(steamid, stats, args, map_page_id,
                                            clutches, index, perf_writable)
            existing_row = existing_perf.get(normalize_notion_id(index[steamid]))
            _, how = upsert_page(existing_row, db("Performance"), props,
                                 f"the Performance row for {name}", ledger)
            print(f"  Performance {how} for {name}")

        if not args.no_match:
            print("  " + link_map_to_match(args, map_page_id, result,
                                           gegner_page_id, match_writable, ledger))
    except (NotionError, KeyboardInterrupt) as exc:
        print(f"\nERROR: {exc}" if isinstance(exc, NotionError) else "\nInterrupted.")
        print("Undoing this run ...")
        undo(ledger)
        return 1

    print(f"\nDone: {len(ledger['created'])} row(s) created, "
          f"{len(ledger['restore'])} updated.")
    return 0


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def print_report(stats, clutches):
    rounds = stats.rounds_total
    print(f"Map: {stats.map_name}, rounds: {rounds}")
    if len(stats.team_score) == 2:
        score = " : ".join(str(v) for _, v in sorted(stats.team_score.items()))
        print(f"Final score (team 2 : team 3): {score}")

    print("\n--- Computed stats (per player) ---")
    ordered = sorted(
        (sid for sid, p in stats.players.items() if p["name"]),
        key=lambda sid: -stats.players[sid]["kills"],
    )
    for steamid in ordered:
        p = stats.players[steamid]
        rates = compute_extra_rates(p, rounds, stats)
        parts = []
        for n in range(1, 6):
            w, t = clutches.get(steamid, {}).get(n, [0, 0])
            if t:
                parts.append(f"1v{n} {w}/{t}")
        clutch_txt = "  ".join(parts) if parts else "no clutch situations"
        print(
            f"{p['name']:15s} | K {p['kills']:2d} D {p['deaths']:2d} A {p['assists']:2d} "
            f"| ADR {p['dmg_dealt'] / max(rounds, 1):5.1f} "
            f"| KAST {p['rounds_with_kast'] / max(rounds, 1) * 100:4.0f}% "
            f"| Rating {compute_rating_2(p, rounds):.2f} "
            f"| Swing {p['swing'] * 100:+.2f}%"
        )
        print(
            f"{'':15s} | K/R {rates['K/R']:.2f} FK/R {rates['FK/R']:.2f} "
            f"FD/R {rates['FD/R']:.2f} UTD/R {rates['UTD/R']:.2f} "
            f"UD/R {rates['UD/R']:.2f} LMS/R {rates['LMS/R']:.2f} "
            f"EF/R {rates['EF/R']:.2f} FLTK/R {rates['FLTK/R']:.2f}"
        )
        print(
            f"{'':15s} | EF {enemies_flashed(p, stats):3d}  util dmg {p['util_dmg_dealt']:.0f}  "
            f"wasted util ${p['wasted_util_value'] / max(rounds, 1):.0f}/R "
            f"(${p['wasted_util_value']} total)  | {clutch_txt}"
        )


def print_clutch_detail(stats, clutches):
    """Every 1vX situation with its outcome, so one can eyeball it against
    the demo. `saved` = the clutcher survived but the team lost the round;
    that still counts as a won clutch."""
    print("\n--- Clutch situations (round by round) ---")
    rows = []
    deaths_by_round = defaultdict(set)
    for k in stats.kill_events:
        if k["victim"]:
            deaths_by_round[k["round"]].add(k["victim"])
    for sid, p in stats.players.items():
        for rnd, enemies, side, _tick in p.get("lms_events", []):
            survived = sid not in deaths_by_round.get(rnd, set())
            won_round = stats.round_winner.get(rnd) == side
            outcome = "won" if won_round else ("saved" if survived else "lost")
            rows.append((rnd, p["name"] or sid, enemies, side, outcome))
    if not rows:
        print("  (none detected)")
        return
    for rnd, name, enemies, side, outcome in sorted(rows):
        print(f"  round {rnd:2d}  {name:15s} 1v{enemies} as {side:2s} -> {outcome}")
    print("\n  totals per player (saves count as won):")
    for sid, p in stats.players.items():
        parts = [f"1v{n} {w}/{t}" for n in range(1, 6)
                 for w, t in [clutches.get(sid, {}).get(n, [0, 0])] if t]
        if parts:
            print(f"    {p['name'] or sid:15s} " + "  ".join(parts))


TEAM_STAT_BUCKETS = {
    "CT%": "CT", "T%": "T", "Pistol WR": "pistol", "Eco WR": "eco",
    "Half-Buy WR": "half_buy", "Buy WR": "buy", "5v4 WR": "5v4",
    "4v5 WR": "4v5", "Conv WR": "conversion",
}


def print_team_stats(stats):
    """Both teams, so the Maps numbers can be checked without touching Notion."""
    teams = teams_in_demo(stats)
    if not teams:
        return
    print("\n--- Team stats (the Maps row) ---")
    for team_num, members in sorted(teams.items()):
        team = compute_team_stats(stats, members)
        if not team:
            continue
        names = ", ".join(sorted(stats.players[sid]["name"] for sid in members))
        counts = team["_counts"]
        print(f"  {names}")
        print(f"    record {team['wins']}-{team['rounds'] - team['wins']}")
        line = []
        for key in TEAM_STAT_PROPS:
            value = team.get(key)
            if value is None:
                line.append(f"{key} -")
                continue
            bucket = TEAM_STAT_BUCKETS.get(key)
            detail = ""
            if bucket and bucket in counts:
                detail = f" ({counts[bucket][0]}/{counts[bucket][1]})"
            line.append(f"{key} {value * 100:.1f}%{detail}")
        for i in range(0, len(line), 3):
            print("    " + "   ".join(line[i:i + 3]))


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--demo", required=True, help="Path to the .dem file")
    ap.add_argument("--gegner", required=True, help="Opponent name, e.g. 'Example Team'")
    ap.add_argument("--liga", default=None, help="League/tournament")
    ap.add_argument("--typ", default="Scrim", choices=["FaceIT", "Scrim", "Official"])
    ap.add_argument("--demo-link", default=None,
                    help="FaceIT/match-room link (fallback: hash of the demo file)")
    ap.add_argument("--datum", default=None,
                    help="Match date YYYY-MM-DD (default: the demo file date)")
    ap.add_argument("--gegner-page", default=None,
                    help="Notion page id of the opponent row in the Gegner DB")
    ap.add_argument("--vod", default=None, help="VOD link for the Maps row")
    ap.add_argument("--serie", default=None,
                    help="Series label for the Match title, when it is spelled "
                         "differently from --liga (default: --liga)")
    ap.add_argument("--match-name", default=None,
                    help="Exact title for an auto-created Match (overrides --serie)")
    ap.add_argument("--match-page", default=None,
                    help="Attach this map to this specific Match row")
    ap.add_argument("--map-nr", type=int, choices=[1, 2, 3], default=None,
                    help="Force the series slot: 1. Map, 2. Map or Decider")
    ap.add_argument("--no-match", action="store_true",
                    help="Do not touch the Matches database")
    ap.add_argument("--dry-run", action="store_true",
                    help="Only compute & print, do not write to Notion")
    ap.add_argument("--debug-clutches", action="store_true",
                    help="Print every 1vX situation with its outcome")
    ap.add_argument("--check", action="store_true",
                    help="Validate the Notion token, player mapping and database "
                         "schemas, then exit without writing anything")
    args = ap.parse_args()

    if not args.datum:
        args.datum = date.fromtimestamp(os.path.getmtime(args.demo)).isoformat()

    with open(args.demo, "rb") as f:
        demo_key = args.demo_link or ("sha1:" + hashlib.sha1(f.read()).hexdigest()[:16])

    print(f"Parsing {args.demo} ...")
    stats = parse_demo(args.demo)
    clutches = compute_clutch_winrates(stats)
    print_report(stats, clutches)
    print_team_stats(stats)
    if args.debug_clutches:
        print_clutch_detail(stats, clutches)

    if args.dry_run:
        print("\n[--dry-run] No Notion write performed.")
        return 0

    if args.check:
        return 0 if preflight(stats, args) else 1

    return write_to_notion(stats, clutches, args, demo_key)


if __name__ == "__main__":
    sys.exit(main())
