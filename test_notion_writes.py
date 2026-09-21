"""Exercises the Notion write path against a fake transport. No network."""
import importlib.util
import json
import os
import sys

import requests

_HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "dtn", os.path.join(_HERE, "demoToNotion.py"))
dtn = importlib.util.module_from_spec(spec)
sys.modules["dtn"] = dtn
spec.loader.exec_module(dtn)

dtn.NOTION_TOKEN = "secret_test"
dtn.time.sleep = lambda s: None          # no real waiting
dtn.NOTION_MIN_INTERVAL = 0.0

PASS, FAIL = [], []
DEFAULT_DB_IDS = dict(dtn.NOTION_DATABASES)

# the data-source / collection ids that used to be configured by mistake
COLLECTION_IDS = {
    "3e1d7b80-7f4d-8063-95b1-000b1069a2e0",   # Maps data source
    "3e1d7b80-7f4d-806c-b771-000ba3637f34",   # Performance data source
    "3e1d7b80-7f4d-807e-afe2-000b959ab644",   # Spieler data source
}

OURS = ("76561198025883289", "76561198083343081")      # CampeR_, VitaXl
THEIRS = ("76561198143686070", "76561199229201679")    # fynux, ELIaaas007
PAGE = {OURS[0]: "11111111-1111-4111-8111-111111111111",
        OURS[1]: "22222222-2222-4222-8222-222222222222"}
GEGNER_PAGE = "33333333-3333-4333-8333-333333333333"
MATCH_PAGE = "44444444-4444-4444-8444-444444444444"
MAP_PAGE = "55555555-5555-4555-8555-555555555555"


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name
          + (("  " + extra) if extra and not cond else ""))


class Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.content = json.dumps(self._body).encode()
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


NOT_FOUND = {"code": "object_not_found",
             "message": "Could not find database with ID: x. Make sure the "
                        "relevant pages and databases are shared with your "
                        "integration."}


