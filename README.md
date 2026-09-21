# demoToNotion

Parses a CS2 demo (`.dem`) and writes one **Maps** row plus one **Performance** row per player into Notion.

It also fills in the **Gegner** relation and files the map into a **Match** in the Matches database, creating either if it does not exist yet.

Re-importing the same demo **updates** the existing rows instead of creating duplicates, so you can re-run after fixing something.

**Note:** You will find some German here and there, like with the parameters. I am at the point where I am to scared to change it and break it. Oups.

---

## Setup

```bash
pip install demoparser2 requests python-dotenv
```

Put your Notion integration token and db refs in a `.env` file next to the script. See `.env.example`.

`.env` is in `.gitignore`.

Then connect the integration to each database in Notion - **`...` → Connections → add your integration** on *Maps*, *Performance* and *Spieler*. Sharing the parent *Statistiken* page does **not** reliably cascade to the databases inside it; that omission is the most common cause of a 404.

Finally, check everything before your first import:

```bash
python demoToNotion.py --demo match.dem --gegner "Example Team" --check
```

---

## Usage

```bash
# look at the numbers, write nothing
python demoToNotion.py --demo match.dem --gegner "Example Team" --liga "DACH S6 Spk5 - Phase 1" --typ Official --dry-run

# real import
python demoToNotion.py --demo match.dem --gegner "Example Team" --liga "DACH S6 Spk5 - Phase 1" --typ Official
```

Exit code is `0` on success, `1` on any failure - safe to use in a script.

---

## Parameters

| Flag                 | Required | Default                   | What it does                                                                                                                                         |
| -------------------- | -------- | ------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--demo PATH`        | **yes**  | -                         | The `.dem` file to parse.                                                                                                                            |
| `--gegner NAME`      | **yes**  | -                         | Opponent name. Used in the `Titel` of the Maps row, in each Performance row's `Name`, and to look up (or create) the `Gegner` row that gets linked.  |
| `--liga NAME`        | no       | empty                     | Written to `Liga/Turnier` (a select).                                                                                                                |
| `--typ`              | no       | `Scrim`                   | One of `FaceIT`, `Scrim`, `Official`. Written to `Typ`.                                                                                              |
| `--demo-link URL`    | no       | `sha1:<hash>` of the file | Written to `Match-Room / Demo-Link`. **This is the key used to recognise a re-import**, so pass the same value (or none) each time for a given demo. |
| `--datum YYYY-MM-DD` | no       | the demo file's date      | Written to `Datum`.                                                                                                                                  |
| `--gegner-page ID`   | no       | looked up from `--gegner` | Notion page id (or URL) of the opponent's row in the *Gegner* database. Skips the name lookup. Rejected up front if it isn't a Notion id.            |
| `--vod URL`          | no       | not set                   | Written to `VOD-Link`.                                                                                                                               |
| `--serie NAME`       | no       | `--liga`                  | Series label for the Match title, for when the series is spelled differently from the league select.                                                 |
| `--match-name TEXT`  | no       | from `--serie`            | Exact title for an auto-created Match. Overrides `--serie`.                                                                                          |
| `--match-page ID`    | no       | looked up                 | Attach this map to a specific Match row.                                                                                                             |
| `--map-nr {1,2,3}`   | no       | next free slot            | Force the series slot: `1. Map`, `2. Map` or `Decider`.                                                                                              |
| `--no-match`         | no       | off                       | Leave the Matches database alone.                                                                                                                    |
| `--dry-run`          | no       | off                       | Parse, print everything, write nothing.                                                                                                              |
| `--check`            | no       | off                       | Validate token, schemas and player mapping, then exit. No writes.                                                                                    |
| `--debug-clutches`   | no       | off                       | Print every 1vX situation round by round with its outcome (`won` / `saved` / `lost`).                                                                |

### Environment variables

| Variable                | Purpose                                   |
| ----------------------- | ----------------------------------------- |
| `NOTION_TOKEN`          | **Required.** Internal integration token. |
| `NOTION_DB_MAPS`        | Override the Maps **database** id.        |
| `NOTION_DB_PERFORMANCE` | Override the Performance database id.     |
| `NOTION_DB_SPIELER`     | Override the Spieler database id.         |
| `NOTION_DB_GEGNER`      | Override the Gegner database id.          |
| `NOTION_DB_MATCHES`     | Override the Matches database id.         |

A full Notion URL, a dashed UUID, or a bare 32-hex id are all accepted.

---

## How re-import works

| Object          | Matched on                                                                                        |
| --------------- | ------------------------------------------------------------------------------------------------- |
| Maps row        | `Match-Room / Demo-Link` equals this run's demo key                                               |
| Performance row | its `Match` relation points at that Maps row **and** its `Spieler` relation points at that player |
| Gegner row      | title equals `--gegner`, case-insensitively                                                       |
| Match row       | same `Gegner` **and** same `Datum`                                                                |
| series slot     | a map already linked keeps its slot; otherwise the first free one                                 |

A match means the row is **updated in place**; no match means a new row. Page ids stay stable, so relations and rollups elsewhere keep working.

The import is all-or-nothing, across every database it touches. If any row fails part-way through, rows created in that run are archived - including an auto-created Gegner or Match - and rows already updated get their previous values written back, so you never end up with a half-updated map.

---

## Opponents and series

The `Gegner` relation on the Maps row is filled in from `--gegner`. The opponent is looked up in the *Gegner* database by title, **case-insensitively** - your database holds `RACZON ACADEMY` while you would type `"Example Team"` - and created if they are new. `--gegner-page` skips the lookup.

The map is then filed into a **Match**:

- a Match for the same opponent on the same `Datum` is reused, otherwise one is created;
- the map goes into the first free slot - `1. Map`, then `2. Map`, then `Decider` (the third slot is called Decider, not `3. Map`);
- a map that is already linked **keeps its slot**, so re-importing never shuffles a series around;
- a series that already holds three maps is left alone rather than overwritten - pass `--map-nr` or `--match-page` to place it yourself.

`Resultat` is **recomputed over every map in the series** each time one of them is imported, so a Bo3 reads `1:0`, then `1:1`, then `2:1` as the maps land. It is always written - never skipped - so it can never be left showing a standing a later map has overtaken. Notion adds a select option it does not have yet, which is how `1:1` appears without you editing the schema.

If no map in the series has a decided result, `Resultat` is cleared rather than left stale. The `1./2./3. Ergebnis` columns are rollups off the slot relations, so Notion fills those in by itself.

An auto-created Match is titled **`"<series> - <opponent>"`**, and the series is `--liga`:

```bash
--liga "DACH S6 Spk5 - Phase 1" --gegner "Example Team"
#  -> DACH S6 Spk5 - Phase 1 - Example Team
```

Pass `--serie` when the series is written differently from the league select, or `--match-name` to set the title outright:

```bash
--serie "DACH S6 - Spk5"      # -> DACH S6 - Spk5 - Example Team
--match-name "Cup Final"      # -> Cup Final
```

Only a run with none of the three falls back to `"<opponent> - <Datum>"`.

Typical Bo3, one command per map:

```bash
for demo in map1.dem map2.dem map3.dem; do
  python demoToNotion.py --demo "$demo" --gegner "Example Team" \
      --liga "DACH S6 Spk5 - Phase 1" --typ Official
