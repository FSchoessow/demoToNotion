// godemo is the fallback demo reader for demoToNotion.py.
//
// Some demos make demoparser2 fail with EntityNotFound / MalformedMessage
// (LaihoE/demoparser#339) while demoinfocs-golang reads them fine. This
// program parses such a demo once and prints, as JSON on stdout, exactly the
// game events and per-tick player values the Python script asks demoparser2
// for, in demoparser2's column names. demoToNotion.py wraps that in a small
// stand-in for DemoParser, so the stats code itself does not change.
//
//	go run . <demo.dem>          JSON export
//	go run . -props <demo.dem>   list the controller's scoreboard property names
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"sort"
	"strconv"
	"strings"

	dem "github.com/markus-wa/demoinfocs-golang/v5/pkg/demoinfocs"
	"github.com/markus-wa/demoinfocs-golang/v5/pkg/demoinfocs/common"
	"github.com/markus-wa/demoinfocs-golang/v5/pkg/demoinfocs/events"
	st "github.com/markus-wa/demoinfocs-golang/v5/pkg/demoinfocs/sendtables"
)

type row map[string]any

type output struct {
	Header map[string]any   `json:"header"`
	Events map[string][]row `json:"events"`
	Ticks  []row            `json:"ticks"`
}

// Scoreboard counters without a demoinfocs helper.
const (
	headshotProp       = "m_pActionTrackingServices.m_iHeadShotKills"
	enemiesFlashedProp = "m_pActionTrackingServices.m_iEnemiesFlashed"
)

// Grenade names as demoparser2 reports them in "inventory", which is what
// UTILITY_PRICE in demoToNotion.py is keyed by.
var inventoryName = map[common.EquipmentType]string{
	common.EqHE:         "High Explosive Grenade",
	common.EqFlash:      "Flashbang",
	common.EqSmoke:      "Smoke Grenade",
	common.EqMolotov:    "Molotov",
	common.EqIncendiary: "Incendiary Grenade",
	common.EqDecoy:      "Decoy Grenade",
}

// Weapon names as demoparser2 reports them in player_hurt. Fire damage
// arrives as "inferno" there, whichever grenade started the fire.
var hurtWeaponName = map[common.EquipmentType]string{
	common.EqHE:         "hegrenade",
	common.EqFlash:      "flashbang",
	common.EqSmoke:      "smokegrenade",
	common.EqDecoy:      "decoy",
	common.EqMolotov:    "inferno",
	common.EqIncendiary: "inferno",
}