class ScriptedSession:
    """Replays a fixed list of responses. Used for transport-level tests."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def request(self, method, url, headers=None, json=None, timeout=None):
        self.calls.append((method, url, json, timeout))
        nxt = self.script.pop(0) if self.script else Resp(200, {"id": "auto"})
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


class RoutedSession:
    """
    Answers by route rather than by position, so a test does not have to
    predict how many calls the code makes or in what order.
    """

    def __init__(self, schemas=None, rows=None, search=None, pages=None, fail=None):
        self.schemas = schemas if schemas is not None else dict(DEFAULT_SCHEMAS)
        self.rows = rows or {}
        self.search = search or []
        self.pages = pages or {}
        self.fail = fail
        self.calls = []
        self.created = []
        self.patched = []
        self._n = 0

    def request(self, method, url, headers=None, json=None, timeout=None):
        body = json
        self.calls.append((method, url, body, timeout))
        if self.fail:
            forced = self.fail(method, url, body)
            if forced is not None:
                return forced
        path = url.split("/v1", 1)[-1]

        if method == "GET" and path.startswith("/databases/"):
            props = self.schemas.get(path.rsplit("/", 1)[-1])
            if props is None:
                return Resp(404, NOT_FOUND)
            return Resp(200, {"properties": {k: {"type": v}
                                             for k, v in props.items()}})

        if method == "POST" and path.endswith("/query"):
            db_id = path.split("/databases/", 1)[-1].split("/", 1)[0]
            return Resp(200, {"results": list(self.rows.get(db_id, [])),
                              "has_more": False, "next_cursor": None})

        if method == "POST" and path == "/search":
            return Resp(200, {"results": self.search})

        if method == "GET" and path.startswith("/pages/"):
            page_id = path.rsplit("/", 1)[-1]
            return Resp(200, self.pages.get(page_id, {"id": page_id, "properties": {}}))

        if method == "POST" and path == "/pages":
            self._n += 1
            self.created.append((body.get("parent", {}).get("database_id"), body))
            return Resp(200, {"id": f"new-{self._n}"})

        if method == "PATCH" and path.startswith("/pages/"):
            page_id = path.rsplit("/", 1)[-1]
            self.patched.append((page_id, body))
            return Resp(200, {"id": page_id})

        return Resp(200, {})

    def posted_to(self, db_id):
        return [b for parent, b in self.created if parent == db_id]

    def patches_of(self, page_id):
        return [b for pid, b in self.patched if pid == page_id]


def use(session):
    dtn._SESSION = session
    dtn._LAST_CALL = 0.0
    return session


def scripted(script):
    return use(ScriptedSession(script))


GOOD_MAP = {**dtn.MAP_PROPS_REQUIRED, **dtn.MAP_PROPS_OPTIONAL}
GOOD_PERF = {**dtn.PERF_PROPS_REQUIRED, **dtn.PERF_PROPS_OPTIONAL, "Map": "rollup"}
GOOD_GEGNER = {**dtn.GEGNER_PROPS_REQUIRED, "Avg. Elo": "number"}
GOOD_MATCH = {**dtn.MATCH_PROPS_REQUIRED, **dtn.MATCH_PROPS_OPTIONAL,
              "1. Ergebnis": "rollup"}
GOOD_SPIELER = {"Name": "title", "SteamID64": "rich_text"}

DEFAULT_SCHEMAS = {
    DEFAULT_DB_IDS["Maps"]: GOOD_MAP,
    DEFAULT_DB_IDS["Performance"]: GOOD_PERF,
    DEFAULT_DB_IDS["Gegner"]: GOOD_GEGNER,
    DEFAULT_DB_IDS["Matches"]: GOOD_MATCH,
    DEFAULT_DB_IDS["Spieler"]: GOOD_SPIELER,
}


def spieler_rows(as_number=False, ids=PAGE):
    rows = []
    for sid, page in ids.items():
        prop = ({"type": "number", "number": float(sid)} if as_number
                else {"type": "rich_text", "rich_text": [{"plain_text": sid}]})
        rows.append({"id": page, "properties": {"SteamID64": prop}})
    return rows


def title_prop(text):
    return {"type": "title",
            "title": [{"plain_text": text, "type": "text",
                       "text": {"content": text}}]}


def gegner_row(name, page_id=GEGNER_PAGE):
    return {"id": page_id, "properties": {"Name": title_prop(name)}}


def relation_prop(page_id):
    return {"type": "relation", "relation": [{"id": page_id}] if page_id else []}


def base_rows(spieler=None, gegner=None, maps=None, perf=None, matches=None):
    return {
        DEFAULT_DB_IDS["Spieler"]: spieler if spieler is not None else spieler_rows(),
        DEFAULT_DB_IDS["Gegner"]: gegner or [],
        DEFAULT_DB_IDS["Maps"]: maps or [],
        DEFAULT_DB_IDS["Performance"]: perf or [],
        DEFAULT_DB_IDS["Matches"]: matches or [],
    }


def demo_stats():
    st = dtn.MapStats()
    st.map_name = "de_ancient"
    st.rounds_total = 2
    st.has_scoreboard = True
    st.team_score = {2: 1, 3: 1}
    names = {OURS[0]: ("CampeR_", 2), OURS[1]: ("VitaXl", 2),
             THEIRS[0]: ("fynux", 3), THEIRS[1]: ("ELIaaas007", 3)}
    for sid, (name, team) in names.items():
        p = st.player(sid, name)
        p.update(kills=10, deaths=8, assists=2, dmg_dealt=216.0, util_dmg_dealt=20.0,
                 enemies_flashed=4, rounds_with_kast=1, swing=0.0467,
                 wasted_util_value=3400, first_kills=1, first_deaths=0,
                 untradable_deaths=1, ef_count=4, fltk_count=1)
        st.final_team_num[sid] = team
    side = {OURS[0]: "CT", OURS[1]: "CT", THEIRS[0]: "T", THEIRS[1]: "T"}
    st.roster = {1: dict(side), 2: dict(side)}
    st.round_winner = {1: "CT", 2: "CT"}
    st.pistol_rounds = {1}
    for rnd in (1, 2):
        st.equip[rnd]["CT"] = 20000
        st.equip[rnd]["T"] = 20000
        st.equip_players[rnd]["CT"] = 2
        st.equip_players[rnd]["T"] = 2
    st.kill_events = [
        {"round": 1, "tick": 100, "attacker": OURS[0], "victim": THEIRS[0],
         "attacker_side": "CT", "victim_side": "T", "is_teamkill": False},
        {"round": 2, "tick": 200, "attacker": THEIRS[0], "victim": OURS[0],
         "attacker_side": "T", "victim_side": "CT", "is_teamkill": False},
    ]
    return st


class Args:
    def __init__(self, **kw):
        self.gegner = "Raczon Academy"
        self.liga = "DACH S6 Spk5 - Phase 1"
        self.typ = "Official"
        self.datum = "2026-09-20"
        self.gegner_page = None
        self.vod = "https://example.com/vod"
        self.serie = "DACH S6 - Spk5"
        self.match_name = None
        self.match_page = None
        self.map_nr = None
        self.no_match = False
        self.__dict__.update(kw)


CLUTCHES = {OURS[0]: {1: [1, 1], 2: [1, 2], 3: [0, 1], 4: [0, 0], 5: [0, 0]}}
st = demo_stats()

# ---------------------------------------------------------------- transport
print("\n[transport]")

s = scripted([Resp(429, {}, {"Retry-After": "2"}), Resp(200, {"id": "ok"})])
check("429 is retried and then succeeds",
      dtn.notion_request("GET", "/x") == {"id": "ok"} and len(s.calls) == 2)

s = scripted([Resp(502, {}), Resp(503, {}), Resp(200, {"id": "ok"})])
dtn.notion_request("GET", "/x")
check("5xx is retried", len(s.calls) == 3)

s = scripted([requests.ConnectionError("boom"), Resp(200, {"id": "ok"})])
dtn.notion_request("GET", "/x")
check("network error is retried", len(s.calls) == 2)

s = scripted([Resp(400, {"code": "validation_error",
                         "message": "Swing is not a property"})])
try:
    dtn.notion_request("POST", "/pages", context="creating the Maps row")
    check("permanent 4xx raises", False)
except dtn.NotionError as exc:
    check("permanent 4xx raises NotionError, not retried", len(s.calls) == 1)
    check("error carries Notion reason",
          "validation_error" in str(exc) and "Swing is not a property" in str(exc)
          and "creating the Maps row" in str(exc), str(exc))

s = scripted([Resp(500, {})] * dtn.NOTION_MAX_ATTEMPTS)
try:
    dtn.notion_request("GET", "/x")
    check("retries are bounded", False)
except dtn.NotionError:
    check("retries are bounded", len(s.calls) == dtn.NOTION_MAX_ATTEMPTS)

s = scripted([Resp(200, {"id": "ok"})])
dtn.notion_request("GET", "/x")
check("every request has a timeout", s.calls[0][3] == dtn.NOTION_TIMEOUT)

# ------------------------------------------------------------ value builders
print("\n[values]")
check("NaN is dropped", dtn.notion_number(float("nan")) is None)
check("Infinity is dropped", dtn.notion_number(float("inf")) is None)
check("None is dropped", dtn.notion_number(None) is None)
check("number is rounded", dtn.notion_number(1.23456, 2) == {"number": 1.23})
check("title is clamped to 2000 chars",
      len(dtn.notion_title("x" * 5000)["title"][0]["text"]["content"]) == 2000)
check("empty select is dropped", dtn.notion_select(None) is None)
check("de_ancient maps to the Ancient select option",
      dtn.map_display_name("de_ancient") == "Ancient")
check("an unknown map still loses its de_ prefix",
      dtn.map_display_name("de_train") == "Train")

props = {}
dtn.put(props, {"A"}, "A", {"number": 1})
dtn.put(props, {"A"}, "B", {"number": 2})
dtn.put(props, {"A", "C"}, "C", None)
check("put() filters unknown and empty properties", props == {"A": {"number": 1}})

# ---------------------------------------------------------------- preflight
print("\n[preflight]")

use(RoutedSession(rows=base_rows()))
res = dtn.preflight(st, Args())
check("clean schema passes", res is not None and sorted(res[3]) == sorted(OURS))
check("the Map rollup is never writable", res is not None and "Map" not in res[1])

use(RoutedSession(
    schemas=dict(DEFAULT_SCHEMAS,
                 **{DEFAULT_DB_IDS["Performance"]:
                    {k: v for k, v in GOOD_PERF.items() if k != "Swing"}}),
    rows=base_rows()))
res = dtn.preflight(st, Args())
check("missing optional property degrades instead of failing",
      res is not None and "Swing" not in res[1] and "ADR" in res[1])

use(RoutedSession(
    schemas=dict(DEFAULT_SCHEMAS,
                 **{DEFAULT_DB_IDS["Performance"]: dict(GOOD_PERF, ADR="rich_text")}),
    rows=base_rows()))
res = dtn.preflight(st, Args())
check("wrong-typed optional property is skipped, not written",
      res is not None and "ADR" not in res[1])

use(RoutedSession(
    schemas=dict(DEFAULT_SCHEMAS,
                 **{DEFAULT_DB_IDS["Performance"]:
                    {k: v for k, v in GOOD_PERF.items() if k != "Name"}}),
    rows=base_rows()))
check("missing required property aborts", dtn.preflight(st, Args()) is None)

use(ScriptedSession([Resp(401, {"code": "unauthorized",
                                "message": "API token is invalid."})]))
check("bad token aborts without writing", dtn.preflight(st, Args()) is None)

use(RoutedSession(rows=base_rows(spieler=[])))
check("empty Spieler database aborts", dtn.preflight(st, Args()) is None)

use(RoutedSession(rows=base_rows(
    spieler=spieler_rows(ids={"76561190000000000": "page-nobody"}))))
check("no matching player aborts", dtn.preflight(st, Args()) is None)

use(RoutedSession(rows=base_rows(spieler=spieler_rows(as_number=True))))
res = dtn.preflight(st, Args())
check("float-rounded SteamID64 numbers do not silently match",
      res is None or sorted(res[3]) != sorted(OURS))

s = use(RoutedSession(rows=base_rows()))
dtn.preflight(st, Args(no_match=True))
check("--no-match does not require the Matches schema",
      not any(c[1].endswith(DEFAULT_DB_IDS["Matches"]) for c in s.calls))

# ------------------------------------------------------------------- write
print("\n[write]")
args = Args()

s = use(RoutedSession(rows=base_rows(gegner=[gegner_row("RACZON ACADEMY")])))
rc = dtn.write_to_notion(st, CLUTCHES, args, "sha1:abc")
check("fresh import returns 0", rc == 0)
map_body = s.posted_to(dtn.db("Maps"))
perf_body = s.posted_to(dtn.db("Performance"))
check("one Maps row and one row per mapped player",
      len(map_body) == 1 and len(perf_body) == 2)

map_props = map_body[0]["properties"]
check("Map is sent as a select named Ancient",
      map_props["Map"] == {"select": {"name": "Ancient"}})
check("Ergebnis uses a real option",
      map_props["Ergebnis"]["select"]["name"] in ("Win", "Loss", "Draw"))
check("Datum is sent as a date",
      map_props["Datum"] == {"date": {"start": "2026-09-20"}})
check("VOD-Link is sent when given", map_props["VOD-Link"]["url"] == args.vod)
check("team rates are fractions, not percentages",
      all(0.0 <= map_props[k]["number"] <= 1.0
          for k in ("CT%", "T%", "Buy WR", "KAST") if k in map_props))

perf_props = perf_body[0]["properties"]
check("no NaN reaches Notion",
      "NaN" not in json.dumps(perf_body[0])
      and "Infinity" not in json.dumps(perf_body[0]))
check("ADR is the value we computed", perf_props["ADR"]["number"] == 108.0)
check("Swing is a fraction under the new name", perf_props["Swing"]["number"] == 0.0467)
check("Wasted Utility/R ($) is per round, not the map total",
      perf_props["Wasted Utility/R ($)"]["number"] == 3400 / st.rounds_total,
      str(perf_props.get("Wasted Utility/R ($)")))
check("the old total-value property name is no longer written",
      "Wasted Utility ($)" not in perf_props)
check("the Performance row carries the match Date",
      perf_props["Date"] == {"date": {"start": "2026-09-20"}})
check("rates use the renamed properties",
      "Kills/R" in perf_props and "Utility Damage/R" in perf_props
      and "Enemies Flashed/R" in perf_props and "K/R" not in perf_props)
check("Spieler relation points at the Spieler page",
      perf_props["Spieler"]["relation"][0]["id"] in PAGE.values())
check("the Map rollup is never written", "Map" not in perf_props)

print("\n[Gegner autofill]")
check("the Gegner relation is filled from the opponent name",
      map_props["Gegner"] == {"relation": [{"id": GEGNER_PAGE}]},
      str(map_props.get("Gegner")))
check("the lookup is case-insensitive (RACZON ACADEMY vs Raczon Academy)",
      not s.posted_to(dtn.db("Gegner")))

s = use(RoutedSession(rows=base_rows(gegner=[gegner_row("Some Other Team")])))
dtn.write_to_notion(st, CLUTCHES, Args(), "sha1:abc")
created_gegner = s.posted_to(dtn.db("Gegner"))
check("an unknown opponent gets a Gegner row created", len(created_gegner) == 1)
check("the new Gegner row carries the name",
      created_gegner[0]["properties"]["Name"]["title"][0]["text"]["content"]
      == "Raczon Academy")

s = use(RoutedSession(rows=base_rows(gegner=[gegner_row("RACZON ACADEMY")])))
dtn.write_to_notion(st, CLUTCHES, Args(gegner_page=GEGNER_PAGE), "sha1:abc")
check("--gegner-page skips the lookup entirely",
      not any("/query" in c[1] and dtn.db("Gegner") in c[1] for c in s.calls))

print("\n[Matches]")
s = use(RoutedSession(rows=base_rows(gegner=[gegner_row("RACZON ACADEMY")])))
dtn.write_to_notion(st, CLUTCHES, Args(), "sha1:abc")
match_body = s.posted_to(dtn.db("Matches"))
check("a Match is created when none exists", len(match_body) == 1)
mp = match_body[0]["properties"]
check("the Match is titled from --serie",
      mp["Name"]["title"][0]["text"]["content"] == "DACH S6 - Spk5 - Raczon Academy",
      str(mp["Name"]))
check("the Match links the Gegner", mp["Gegner"] == {"relation": [{"id": GEGNER_PAGE}]})
check("the first map lands in '1. Map'", "1. Map" in mp and "2. Map" not in mp)
check("the Match carries the date", mp["Datum"] == {"date": {"start": "2026-09-20"}})
check("a single won map reads 1:0", mp["Resultat"] == {"select": {"name": "1:0"}})

existing_match = {
    "id": MATCH_PAGE,
    "properties": {
        "Gegner": relation_prop(GEGNER_PAGE),
        "1. Map": relation_prop(MAP_PAGE),
        "2. Map": relation_prop(None),
        "Decider": relation_prop(None),
    },
}
s = use(RoutedSession(
    rows=base_rows(gegner=[gegner_row("RACZON ACADEMY")], matches=[existing_match]),
    pages={MAP_PAGE: {"id": MAP_PAGE,
                      "properties": {"Ergebnis": {"type": "select",
                                                  "select": {"name": "Loss"}}}}}))
dtn.write_to_notion(st, CLUTCHES, Args(), "sha1:abc")
check("an existing Match is reused, not duplicated", not s.posted_to(dtn.db("Matches")))
patch = s.patches_of(MATCH_PAGE)
check("the second map lands in '2. Map'",
      bool(patch) and "2. Map" in patch[0]["properties"]
      and "1. Map" not in patch[0]["properties"], str(patch))
check("a series standing at 1:1 is written, not skipped",
      patch[0]["properties"]["Resultat"] == {"select": {"name": "1:1"}},
      str(patch[0]["properties"].get("Resultat")))
check("Resultat is always sent, so it can never go stale",
      "Resultat" in patch[0]["properties"])

# decider: map 1 won, map 2 lost, this one won -> 2:1
decider_match = {
    "id": MATCH_PAGE,
    "properties": {
        "Gegner": relation_prop(GEGNER_PAGE),
        "1. Map": relation_prop("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        "2. Map": relation_prop("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        "Decider": relation_prop(None),
    },
}


def ergebnis_page(page_id, value):
    return {"id": page_id,
            "properties": {"Ergebnis": {"type": "select", "select": {"name": value}}}}


s = use(RoutedSession(
    rows=base_rows(gegner=[gegner_row("RACZON ACADEMY")], matches=[decider_match]),
    pages={"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa": ergebnis_page("a", "Win"),
           "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb": ergebnis_page("b", "Loss")}))
dtn.write_to_notion(st, CLUTCHES, Args(), "sha1:abc")
decided = s.patches_of(MATCH_PAGE)[0]["properties"]
check("the decider updates Resultat to 2:1",
      decided["Resultat"] == {"select": {"name": "2:1"}}, str(decided.get("Resultat")))
check("the decider lands in the Decider slot", "Decider" in decided)

check("a series with no decided map clears Resultat instead of leaving it stale",
      dtn.series_result({"properties": {}}, "1. Map", None, {}) is None)
check("a lost opener reads 0:1",
      dtn.series_result({"properties": {}}, "1. Map", "Loss", {}) == "0:1")
_PRIOR = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
check("two wins read 2:0",
      dtn.series_result({"properties": {"1. Map": relation_prop(_PRIOR)}},
                        "2. Map", "Win", {_PRIOR: "Win"}) == "2:0")
check("a drawn map counts for neither side",
      dtn.series_result({"properties": {"1. Map": relation_prop(_PRIOR)}},
                        "2. Map", "Win", {_PRIOR: "Draw"}) == "1:0")

full_match = {
    "id": MATCH_PAGE,
    "properties": {
        "Gegner": relation_prop(GEGNER_PAGE),
        "1. Map": relation_prop("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        "2. Map": relation_prop("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        "Decider": relation_prop("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
    },
}
s = use(RoutedSession(
    rows=base_rows(gegner=[gegner_row("RACZON ACADEMY")], matches=[full_match])))
rc = dtn.write_to_notion(st, CLUTCHES, Args(), "sha1:abc")
check("a full series is left alone rather than overwritten",
      rc == 0 and not s.patches_of(MATCH_PAGE))

s = use(RoutedSession(rows=base_rows(gegner=[gegner_row("RACZON ACADEMY")])))
dtn.write_to_notion(st, CLUTCHES, Args(map_nr=3), "sha1:abc")
mp = s.posted_to(dtn.db("Matches"))[0]["properties"]
check("--map-nr 3 uses the Decider slot", "Decider" in mp and "1. Map" not in mp)

s = use(RoutedSession(rows=base_rows(gegner=[gegner_row("RACZON ACADEMY")])))
dtn.write_to_notion(st, CLUTCHES, Args(no_match=True), "sha1:abc")
check("--no-match leaves the Matches database untouched",
      not s.posted_to(dtn.db("Matches"))
      and not any(dtn.db("Matches") in c[1] for c in s.calls))

s = use(RoutedSession(rows=base_rows(gegner=[gegner_row("RACZON ACADEMY")])))
dtn.write_to_notion(st, CLUTCHES, Args(match_name="Custom Title"), "sha1:abc")
check("--match-name overrides --serie",
      s.posted_to(dtn.db("Matches"))[0]["properties"]["Name"]["title"][0]
      ["text"]["content"] == "Custom Title")

s = use(RoutedSession(rows=base_rows(gegner=[gegner_row("RACZON ACADEMY")])))
dtn.write_to_notion(st, CLUTCHES, Args(serie=None), "sha1:abc")
check("without --serie the Match is titled from --liga, not the date",
      s.posted_to(dtn.db("Matches"))[0]["properties"]["Name"]["title"][0]
      ["text"]["content"] == "DACH S6 Spk5 - Phase 1 - Raczon Academy",
      s.posted_to(dtn.db("Matches"))[0]["properties"]["Name"]["title"][0]
      ["text"]["content"])

s = use(RoutedSession(rows=base_rows(gegner=[gegner_row("RACZON ACADEMY")])))
dtn.write_to_notion(st, CLUTCHES, Args(serie=None, liga=None), "sha1:abc")
check("with neither the title falls back to opponent + date",
      s.posted_to(dtn.db("Matches"))[0]["properties"]["Name"]["title"][0]
      ["text"]["content"] == "Raczon Academy - 2026-09-20")

print("\n[re-import / upsert]")
EXISTING_MAP = {
    "id": "map-old",
    "properties": {
        "Titel": title_prop("Ancient - Old"),
        "Ergebnis": {"type": "select", "select": {"name": "Win"}},
        "CT%": {"type": "number", "number": 0.25},
        "Map": {"type": "select", "select": None},
    },
}
EXISTING_PERF = {
    "id": "perf-old",
    "properties": {
        "Spieler": relation_prop(PAGE[OURS[0]]),
        "ADR": {"type": "number", "number": 55.5},
    },
}

s = use(RoutedSession(rows=base_rows(gegner=[gegner_row("RACZON ACADEMY")],
                                     maps=[EXISTING_MAP], perf=[EXISTING_PERF])))
rc = dtn.write_to_notion(st, CLUTCHES, Args(), "sha1:abc")
check("re-import succeeds", rc == 0)
check("the existing Maps row is updated, not duplicated",
      not s.posted_to(dtn.db("Maps")) and bool(s.patches_of("map-old")))
check("the matching Performance row is patched, not duplicated",
      bool(s.patches_of("perf-old")))
check("a player with no existing row still gets one created",
      len(s.posted_to(dtn.db("Performance"))) == 1)
check("the refreshed Maps row carries the new values",
      s.patches_of("map-old")[0]["properties"]["Map"] == {"select": {"name": "Ancient"}})


def fail_perf(method, url, body):
    if method == "PATCH" and url.endswith("/pages/perf-old"):
        return Resp(400, {"code": "validation_error", "message": "nope"})
    return None


s = use(RoutedSession(rows=base_rows(gegner=[gegner_row("RACZON ACADEMY")],
                                     maps=[EXISTING_MAP], perf=[EXISTING_PERF]),
                      fail=fail_perf))
rc = dtn.write_to_notion(st, CLUTCHES, Args(), "sha1:abc")
check("a failed re-import returns non-zero", rc == 1)
restores = s.patches_of("map-old")
check("the updated row is written back, not left half-changed", len(restores) == 2,
      str(len(restores)))
restored = restores[-1]["properties"]
check("restore puts back the previous values",
      restored["CT%"] == {"number": 0.25}
      and restored["Ergebnis"] == {"select": {"name": "Win"}}, str(restored))
check("a property that was empty restores as empty",
      restored["Map"] == {"select": None}, str(restored.get("Map")))
check("rollups are never restored", "Map" not in dtn.snapshot_properties(
      {"properties": {"Map": {"type": "rollup", "rollup": {"number": 1}}}}, ["Map"]))


def fail_match(method, url, body):
    if method == "POST" and url.endswith("/pages") \
            and body.get("parent", {}).get("database_id") == dtn.db("Matches"):
        return Resp(400, {"code": "validation_error", "message": "nope"})
    return None


s = use(RoutedSession(rows=base_rows(gegner=[gegner_row("Some Other Team")]),
                      fail=fail_match))
rc = dtn.write_to_notion(st, CLUTCHES, Args(), "sha1:abc")
check("a failed Match step rolls the whole import back", rc == 1)
archived = [b for pid, b in s.patched if b == {"archived": True}]
check("the auto-created Gegner row is archived too", len(archived) >= 3, str(len(archived)))

# ---------------------------------------------------------------- db ids
print("\n[database ids]")
check("shipped ids are database ids, not data-source ids",
      not (set(DEFAULT_DB_IDS.values()) & COLLECTION_IDS))
check("every shipped id is a well-formed uuid",
      all(len(v) == 36 and v.count("-") == 4 for v in DEFAULT_DB_IDS.values()))
check("a Notion URL is accepted as an id",
      dtn.normalize_notion_id(
          "https://www.notion.so/ws/Maps-3e1d7b807f4d80de9636c242e3f87d05?v=abc")
      == "3e1d7b80-7f4d-80de-9636-c242e3f87d05")
check("an undashed id is accepted",
      dtn.normalize_notion_id("3e1d7b807f4d80de9636c242e3f87d05")
      == "3e1d7b80-7f4d-80de-9636-c242e3f87d05")
check("a dashed id survives unchanged",
      dtn.normalize_notion_id("3E1D7B80-7F4D-80DE-9636-C242E3F87D05")
      == "3e1d7b80-7f4d-80de-9636-c242e3f87d05")
check("junk is rejected rather than half-parsed",
      dtn.normalize_notion_id("not-an-id") == "")

dtn.NOTION_DATABASES["Maps"] = "3e1d7b80-7f4d-8063-95b1-000b1069a2e0"
s = scripted([Resp(404, NOT_FOUND),
              Resp(200, {"results": [{"id": DEFAULT_DB_IDS["Maps"],
                                      "title": [{"plain_text": "Maps"}]}]}),
              Resp(200, {"properties": {k: {"type": v}
                                        for k, v in GOOD_MAP.items()}})])
sch = dtn.fetch_schema("Maps")
check("a data-source id 404 is repaired by title lookup",
      dtn.db("Maps") == DEFAULT_DB_IDS["Maps"] and "Titel" in sch)
check("the repair searched instead of guessing",
      any(c[1].endswith("/search") for c in s.calls))

dtn.NOTION_DATABASES["Maps"] = "3e1d7b80-7f4d-8063-95b1-000b1069a2e0"
scripted([Resp(404, NOT_FOUND), Resp(200, {"results": []})])
try:
    dtn.fetch_schema("Maps")
    check("404 with no title match still fails", False)
except dtn.NotionError as exc:
    check("404 with no title match still fails", exc.status == 404)

dtn.NOTION_DATABASES["Maps"] = "3e1d7b80-7f4d-8063-95b1-000b1069a2e0"
scripted([Resp(404, NOT_FOUND),
          Resp(200, {"results": [{"id": "some-other-db",
                                  "title": [{"plain_text": "Maps Archive"}]}]})])
try:
    dtn.fetch_schema("Maps")
    check("a near-miss title is not accepted", False)
except dtn.NotionError as exc:
    check("a near-miss title is not accepted", exc.status == 404)

dtn.NOTION_DATABASES.update(DEFAULT_DB_IDS)

print("\n[clutch buckets]")
check("clutch buckets the player faced are written",
      all(f"1v{n} WR/SR%" in perf_props for n in (1, 2, 3)),
      str([k for k in perf_props if "WR/SR" in k]))
check("buckets the player never faced stay empty",
      not any(f"1v{n} WR/SR%" in perf_props for n in (4, 5)))
check("a save counts toward the rate", perf_props["1v2 WR/SR%"]["number"] == 0.5)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for f in FAIL:
        print("  FAILED: " + f)
    sys.exit(1)