done
```

The first creates the Match, the other two slot into it.

---

## Players

Players are matched to the *Spieler* database on its **`SteamID64`** property, which **must be a Text property**.

> As a Number it is a float64: 53 bits of integer precision against the 57 a SteamID64 needs. Notion silently rounds every id by up to 8, and nothing matches. `--check` warns if it finds Numbers there.

Which players exist in *Spieler* also tells the script **which team is yours**, and therefore which side you played each round. That drives `Ergebnis` and every team rate on the Maps row. Opponents simply have no Spieler row and are reported and skipped.

---

## What gets written

**Matches** - `Name`, `Gegner`, `Datum`, one of `1. Map`/`2. Map`/`Decider`, and `Resultat`.

**Gegner** - `Name`, only when the opponent is new.

**Maps** - `Titel`, `Match-Room / Demo-Link`, `Map`, `Typ`, `Ergebnis`, `Liga/Turnier`, `Datum`, `Gegner`, `VOD-Link`, and the team rates `CT%`, `T%`, `Pistol WR`, `Eco WR`, `Half-Buy WR`, `Buy WR`, `5v4 WR`, `4v5 WR`, `Conv WR`, `KAST`.

**Performance** - `Name`, `Spieler`, `Match`, `Rating`, `Swing`, `KAST`, `ADR`, `Kills/R`, `First Kills/R`, `First Death/R`, `Untradable Deaths/R`, `Utility Damage/R`, `Last Man Standing/R`, `Enemies Flashed/R`, `Flash Leading to Kills/R`, `1v1`–`1v5 WR/SR%`, `Wasted Utility/R ($)`, `Date`.

`Performance.Map` is a rollup off `Match` - Notion computes it, the script never writes it.

Nothing has to exist before the first run. `--check` reports exactly what is missing or wrongly typed; anything missing is left out of the write. Only the titles and the relations are mandatory.

Percent-formatted properties take **fractions**, `0.0467` renders as `4.67%`,  which is what the script sends.

---

## Where the numbers come from

Kills, deaths, assists, damage, utility damage and enemies flashed are read from **CS2's own per-player match counters** (`m_iKills`, `m_iDamage`, `m_iUtilityDamage`, `m_iEnemiesFlashed`), i.e. exactly the in-game scoreboard. That means teamkills and suicides are not kills, and team damage, self damage and overkill damage do not inflate ADR. The round count comes from the team score, so the knife round and warmup are excluded automatically - MR12, MR15 and overtime all work without configuration.

Everything with no scoreboard counter - opening duels, untradable deaths, KAST, clutches, flash assists, round swing - is derived from the event timeline, restricted to the rounds of the real match.

### Metric definitions

| Metric                      | Definition                                                                                                                                                                                                            |
| --------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Swing**                   | Round swing: how much the player moved their team's chance of winning the round, averaged over the map. Written as a fraction.                                                                                        |
| **Wasted Utility/R ($)**    | Value of grenades still held **at the moment of death**, per round - the map total divided by rounds played, like every other `/R` metric. Utility carried through a round you survived is a save and is not counted. |
| **1vX WR/SR%**              | Win **or save** rate in 1vX situations. Surviving a lost round counts as won.                                                                                                                                         |
| **KAST**                    | Share of rounds with a kill, assist, survival, or a traded death.                                                                                                                                                     |
| **Untradable Deaths/R**     | Deaths not avenged by a teammate within 5s.                                                                                                                                                                           |
| **Conv WR**                 | Rounds you converted, out of rounds you *should* have: you were ever up a man, or it was the round after you won a pistol, or the enemy was on eco. Counted once per round.                                           |
| **Eco / Half-Buy / Buy WR** | By team equipment value at freeze-end, per player: eco `< $1,200`, half-buy `< $3,600`, otherwise full buy.                                                                                                           |
| **Pistol WR**               | Round 1 and every round where the sides swapped - so halftime and each overtime switch are detected automatically.                                                                                                    |

---

## Caveats

- The biggest first, this is extremely fit to just the Notion notebook that I am using. If yours is not one to one like mine I am unsure how good it will go.
- **Rating** approximates HLTV Rating 2.0. HLTV never published the exact formula, so this is a community reconstruction, not the official value.
- **Swing** depends on a win-probability model. Neither HLTV nor Leetify publish their tables, so the constants (`SWING_*` at the top of the script) were fitted against one map whose per-player swing was known, reproducing all five players within 0.95 percentage points. The implied probabilities are sane (5v5 51.5% CT, 5v4 72.0%, 1v2 clutch 28-31% by side), but it is a fit to a single map - expect some drift elsewhere, and recalibrate as you collect reference values. `SWING_CT_BASE` is map-specific.
- Flash attribution uses a 5s window and a 0.7s blind threshold (`FIVE_SEC_WINDOW`, `FLASH_EFFECTIVE_THRESHOLD`).

---

## Troubleshooting

**`HTTP 404 [object_not_found] - Could not find database with ID ...`** Notion returns the same 404 for a wrong id and for a missing connection. The script tells them apart for you: it looks the database up by title, and if it finds one it reports the correct id and uses it for that run. If it finds nothing, the integration genuinely has no access - connect it on that specific database.

A common cause is configuring a **data-source (collection) id** instead of a **database id**. They look alike but the API only accepts the latter.

**`HTTP 401 / 403`** - bad or missing token, or the integration is not connected to that database.

**A player got no Performance row** - no *Spieler* row matched their SteamID64. `--dry-run` prints the SteamIDs found in the demo.

**Rate limits / flaky network** - handled: requests time out, are paced to ~3/s, and retry 429 and 5xx with exponential backoff.

---

## Tests

```bash
python test_notion_writes.py
```

83 checks covering the transport (retries, backoff, timeouts), value sanitising (NaN, length limits), preflight, the Gegner lookup, Match slotting, the re-import/rollback path and id handling. Runs against a fake transport - **no network calls**.

---

Made with coffee and claude. Although the later I am afraid get's harder and harder to recommend.