func main() {
	listProps := flag.Bool("props", false, "list the scoreboard property names and exit")
	flag.Parse()
	if flag.NArg() != 1 {
		fmt.Fprintln(os.Stderr, "usage: godemo [-props] <demo.dem>")
		os.Exit(2)
	}
	f, err := os.Open(flag.Arg(0))
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	defer f.Close()

	p := dem.NewParser(f)
	defer p.Close()
	gs := p.GameState()

	if *listProps {
		printScoreboardProps(p)
		return
	}

	out := output{Events: map[string][]row{}}
	for _, name := range []string{"round_end", "round_freeze_end", "player_death",
		"player_hurt", "bomb_planted", "flashbang_detonate"} {
		out.Events[name] = []row{}
	}
	ticks := map[int]map[string]row{} // tick -> steamid -> values

	// Snapshots: the state at the end of a frame is what demoparser2 returns
	// for that tick. `prev` is kept around for the tick-1 reads the script
	// does (inventory before a death, flash_duration before a detonation).
	var prev []row
	prevTick := -1
	wantNow := false
	wantPrev := false

	tick := func() int { return gs.IngameTick() }
	withState := func(r row) row {
		r["tick"] = tick()
		r["total_rounds_played"] = gs.TotalRoundsPlayed()
		r["is_warmup_period"] = gs.IsWarmupPeriod()
		return r
	}

	p.RegisterEventHandler(func(e events.RoundEnd) {
		out.Events["round_end"] = append(out.Events["round_end"],
			withState(row{"winner": int(e.Winner)}))
		wantNow = true
	})
	p.RegisterEventHandler(func(events.RoundFreezetimeEnd) {
		out.Events["round_freeze_end"] = append(out.Events["round_freeze_end"], withState(row{}))
		wantNow = true
	})
	p.RegisterEventHandler(func(e events.Kill) {
		r := withState(row{})
		addPlayer(r, "user", e.Victim)
		addPlayer(r, "attacker", e.Killer)
		r["assister_steamid"] = steamID(e.Assister)
		out.Events["player_death"] = append(out.Events["player_death"], r)
		wantPrev = true
	})
	p.RegisterEventHandler(func(e events.PlayerHurt) {
		r := withState(row{"health": e.Health, "weapon": hurtWeapon(e.Weapon)})
		addPlayer(r, "user", e.Player)
		addPlayer(r, "attacker", e.Attacker)
		out.Events["player_hurt"] = append(out.Events["player_hurt"], r)
	})
	p.RegisterEventHandler(func(events.BombPlanted) {
		out.Events["bomb_planted"] = append(out.Events["bomb_planted"], withState(row{}))
	})
	// demoinfocs reports each flash detonation twice (game event and entity),
	// demoparser2 once.
	seenFlash := map[[2]int]bool{}
	p.RegisterEventHandler(func(e events.FlashExplode) {
		key := [2]int{tick(), e.GrenadeEntityID}
		if seenFlash[key] {
			return
		}
		seenFlash[key] = true
		out.Events["flashbang_detonate"] = append(out.Events["flashbang_detonate"],
			withState(row{"user_steamid": steamID(e.Thrower)}))
		wantNow = true
		wantPrev = true
	})
	p.RegisterEventHandler(func(events.FrameDone) {
		t := tick()
		if wantPrev && prev != nil {
			store(ticks, t-1, prev)
		}
		cur := snapshot(gs)
		if wantNow {
			store(ticks, t, cur)
		}
		if t != prevTick {
			prev, prevTick = cur, t
		}
		wantNow, wantPrev = false, false
	})

	if err := p.ParseToEnd(); err != nil {
		fmt.Fprintln(os.Stderr, "parse error:", err)
		os.Exit(1)
	}

	// demoinfocs v5 does not expose the map name; the Python side takes it
	// from demoparser2's parse_header, which still works on these demos.
	out.Header = map[string]any{"tick_rate": p.TickRate()}

	tickNums := make([]int, 0, len(ticks))
	for t := range ticks {
		tickNums = append(tickNums, t)
	}
	sort.Ints(tickNums)
	for _, t := range tickNums {
		for _, r := range ticks[t] {
			out.Ticks = append(out.Ticks, r)
		}
	}

	enc := json.NewEncoder(os.Stdout)
	if err := enc.Encode(out); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func store(ticks map[int]map[string]row, t int, rows []row) {
	if ticks[t] == nil {
		ticks[t] = map[string]row{}
	}
	for _, r := range rows {
		c := row{}
		for k, v := range r {
			c[k] = v
		}
		c["tick"] = t
		ticks[t][r["steamid"].(string)] = c
	}
}

// snapshot reads the per-tick values the script asks parse_ticks for.
func snapshot(gs dem.GameState) []row {
	var rows []row
	for _, pl := range gs.Participants().Playing() {
		sid := steamID(pl)
		if sid == "" || pl.Entity == nil {
			continue
		}
		score := 0
		if ts := gs.Team(pl.Team); ts != nil {
			score = ts.Score()
		}
		rows = append(rows, row{
			"steamid":               sid,
			"name":                  pl.Name,
			"team_num":              int(pl.Team),
			"is_alive":              pl.IsAlive(),
			"kills_total":           pl.Kills(),
			"deaths_total":          pl.Deaths(),
			"assists_total":         pl.Assists(),
			"damage_total":          pl.TotalDamage(),
			"utility_damage_total":  pl.UtilityDamage(),
			"headshot_kills_total":  intProp(pl.Entity, headshotProp),
			"enemies_flashed_total": intProp(pl.Entity, enemiesFlashedProp),
			"team_rounds_total":     score,
			"current_equip_value":   pl.EquipmentValueCurrent(),
			"flash_duration":        pl.FlashDuration,
			"inventory":             inventory(pl),
		})
	}
	return rows
}

func inventory(pl *common.Player) []string {
	items := []string{}
	for _, w := range pl.Weapons() {
		if w == nil {
			continue
		}
		name, ok := inventoryName[w.Type]
		if !ok {
			continue // only grenades are priced by the script
		}
		n := 1
		if w.Type == common.EqFlash {
			n = max(1, int(pl.FlashbangCount()))
		}
		for range n {
			items = append(items, name)
		}
	}
	return items
}

func addPlayer(r row, prefix string, pl *common.Player) {
	r[prefix+"_steamid"] = steamID(pl)
	if pl == nil {
		return
	}
	r[prefix+"_name"] = pl.Name
	r[prefix+"_team_num"] = int(pl.Team)
}

func steamID(pl *common.Player) string {
	if pl == nil || pl.SteamID64 == 0 {
		return ""
	}
	return strconv.FormatUint(pl.SteamID64, 10)
}

func hurtWeapon(w *common.Equipment) string {
	if w == nil {
		return ""
	}
	if name, ok := hurtWeaponName[w.Type]; ok {
		return name
	}
	return strings.ToLower(w.String())
}

func intProp(e st.Entity, name string) int {
	if v, ok := e.PropertyValue(name); ok && v.Any != nil {
		return v.Int()
	}
	return 0
}

func printScoreboardProps(p dem.Parser) {
	done := false
	p.RegisterEventHandler(func(events.RoundEnd) {
		if done {
			return
		}
		for _, pl := range p.GameState().Participants().Playing() {
			if pl.Entity == nil {
				continue
			}
			for _, prop := range pl.Entity.Properties() {
				if strings.Contains(prop.Name(), "ActionTracking") {
					fmt.Println(prop.Name(), prop.Value().Any)
				}
			}
			done = true
			return
		}
	})
	_ = p.ParseToEnd()
}
