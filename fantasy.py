"""
Fantasy lineup report, v2.

For every Sleeper league you're in (skipping leagues you haven't drafted in),
this builds a weighted projection for each player and writes one static HTML
page, lineups.html, with a tab per league.

How a projection is built (all weights are in the W dict below):
    recent form          decayed average of the player's fantasy points,
                         using YOUR league's scoring settings
  x opponent defense     how many points that defense allows to the position,
                         vs the league average (shrunk toward 1.0 early on)
  x Vegas environment    team implied points from spread and total
  x history vs opponent  how the player has scored against this team before
  x home/away, injury    small adjustments

Setup (once):
    py -m pip install requests anthropic
Run:
    py fantasy.py
To force fresh injury data from Sleeper:  py fantasy.py --refresh
Optional: set ANTHROPIC_API_KEY to also get a news check and a written
explanation on close calls. Without a key, the script writes the explanations
itself from the numbers.
"""
import csv
import html
import json
import math
import os
import re
import socket
import sys
import threading
import time
import webbrowser
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
SLEEPER_USERNAME = "mayrek543"
SEASON = None          # None = use Sleeper's current season
WEEK = None            # None = use Sleeper's current week (set a number to override)
HISTORY_SEASONS = 3    # current season plus this many minus one prior seasons
USE_CLAUDE = True      # Claude makes the final lineup call and writes reasoning for each player
AHEAD_WEEKS = 4        # how many weeks to project, starting with this one
WAIVER_PICKS = 3       # free agents to suggest per league
WAIVER_POOL = 50       # how many free agents to evaluate in depth
# Start from Sleeper's own projections (undocumented endpoint; falls back to the model). Env FANTASY_SLEEPER_PROJECTIONS=0 turns off.
USE_SLEEPER_PROJECTIONS = os.environ.get("FANTASY_SLEEPER_PROJECTIONS", "1") != "0"
# Player photos and team logos load from Sleeper's image server. Env FANTASY_IMAGES=0 shows plain team chips instead.
SHOW_IMAGES = os.environ.get("FANTASY_IMAGES", "1") != "0"
BUY_LOW = True         # also list underperforming players on other teams to trade for (set False to skip)
BUY_LOW_COUNT = 5
CLAUDE_MODEL = "claude-sonnet-5-5"
OUTPUT_FILE = "lineups.html"
OPEN_WHEN_DONE = True

# A pick is a "close call" when the best bench alternative is within this gap.
CLOSE_CALL_PTS = 1.5
CLOSE_CALL_PCT = 0.08  # or this share of the starter's projection, whichever is bigger

# Model weights. Tweak these to change how the projections lean.
W = {
    "window": 10,                 # most recent games considered
    "decay": 0.80,                # each older game counts this much less
    "prior_season_weight": 0.80,  # (defense table) discount for last season's games
    # How much a game counts depending on how many seasons ago it was. The current season is
    # boosted so a player's present form drives the projection; old seasons fade fast.
    "season_weight": {0: 1.3, 1: 0.55, 2: 0.25},
    "cur_k": 1.0,                 # this season's share of "form" = games / (games + cur_k)
    "cur_decay": 0.85,            # within this season, each older game counts this much less
    # How the offense has actually been playing (points scored, pass and rush volume vs league average)
    "team": {
        "k": 3.0,
        "pts": {"QB": 0.50, "RB": 0.30, "WR": 0.40, "TE": 0.30},
        "vol": {"QB": 0.30, "RB": 0.40, "WR": 0.40, "TE": 0.30},
        "cap": (0.88, 1.15),
    },
    # Reality check against what the player has actually done this season
    "anchor": {"over": 1.05, "role_extra": 0.30, "soft": 0.30, "under": 0.85},
    # Position ladder: share of vacated volume by spot among AVAILABLE players at his position
    "depth_weight": {1: 1.0, 2: 0.55, 3: 0.25},
    # Sleeper's projection is the starting point. The model only nudges it, within these limits.
    "blend": {"model_weight": 0.30, "cap": 0.20, "min_cap": 1.5, "final_cap": 1.20},
    # Future weeks: how long an injury effect is assumed to last, and sustainability of production
    "future": {"team_persist": 0.75, "own_persist": 0.5},
    "sust": {"strength": 0.30, "cap": (0.92, 1.08)},
    # Learning from the model's own misses: replay recent weeks, compare with what happened
    "learn": {"weeks": 4, "player_k": 3.0, "pos_k": 10.0, "cap": (0.75, 1.30), "pos_cap": (0.90, 1.10)},
    "prior_pseudo_games": 0.5,    # pull small samples toward replacement level
    "def_window": 12,             # games of points-allowed history per defense
    "def_decay": 0.88,
    "matchup_k": 4.0,             # higher = trust defense rankings less
    "matchup_cap": (0.80, 1.25),
    "history_k": 3.0,             # higher = trust past games vs opponent less
    "history_cap": (0.88, 1.12),
    "vegas": {"QB": 0.70, "RB": 0.50, "WR": 0.60, "TE": 0.50},  # sensitivity to implied points
    "vegas_cap": (0.80, 1.25),
    "home_edge": 0.02,
    # Teammate injuries ("next man up")
    "inj": {
        "min_opps": 3.0,          # touches + targets a game for a teammate to count as a key starter
        "qb_min_attempts": 15.0,  # pass attempts a game for a QB to count as the starter
        "absorb": {"RB": 0.70, "WR": 0.70, "TE": 0.60},  # share of vacated volume that stays in the
                                  # same position group (the rest spreads to other positions)
        "cross_wr_te": 0.60,      # a WR out helps TEs (and vice versa) at this fraction
        "prior_cap": 3.5,         # most a usage estimate can multiply a player's scoring
        "eff_own": 0.65,          # points per touch: share from his own record vs. the injured starter's
        "abs_k": 3.0,             # games without the starter needed to fully trust the history
        "pres_k": 2.0,
        "qb_out_prior": 0.93,     # pass catchers lose a bit when the starting QB is out
        "backup_qb_share": 0.80,  # backup QB with little history: share of the starter's output
        "factor_cap": (0.85, 3.0),
        "history_seasons": 2,     # only games without the starter from this season and last count
        "form_floor": 0.5,        # a player scoring far below his norm keeps at least this share of a boost
        "share_by_usage": 0.5,    # vacated volume is split half by workload, half by recent production
    },
}
REPLACEMENT = {"QB": 14.0, "RB": 6.5, "WR": 6.5, "TE": 4.5}
INJURY_MULT = {"Questionable": 0.93, "Doubtful": 0.25}
LONG_TERM = {"IR", "PUP", "Sus"}  # assumed out for the foreseeable future
OUT_STATUSES = {"Out", "IR", "PUP", "Sus", "NA", "COV"}
# Use this to tell the script about injuries Sleeper hasn't updated yet, e.g.
#   MANUAL_STATUS = {"Player Name": "Out", "Other Player": "Doubtful"}
MANUAL_STATUS = {}
PLAYERS_CACHE_HOURS = 12  # Sleeper asks for at most one full player download a day; injuries go stale

OUT_ROSTER_STATUS = {"Inactive", "Injured Reserve", "Reserve/PUP", "Suspended", "Physically Unable to Perform",
                     "Non Football Injury", "Reserve/Injured"}
OUTAGE_WEIGHT = {"Doubtful": 0.75, "Questionable": 0.20}  # how "out" a teammate is treated as

# On Vercel (and other serverless hosts) only /tmp is writable.
_SERVERLESS = bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))
CACHE_DIR = os.environ.get("FANTASY_CACHE_DIR") or (
    "/tmp/fantasy-cache" if _SERVERLESS else os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache"))
SLEEPER = "https://api.sleeper.app/v1"
GAMES_URL = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
STATS_URLS = [
    "https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_week_{season}.csv",
    "https://github.com/nflverse/nflverse-data/releases/download/player_stats/player_stats_{season}.csv",
]

SKILL = ("QB", "RB", "WR", "TE")
ELIGIBLE = {
    "QB": ("QB",), "RB": ("RB",), "WR": ("WR",), "TE": ("TE",),
    "K": ("K",), "DEF": ("DEF",),
    "REC_FLEX": ("WR", "TE"), "WRRB_FLEX": ("WR", "RB"),
    "FLEX": ("RB", "WR", "TE"), "SUPER_FLEX": ("QB", "RB", "WR", "TE"),
}
SLOT_ORDER = ["QB", "RB", "WR", "TE", "K", "DEF", "REC_FLEX", "WRRB_FLEX", "FLEX", "SUPER_FLEX"]
SLOT_LABEL = {"REC_FLEX": "W/T", "WRRB_FLEX": "W/R", "FLEX": "FLEX", "SUPER_FLEX": "SFLX"}

# Primary team colors, used for accents only (photo underline, range bars, logo chips).
TEAM_COLORS = {
    "ARI": "#97233F", "ATL": "#A71930", "BAL": "#241773", "BUF": "#00338D", "CAR": "#0085CA",
    "CHI": "#0B2A55", "CIN": "#FB4F14", "CLE": "#FF3C00", "DAL": "#003594", "DEN": "#FB4F14",
    "DET": "#0076B6", "GB": "#203731", "HOU": "#03202F", "IND": "#002C5F", "JAX": "#006778",
    "KC": "#E31837", "LV": "#565A5C", "LAC": "#0080C6", "LA": "#003594", "MIA": "#008E97",
    "MIN": "#4F2683", "NE": "#002244", "NO": "#B39B5E", "NYG": "#0B2265", "NYJ": "#125740",
    "PHI": "#004C54", "PIT": "#FFB612", "SF": "#AA0000", "SEA": "#69BE28", "TB": "#D50A0A",
    "TEN": "#4B92DB", "WAS": "#5A1414",
}
LOGO = "https://sleepercdn.com/images/team_logos/nfl/{team}.png"

TEAM_FIX = {"LAR": "LA", "STL": "LA", "OAK": "LV", "SD": "LAC", "WSH": "WAS", "JAC": "JAX"}
TEAM_SHOW = {"LA": "LAR"}

STAT_COLUMNS = {
    "pass_yd": ["passing_yards"], "pass_td": ["passing_tds"],
    "pass_int": ["passing_interceptions", "interceptions"],
    "pass_cmp": ["completions"], "pass_att": ["attempts"],
    "pass_2pt": ["passing_2pt_conversions"], "pass_fd": ["passing_first_downs"],
    "rush_yd": ["rushing_yards"], "rush_td": ["rushing_tds"], "rush_att": ["carries"],
    "rush_2pt": ["rushing_2pt_conversions"], "rush_fd": ["rushing_first_downs"],
    "rec": ["receptions"], "rec_yd": ["receiving_yards"], "rec_td": ["receiving_tds"],
    "rec_tgt": ["targets"], "rec_2pt": ["receiving_2pt_conversions"],
    "rec_fd": ["receiving_first_downs"],
}
FUMBLE_COLUMNS = ["rushing_fumbles_lost", "receiving_fumbles_lost", "sack_fumbles_lost"]


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def log(msg):
    print(msg, flush=True)


def clamp(x, lo_hi):
    lo, hi = lo_hi
    return max(lo, min(hi, x))


def norm_team(t):
    if not t:
        return None
    t = str(t).upper().strip()
    return TEAM_FIX.get(t, t)


def show_team(t):
    return TEAM_SHOW.get(t, t) if t else ""


def num(row, *keys):
    for k in keys:
        v = row.get(k)
        if v not in (None, ""):
            try:
                return float(v)
            except ValueError:
                pass
    return 0.0


def norm_name(s):
    s = (s or "").lower()
    s = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b\.?", "", s)
    return re.sub(r"[^a-z0-9]", "", s)


def as_int(x):
    try:
        v = int(x)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def meta_name(meta, fallback):
    return (meta.get("full_name")
            or f"{meta.get('first_name', '')} {meta.get('last_name', '')}".strip() or fallback)


def outage_weight(meta):
    """1.0 = definitely out, 0.0 = healthy. Used for teammates, not for the player himself."""
    inj = meta.get("injury_status")
    if inj in OUT_STATUSES or meta.get("status") in OUT_ROSTER_STATUS:
        return 1.0
    return OUTAGE_WEIGHT.get(inj, 0.0)


def esc(x):
    return html.escape(str(x), quote=True)


def join_names(names):
    names = list(names)
    if len(names) <= 2:
        return " and ".join(names)
    return ", ".join(names[:-1]) + ", and " + names[-1]


# One shared session: it reuses connections (fewer DNS lookups) and retries when the network hiccups.
HTTP = requests.Session()
HTTP.mount("https://", HTTPAdapter(max_retries=Retry(
    total=5, connect=5, read=3, backoff_factor=1.5, status_forcelist=[429, 500, 502, 503, 504])))

# Sleeper's stats/projection feeds are undocumented and large. Give them short timeouts and no long retry
# loops so a slow or missing feed costs seconds, not minutes.
FEED_HTTP = requests.Session()
FEED_HTTP.mount("https://", HTTPAdapter(max_retries=Retry(total=1, connect=1, read=0, backoff_factor=1.0)))
FEED_TIMEOUT = (10, 45)  # seconds to connect, seconds to wait for data
_FEED_FAILS = {"stats": 0, "projections": 0}

NETWORK_HELP = """
Your computer could not look up {hosts}. That is a network or DNS problem on your side, not a problem with
your leagues or the script. Things to try, in order:
  1. Check you are online and open https://api.sleeper.app/v1/state/nfl in your browser. You should see a few lines of text.
  2. If you use a VPN, turn it off and run the script again.
  3. Open a Command Prompt and run:   ipconfig /flushdns
  4. Pause antivirus or firewall web protection briefly, or allow Python through the firewall.
  5. Switch your DNS to 8.8.8.8 and 1.1.1.1 in your network adapter settings.
  6. Try another network, for example your phone's hotspot.
Then run the script again. Your previous lineups.html has been left as it was.
"""


def load_api_key():
    """Use ANTHROPIC_API_KEY if it is set. Otherwise look in this script's folder for a text file holding the key,
    whatever it is called (anthropic_key.txt, ANTHROPIC_API_KEY.txt, even ...txt.txt). Returns where it came from."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "the ANTHROPIC_API_KEY setting"
    folder = os.path.dirname(os.path.abspath(__file__))
    try:
        files = sorted(os.listdir(folder))
    except OSError:
        return None
    preferred = ["anthropic_key.txt", "anthropic_api_key.txt"]
    ordered = sorted((f for f in files if f.lower().endswith(".txt") or ".txt." in f.lower()),
                     key=lambda f: (f.lower() not in preferred, "key" not in f.lower(), f.lower()))
    for name in ordered:
        path = os.path.join(folder, name)
        if not os.path.isfile(path) or os.path.getsize(path) > 10_000:
            continue
        for enc in ("utf-8-sig", "utf-16"):  # Notepad can save either way
            try:
                with open(path, encoding=enc) as f:
                    text = f.read()
            except (UnicodeError, OSError):
                continue
            m = re.search(r"sk-ant-[A-Za-z0-9_\-]{20,}", text)  # tolerates quotes, spaces, "NAME=key" lines
            if m:
                os.environ["ANTHROPIC_API_KEY"] = m.group(0)
                return name
    return None


def ensure_network():
    """Stop early, with plain instructions, if the machine cannot reach the services this script needs."""
    bad = []
    for host in ("api.sleeper.app", "raw.githubusercontent.com", "github.com"):
        for attempt in range(4):
            try:
                socket.gethostbyname(host)
                break
            except OSError:
                if attempt == 3:
                    bad.append(host)
                else:
                    time.sleep(2 * (attempt + 1))
    if bad:
        log(NETWORK_HELP.format(hosts=" and ".join(bad)))
        raise SystemExit(1)


NOW_OVERRIDE = None  # set to a timezone-aware datetime to test "what if it were Sunday night"


def et_offset_hours(y, m, d):
    """US Eastern offset from UTC on a date (daylight time from the 2nd Sunday of March to the 1st of November)."""
    def nth_sunday(year, month, n):
        first = date(year, month, 1)
        return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))
    return -4 if nth_sunday(y, 3, 2) <= date(y, m, d) < nth_sunday(y, 11, 1) else -5


def game_state(kick, now):
    """upcoming, live (kicked off within about 3.5 hours), or final."""
    if not kick or not now or now < kick:
        return "upcoming"
    return "live" if now < kick + timedelta(hours=3.5) else "final"


def pct_label(mult):
    v = round((mult - 1) * 100)
    return "0%" if v == 0 else f"{v:+d}%"


# --------------------------------------------------------------------------
# Data loading
# --------------------------------------------------------------------------
def sleeper(path):
    r = HTTP.get(SLEEPER + path, timeout=30)
    r.raise_for_status()
    return r.json()


def download_csv(url, cache_name, max_age_hours=None):
    """Download a CSV (cached on disk) and return a list of row dicts, or None on 404."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, cache_name)
    fresh = os.path.exists(path) and (
        max_age_hours is None or time.time() - os.path.getmtime(path) < max_age_hours * 3600
    )
    if not fresh:
        try:
            r = HTTP.get(url, timeout=180)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            with open(path, "wb") as f:
                f.write(r.content)
        except requests.RequestException as e:
            if not os.path.exists(path):
                log(f"  could not download {url}: {e}")
                return None
            log(f"  download failed ({e}); using older cached copy of {cache_name}")
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def load_sleeper_players():
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, "sleeper_players.json")
    stale = "--refresh" in sys.argv  # py fantasy.py --refresh  forces a fresh Sleeper download
    if not stale and os.path.exists(path) and time.time() - os.path.getmtime(path) < PLAYERS_CACHE_HOURS * 3600:
        with open(path, encoding="utf-8") as f:
            return apply_manual_status(json.load(f))
    data = sleeper("/players/nfl")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return apply_manual_status(data)


def apply_manual_status(db):
    if MANUAL_STATUS:
        want = {norm_name(k): v for k, v in MANUAL_STATUS.items()}
        for meta in db.values():
            nm = norm_name(meta_name(meta, ""))
            if nm in want and meta.get("team"):
                meta["injury_status"] = want[nm]
    return db


def load_schedule(season):
    """Returns (sched, avg_implied, results). sched[team][week] = matchup dict."""
    rows = download_csv(GAMES_URL, "games.csv", max_age_hours=6)
    if not rows:
        raise SystemExit("Could not load the NFL schedule (games.csv). Check your internet connection.")
    sched = defaultdict(dict)
    results = defaultdict(list)  # team -> [(week, points for, points against)] for games already played
    implied_all = []
    for r in rows:
        if r.get("season") != str(season) or r.get("game_type") != "REG":
            continue
        try:
            week = int(r["week"])
        except (KeyError, ValueError):
            continue
        home, away = norm_team(r.get("home_team")), norm_team(r.get("away_team"))
        spread = total = None
        try:
            spread = float(r["spread_line"])
            total = float(r["total_line"])
        except (KeyError, ValueError):
            pass
        if spread is not None and total is not None:
            imp_home, imp_away = (total + spread) / 2, (total - spread) / 2
            implied_all += [imp_home, imp_away]
        else:
            imp_home = imp_away = None
        try:
            hs, as_ = float(r["home_score"]), float(r["away_score"])
            results[home].append((week, hs, as_))
            results[away].append((week, as_, hs))
        except (KeyError, ValueError):
            pass
        kick = None
        try:
            y, mo, dd = (int(x) for x in r["gameday"].split("-"))
            hh, mi = (int(x) for x in (r.get("gametime") or "13:00").split(":")[:2])
            kick = datetime(y, mo, dd, hh, mi, tzinfo=timezone.utc) - timedelta(hours=et_offset_hours(y, mo, dd))
        except (KeyError, ValueError, AttributeError):
            pass
        sched[home][week] = {"opp": away, "home": True, "implied_for": imp_home,
                             "implied_against": imp_away, "spread": spread, "total": total, "kick": kick}
        sched[away][week] = {"opp": home, "home": False, "implied_for": imp_away,
                             "implied_against": imp_home,
                             "spread": -spread if spread is not None else None, "total": total, "kick": kick}
    avg = sum(implied_all) / len(implied_all) if implied_all else 22.5
    return sched, avg, results


SLEEPER_POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")


_FEED_WARNED = set()


def feed_problem(kind, why):
    if (kind, why) not in _FEED_WARNED:
        _FEED_WARNED.add((kind, why))
        log(f"  Sleeper's {kind} feed was not available ({why}).")


def sleeper_feed(kind, season, week, max_age_hours=None):
    """Weekly stat lines ('stats') or projections ('projections') straight from Sleeper, as {player_id: stats}.
    These endpoints are undocumented, so every failure just returns {} and the script carries on without them."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"sleeper_{kind}_{season}_{week}.json")
    if os.path.exists(path) and (max_age_hours is None or time.time() - os.path.getmtime(path) < max_age_hours * 3600):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        log(f"  {kind} week {week}: {len(data)} players (saved copy)")
        return data
    if _FEED_FAILS[kind] >= 2 and not os.path.exists(path):  # two misses in a row: stop wasting time
        return {}
    pos_q = "&".join(f"position[]={p}" for p in SLEEPER_POSITIONS)
    urls = [f"https://api.sleeper.app/v1/{kind}/nfl/regular/{season}/{week}",
            f"https://api.sleeper.app/{kind}/nfl/{season}/{week}?season_type=regular&{pos_q}"]
    started = time.time()
    for url in urls:
        try:
            r = FEED_HTTP.get(url, timeout=FEED_TIMEOUT)
            if r.status_code != 200:
                feed_problem(kind, f"HTTP {r.status_code}")
                continue
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            feed_problem(kind, type(e).__name__)
            continue
        items = data if isinstance(data, list) else [{"player_id": k, **(v if isinstance(v, dict) else {"stats": v})}
                                                     for k, v in (data or {}).items()]
        out = {}
        for it in items:
            if not isinstance(it, dict):
                continue
            pid, st = str(it.get("player_id") or ""), it.get("stats") or {}
            if pid and isinstance(st, dict) and st:
                out[pid] = st
        if out:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(out, f)
            _FEED_FAILS[kind] = 0
            log(f"  {kind} week {week}: {len(out)} players ({time.time() - started:.0f}s)")
            return out
    _FEED_FAILS[kind] += 1
    if os.path.exists(path):  # stale is better than nothing
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    log(f"  {kind} week {week}: not available" + (" (skipping the remaining weeks)" if _FEED_FAILS[kind] >= 2 else ""))
    return {}


def sleeper_points(stats, scoring, pos):
    """Fantasy points for a Sleeper stat line under a league's own scoring settings (how Sleeper scores it)."""
    if not stats:
        return None
    if pos in ("K", "DEF"):  # bucketed scoring is not reproducible from the stat line; use Sleeper's total
        v = stats.get("pts_ppr") if stats.get("pts_ppr") is not None else stats.get("pts_std")
        return float(v) if v is not None else None
    pts = 0.0
    for k, v in stats.items():
        w = scoring.get(k)
        if w and isinstance(v, (int, float)):
            pts += w * v
    return pts


def load_stat_rows(season, week):
    """All regular-season player-week rows played BEFORE the target week."""
    rows_out = []
    loaded = []
    for s in range(season, season - HISTORY_SEASONS, -1):
        raw = None
        for tmpl in STATS_URLS:
            age = 6 if s == season else None
            raw = download_csv(tmpl.format(season=s), f"stats_{s}_{tmpl.split('/')[-2]}.csv", max_age_hours=age)
            if raw:
                break
        if not raw:
            log(f"  no player stats found for {s} (skipping)")
            continue
        loaded.append(s)
        for r in raw:
            if r.get("season_type") not in (None, "", "REG"):
                continue
            try:
                s_i, wk = int(r.get("season") or s), int(r["week"])
            except (KeyError, ValueError):
                continue
            if s_i == season and wk >= week:
                continue
            pos = (r.get("position") or "").upper()
            pos = "RB" if pos == "FB" else pos
            if pos not in SKILL:
                continue
            stats = {k: num(r, *cols) for k, cols in STAT_COLUMNS.items()}
            stats["fum_lost"] = sum(num(r, c) for c in FUMBLE_COLUMNS)
            rows_out.append({
                "pid": r.get("player_id"),
                "name": r.get("player_display_name") or r.get("player_name") or "",
                "pos": pos,
                "team": norm_team(r.get("team") or r.get("recent_team")),
                "opp": norm_team(r.get("opponent_team")),
                "season": s_i, "week": wk, "s": stats,
            })
    if season not in loaded:
        raise SystemExit(
            f"Could not load {season} player stats from nflverse. If the season just started, "
            "the first week of data may not be published yet, or the file name changed."
        )
    rows_out.sort(key=lambda r: (r["season"], r["week"]))
    return rows_out


# --------------------------------------------------------------------------
# Scoring and the projection model
# --------------------------------------------------------------------------
def fantasy_points(s, sc, pos):
    pts = 0.0
    for k, v in s.items():
        pts += v * sc.get(k, 0)
    pts += max(0.0, s["pass_att"] - s["pass_cmp"]) * sc.get("pass_inc", 0)
    for yds, key, cuts in (
        (s["pass_yd"], "bonus_pass_yd_", (300, 400)),
        (s["rush_yd"], "bonus_rush_yd_", (100, 200)),
        (s["rec_yd"], "bonus_rec_yd_", (100, 200)),
    ):
        for c in cuts:
            if yds >= c:
                pts += sc.get(f"{key}{c}", 0)
    pts += s["rec"] * sc.get(f"bonus_rec_{pos.lower()}", 0)
    return pts


def build_defense_table(scored_rows, season):
    totals = defaultdict(float)
    for r in scored_rows:
        if r["opp"]:
            totals[(r["opp"], r["pos"], r["season"], r["week"])] += r["pts"]
    series = defaultdict(list)
    for (opp, pos, s, wk), t in totals.items():
        series[(opp, pos)].append((s, wk, t))
    avg, neff = {}, {}
    for key, lst in series.items():
        lst.sort()
        recent = lst[-W["def_window"]:][::-1]
        ws = [(W["def_decay"] ** i) * (W["prior_season_weight"] if s < season else 1.0)
              for i, (s, _, _) in enumerate(recent)]
        avg[key] = sum(w * t for w, (_, _, t) in zip(ws, recent)) / sum(ws)
        neff[key] = sum(ws)
    by_pos = defaultdict(list)
    for (_, pos), a in avg.items():
        by_pos[pos].append(a)
    league_avg = {pos: sum(v) / len(v) for pos, v in by_pos.items()}
    table = {}
    for (opp, pos), a in avg.items():
        raw = a / league_avg[pos] if league_avg[pos] else 1.0
        n = neff[(opp, pos)]
        mult = clamp(1 + (raw - 1) * n / (n + W["matchup_k"]), W["matchup_cap"])
        table[(opp, pos)] = {"mult": mult, "raw": raw}
    return table


class Model:
    def __init__(self, stat_rows, scoring, season, week, sched, avg_implied, players_db,
                 results=None, learn=None, historical=False, actuals=None, league_proj=None, now=None):
        self.season, self.week = season, week
        self.now = now
        self.actuals = actuals or {}
        self.league_proj = league_proj or {}   # week -> {sleeper player id: Sleeper's projection, league scoring}
        self.audit_rows = []                   # (gsis id, week, my computed points, Sleeper's actual points)
        self.gid_name = {}
        self.sched, self.avg_implied = sched, avg_implied
        self.players_db = players_db
        self.learn = learn or {}
        self.historical = historical  # backtests ignore today's injury news
        scored = []
        # gsis id -> [(season, week, opponent, points, opportunities, team)], oldest first
        self.games = defaultdict(list)
        self.name_index = {}
        team_weeks = defaultdict(set)
        vol = defaultdict(lambda: [0.0, 0.0])  # (team, week) -> [pass attempts, rush attempts], this season
        ppo = defaultdict(lambda: [0.0, 0.0])  # position -> [points, opportunities]
        for r in stat_rows:
            pts = fantasy_points(r["s"], scoring, r["pos"])
            if actuals and r["pid"]:  # Sleeper's own numbers beat anything recomputed from raw stats
                a = actuals.get((r["pid"], r["season"], r["week"]))
                if a is not None:
                    if abs(a - pts) > 0.01:
                        self.audit_rows.append((r["pid"], r["week"], pts, a))
                    pts = a
            scored.append({**r, "pts": pts})
            if r["pid"]:
                self.gid_name[r["pid"]] = r["name"]
                s = r["s"]
                opps = s["pass_att"] if r["pos"] == "QB" else s["rush_att"] + s["rec_tgt"]
                self.games[r["pid"]].append((r["season"], r["week"], r["opp"], pts, opps, r["team"]))
                self.name_index[(norm_name(r["name"]), r["pos"])] = r["pid"]
                if opps >= 1:
                    ppo[r["pos"]][0] += pts
                    ppo[r["pos"]][1] += opps
            if r["team"]:
                team_weeks[r["team"]].add((r["season"], r["week"]))
                if r["season"] == season:
                    v = vol[(r["team"], r["week"])]
                    v[0] += r["s"]["pass_att"]
                    v[1] += r["s"]["rush_att"]
        self.team_weeks = {t: sorted(w) for t, w in team_weeks.items()}
        self.ppo = {pos: v[0] / v[1] for pos, v in ppo.items() if v[1] > 0}
        self.defense = build_defense_table(scored, season)
        self.team_form = self._build_team_form(results or {}, vol)
        # Current teammates by team, with how "out" each one is right now
        self.by_team = defaultdict(list)
        self.unmatched = defaultdict(list)  # injured teammates we have no stats for
        for sid, meta in players_db.items():
            pos, gid = meta.get("position"), meta.get("gsis_id")
            t = norm_team(meta.get("team"))
            if pos in SKILL and t and gid not in self.games:
                gid = self.name_index.get((norm_name(meta_name(meta, "")), pos))
            w_out = 0.0 if historical else outage_weight(meta)
            if pos in SKILL and t and gid not in self.games and w_out > 0:
                self.unmatched[t].append((meta_name(meta, sid), pos, meta.get("injury_status") or "out"))
            if pos in SKILL and t and gid in self.games:
                self.by_team[t].append({
                    "gid": gid, "pos": pos, "w": w_out,
                    "name": meta_name(meta, sid),
                    "tag": meta.get("injury_status") or meta.get("status") or "out",
                    "depth": as_int(meta.get("depth_chart_order")),
                })

    def _build_team_form(self, results, vol):
        """How each offense has actually been playing this season: scoring and pass/rush volume."""
        k = W["team"]["k"]
        pts = {}
        for t, lst in results.items():
            vals = [pf for (wk, pf, _pa) in lst if wk < self.week]
            if vals:
                pts[t] = vals
        league_ppg = (sum(sum(v) / len(v) for v in pts.values()) / len(pts)) if pts else None
        per_team = defaultdict(list)
        for (t, _wk), (pa, ra) in vol.items():
            per_team[t].append((pa, ra))
        lg_pass = lg_rush = None
        if per_team:
            lg_pass = sum(sum(x[0] for x in v) / len(v) for v in per_team.values()) / len(per_team)
            lg_rush = sum(sum(x[1] for x in v) / len(v) for v in per_team.values()) / len(per_team)
        form = {}
        for t in set(pts) | set(per_team):
            f = {"n": 0, "ppg": None, "pts_raw": 1.0, "pts_ratio": 1.0, "pass_ratio": 1.0, "rush_ratio": 1.0}
            if t in pts and league_ppg:
                n = len(pts[t])
                ppg = sum(pts[t]) / n
                raw = ppg / league_ppg
                f.update(n=n, ppg=ppg, pts_raw=raw, pts_ratio=1 + (raw - 1) * n / (n + k))
            if t in per_team and lg_pass and lg_rush:
                n = len(per_team[t])
                pr = (sum(x[0] for x in per_team[t]) / n) / lg_pass
                rr = (sum(x[1] for x in per_team[t]) / n) / lg_rush
                f["pass_ratio"] = 1 + (pr - 1) * n / (n + k)
                f["rush_ratio"] = 1 + (rr - 1) * n / (n + k)
            form[t] = f
        return form

    def _sleeper(self, sid, wk):
        v = self.league_proj.get(wk, {}).get(sid)
        return v

    @staticmethod
    def _blend(S, Pm):
        """Sleeper's number first; the model may move it only a little (a share of the gap, capped)."""
        if S is None:
            return Pm
        cfg = W["blend"]
        cap = max(cfg["cap"] * abs(S), cfg["min_cap"])
        return max(0.0, S + clamp(cfg["model_weight"] * (Pm - S), (-cap, cap)))

    def gsis_for(self, meta, pos):
        gid = meta.get("gsis_id")
        if gid and gid in self.games:
            return gid
        return self.name_index.get((norm_name(meta_name(meta, "")), pos))

    # ---- form: this season first ---------------------------------------------
    def _season_weight(self, season):
        sw = W["season_weight"]
        return sw.get(max(0, self.season - season), sw[max(sw)])

    def _wavg(self, games):
        """Decayed weighted (mean, sd, weight sum) over a chronological list of games."""
        recent = games[-W["window"]:][::-1]
        if not recent:
            return None
        ws = [(W["decay"] ** i) * self._season_weight(g[0]) for i, g in enumerate(recent)]
        vs = [g[3] for g in recent]
        wsum = sum(ws)
        mean = sum(w * v for w, v in zip(ws, vs)) / wsum
        var = sum(w * (v - mean) ** 2 for w, v in zip(ws, vs)) / wsum
        return mean, math.sqrt(var), wsum

    def _form(self, games):
        """(mean, sd, evidence). This season's games lead; older seasons fill in while the sample is small."""
        cur = [g for g in games if g[0] == self.season]
        old = [g for g in games if g[0] < self.season]
        om = self._wavg(old)
        cm = None
        if cur:
            ordered = cur[::-1]
            ws = [W["cur_decay"] ** i for i in range(len(ordered))]
            vs = [g[3] for g in ordered]
            wsum = sum(ws)
            mean = sum(w * v for w, v in zip(ws, vs)) / wsum
            var = sum(w * (v - mean) ** 2 for w, v in zip(ws, vs)) / wsum
            cm = (mean, math.sqrt(var))
        if cm is None and om is None:
            return None
        n = len(cur)
        evidence = n + 0.5 * min(len(old), 8)
        if cm is None:
            return om[0], om[1], evidence
        if om is None:
            return cm[0], max(cm[1], 0.3 * cm[0]), evidence
        alpha = n / (n + W["cur_k"])  # one game ~50% this season, three ~75%, eight ~89%
        mean = alpha * cm[0] + (1 - alpha) * om[0]
        var = alpha * cm[1] ** 2 + (1 - alpha) * om[1] ** 2 + alpha * (1 - alpha) * (cm[0] - om[0]) ** 2
        return mean, max(math.sqrt(var), 0.25 * mean), evidence

    def usage(self, gid):
        """Average opportunities (rush attempts plus targets; pass attempts for QBs), last 6 games."""
        gs = self.games.get(gid, [])[-6:]
        return sum(g[4] for g in gs) / len(gs) if gs else 0.0

    # ---- is his production sustainable? ----------------------------------------
    def profile(self, gid, pos, team):
        """Season production vs. what his workload and schedule justify. None with under 2 games."""
        games = self.games.get(gid, [])
        cur = [g for g in games if g[0] == self.season]
        ppo = self.ppo.get(pos)
        if len(cur) < 2 or not ppo:
            return None
        n = len(cur)
        actual = sum(g[3] for g in cur) / n
        vol_exp = sum(g[4] * ppo for g in cur) / n
        prior = self._wavg([g for g in games if g[0] < self.season])
        expected = 0.6 * vol_exp + 0.4 * prior[0] if prior else vol_exp
        mults = [self.defense[(g[2], pos)]["mult"] for g in cur if (g[2], pos) in self.defense]
        past = sum(mults) / len(mults) if mults else 1.0
        fut = []
        for wk in range(self.week, min(self.week + AHEAD_WEEKS, 19)):
            mm = self.sched.get(team, {}).get(wk)
            if mm and (mm["opp"], pos) in self.defense:
                fut.append(self.defense[(mm["opp"], pos)]["mult"])
        future = sum(fut) / len(fut) if fut else None
        opps = [g[4] for g in cur]
        trend = (sum(opps[-3:]) / len(opps[-3:])) / (sum(opps) / n) if n >= 4 and sum(opps) > 0 else 1.0
        return {"n": n, "actual": actual, "expected": expected,
                "over": actual / expected if expected > 0 else 1.0,
                "past": past, "future": future, "trend": trend}

    def flags(self, p, prof):
        """Sell-high and buy-low signals with plain-English reasons."""
        sell, buy = [], []
        ss = bs = 0.0
        over, past, fut, trend = prof["over"], prof["past"], prof["future"], prof["trend"]
        inflated = p["inj_boost"] < 0.95 and not p["inj_names"] and bool(p["inj_text"])
        growing = p["inj_boost"] >= 1.10 and bool(p["inj_names"])
        if over >= 1.2:
            ss += min((over - 1.15) / 0.15, 2.0)
            sell.append(f"He is scoring {prof['actual']:.1f} a game, but his workload normally produces about "
                        f"{prof['expected']:.1f}. Touchdowns and big plays at that rate tend to fade.")
        if past >= 1.04:
            ss += min((past - 1.0) / 0.05, 1.5)
            sell.append(f"He has faced soft defenses for his position ({pct_label(past)} vs the league average).")
        if fut is not None and fut <= 0.98:
            ss += min((1.0 - fut) / 0.04, 1.5)
            sell.append(f"His next {AHEAD_WEEKS} opponents are tougher ({pct_label(fut)} vs the average).")
        if inflated:
            ss += 1.5
            sell.append("His recent numbers were boosted by teammates who were hurt. That starter is healthy again.")
        if trend <= 0.8:
            ss += 1.0
            sell.append("His workload has dropped over the last 3 games compared with the season.")
        u = 1 / over if over > 0 else 1.0
        if over <= 0.88:
            bs += min((u - 1.1) / 0.15, 2.0)
            buy.append(f"He is scoring {prof['actual']:.1f} a game, but his workload normally produces about "
                       f"{prof['expected']:.1f}. A bounce back is likely.")
        if past <= 0.97:
            bs += min((1.0 - past) / 0.04, 1.5)
            buy.append(f"He has faced tough defenses for his position ({pct_label(past)} vs the league average).")
        if fut is not None and fut >= 1.02:
            bs += min((fut - 1.0) / 0.04, 1.5)
            buy.append(f"His next {AHEAD_WEEKS} opponents are friendlier ({pct_label(fut)} vs the average).")
        if growing:
            bs += 1.5
            buy.append(f"His role is growing with {join_names(p['inj_names'])} out.")
        if trend >= 1.2:
            bs += 1.0
            buy.append("His workload has climbed over the last 3 games compared with the season.")
        p["sell_score"] = ss if (ss >= 2.0 and (over >= 1.15 or inflated or past >= 1.05)) else 0.0
        p["buy_score"] = bs if (bs >= 2.0 and (over <= 0.9 or growing or trend >= 1.2)) else 0.0
        p["sell"] = sell if p["sell_score"] else []
        p["buy"] = buy if p["buy_score"] else []

    # ---- teammate injuries and depth chart -------------------------------------
    def _injury_adjust(self, gid, pos, team, base_all, games, name, debug=None, depth=None):
        """Next-man-up adjustment. Returns (new_base, explanation, [teammate names]) or None.

        * Position matters: players ahead of him at his position are ranked (by the depth chart when
          Sleeper has it, otherwise by workload and production). With starters out he moves up that
          ladder, and a higher spot gets a bigger share of the vacated volume.
        * Volume is split in proportion to workload and recent production, never handed over one for one,
          and converted to points with a blend of his own efficiency and the injured starters'.
        * Games without the starter (this season and last only) are compared with games with him; that
          history is trusted more the more recent games without the starter we have.
        * A player scoring well below his own norm this season gets a smaller boost.
        base_all is the player's current-form average (before any adjustment).
        """
        if pos == "QB" or not games:
            return None
        mine = {(g[0], g[1]): g for g in games if g[5] == team}
        if not mine:
            return None
        inj = W["inj"]
        group = {"RB"} if pos == "RB" else {"WR", "TE"}

        debug = debug if debug is not None else []
        injured, healthy = [], []
        for t in self.by_team.get(team, []):
            if t["gid"] == gid:
                continue
            if t["pos"] == "QB":
                rel, floor = 1.0, inj["qb_min_attempts"]
            elif t["pos"] == pos:
                rel, floor = 1.0, inj["min_opps"]
            elif {t["pos"], pos} == {"WR", "TE"}:
                rel, floor = inj["cross_wr_te"], inj["min_opps"]
            else:
                continue
            use = self.usage(t["gid"])
            if use >= floor:
                (injured if t["w"] > 0 else healthy).append((use * rel, use, rel, t))
            elif t["w"] > 0:
                debug.append(f"{t['name']} ({t['tag']}) was ignored: only {use:.1f} touches a game, "
                             f"below the {floor:g} needed to count as a key player.")
        injured.sort(key=lambda c: -c[0])
        healthy.sort(key=lambda c: -c[0])
        cands = injured[:4] + healthy[:2]  # injured teammates always get considered

        # Pass 1: for each teammate, which weeks was he absent?
        found = []
        for _, use, rel, t in cands:
            theirs = {(g[0], g[1]): g for g in self.games[t["gid"]] if g[5] == team}
            if not theirs:
                continue
            first = min(theirs)
            here = {wk for wk, g in theirs.items() if g[4] >= max(2.0, 0.35 * use)}
            absent = {wk for wk in self.team_weeks.get(team, []) if wk >= first and wk not in here}
            found.append((use, rel, t, first, absent))
        if not found:
            return None
        if not any(f[2]["w"] > 0 or any(wk in mine for wk in f[4]) for f in found):
            return None  # nobody is out now and no past absences touched this player's games
        for f in found:
            if f[2]["w"] > 0:
                debug.append(f"{f[2]['name']} ({f[2]['tag']}) counted: {f[0]:.1f} touches a game, "
                             f"missed {len([w for w in f[4] if w in mine])} games this player also played.")

        union = set().union(*(f[4] for f in found))
        earliest = min(f[3] for f in found)
        pres_all = [g for wk, g in sorted(mine.items()) if wk >= earliest and wk not in union]
        pa = self._form(pres_all)
        n_pres = len(pres_all)
        base_pres_all = pa[0] if pa else base_all
        pres_world = (n_pres * base_pres_all + inj["pres_k"] * base_all) / (n_pres + inj["pres_k"])
        recent_pres = pres_all[-6:]
        p_use = (sum(g[4] for g in recent_pres) / len(recent_pres)) if recent_pres else self.usage(gid)

        # Depth ladder at his position: who is ahead of him, and who is actually available?
        members = []
        for t in self.by_team.get(team, []):
            if t["gid"] == gid or t["pos"] not in group:
                continue
            f = self._form(self.games[t["gid"]])
            use = self.usage(t["gid"])
            if use < 1.0 and (not f or f[0] < 1.0):
                continue
            members.append({"use": use, "pts": f[0] if f else 0.0, "depth": t["depth"], "out": t["w"] >= 0.5})
        me = {"use": p_use, "pts": max(base_all, 0.0), "depth": depth, "out": False}
        allm = members + [me]
        use_max = max(m["use"] for m in allm) or 1.0
        pts_max = max(m["pts"] for m in allm) or 1.0
        for m in allm:
            m["score"] = 0.5 * m["use"] / use_max + 0.5 * m["pts"] / pts_max
        use_depth = pos in ("RB", "TE") and all(m["depth"] for m in allm)
        order = (lambda ms: sorted(ms, key=lambda m: m["depth"])) if use_depth else \
                (lambda ms: sorted(ms, key=lambda m: -m["score"]))
        rank_before = order(allm).index(me) + 1
        avail = order([m for m in allm if not m["out"]])
        for i, m in enumerate(avail):
            m["rank_after"] = i + 1
        rank_after = me["rank_after"]
        use_tot = sum(m["use"] for m in avail) or 1.0
        pts_tot = sum(m["pts"] for m in avail) or 1.0
        w_use = inj["share_by_usage"]
        for m in avail:
            m["a"] = W["depth_weight"].get(m["rank_after"], 0.1) * (
                w_use * m["use"] / use_tot + (1 - w_use) * m["pts"] / pts_tot)
        share = me["a"] / (sum(m["a"] for m in avail) or 1.0)

        # Form check: is he scoring well below what he used to? Then volume alone won't help as much.
        cur = [g[3] for g in games if g[0] == self.season]
        older_ref = self._wavg([g for g in games if g[0] < self.season])
        damp = 1.0
        if len(cur) >= 2 and older_ref and older_ref[0] > 3:
            damp = clamp((sum(cur) / len(cur)) / older_ref[0], (inj["form_floor"], 1.0))

        # Pass 2: estimate this player's scoring with each teammate out
        entries = []
        for use, rel, t, first, absent in found:
            hist_min = self.season - inj["history_seasons"] + 1
            all_abs = [mine[wk] for wk in sorted(absent) if wk in mine]
            abs_g = [g for g in all_abs if g[0] >= hist_min]  # older absences say little about now
            if len(all_abs) > len(abs_g) and t["w"] > 0:
                debug.append(f"Ignored {len(all_abs) - len(abs_g)} games without {t['name']} from more than "
                             "a season ago.")
            with_g = [g for wk, g in sorted(mine.items())
                      if wk >= first and wk not in absent and g[0] >= hist_min]
            ab, pr = self._form(abs_g), self._form(with_g)
            base_abs = ab[0] if ab else None
            base_with = pr[0] if pr else base_pres_all
            # Recent games without him count fully; last season's count about half.
            n_abs = sum(self._season_weight(g[0]) for g in abs_g) / self._season_weight(self.season)
            new_opps = None
            if t["pos"] == "QB":
                prior = inj["qb_out_prior"]
            else:
                alpha = inj["absorb"].get(pos, 0.5)
                new_opps = p_use + alpha * rel * use * share
                sf = self._form(self.games[t["gid"]])
                eff_s = (sf[0] if sf else 0.0) / max(use, 1.0)
                eff_me = max(base_all, 0.0) / max(p_use, 1.5)
                eff = inj["eff_own"] * eff_me + (1 - inj["eff_own"]) * eff_s  # not full starter efficiency
                prior = clamp(new_opps * eff / max(base_with, 1.0), (1.0, inj["prior_cap"]))
            w_abs = n_abs / (n_abs + inj["abs_k"])
            est = (w_abs * base_abs if base_abs is not None else 0.0) + (1 - w_abs) * base_with * prior
            entries.append({"t": t, "use": use, "w": t["w"], "ratio": est / max(base_with, 1.0),
                            "n_abs": n_abs, "base_abs": base_abs, "base_with": base_with,
                            "new_opps": new_opps})

        factor = 1.0
        for e in entries:
            if e["w"] > 0:
                factor *= 1 + (e["ratio"] - 1) * e["w"]
        factor = clamp(factor, inj["factor_cap"])
        if factor > 1 and damp < 1:
            factor = 1 + (factor - 1) * damp
            debug.append(f"Scoring {1 - damp:.0%} below his usual level this season, so the boost was cut "
                         "by that much.")
        new_base = pres_world * factor
        if abs(new_base / max(base_all, 0.1) - 1) < 0.03:
            if any(e["w"] > 0 for e in entries):
                debug.append("The adjustment came out under 3%, so it was not applied.")
            return None

        out = [e for e in entries if e["w"] > 0]
        if out:
            e = max(out, key=lambda e: e["w"] * (e["ratio"] - 1))
            t = e["t"]
            label = t["tag"] if e["w"] < 1 else "out"
            unit = "pass attempts" if t["pos"] == "QB" else "touches and targets"
            text = [f"{t['name']} ({label}) averages {e['use']:.0f} {unit} a game."]
            if rank_after < rank_before:
                text.append(f"{name} moves from #{rank_before} to #{rank_after} among the available "
                            f"{'RBs' if pos == 'RB' else 'pass catchers'} "
                            f"({'depth chart' if use_depth else 'workload and production'}).")
            if e["new_opps"]:
                if round(e["new_opps"]) > round(p_use):
                    text.append(f"His workload should rise from about {p_use:.0f} to {e['new_opps']:.0f}, "
                                "a share of the vacated volume, not a straight handoff.")
                else:
                    text.append(f"He should see only a small bump in workload, since most of the "
                                "vacated volume goes to other players.")
            if e["n_abs"] >= 0.5 and e["base_abs"] is not None:
                text.append(f"In recent games without him, {name} averaged {e['base_abs']:.1f} points "
                            f"vs {e['base_with']:.1f} with him (games from this year count most).")
            else:
                text.append("There are no recent games without him, so this leans on the usage estimate.")
            if damp < 0.9:
                text.append(f"{name} is scoring below his usual level this season, so the boost is reduced.")
            return new_base, " ".join(text), [x["t"]["name"] for x in out]
        e = max(entries, key=lambda e: e["n_abs"])
        if new_base < base_all * 0.95 and e["n_abs"]:
            return (new_base, f"Recent scoring was boosted by games without {e['t']['name']}, who is healthy "
                              "now, so this projection leans on games with him in the lineup.", [])
        return None

    def _backup_qb(self, gid, team, base, n_games, name):
        """A backup QB with little history: assume a share of the injured starter's production."""
        for t in self.by_team.get(team, []):
            if t["gid"] == gid or t["pos"] != "QB" or t["w"] < 0.75:
                continue
            if self.usage(t["gid"]) < W["inj"]["qb_min_attempts"] or n_games >= 4:
                continue
            s = self._form(self.games[t["gid"]])
            if s and s[0] * W["inj"]["backup_qb_share"] > base:
                return (s[0] * W["inj"]["backup_qb_share"],
                        f"{t['name']} is out. With little game history of his own, this assumes "
                        f"about {W['inj']['backup_qb_share']:.0%} of the starter's recent production.",
                        [t["name"]])
        return None

    # ---- the projection -------------------------------------------------------
    def project(self, sid, week=None):
        meta = self.players_db.get(sid, {})
        pos = meta.get("position")
        team = norm_team(meta.get("team") or (sid if pos == "DEF" else None))
        name = meta_name(meta, sid)
        injury = None if self.historical else meta.get("injury_status")
        wk = week or self.week
        ahead = wk - self.week  # 0 = this week; later weeks assume injuries fade
        m = self.sched.get(team, {}).get(wk) if team else None
        p = {
            "sid": sid, "name": name, "pos": pos, "team": team, "team_show": show_team(team),
            "injury": injury, "opp": None, "home": None, "implied_for": None,
            "implied_against": None, "base": 0.0, "base_form": 0.0, "f_match": 1.0, "f_vegas": 1.0,
            "f_hist": 1.0, "f_home": 1.0, "f_inj": 1.0, "f_team": 1.0, "f_learn": 1.0,
            "proj": 0.0, "floor": 0.0, "ceil": 0.0, "n_games": 0, "status": "ok", "note": "",
            "match_raw": None, "inj_text": "", "inj_boost": 1.0, "inj_names": [], "inj_debug": [],
            "l3": None, "season_avg": None, "season_max": None, "cur_n": 0,
            "team_ppg": None, "team_pts_raw": None, "anchor_note": "", "learn_note": "",
            "proj_raw": 0.0, "hist_n": 0, "week": wk, "f_sust": 1.0, "over_ratio": None,
            "expected_avg": None, "past_mult": None, "future_mult": None, "usage_trend": None,
            "sell": [], "buy": [], "sell_score": 0.0, "buy_score": 0.0,
            "sleeper_proj": None, "model_proj": 0.0, "sleeper_note": "", "log": [],
            "game_state": "upcoming", "kick": None, "actual_now": None,
        }
        if not team or team == "FA":
            p.update(status="nodata", note="No NFL team")
            return p
        if not m:
            p.update(status="bye", note="Bye week")
            return p
        p.update(opp=m["opp"], home=m["home"], implied_for=m["implied_for"],
                 implied_against=m["implied_against"])
        if ahead == 0 and not self.historical:
            p["kick"] = m.get("kick")
            p["game_state"] = game_state(m.get("kick"), self.now)

        if injury in OUT_STATUSES and (ahead == 0 or injury in LONG_TERM):
            p.update(status="out", note=injury)
        inj_mult = INJURY_MULT.get(injury, 0.0 if injury in OUT_STATUSES else 1.0)
        if ahead > 0 and injury not in LONG_TERM and inj_mult < 1:
            inj_mult = 1 - (1 - inj_mult) * W["future"]["own_persist"] ** ahead

        if pos == "DEF":
            return self._estimate(p, 7.5, 0.55, m["implied_against"], inverse=True)
        if pos == "K":
            return self._estimate(p, 7.8, 0.30, m["implied_for"], inverse=False)

        gid = self.gsis_for(meta, pos)
        games = self.games.get(gid, []) if gid else []
        p["n_games"] = len(games)

        repl = REPLACEMENT.get(pos, 5.0)
        stats = self._form(games)
        mean = wsum = 0.0
        if stats:
            mean, sd, wsum = stats
            p["base"] = (wsum * mean + W["prior_pseudo_games"] * repl) / (wsum + W["prior_pseudo_games"])
        else:
            p["base"] = repl * 0.7
            sd = repl * 0.6
            if p["status"] == "ok":
                p["status"] = "nodata"
                p["note"] = "No NFL game history found"
        p["base_form"] = p["base"]

        cur = [g[3] for g in games if g[0] == self.season]
        if games:
            p["l3"] = sum(g[3] for g in games[-3:]) / len(games[-3:])
            p["season_avg"] = sum(cur) / len(cur) if cur else None
            p["season_max"] = max(cur) if cur else None
            p["cur_n"] = len(cur)
            p["log"] = [round(x, 1) for x in cur]

        # Teammate injuries and the depth chart: next man up (and the reverse when a starter returns)
        for nm, tpos, tag in self.unmatched.get(team, []):
            if tpos == pos or {tpos, pos} == {"WR", "TE"}:
                p["inj_debug"].append(f"{nm} ({tag}) is injured but could not be matched to any NFL stats, "
                                      "so his absence is not counted.")
        if not games and pos in SKILL:
            p["inj_debug"].append("This player could not be matched to NFL game history, so no role "
                                  "adjustment is possible.")
        adj = None
        if games and pos in SKILL:
            adj = (self._backup_qb(gid, team, p["base"], len(games), name) if pos == "QB"
                   else self._injury_adjust(gid, pos, team, mean, games, name, p["inj_debug"],
                                            as_int(meta.get("depth_chart_order"))))
        if adj:
            before = p["base"]
            new_raw, p["inj_text"], p["inj_names"] = adj
            adjusted = (wsum * new_raw + W["prior_pseudo_games"] * repl) / (wsum + W["prior_pseudo_games"])
            if ahead > 0:  # teammates' injuries are assumed to matter less the further out we look
                adjusted = before + (adjusted - before) * W["future"]["team_persist"] ** ahead
            p["base"] = adjusted
            p["inj_boost"] = p["base"] / max(before, 0.1)

        d = self.defense.get((m["opp"], pos))
        if d:
            p["f_match"] = d["mult"]
            p["match_raw"] = d["raw"]

        if m["implied_for"]:
            w = W["vegas"].get(pos, 0.5)
            p["f_vegas"] = clamp(1 + w * (m["implied_for"] / self.avg_implied - 1), W["vegas_cap"])

        # How this offense has actually been playing this season
        tf = self.team_form.get(team)
        if tf and pos in SKILL and tf["n"]:
            tc = W["team"]
            vol = tf["rush_ratio"] if pos == "RB" else tf["pass_ratio"]
            raw = 1 + tc["pts"][pos] * (tf["pts_ratio"] - 1) + tc["vol"][pos] * (vol - 1)
            p["f_team"] = clamp(raw, tc["cap"])
            p["team_ppg"], p["team_pts_raw"] = tf["ppg"], tf["pts_raw"]

        opp_games = [g[3] for g in games if g[2] == m["opp"]]
        if opp_games and games:
            overall = sum(g[3] for g in games) / len(games)
            if overall > 0:
                ratio = (sum(opp_games) / len(opp_games)) / overall
                n = len(opp_games)
                p["f_hist"] = clamp(1 + (ratio - 1) * n / (n + W["history_k"]), W["history_cap"])
            p["hist_n"] = len(opp_games)

        p["f_home"] = 1 + W["home_edge"] if m["home"] else 1 - W["home_edge"]
        p["f_inj"] = inj_mult

        # What the model has learned from its own misses on this roster this season
        lf = self.learn.get("player", {}).get(sid)
        pf = self.learn.get("pos", {}).get(pos, 1.0)
        p["f_learn"] = clamp(pf * (lf["f"] if lf else 1.0), W["learn"]["cap"])
        if lf and abs(lf["f"] - 1) >= 0.05:
            p["learn_note"] = (f"Over {lf['n']} games this season, my earlier projections for him averaged "
                               f"{lf['proj']:.1f} against {lf['actual']:.1f} actual, so this one is adjusted "
                               f"{pct_label(p['f_learn'])}.")
        elif abs(pf - 1) >= 0.05:
            p["learn_note"] = (f"My projections for {pos}s on this roster have run "
                               f"{'high' if pf < 1 else 'low'} this season, so this one is adjusted "
                               f"{pct_label(p['f_learn'])}.")

        # Is his production sustainable? Regress touchdown-driven spikes (and slumps) a little.
        prof = self.profile(gid, pos, team) if games and pos in SKILL else None
        if prof:
            p.update(over_ratio=prof["over"], expected_avg=prof["expected"], past_mult=prof["past"],
                     future_mult=prof["future"], usage_trend=prof["trend"])
            if abs(prof["over"] - 1) > 0.15:
                n = prof["n"]
                p["f_sust"] = clamp(1 - W["sust"]["strength"] * (prof["over"] - 1) * n / (n + 3), W["sust"]["cap"])
            self.flags(p, prof)

        mult = (p["f_match"] * p["f_vegas"] * p["f_hist"] * p["f_home"] * p["f_inj"]
                * p["f_team"] * p["f_learn"] * p["f_sust"])
        p["proj_raw"] = proj = p["base"] * mult

        # Reality check: don't project far beyond anything he has done this season unless his role changed
        if len(cur) >= 2 and proj > 0:
            a = W["anchor"]
            role_credit = clamp((p["inj_boost"] - 1) / 0.5, (0.0, 1.0))
            hi = max(cur) * (a["over"] + a["role_extra"] * role_credit)
            lo = min(cur) * a["under"]
            if proj > hi:
                new = hi + a["soft"] * (proj - hi)
                p["anchor_note"] = (f"Pulled down from {proj:.1f} to {new:.1f}: his best game this season is "
                                    f"{max(cur):.1f}" + ("" if role_credit else " and nothing about his role "
                                    "has changed enough to justify more") + ".")
                proj = new
            elif proj < lo and inj_mult == 1.0:
                new = lo - a["soft"] * (lo - proj)
                p["anchor_note"] = (f"Lifted from {proj:.1f} to {new:.1f}: his worst game this season is "
                                    f"{min(cur):.1f}.")
                proj = new
        p["model_proj"] = proj
        S = self._sleeper(sid, wk)
        p["sleeper_proj"] = S
        if S is not None:
            final = self._blend(S, proj)
            if len(cur) >= 2 and final > 0:  # even Sleeper's number should not be wildly beyond this season's best
                hi2 = max(cur) * (W["blend"]["final_cap"] + 0.30 * clamp((p["inj_boost"] - 1) / 0.5, (0.0, 1.0)))
                if final > hi2:
                    shaded = hi2 + 0.5 * (final - hi2)
                    p["sleeper_note"] = (f"Shaded from {final:.1f} to {shaded:.1f}: his best game this season is "
                                         f"{max(cur):.1f}.")
                    final = shaded
            proj = final
        elif self.league_proj.get(wk):
            p["sleeper_note"] = "Sleeper had no projection for him, so the model's number is used."
        scale = proj / p["proj_raw"] if p["proj_raw"] > 0 else 1.0
        p["proj"] = proj
        p["floor"] = max(0.0, (proj - 0.9 * sd * mult * scale))
        p["ceil"] = proj + 1.1 * sd * mult * scale
        if p["status"] == "ok" and 0 < p["n_games"] < 4:
            p["note"] = "Limited game history"
        return p

    def _estimate(self, p, base, slope, implied, inverse):
        """Kickers and defenses: a rough, Vegas-only estimate."""
        p["base"] = base
        p["n_games"] = 0
        if implied is None:
            p["proj"] = base
        elif inverse:
            p["proj"] = max(0.0, base + slope * (self.avg_implied - implied))
        else:
            p["proj"] = max(0.0, base + slope * (implied - self.avg_implied))
        p["f_vegas"] = p["proj"] / base
        p["proj_raw"] = p["model_proj"] = p["proj"]
        S = self._sleeper(p["sid"], p["week"])
        p["sleeper_proj"] = S
        if S is not None:
            p["proj"] = self._blend(S, p["proj"])
        p["floor"], p["ceil"] = max(0.0, p["proj"] - 4), p["proj"] + 6
        if p["status"] == "ok":
            p["note"] = "Estimate from Vegas lines only"
        return p


# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# Lineup building
# --------------------------------------------------------------------------
def build_lineup(slots, cands, fixed=None, excluded=None):
    """Fill the slots. `fixed` maps slot index to a player who is locked in (his game has started), and
    `excluded` holds players who cannot be added because their game has already started."""
    fixed, excluded = fixed or {}, excluded or set()
    chosen, used = dict(fixed), {c["sid"] for c in fixed.values()}
    order = sorted((i for i, s in enumerate(slots) if s in ELIGIBLE and i not in fixed),
                   key=lambda i: (SLOT_ORDER.index(slots[i]), i))
    for i in order:
        pool = [c for c in cands if c["pos"] in ELIGIBLE[slots[i]] and c["sid"] not in used
                and c["sid"] not in excluded]
        if pool:
            best = max(pool, key=lambda c: c["proj"])
            chosen[i] = best
            used.add(best["sid"])
    return chosen, used


def find_close_calls(slots, chosen, used, cands, fixed=None, excluded=None):
    fixed, excluded = fixed or {}, excluded or set()
    pairs = []
    for i, c in chosen.items():
        if c["proj"] <= 0 or i in fixed:
            continue
        alts = [a for a in cands if a["pos"] in ELIGIBLE[slots[i]]
                and a["sid"] not in used and a["sid"] not in excluded and a["proj"] > 0]
        if not alts:
            continue
        alt = max(alts, key=lambda a: a["proj"])
        gap = c["proj"] - alt["proj"]
        if gap <= max(CLOSE_CALL_PTS, CLOSE_CALL_PCT * c["proj"]):
            pairs.append((gap, i, c, alt))
    pairs.sort(key=lambda t: t[0])
    calls, seen = [], set()
    for gap, i, c, alt in pairs:
        if c["sid"] in seen or alt["sid"] in seen:
            continue
        seen |= {c["sid"], alt["sid"]}
        calls.append({"id": f"{c['sid']}-{alt['sid']}", "slot": slots[i], "slot_index": i,
                      "start": c, "sit": alt, "gap": gap})
    return calls


def explain(call):
    a, b = call["start"], call["sit"]

    def lr(x, y):
        return math.log(x / y) if x > 0 and y > 0 else 0.0

    comps = [("base", lr(a["base"], b["base"])), ("match", lr(a["f_match"], b["f_match"])),
             ("vegas", lr(a["f_vegas"], b["f_vegas"])), ("hist", lr(a["f_hist"], b["f_hist"])),
             ("inj", lr(a["f_inj"], b["f_inj"]))]
    top = [c for c in sorted(comps, key=lambda c: -c[1]) if c[1] > 0.01][:2]

    def reason(key, x, y):
        if key == "base":
            if x["inj_boost"] >= 1.1 and x["inj_names"]:
                return (f"{x['name']} steps into a bigger role with {join_names(x['inj_names'])} out "
                        f"({x['base']:.1f} vs {y['base']:.1f} expected points a game before matchup)")
            return (f"{x['name']} has been scoring more lately ({x['base']:.1f} vs {y['base']:.1f} "
                    "points per game, with recent games counting most)")
        if key == "match":
            return (f"{x['name']} has the friendlier matchup ({show_team(x['opp'])} allows "
                    f"{pct_label(x['f_match'])} to {x['pos']}s vs {pct_label(y['f_match'])} for "
                    f"{y['name']}'s opponent)")
        if key == "vegas":
            return (f"{x['name']}'s team is expected to score more "
                    f"({x['implied_for']:.1f} vs {y['implied_for']:.1f} implied points)")
        if key == "hist":
            return f"{x['name']} has historically done better against {show_team(x['opp'])}"
        if key == "inj":
            return f"{y['name']} carries an injury tag ({y['injury']})"
        return ""

    against = [c for c in sorted(comps, key=lambda c: c[1]) if c[1] < -0.02][:1]

    parts = [f"{a['name']} projects at {a['proj']:.1f} points and {b['name']} at {b['proj']:.1f}."]
    if top:
        bits = [reason(k, a, b) for k, _ in top if reason(k, a, b)]
        if bits:
            parts.append("The edge: " + "; and ".join(bits) + ".")
        if against and reason(against[0][0], b, a):
            parts.append(f"Working the other way: {reason(against[0][0], b, a)}.")
    else:
        parts.append("The model sees almost no difference between them.")
    if b["floor"] > a["floor"] + 1:
        parts.append(f"{b['name']} has the safer floor ({b['floor']:.0f} vs {a['floor']:.0f}), "
                     "so pick them if you are protecting a lead.")
    if a["ceil"] > b["ceil"] + 1:
        parts.append(f"{a['name']} has more upside ({a['ceil']:.0f} vs {b['ceil']:.0f}), "
                     "which matters if you are chasing points.")
    parts.append("At this gap the pick can flip on one piece of news, so check inactives before kickoff.")
    return " ".join(parts)


def wavg_weeks(vals):
    """Average of upcoming weeks, nearer weeks counting more."""
    ws = [1.0, 0.8, 0.6, 0.5, 0.4][:len(vals)]
    return sum(w * v for w, v in zip(ws, vals)) / sum(ws)


def build_outlook(model, slots, cands, chosen, week):
    """Project every roster player for this week and the next few; total up the model's lineup per week."""
    weeks = [w for w in range(week, week + AHEAD_WEEKS) if w <= 18]
    by_week = {w: {p["sid"]: (p if w == week else model.project(p["sid"], w)) for p in cands} for w in weeks}
    starter_ids = {c["sid"] for c in chosen.values()}
    totals, byes = {}, {}
    for w in weeks:
        lineup, _ = build_lineup(slots, list(by_week[w].values()))
        totals[w] = sum(c["proj"] for c in lineup.values())
        byes[w] = [p["name"] for sid, p in by_week[w].items() if p["status"] == "bye" and sid in starter_ids]
    avg = {}
    for sid, p0 in by_week[weeks[0]].items():
        ws = weeks if (p0["game_state"] == "upcoming" or len(weeks) == 1) else weeks[1:]
        avg[sid] = wavg_weeks([by_week[w][sid]["proj"] for w in ws])
    return {"weeks": weeks, "by_week": by_week, "totals": totals, "byes": byes, "avg": avg}


def find_waiver(model, ctx, rostered, slots, chosen, cands, outlook):
    """Best free agents: judged by upcoming weeks, and by how much they upgrade a real starter."""
    db, season = ctx["players_db"], ctx["season"]
    weeks = outlook["weeks"]
    pool = []
    for sid, meta in db.items():
        pos, team = meta.get("position"), norm_team(meta.get("team"))
        if pos not in SKILL or not team or sid in rostered or meta.get("injury_status") in LONG_TERM:
            continue
        gid = model.gsis_for(meta, pos)
        if not gid or not any(g[0] == season for g in model.games[gid]):
            continue
        use = model.usage(gid)
        if use < (10 if pos == "QB" else 3):
            continue
        f = model._form(model.games[gid])
        pool.append((f[0] + 0.3 * use, sid))
    pool.sort(reverse=True)
    starter_ids = {c["sid"] for c in chosen.values()}
    bench = [p for p in cands if p["sid"] not in starter_ids]
    droppable = [p for p in bench if not p["sell"]] or bench  # sell-high players are better traded than dropped
    drop = min(droppable, key=lambda p: outlook["avg"].get(p["sid"], 0)) if droppable else None
    picks = []
    for _, sid in pool[:WAIVER_POOL]:
        ps = {w: model.project(sid, w) for w in weeks}
        p0 = ps[weeks[0]]
        refs = [c for i, c in chosen.items() if p0["pos"] in ELIGIBLE.get(slots[i], ())]
        if not refs:
            continue
        ref = min(refs, key=lambda c: outlook["avg"].get(c["sid"], 0))
        use_weeks = weeks if (p0["game_state"] == "upcoming" or len(weeks) == 1) else weeks[1:]
        avg = wavg_weeks([ps[w]["proj"] for w in use_weeks])
        picks.append({"p": p0, "ps": ps, "avg": avg, "ref": ref,
                      "gain": avg - outlook["avg"].get(ref["sid"], 0), "drop": drop})
    picks = [x for x in sorted(picks, key=lambda x: -x["gain"]) if x["gain"] >= 0.5]
    return picks[:WAIVER_PICKS]


def find_buy_low(model, ctx, rosters, mine, users, weeks):
    """Underperforming players on other teams whose numbers should bounce back."""
    db = ctx["players_db"]
    found = []
    for r in rosters:
        if r is mine:
            continue
        owner = users.get(r.get("owner_id"), "another team")
        skip = set(r.get("reserve") or []) | set(r.get("taxi") or [])
        for sid in (r.get("players") or []):
            if sid in skip:
                continue
            meta = db.get(sid, {})
            pos, team = meta.get("position"), norm_team(meta.get("team"))
            if pos not in SKILL or not team:
                continue
            gid = model.gsis_for(meta, pos)
            prof = model.profile(gid, pos, team) if gid else None
            if not prof or model.usage(gid) < (10 if pos == "QB" else 6):
                continue
            fut = prof["future"]
            if prof["over"] <= 0.92 or prof["trend"] >= 1.2 or (fut and fut >= 1.04 and prof["past"] <= 0.97):
                found.append((sid, owner))
    out = []
    for sid, owner in found[:80]:
        p = model.project(sid)
        if p["buy_score"] and p["status"] != "out":
            out.append((p["buy_score"], p, owner))
    out.sort(key=lambda t: -t[0])
    res = []
    for _, p, owner in out[:BUY_LOW_COUNT]:
        ps = [p] + [model.project(p["sid"], w) for w in weeks[1:]]
        res.append({"p": p, "owner": owner, "avg": wavg_weeks([x["proj"] for x in ps])})
    return res


def run_backtest(ctx, scoring, base_model, roster_sids):
    """Learn from the model's own misses: replay each recent completed week as if it hadn't happened yet,
    compare the projection with what the player actually scored, and turn the misses into small corrections."""
    season, week = ctx["season"], ctx["week"]
    cfg = W["learn"]
    weeks = list(range(max(2, week - cfg["weeks"]), week))
    out = {"player": {}, "pos": {}, "records": [], "mae": None, "weeks": weeks, "bias": None}
    if not weeks:
        return out
    records = []
    for w in weeks:
        rows = [r for r in ctx["stat_rows"] if r["season"] < season or r["week"] < w]
        past = Model(rows, scoring, season, w, ctx["sched"], ctx["avg_implied"], ctx["players_db"],
                     ctx["results"], learn=None, historical=True, actuals=base_model.actuals)
        for sid in roster_sids:
            meta = ctx["players_db"].get(sid, {})
            pos = meta.get("position")
            if pos not in SKILL:
                continue
            gid = base_model.gsis_for(meta, pos)
            actual = next((g[3] for g in base_model.games.get(gid, []) if g[0] == season and g[1] == w), None)
            if actual is None:
                continue  # he did not play that week
            p = past.project(sid)
            if p["status"] != "ok" or p["proj"] <= 0:
                continue
            records.append({"sid": sid, "name": p["name"], "pos": pos, "week": w,
                            "proj": p["proj"], "actual": actual})
    if not records:
        return out
    by_player, by_pos = defaultdict(list), defaultdict(list)
    for r in records:
        by_player[r["sid"]].append(r)
        by_pos[r["pos"]].append(r)
    for sid, rs in by_player.items():
        n, sp, sa = len(rs), sum(r["proj"] for r in rs), sum(r["actual"] for r in rs)
        ratio = sa / sp if sp > 0 else 1.0
        out["player"][sid] = {"f": clamp(1 + (ratio - 1) * n / (n + cfg["player_k"]), cfg["cap"]),
                              "n": n, "proj": sp / n, "actual": sa / n, "name": rs[0]["name"],
                              "pos": rs[0]["pos"]}
    for pos, rs in by_pos.items():
        n, sp, sa = len(rs), sum(r["proj"] for r in rs), sum(r["actual"] for r in rs)
        ratio = sa / sp if sp > 0 else 1.0
        out["pos"][pos] = clamp(1 + (ratio - 1) * n / (n + cfg["pos_k"]), cfg["pos_cap"])
    out["records"] = records
    out["mae"] = sum(abs(r["proj"] - r["actual"]) for r in records) / len(records)
    out["bias"] = sum(r["proj"] - r["actual"] for r in records) / len(records)
    return out


def factor_items(p):
    """Every consideration behind a projection: (label, effect or None, plain-English detail)."""
    items = []
    if p["pos"] in ELIGIBLE and p["status"] != "bye":
        if p["sleeper_proj"] is not None:
            items.append(("Sleeper's projection", None,
                          f"Sleeper projects {p['sleeper_proj']:.1f} for this league's scoring. "
                          "This is the starting point."))
            items.append(("Model adjustment", p["proj"] / p["sleeper_proj"] if p["sleeper_proj"] > 0 else None,
                          f"The model projects {p['model_proj']:.1f} from this season's form, team, role, "
                          "matchup and its own past misses. It may only move Sleeper's number slightly. "
                          "The rows below are what it looked at." + (f" {p['sleeper_note']}" if p["sleeper_note"] else "")))
        else:
            items.append(("Sleeper's projection", None,
                          p["sleeper_note"] or "Not available, so the model's number is used."))
    if p["pos"] in SKILL:
        if p["season_avg"] is not None:
            n = p["cur_n"]
            form = (f"{p['season_avg']:.1f} a game over {n} game{'s' if n != 1 else ''} this season "
                    f"(best {p['season_max']:.1f}), {p['l3']:.1f} over the last 3.")
        elif p["l3"] is not None:
            form = f"No games yet this season; {p['l3']:.1f} over his last 3."
        else:
            form = "No NFL game history found."
        items.append(("Current form", None,
                      f"{form} Starting point after weighting this season first: {p['base_form']:.1f}."))
        items.append(("Role and depth chart", p["inj_boost"] if p["inj_text"] else 1.0,
                      p["inj_text"] or "No teammate injuries change his workload."))
        if p["team_ppg"] is not None:
            items.append(("How his team is playing", p["f_team"],
                          f"{p['team_show']} is scoring {p['team_ppg']:.1f} points a game "
                          f"({pct_label(p['team_pts_raw'])} vs the league) and passing and running at the "
                          "rate this season's games show."))
        if p["opp"]:
            items.append(("Matchup", p["f_match"],
                          f"{show_team(p['opp'])} allows {pct_label(p['f_match'])} to {p['pos']}s compared "
                          "with the league average."))
        if p["implied_for"]:
            items.append(("Game script", p["f_vegas"],
                          f"Vegas expects {p['implied_for']:.1f} points from {p['team_show']}."))
        if p["hist_n"]:
            items.append(("History vs this opponent", p["f_hist"],
                          f"{p['hist_n']} past game{'s' if p['hist_n'] != 1 else ''} against "
                          f"{show_team(p['opp'])}, lightly weighted."))
        if p["learn_note"]:
            items.append(("What I learned from earlier weeks", p["f_learn"], p["learn_note"]))
        if p["over_ratio"] is not None and (abs(p["f_sust"] - 1) >= 0.01 or p["sell"] or p["buy"]):
            items.append(("Is his production sustainable?", p["f_sust"],
                          " ".join(p["sell"] or p["buy"]) or
                          f"Scoring {p['season_avg']:.1f} a game against about {p['expected_avg']:.1f} expected."))
        if p["injury"]:
            items.append(("Injury report", p["f_inj"], f"Listed {p['injury']}."))
        if p["anchor_note"]:
            items.append(("Reality check", p["proj"] / p["proj_raw"] if p["proj_raw"] else 1.0, p["anchor_note"]))
    elif p["opp"]:
        items.append(("Game environment", p["f_vegas"],
                      f"Estimated from Vegas lines only: {p['implied_for'] or 0:.1f} points expected for "
                      f"{p['team_show']}, {p['implied_against'] or 0:.1f} for {show_team(p['opp'])}."))
    return items


def drivers(p, n=2):
    ranked = [(abs(math.log(v)), label, text) for label, v, text in factor_items(p)
              if v and v > 0 and abs(math.log(v)) >= 0.04]
    ranked.sort(reverse=True)
    return [f"{label[0].lower() + label[1:]} ({text.split('. ')[0].rstrip('.')})" for _, label, text in ranked[:n]]


def assign_decisions(cands, slots, chosen, model_ids, ai_players, fixed=None, excluded=None):
    """Give every player a visible decision: start or sit, why, and who he was compared with."""
    fixed, excluded = fixed or {}, excluded or set()
    locked_ids = {p["sid"] for p in fixed.values()}
    slot_of = {c["sid"]: slots[i] for i, c in chosen.items()}
    for p in cands:
        ai_p = ai_players.get(norm_name(p["name"]), {})
        p["model_verdict"] = "Start" if p["sid"] in model_ids else "Sit"
        p["verdict"] = "Start" if p["sid"] in slot_of else "Sit"
        p["confidence"] = str(ai_p.get("confidence", "") or "")
        p["why_ai"] = str(ai_p.get("reason", "") or "")
        a = p.get("actual_now")
        state = "is final" if p["game_state"] == "final" else "is in progress"
        if p["sid"] in locked_ids:
            slot = slot_of.get(p["sid"], p["pos"])
            p["verdict"] = p["model_verdict"] = "Locked"
            p["slot"] = slot
            p["why_ai"] = ""
            p["why_head"] = (f"Locked in at {SLOT_LABEL.get(slot, slot)}: his game {state}. "
                             + (f"He has scored {a:.1f} points so far (projected {p['proj']:.1f}). " if a is not None else "")
                             + "He stays where he is.")
            continue
        if p["sid"] in excluded:
            p["why_ai"] = ""
            p["why_head"] = ("Sits: his game already started, so he cannot be moved into the lineup."
                             + (f" He scored {a:.1f} points." if a is not None else ""))
            continue
        if p["sid"] in slot_of:
            slot = slot_of[p["sid"]]
            alts = [x for x in cands if x["sid"] not in slot_of and x["sid"] not in excluded
                    and x["pos"] in ELIGIBLE[slot] and x["proj"] > 0]
            alt = max(alts, key=lambda x: x["proj"]) if alts else None
            p["slot"] = slot
            head = (f"Starts at {SLOT_LABEL.get(slot, slot)}: projects {p['proj']:.1f}, "
                    + (f"{p['proj'] - alt['proj']:+.1f} against the best bench option, {alt['name']} "
                       f"({alt['proj']:.1f})." if alt else "and nobody on the bench can fill the slot."))
        else:
            rivals = [c for i, c in chosen.items() if p["pos"] in ELIGIBLE.get(slots[i], ()) and i not in fixed]
            rival = min(rivals, key=lambda r: r["proj"]) if rivals else None
            head = (f"Sits: projects {p['proj']:.1f}, "
                    + (f"{rival['proj'] - p['proj']:+.1f} against the starter he would replace, "
                       f"{rival['name']} ({rival['proj']:.1f})." if rival else "with no open slot for his position."))
            if p["status"] in ("out", "bye"):
                head = f"Sits: {p['note'].lower() if p['note'] else p['status']}."
        d = drivers(p)
        p["why_head"] = head + (f" Biggest drivers: {'; '.join(d)}." if d else "")


def claude_decide(league_name, week, calls, cands, chosen, slots, learn, fixed=None, excluded=None, api_key=""):
    """Claude makes the final call with all of the evidence. Returns lineup, a verdict for every player,
    and explanations for close calls. Everything is validated in code before it is used."""
    empty = {"calls": {}, "review": "", "lineup": [], "players": {}}
    fixed, excluded = fixed or {}, excluded or set()
    if not (USE_CLAUDE and api_key):  # the key is always passed in, so one visitor's key can never reach another's request
        return empty
    try:
        import anthropic
    except ImportError:
        log("  (anthropic package not installed; skipping the AI decision)")
        return empty

    def brief(p):
        return {"name": p["name"], "pos": p["pos"], "team": p["team_show"],
                "opponent": show_team(p["opp"]), "injury_tag": p["injury"], "status": p["status"],
                "final_projection": round(p["proj"], 1),
                "sleeper_projection": p["sleeper_proj"] and round(p["sleeper_proj"], 1),
                "model_only_projection": round(p["model_proj"], 1),
                "game_state_this_week": p["game_state"],
                "points_scored_this_week_so_far": p["actual_now"] and round(p["actual_now"], 1),
                "points_each_game_this_season": p["log"], "floor": round(p["floor"], 1),
                "ceiling": round(p["ceil"], 1),
                "season_avg": p["season_avg"] and round(p["season_avg"], 1),
                "games_this_season": p["cur_n"],
                "season_best_game": p["season_max"] and round(p["season_max"], 1),
                "last_3_games_avg": p["l3"] and round(p["l3"], 1),
                "form_starting_point": round(p["base_form"], 1),
                "team_points_per_game": p["team_ppg"] and round(p["team_ppg"], 1),
                "opp_allows_vs_position": pct_label(p["f_match"]),
                "team_implied_points": p["implied_for"] and round(p["implied_for"], 1),
                "role_and_depth_chart_note": p["inj_text"] or None,
                "reality_check_note": p["anchor_note"] or None,
                "learning_note": p["learn_note"] or None}

    slot_list = [{"index": i, "slot": s} for i, s in enumerate(slots) if s in ELIGIBLE]
    model_lineup = [{"index": i, "slot": slots[i], "player": c["name"]} for i, c in sorted(chosen.items())]
    record = [{"name": v["name"], "games": v["n"], "avg_projected": round(v["proj"], 1),
               "avg_actual": round(v["actual"], 1)} for v in learn.get("player", {}).values()]
    close = [{"id": c["id"], "slot": c["slot"], "model_pick": c["start"]["name"],
              "alternative": c["sit"]["name"]} for c in calls]
    prompt = f"""You are the decision-maker for a fantasy football lineup. NFL week {week}, league "{league_name}".
A projection model gathered the evidence and proposed a lineup. Make the FINAL decision yourself.
Each "final_projection" starts from Sleeper's own projection for this league's scoring and the model only
adjusts it slightly; treat Sleeper's number as the primary projection.

Weigh the evidence in this order:
1. Past performance, weighted heavily toward THIS season (season average, last 3 games, best game).
   Be skeptical of any projection above anything the player has done this season unless his role truly changed.
   A veteran who is playing poorly now should not be rated on what he did in past seasons.
2. How his team is playing this season (points per game, pass/run volume).
3. Position and role: where he sits on the depth chart among AVAILABLE players. Someone with a small role
   whose teammates in front of him are hurt moves up and deserves real weight, in proportion to his own
   production when given work. Do not hand a poor player a starter's numbers.
4. This week's matchup and Vegas game environment.
5. Older history, which matters least.
Also use the model's track record below: where it has run high or low for a player, correct for it.

Slots to fill (fill every one exactly once):
{json.dumps(slot_list)}

LOCKED SLOTS. These starters' games have already started, so they cannot be moved. Keep exactly this player in
each of these slots:
{json.dumps([{"index": i, "slot": slots[i], "player": p["name"], "game": p["game_state"],
              "points_so_far": p["actual_now"] and round(p["actual_now"], 1)} for i, p in sorted(fixed.items())])}
Players whose games have already started and who are not locked in a slot above cannot be added to the lineup:
{json.dumps([p["name"] for p in cands if p["sid"] in excluded])}

The model's proposed lineup:
{json.dumps(model_lineup)}

The model's track record this season on this roster (projected vs actual points):
{json.dumps(record)}

Every eligible player on the roster, with the evidence:
{json.dumps([brief(p) for p in sorted(cands, key=lambda p: -p["proj"])], indent=1)}

Close calls the model flagged:
{json.dumps(close)}

Use web search to check the latest injury reports, practice participation and role news for the players
who matter most. Do not invent news. Never start a player who is ruled out or on a bye.

Respond with ONLY a JSON object, no markdown fences:
{{"review": "<3-4 sentences: your overall verdict, leading with current performance and team form>",
 "lineup": [{{"index": <slot index>, "player": "<exact player name>"}}],
 "players": [{{"name": "<exact name>", "verdict": "Start" or "Sit", "confidence": "High" or "Medium" or "Low",
              "reason": "<1-2 sentences covering form, team, role and matchup>"}}],
 "calls": [{{"id": "<close call id>", "pick": "<player you would start>", "explanation": "<2-3 sentences>"}}]}}
Include every player in "players". The lineup must have exactly one entry per slot index above."""
    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=CLAUDE_MODEL, max_tokens=8000,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 10}],
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        data = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
        return {
            "calls": {d["id"]: d for d in data.get("calls", []) if isinstance(d, dict) and "id" in d},
            "review": str(data.get("review", "")),
            "lineup": [d for d in data.get("lineup", []) if isinstance(d, dict)],
            "players": {norm_name(d["name"]): d for d in data.get("players", [])
                        if isinstance(d, dict) and d.get("name")},
        }
    except Exception as e:  # network, auth, parsing: the model's lineup is used instead
        log(f"  (AI decision skipped: {type(e).__name__}: {e})")
        return empty


def validate_ai_lineup(ai_lineup, slots, cands, fixed=None, excluded=None, fillable=None):
    """Accept Claude's lineup only if it is complete, legal, keeps locked players in place, and starts
    nobody who cannot play."""
    fixed, excluded = fixed or {}, excluded or set()
    if not ai_lineup:
        return None, "no lineup returned"
    by_name = {norm_name(p["name"]): p for p in cands}
    needed = {i for i, s in enumerate(slots) if s in ELIGIBLE}
    chosen, used = dict(fixed), {p["sid"] for p in fixed.values()}
    seen = set()
    for item in ai_lineup:
        try:
            i = int(item["index"])
        except (KeyError, TypeError, ValueError):
            return None, "a lineup entry had no slot index"
        p = by_name.get(norm_name(item.get("player", "")))
        if p is None:
            return None, f"{item.get('player')} is not an eligible player on this roster"
        if i in seen:
            return None, f"slot {i} was filled twice"
        seen.add(i)
        if i in fixed:
            if p["sid"] != fixed[i]["sid"]:
                return None, f"it tried to move {fixed[i]['name']}, whose game already started"
            continue
        if i not in needed:
            return None, f"slot {i} was invalid"
        if p["pos"] not in ELIGIBLE[slots[i]]:
            return None, f"{p['name']} cannot play {slots[i]}"
        if p["sid"] in used:
            return None, f"{p['name']} was started twice"
        if p["sid"] in excluded:
            return None, f"{p['name']}'s game already started, so he cannot be added"
        chosen[i] = p
        used.add(p["sid"])
    must_fill = needed if fillable is None else (set(fillable) | set(fixed))  # a slot nobody can fill may stay empty
    if not must_fill.issubset(set(chosen)):
        return None, "some slots were left empty"
    for i, p in chosen.items():  # a player who cannot play is only acceptable when nobody else fits the slot
        if i not in fixed and p["status"] in ("bye", "out"):
            spare = [a for a in cands if a["pos"] in ELIGIBLE[slots[i]] and a["sid"] not in used
                     and a["sid"] not in excluded and a["status"] not in ("bye", "out")]
            if spare:
                return None, f"{p['name']} cannot play this week and healthy options were available"
    return chosen, ""


# --------------------------------------------------------------------------
# One league
# --------------------------------------------------------------------------
def scoring_label(sc):
    rec = sc.get("rec", 0)
    base = "PPR" if rec >= 1 else "Half PPR" if rec >= 0.5 else "Standard" if rec == 0 else f"{rec:g} per catch"
    if sc.get("bonus_rec_te"):
        base += ", TE premium"
    if sc.get("pass_td", 4) >= 6:
        base += ", 6-pt pass TD"
    return base


def league_actuals(ctx, lg, scoring):
    """What every player actually scored, in THIS league's scoring: Sleeper's stat lines run through the
    league's scoring settings, then replaced with the exact points from the league's own matchups."""
    season, db, sid2gid = ctx["season"], ctx["players_db"], ctx["sid2gid"]
    actuals, exact = {}, 0
    for w, feed in ctx["sl_stats"].items():
        for pid, st in feed.items():
            gid = sid2gid.get(pid)
            if gid:
                pts = sleeper_points(st, scoring, db.get(pid, {}).get("position"))
                if pts is not None:
                    actuals[(gid, season, w)] = pts
    for w in range(1, ctx["week"]):
        try:
            matchups = sleeper(f"/league/{lg['league_id']}/matchups/{w}")
        except Exception:
            continue
        for m in matchups or []:
            for pid, pts in (m.get("players_points") or {}).items():
                gid = sid2gid.get(pid)
                if gid and pts is not None:
                    actuals[(gid, season, w)] = float(pts)
                    exact += 1
    return actuals, exact


def league_projections(ctx, scoring):
    """Sleeper's projected stat lines for upcoming weeks, scored with this league's settings."""
    out = {}
    if not USE_SLEEPER_PROJECTIONS:
        return out
    db = ctx["players_db"]
    for w, feed in ctx["sl_proj"].items():
        d = {}
        for pid, st in feed.items():
            pos = db.get(pid, {}).get("position")
            if pos in ELIGIBLE:
                pts = sleeper_points(st, scoring, pos)
                if pts is not None:
                    d[pid] = pts
        if d:
            out[w] = d
    return out


def this_week_points(ctx, lg, mine, scoring):
    """Points scored so far this week in this league's scoring: {sleeper player id: points}."""
    db, out = ctx["players_db"], {}
    for pid, st in ctx["sl_stats"].get(ctx["week"], {}).items():
        pos = db.get(pid, {}).get("position")
        if pos in ELIGIBLE:
            pts = sleeper_points(st, scoring, pos)
            if pts is not None:
                out[pid] = pts
    try:  # exact points for your own roster, straight from the league
        for m in sleeper(f"/league/{lg['league_id']}/matchups/{ctx['week']}") or []:
            if m.get("roster_id") == mine.get("roster_id"):
                for pid, pts in (m.get("players_points") or {}).items():
                    if pts is not None:
                        out[pid] = float(pts)
    except Exception:
        pass
    return out


def add_finished_games(model, ctx, week_pts):
    """Fold this week's finished games into the form data so the following weeks reflect them."""
    season, week, db = ctx["season"], ctx["week"], ctx["players_db"]
    for pid, st in ctx["sl_stats"].get(week, {}).items():
        gid, meta = ctx["sid2gid"].get(pid), db.get(pid, {})
        pos = meta.get("position")
        if not gid or pos not in SKILL or pid not in week_pts:
            continue
        team = norm_team(meta.get("team"))
        m = ctx["sched"].get(team, {}).get(week)
        if not m or game_state(m.get("kick"), ctx["now"]) != "final":
            continue
        if any(g[0] == season and g[1] == week for g in model.games[gid]):
            continue
        opps = st.get("pass_att", 0) if pos == "QB" else (st.get("rush_att", 0) or 0) + (st.get("rec_tgt", 0) or 0)
        model.games[gid].append((season, week, m["opp"], week_pts[pid], float(opps or 0), team))


def effective_points(p):
    """What a player counts for in this week's total: his real score once he has played."""
    a = p.get("actual_now")
    if a is None:
        return p["proj"]
    return a if p["game_state"] == "final" else max(a, p["proj"])


def analyze_league(lg, user_id, ctx):
    name = lg["name"]
    if lg.get("status") in ("pre_draft", "drafting"):
        log(f"  skipping {name}: draft has not finished")
        return None
    rosters = sleeper(f"/league/{lg['league_id']}/rosters")
    mine = next((r for r in rosters if r.get("owner_id") == user_id
                 or user_id in (r.get("co_owners") or [])), None)
    if not mine or not (mine.get("players") or []):
        log(f"  skipping {name}: no players on your roster")
        return None

    scoring = lg.get("scoring_settings") or {}
    actuals, exact = league_actuals(ctx, lg, scoring)
    league_proj = league_projections(ctx, scoring)
    model = Model(ctx["stat_rows"], scoring, ctx["season"], ctx["week"], ctx["sched"],
                  ctx["avg_implied"], ctx["players_db"], ctx["results"],
                  actuals=actuals, league_proj=league_proj, now=ctx["now"])
    week_pts = this_week_points(ctx, lg, mine, scoring)
    add_finished_games(model, ctx, week_pts)
    learn = run_backtest(ctx, scoring, model, mine["players"])
    model.learn = learn  # corrections apply to this week's projections
    if learn["records"]:
        log(f"    learned from {len(learn['records'])} player-weeks; average miss "
            f"{learn['mae']:.1f} points, bias {learn['bias']:+.1f}")
    unavailable = set(mine.get("reserve") or []) | set(mine.get("taxi") or [])
    projections = {sid: model.project(sid) for sid in mine["players"]}
    # Audit: does my recomputation of the raw stats agree with Sleeper's actual points?
    roster_gids = {ctx["sid2gid"].get(sid) for sid in mine["players"]}
    gaps = [(model.gid_name.get(g, g), w, c, a) for g, w, c, a in model.audit_rows if g in roster_gids]
    audit = {"exact": exact, "n_actuals": len(actuals), "diffs": len(gaps),
             "mae": (sum(abs(c - a) for _, _, c, a in gaps) / len(gaps)) if gaps else 0.0,
             "worst": sorted(gaps, key=lambda t: -abs(t[2] - t[3]))[:8]}
    log(f"    Sleeper actuals for {len(actuals)} player-weeks ({exact} exact from the league); "
        f"{'Sleeper projections loaded' if league_proj.get(ctx['week']) else 'NO Sleeper projections (model only)'}")
    if gaps:
        log(f"    raw-stat recomputation differed from Sleeper on {len(gaps)} of your player-weeks "
            f"(average gap {audit['mae']:.1f}); Sleeper's numbers are used")
    cands = [p for sid, p in projections.items() if sid not in unavailable and p["pos"] in ELIGIBLE]

    slots = [s for s in (lg.get("roster_positions") or []) if s != "BN"]
    for p in projections.values():  # players whose games have started: their real points so far
        if p["game_state"] in ("live", "final") and p["sid"] in week_pts:
            p["actual_now"] = week_pts[p["sid"]]
    # A starter whose game has kicked off is locked in his slot; nobody else whose game started can be added.
    current_list = list(mine.get("starters") or [])
    fixed = {}
    for i, slot in enumerate(slots):
        cur = current_list[i] if i < len(current_list) else None
        pc = projections.get(cur)
        if (slot in ELIGIBLE and pc and cur not in unavailable and pc["pos"] in ELIGIBLE[slot]
                and pc["game_state"] in ("live", "final")):
            fixed[i] = pc
    locked_ids = {p["sid"] for p in fixed.values()}
    excluded = {p["sid"] for p in cands if p["game_state"] in ("live", "final")} - locked_ids
    if fixed or excluded:
        log(f"    {len(fixed)} starters locked (games already started); "
            f"{len(excluded)} other players can no longer be added")
    model_chosen, model_used = build_lineup(slots, cands, fixed, excluded)
    calls = find_close_calls(slots, model_chosen, model_used, cands, fixed, excluded)
    for c in calls:
        c["text"] = explain(c)

    claude_on = bool(USE_CLAUDE and ctx.get("api_key"))
    ai = claude_decide(name, ctx["week"], calls, cands, model_chosen, slots, learn, fixed, excluded,
                       api_key=ctx.get("api_key") or "")
    chosen, decided_by, note = model_chosen, "model", ""
    if ai["lineup"]:
        ai_chosen, why_not = validate_ai_lineup(ai["lineup"], slots, cands, fixed, excluded,
                                                fillable=set(model_chosen))
        if ai_chosen:
            chosen, decided_by = ai_chosen, "claude"
        else:
            note = f"Claude's lineup was not used ({why_not}), so the model's lineup stands."
            log(f"    {note}")
    used = {c["sid"] for c in chosen.values()}
    if claude_on:
        if decided_by == "claude":
            n_diff = sum(1 for i in chosen if i in model_chosen and chosen[i]["sid"] != model_chosen[i]["sid"])
            log(f"    Claude made the final call ({n_diff} slot{'s' if n_diff != 1 else ''} different from the model)")
        elif not ai["lineup"]:
            log("    Claude did not return a lineup for this league, so the model decided")
    if decided_by == "claude":  # close calls are about the lineup that is actually recommended
        calls = find_close_calls(slots, chosen, used, cands, fixed, excluded)
        for c in calls:
            c["text"] = explain(c)
    model_ids = {c["sid"] for c in model_chosen.values()}
    assign_decisions(cands, slots, chosen, model_ids, ai["players"], fixed, excluded)
    changes = [(chosen[i], model_chosen[i]) for i in sorted(chosen)
               if i in model_chosen and chosen[i]["sid"] != model_chosen[i]["sid"]]

    current = current_list
    current_set = {x for x in current if x and x != "0"}
    supported_current = {x for x in current_set if projections.get(x, {}).get("pos") in ELIGIBLE}
    chosen_ids = {c["sid"] for c in chosen.values()}
    adds = [c["name"] for c in chosen.values() if c["sid"] not in current_set]
    drops = [projections[x]["name"] for x in supported_current if x not in chosen_ids]

    bench = sorted([p for p in projections.values() if p["sid"] not in chosen_ids
                    and p["sid"] not in unavailable], key=lambda p: -p["proj"])
    report = [p for p in projections.values() if p["inj_debug"] or p["inj_text"]]
    for p in sorted(report, key=lambda p: -abs(p["inj_boost"] - 1)):
        log(f"    [{p['team_show']}] {p['name']}: role change {pct_label(p['inj_boost'])}")
        for line in p["inj_debug"]:
            log(f"        - {line}")
    # Looking ahead, waiver wire, and trade watch
    rostered = {sid for r in rosters for sid in
                (r.get("players") or []) + (r.get("reserve") or []) + (r.get("taxi") or [])}
    outlook = build_outlook(model, slots, cands, chosen, ctx["week"])
    waiver = find_waiver(model, ctx, rostered, slots, chosen, cands, outlook)
    users = {}
    try:
        for u in sleeper(f"/league/{lg['league_id']}/users"):
            users[u["user_id"]] = (u.get("metadata") or {}).get("team_name") or u.get("display_name") or "another team"
    except Exception:
        pass
    buy_low = find_buy_low(model, ctx, rosters, mine, users, outlook["weeks"]) if BUY_LOW else []
    outlook["totals"][ctx["week"]] = sum(effective_points(c) for c in chosen.values())
    sell = sorted([p for p in cands if p["sell"]], key=lambda p: -p["sell_score"])[:5]
    for p in sell:
        log(f"    sell-high: {p['name']} ({p['sell_score']:.1f})")
    for x in waiver:
        log(f"    waiver: {x['p']['name']} avg {x['avg']:.1f}, +{x['gain']:.1f} over {x['ref']['name']}")
    st = mine.get("settings") or {}
    record = f"{st.get('wins', 0)}-{st.get('losses', 0)}" + (f"-{st['ties']}" if st.get("ties") else "")
    return {
        "audit": audit, "outlook": outlook, "waiver": waiver, "buy_low": buy_low, "sell": sell,
        "id": str(lg["league_id"]), "name": name, "record": record,
        "teams": lg.get("total_rosters"), "scoring": scoring_label(scoring),
        "slots": slots, "chosen": chosen, "model_chosen": model_chosen, "calls": calls,
        "enrich": ai["calls"], "ai": ai, "decided_by": decided_by, "claude_on": claude_on, "ai_note": note, "changes": changes,
        "adds": adds, "drops": drops, "bench": bench, "inj_report": report, "learn": learn,
        "unavailable": [projections[s] for s in unavailable if s in projections],
        "current": current, "projections": projections,
        "total": sum(effective_points(c) for c in chosen.values()),
        "scored": sum(c["actual_now"] for c in chosen.values()
                      if c["game_state"] == "final" and c["actual_now"] is not None),
        "locked": list(fixed.values()), "week_pts": week_pts,
    }


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------
CSS = """
:root{
  --paper:#f3efe4; --panel:#e9e4d5; --ink:#17140f; --ink-2:#5c564a; --hair:#c2bba8;
  --red:#a41f19; --mid:#8b8472; --green:#2b7a4b;
  --blackletter:"UnifrakturCook","Old English Text MT",Georgia,serif;
  --head:"Playfair Display","Didot","Bodoni MT",Georgia,"Times New Roman",serif;
  --text:"Source Serif 4","Source Serif Pro",Georgia,"Times New Roman",serif;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--paper);color:var(--ink);font:400 1.02rem/1.55 var(--text)}
.wrap{max-width:70rem;margin:0 auto;padding:1.5rem 1.25rem 4rem}
h1,h2,h3,h4{font-family:var(--head);margin:0;line-height:1.08}
.masthead{text-align:center;padding-top:.5rem}
.masthead h1{font:400 clamp(2.8rem,10vw,5.6rem)/1 var(--blackletter);letter-spacing:.01em}
.dateline{display:flex;justify-content:space-between;gap:1rem;flex-wrap:wrap;margin-top:.7rem;padding:.35rem 0;
  border-top:3px double var(--ink);border-bottom:1px solid var(--ink);font-size:.88rem;font-style:italic}
.lede-note{color:var(--ink-2);margin:.8rem auto 0;max-width:44rem;text-align:center;font-style:italic}
.tabs{display:flex;gap:0;overflow-x:auto;margin-top:1.1rem;border-top:1px solid var(--ink);border-bottom:1px solid var(--ink);
  scrollbar-width:thin}
.tab{flex:none;font:700 1rem/1.2 var(--head);color:var(--ink);background:transparent;border:0;
  border-right:1px solid var(--hair);padding:.7rem 1.1rem;cursor:pointer;text-align:left}
.tab:last-child{border-right:0}
.tab small{display:block;font:italic 400 .8rem/1.3 var(--text);color:var(--ink-2)}
.tab[aria-selected="true"]{background:var(--ink);color:var(--paper)}
.tab[aria-selected="true"] small{color:#d8d2c0}
.tab:focus-visible,summary:focus-visible{outline:3px solid var(--red);outline-offset:2px}
.js .panel[hidden]{display:none}
.panel{padding-top:1.8rem}
.panel h2{font-size:clamp(2.1rem,6vw,3.4rem);font-weight:900;letter-spacing:-.01em}
.deck{font-style:italic;color:var(--ink-2);margin:.4rem 0 0;font-size:1.1rem}
.lede{margin:1.2rem 0 0;padding:1rem 0;border-top:1px solid var(--ink);border-bottom:1px solid var(--hair);
  max-width:52rem;font-size:1.12rem}
.lede p{margin:0 0 .5rem}
.lede b{font-weight:700}
.total{font:900 1.35rem/1 var(--head)}
.section-title{font-size:1.7rem;font-weight:900;margin:2.4rem 0 .3rem;padding-bottom:.35rem;border-bottom:3px double var(--ink)}
.legend{color:var(--ink-2);font-size:.88rem;font-style:italic;margin:.5rem 0 1rem}
.briefs{list-style:none;margin:0;padding:0;display:grid;grid-template-columns:1fr 1fr;column-gap:0}
.brief{padding:1rem 1.4rem 1.1rem 0;border-bottom:1px solid var(--hair);position:relative}
.brief[style]::before{content:"";position:absolute;top:0;left:0;width:3.4rem;height:3px;background:var(--tc)}
.brief[style]:nth-child(even)::before{left:1.4rem}
.brief:nth-child(even){padding:1rem 0 1.1rem 1.4rem;border-left:1px solid var(--hair)}
.brief.plain{display:block}
.b-top{display:grid;grid-template-columns:5.2rem 1fr auto;gap:.9rem;align-items:start}
.portrait{position:relative;width:5.2rem;height:5.2rem;flex:none}
.face{width:100%;height:100%;object-fit:cover;object-position:top;display:block;background:var(--panel);
  border:1px solid var(--ink);border-bottom:4px solid var(--tc,var(--ink))}
.crest{position:relative;width:100%;height:100%;display:flex;align-items:center;justify-content:center;
  font:900 1.6rem/1 var(--head);background:var(--tc,var(--panel));color:var(--on-tc,var(--ink));
  border:1px solid var(--ink);overflow:hidden}
.crest img{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;padding:.35rem;background:#fff}
.logo{position:absolute;right:-.5rem;bottom:-.55rem;width:2rem;height:2rem;border-radius:50%;
  background:var(--tc,var(--ink));color:var(--on-tc,#fff);border:2px solid var(--paper);
  box-shadow:0 0 0 1px var(--ink);display:flex;align-items:center;justify-content:center;
  font:800 .55rem/1 var(--head);overflow:hidden}
.logo img{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;padding:.2rem;background:#fff}
.portrait.mini{width:2.4rem;height:2.4rem}
.portrait.mini .logo{width:1.15rem;height:1.15rem;right:-.35rem;bottom:-.35rem;border-width:1px;font-size:.4rem}
.portrait.mini .logo img{padding:.1rem}
.portrait.mini .crest{font-size:.8rem}
.kicker{font:italic 400 .85rem/1 var(--text);color:var(--red);display:block;margin-bottom:.25rem}
.b-text h4{font-size:1.4rem;font-weight:800;overflow-wrap:anywhere}
.sub{margin:.15rem 0 0;color:var(--ink-2);font-size:.9rem}
.game{margin:.1rem 0 0;font-size:.92rem}
.form{margin:.15rem 0 0;font-size:.84rem;color:var(--ink-2);font-style:italic}
.ahead-wrap{overflow-x:auto;margin-top:.4rem}
.ahead{border-collapse:collapse;width:100%;min-width:38rem;font-size:.92rem;table-layout:fixed}
.ahead th:first-child{width:11rem}.ahead th.num,.ahead td.num{width:4.5rem}
.ahead th,.ahead td{padding:.45rem .5rem;border-bottom:1px solid var(--hair);text-align:left;vertical-align:middle}
.ahead thead th{font:italic 400 .82rem/1 var(--text);color:var(--ink-2);border-bottom:1px solid var(--ink)}
.ahead tbody th{font-weight:600;min-width:9rem}
.ahead td .g{display:block;font-size:.76rem;color:var(--ink-2);font-style:italic}
.ahead td b{font:800 1.15rem/1 var(--head)}
.ahead .played{background:var(--panel);border-left:3px solid var(--ink)}
.ahead .bye{color:var(--ink-2);font-style:italic;background:var(--panel)}
.ahead .num{text-align:right;font:800 1.15rem/1 var(--head)}
.ahead .sum th,.ahead .sum td{border-top:1px solid var(--ink)}
.picks{list-style:none;margin:0;padding:0;display:grid;grid-template-columns:repeat(auto-fit,minmax(19rem,1fr));gap:0 2rem}
.pick{padding:1rem 0 1.1rem;border-top:3px solid var(--tc,var(--ink));border-bottom:1px solid var(--hair)}
.why-list{margin:.7rem 0 .4rem;padding-left:1.1rem;font-size:.92rem}
.drop{margin:.4rem 0 0;font-style:italic}
.trade{margin:.2rem 0 1rem;padding-left:1.1rem}
.trade li{margin:.5rem 0}
.trade ul{margin:.2rem 0;font-size:.92rem;color:var(--ink-2);font-style:italic}
.sub-title{font:800 1.15rem/1.2 var(--head);margin:1.2rem 0 .2rem}
.why{margin-top:.8rem;background:var(--panel);padding:.1rem .9rem}
.why summary{cursor:pointer;padding:.55rem 0;font:700 .95rem/1.2 var(--head)}
.why p{margin:.2rem 0 .7rem;font-size:.93rem}
.ai-why{border-left:3px solid var(--green);padding-left:.6rem}
.fx-table{width:100%;border-collapse:collapse;font-size:.86rem;margin:.3rem 0 .6rem}
.fx-table th{font:italic 400 .8rem/1 var(--text);color:var(--ink-2);text-align:left;padding:.3rem .4rem;border-bottom:1px solid var(--ink)}
.fx-table td{padding:.35rem .4rem;border-bottom:1px solid var(--hair);vertical-align:top}
.fx-table td:first-child{white-space:nowrap;font-weight:600}
.fx{white-space:nowrap;font-weight:700}
.fx.up{color:var(--green)}.fx.dn{color:var(--red)}
.final{font-style:italic}
.bench-table .why{font-weight:400}
.editor{margin:1.2rem 0 0;padding:.9rem 1.1rem;background:var(--panel);border-top:3px solid var(--ink);max-width:52rem}
.editor h3{font-size:1.15rem;font-weight:800;margin-bottom:.35rem}
.editor p{margin:.2rem 0}
.editor ul{margin:.4rem 0 0;padding-left:1.1rem}
.b-pts{text-align:right}
.b-pts span{font:900 2.5rem/1 var(--head);display:block}
.b-pts small{font-style:italic;color:var(--ink-2);font-size:.78rem;display:block}
.b-pts .sl{font-style:normal;font-weight:600}
.b-mx{margin-top:.7rem;display:grid;grid-template-columns:7rem 1fr;gap:.7rem;align-items:center;
  font-size:.84rem;font-style:italic;color:var(--ink-2)}
.mx{position:relative;height:6px;border-top:1px solid var(--ink);border-bottom:1px solid var(--ink);background:transparent}
.mx::after{content:"";position:absolute;left:50%;top:-4px;bottom:-4px;width:1px;background:var(--ink)}
.mx i{position:absolute;top:0;bottom:0}
.mx i.up{left:50%;background:var(--green)}
.mx i.dn{right:50%;background:var(--red)}
.rg{--m:40;position:relative;height:10px;margin-top:.7rem;border-bottom:1px solid var(--ink);
  background-image:repeating-linear-gradient(to right,var(--hair) 0 1px,transparent 1px calc(100% * 10 / var(--m)))}
.rg b{position:absolute;top:2px;bottom:2px;background:var(--tc,var(--mid));opacity:.8}
.rg u{position:absolute;top:-3px;bottom:0;width:3px;margin-left:-1px;background:var(--ink);text-decoration:none}
.role{margin:.7rem 0 0;padding-left:.7rem;border-left:3px solid var(--ink);font-size:.9rem;font-style:italic}
.tags{display:flex;flex-wrap:wrap;gap:.35rem;margin-top:.6rem}
.tags:empty{display:none}
.tag{font:italic 400 .8rem/1 var(--text);padding:.28rem .5rem;border:1px solid var(--ink-2);color:var(--ink-2)}
.tag.flag{border-color:var(--red);color:var(--red);font-weight:600}
.tag.in{border-color:var(--green);color:var(--green);font-weight:600}
.tag.warn{border-color:var(--red);color:var(--red)}
details.call{margin-top:.8rem;background:var(--panel);border-top:3px solid var(--red);padding:.1rem .9rem}
details.call summary{cursor:pointer;padding:.6rem 0;font:800 1.05rem/1.2 var(--head);color:var(--ink)}
details.call p{margin:.2rem 0 .8rem}
.chips{display:grid;gap:.25rem;margin:.2rem 0 .8rem;font-size:.84rem;color:var(--ink-2);font-style:italic}
.ai{border-top:1px solid var(--hair);padding-top:.6rem}
.bench-table{width:100%;border-collapse:collapse;font-size:.95rem}
.bench-table th{font:italic 400 .82rem/1 var(--text);color:var(--ink-2);text-align:left;padding:.4rem .5rem;
  border-bottom:1px solid var(--ink)}
.bench-table td{padding:.5rem;border-bottom:1px solid var(--hair);vertical-align:middle}
.bench-table .num{text-align:right;font:800 1.15rem/1 var(--head);white-space:nowrap}
.bench-table strong{font-family:var(--head);font-weight:700}
.note{color:var(--ink-2);font-style:italic}
.method{margin-top:3rem;border-top:3px double var(--ink);padding-top:.8rem;color:var(--ink-2);font-size:.92rem;max-width:48rem}
.method summary{cursor:pointer;font:800 1.1rem/1.2 var(--head);color:var(--ink)}
.check{padding-left:1.1rem}.check ul{margin:.2rem 0 .7rem;padding-left:1.1rem;font-style:italic}
.method code{background:var(--panel);padding:.05rem .3rem}
@media (max-width:820px){
  .briefs{grid-template-columns:1fr}
  .brief,.brief:nth-child(even){padding:1rem 0 1.1rem 0;border-left:0}
  .b-top{grid-template-columns:4.2rem 1fr auto;gap:.7rem}
  .portrait{width:4.2rem;height:4.2rem}
  .portrait.mini{width:2.4rem;height:2.4rem}
  .b-mx{grid-template-columns:5.5rem 1fr}
  .dateline{justify-content:center;text-align:center}
  .bench-table .hide-sm{display:none}
}
@media print{.js .panel[hidden]{display:block}.tabs{display:none}}
"""

JS = """
document.documentElement.classList.add('js');
const tabs=[...document.querySelectorAll('[role=tab]')];
function show(id,remember){
  tabs.forEach(t=>{const on=t.dataset.target===id;
    t.setAttribute('aria-selected',on?'true':'false');t.tabIndex=on?0:-1;
    document.getElementById(t.dataset.target).hidden=!on;});
  if(remember)history.replaceState(null,'','#'+id);
}
tabs.forEach((t,i)=>{
  t.addEventListener('click',()=>show(t.dataset.target,true));
  t.addEventListener('keydown',e=>{
    if(e.key!=='ArrowRight'&&e.key!=='ArrowLeft')return;
    const n=tabs[(i+(e.key==='ArrowRight'?1:tabs.length-1))%tabs.length];
    n.focus();show(n.dataset.target,true);});
});
const want=location.hash.slice(1);
show(tabs.some(t=>t.dataset.target===want)?want:tabs[0].dataset.target,false);
"""

HEADSHOT = "https://sleepercdn.com/content/nfl/players/thumb/{sid}.jpg"
# Shown if a headshot fails to load (offline, or a player Sleeper has no photo for).
SILHOUETTE = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 80 80'%3E"
              "%3Crect width='80' height='80' fill='%23e9e4d5'/%3E%3Ccircle cx='40' cy='31' r='14' fill='%23b8b19e'/%3E"
              "%3Cpath d='M10 80c2-22 16-30 30-30s28 8 30 30z' fill='%23b8b19e'/%3E%3C/svg%3E")


def team_color(p):
    return TEAM_COLORS.get(p["team"] or "", "#5c564a")


def on_color(hex_color):
    """White or near-black text, whichever reads better on this background."""
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return "#17140f" if (0.299 * r + 0.587 * g + 0.114 * b) > 150 else "#ffffff"


def style_tc(p):
    c = team_color(p)
    return f"--tc:{c};--on-tc:{on_color(c)}"


def logo_url(p):
    return LOGO.format(team=(p["team_show"] or "").lower())


def face_html(p, size=""):
    """Color headshot with a small team logo badge. Falls back gracefully when images don't load."""
    abbr = esc(p["team_show"] or "FA")
    logo_img = (f'<img src="{logo_url(p)}" alt="" loading="lazy" onerror="this.remove()">'
                if p["team_show"] and SHOW_IMAGES else "")
    cls = f"portrait {size}".strip()
    if p["pos"] == "DEF":
        return (f'<div class="{cls}" style="{style_tc(p)}" aria-hidden="true">'
                f'<div class="crest">{abbr}{logo_img}</div></div>')
    if not SHOW_IMAGES:
        return (f'<div class="{cls}" style="{style_tc(p)}" aria-hidden="true">'
                f'<div class="crest">{abbr}</div></div>')
    url = HEADSHOT.format(sid=esc(p["sid"]))
    return (f'<div class="{cls}" style="{style_tc(p)}">'
            f'<img class="face" src="{url}" alt="" loading="lazy" '
            f'onerror="this.onerror=null;this.src=\'{SILHOUETTE}\'">'
            f'<span class="logo" title="{abbr}">{abbr}{logo_img}</span></div>')


def matchup_cell(p):
    if p["pos"] in SKILL and p["match_raw"] is not None and p["opp"]:
        v = (p["f_match"] - 1) * 100
        half = min(abs(v), 25) / 25 * 50
        cls = "up" if v >= 0 else "dn"
        return (f'<div class="b-mx"><div class="mx" aria-hidden="true"><i class="{cls}" style="width:{half:.1f}%"></i></div>'
                f'<span>{esc(show_team(p["opp"]))} allows {pct_label(p["f_match"])} to {esc(p["pos"])}s</span></div>')
    if p["pos"] in ("K", "DEF"):
        return '<div class="b-mx"><span>Estimate from Vegas lines only</span></div>'
    return ""


def range_bar(p, scale):
    lo = p["floor"] / scale * 100
    hi = min(100.0, p["ceil"] / scale * 100)
    at = min(100.0, p["proj"] / scale * 100)
    return (f'<div class="rg" style="--m:{scale}" role="img" aria-label="Likely range {p["floor"]:.0f} to '
            f'{p["ceil"]:.0f}, projection {p["proj"]:.1f}"><b style="left:{lo:.1f}%;width:{max(hi - lo, 1):.1f}%">'
            f'</b><u style="left:{at:.1f}%"></u></div>')


def game_line(p):
    if not p["opp"]:
        return esc(p["note"] or "No game this week")
    where = "vs" if p["home"] else "at"
    exp = f', {p["implied_for"]:.1f} points expected' if p["implied_for"] else ", line not posted"
    if p["game_state"] == "final" and p["week"] == p.get("week"):
        exp = ", game final"
    elif p["game_state"] == "live":
        exp = ", game in progress"
    return f'{where} {esc(show_team(p["opp"]))}{exp}'


def form_line(p):
    if p["l3"] is None or p["pos"] not in SKILL:
        return ""
    if p["season_avg"] is None:
        return f'<p class="form">No games yet this season. Last 3 games: {p["l3"]:.1f}.</p>'
    n = p["cur_n"]
    log = ", ".join(f"{x:.1f}" for x in p["log"][-6:])
    return (f'<p class="form">This season: {p["season_avg"]:.1f} a game over {n} game{"s" if n != 1 else ""} '
            f'({log}).</p>')


def player_tags(p, tags=""):
    if p.get("verdict") == "Locked":
        tags += (f'<span class="tag in">Locked in place, game '
                 f'{"final" if p["game_state"] == "final" else "in progress"}</span>')
    elif p["game_state"] in ("live", "final") and p["actual_now"] is not None:
        tags += '<span class="tag">Already played</span>'
    if p["sell"]:
        tags += '<span class="tag warn">Sell high</span>'
    elif p["buy"]:
        tags += '<span class="tag in">Due for a bounce</span>'
    if p["inj_boost"] >= 1.10 and p["inj_names"]:
        tags += '<span class="tag in">Bigger role</span>'
    elif p["inj_boost"] <= 0.93:
        tags += '<span class="tag warn">Smaller role</span>'
    if p["injury"]:
        tags += f'<span class="tag warn">{esc(p["injury"])}</span>'
    if p["status"] == "bye":
        tags += '<span class="tag warn">Bye week</span>'
    elif p["note"] and p["status"] in ("ok", "nodata") and p["pos"] in SKILL:
        tags += f'<span class="tag">{esc(p["note"])}</span>'
    return tags


def why_html(p):
    """The visible decision for one player: verdict, reasoning, and every factor with its effect."""
    if "why_head" not in p:
        return ""
    conf = f" ({p['confidence']} confidence)" if p.get("confidence") else ""
    rows = ""
    for label, v, text in factor_items(p):
        effect = "" if v is None else pct_label(v)
        cls = "" if v is None or abs(v - 1) < 0.02 else (" up" if v > 1 else " dn")
        rows += f'<tr><td>{esc(label)}</td><td class="fx{cls}">{esc(effect)}</td><td>{esc(text)}</td></tr>'
    ai = (f'<p class="ai-why"><b>Claude\'s reasoning.</b> {esc(p["why_ai"])}</p>' if p.get("why_ai") else "")
    diff = ""
    if p.get("verdict") != p.get("model_verdict"):
        diff = (f'<p class="note">The projection model alone would have said '
                f'{esc(p["model_verdict"].lower())}.</p>')
    return (f'<details class="why"><summary>Why: {esc(p["verdict"].lower())}{esc(conf)}</summary>'
            f'<p>{esc(p["why_head"])}</p>{ai}{diff}'
            f'<table class="fx-table"><thead><tr><th>Consideration</th><th>Effect</th><th>What it shows</th>'
            f'</tr></thead><tbody>{rows}</tbody></table>'
            f'<p class="final">Final projection <b>{p["proj"]:.1f}</b>, likely range '
            f'{p["floor"]:.0f} to {p["ceil"]:.0f}.</p></details>')


def track_html(L):
    lr = L.get("learn") or {}
    if not lr.get("records"):
        return ('<details class="method"><summary>What the model learned this season</summary>'
                '<p>There are not enough completed games yet to check the model against. Once a couple of '
                'weeks are in, the script replays them, compares each projection with what the player '
                'actually scored, and corrects for the misses.</p></details>')
    rows = "".join(
        f'<tr><td>{esc(v["name"])}</td><td>{v["n"]}</td><td>{v["proj"]:.1f}</td><td>{v["actual"]:.1f}</td>'
        f'<td>{pct_label(v["f"])}</td></tr>'
        for v in sorted(lr["player"].values(), key=lambda v: -abs(v["f"] - 1)))
    wk = lr["weeks"]
    return ('<details class="method"><summary>What the model learned this season</summary>'
            f'<p>The script replayed weeks {wk[0]} to {wk[-1]} as if they had not happened yet and compared '
            f'each projection with the real result. Average miss: {lr["mae"]:.1f} points per player. '
            f'Bias: {lr["bias"]:+.1f} (positive means projections ran too high). Those misses now nudge '
            f'this week\'s projections, shrunk heavily because the sample is small.</p>'
            f'<table class="fx-table"><thead><tr><th>Player</th><th>Games</th><th>Avg projected</th>'
            f'<th>Avg actual</th><th>Adjustment</th></tr></thead><tbody>{rows}</tbody></table></details>')


def points_block(p):
    """The big number: real points once he has played, otherwise the projection (with Sleeper's for reference)."""
    if p["actual_now"] is not None and p["game_state"] in ("live", "final"):
        return (f'<span>{p["actual_now"]:.1f}</span>'
                f'<small>{"scored" if p["game_state"] == "final" else "so far"}</small>'
                f'<small class="sl">projected {p["proj"]:.1f}</small>')
    return (f'<span>{p["proj"]:.1f}</span><small>projected</small>'
            + (f'<small class="sl">Sleeper {p["sleeper_proj"]:.1f}</small>' if p["sleeper_proj"] is not None else ""))


def brief_html(label, p, scale, tags="", detail=""):
    return (
        f'<li class="brief" style="{style_tc(p)}"><div class="b-top">{face_html(p)}'
        f'<div class="b-text"><span class="kicker">{esc(label)}</span><h4>{esc(p["name"])}</h4>'
        f'<p class="sub">{esc(p["pos"] or "")} on {esc(p["team_show"] or "FA")}</p>'
        f'<p class="game">{game_line(p)}</p>{form_line(p)}</div>'
        f'<div class="b-pts">{points_block(p)}</div></div>'
        f'{matchup_cell(p)}'
        + (f'<p class="role">{esc(p["inj_text"])}</p>' if p["inj_text"] else "")
        + f'{range_bar(p, scale)}'
        f'<div class="tags">{player_tags(p, tags)}</div>{detail}{why_html(p)}</li>'
    )


def call_detail(call, enrich):
    a, b = call["start"], call["sit"]
    chips = "".join(f"<span>{esc(t)}</span>" for t in [
        f"{a['name']}: recent {a['base']:.1f}, matchup {pct_label(a['f_match'])}, "
        f"Vegas {pct_label(a['f_vegas'])}, history {pct_label(a['f_hist'])}",
        f"{b['name']}: recent {b['base']:.1f}, matchup {pct_label(b['f_match'])}, "
        f"Vegas {pct_label(b['f_vegas'])}, history {pct_label(b['f_hist'])}",
    ])
    ai = ""
    if enrich:
        lean = ""
        if enrich.get("pick") and norm_name(enrich["pick"]) == norm_name(b["name"]):
            lean = f' <span class="tag warn">News check leans {esc(b["name"])}</span>'
        ai = f'<div class="ai"><p><b>News check</b>{lean}<br>{esc(enrich.get("explanation", ""))}</p></div>'
    return (f'<details class="call"><summary>Too close to call: {esc(a["name"])} or {esc(b["name"])}</summary>'
            f'<p>{esc(call["text"])}</p><div class="chips">{chips}</div>{ai}</details>')


def bench_row(p):
    opp = game_line(p).replace(", ", " (", 1) + ")" if p["opp"] and p["implied_for"] else game_line(p)
    note = p["injury"] or ("Bye week" if p["status"] == "bye" else p["note"] or "")
    if p["actual_now"] is not None and p["game_state"] in ("live", "final"):
        note = f'Played: {p["actual_now"]:.1f} points' + (" so far" if p["game_state"] == "live" else "")
    return (f'<tr><td style="width:3.2rem">{face_html(p, "mini")}</td>'
            f'<td><strong>{esc(p["name"])}</strong><br><span class="sub">{esc(p["pos"] or "")} on '
            f'{esc(p["team_show"] or "FA")}</span>{why_html(p)}</td>'
            f'<td class="hide-sm">{opp}</td><td class="hide-sm note">{esc(note)}</td>'
            f'<td class="num">{p["proj"]:.1f}</td></tr>')


def injury_check_html(L):
    items = []
    for p in sorted(L["inj_report"], key=lambda p: -abs(p["inj_boost"] - 1)):
        lines = "".join(f"<li>{esc(x)}</li>" for x in p["inj_debug"])
        head = f'{esc(p["name"])} ({esc(p["pos"])}, {esc(p["team_show"])}): role change {pct_label(p["inj_boost"])}'
        items.append(f'<li><strong>{head}</strong><ul>{lines}</ul></li>')
    if not items:
        return ('<details class="method"><summary>Injury check</summary><p>No injured teammates were found '
                'for any player on this roster. If you expected one, add the player to '
                '<code>MANUAL_STATUS</code> near the top of <code>fantasy.py</code>.</p></details>')
    return ('<details class="method"><summary>Injury check: how teammate injuries were handled</summary>'
            f'<ul class="check">{"".join(items)}</ul>'
            '<p>If a teammate is missing here, Sleeper may not have updated his status yet. Add him to '
            '<code>MANUAL_STATUS</code> near the top of <code>fantasy.py</code> and run again.</p></details>')


def ahead_html(L):
    o = L["outlook"]
    weeks = o["weeks"]
    if not weeks:
        return ""
    head = "".join(f"<th>Week {w}</th>" for w in weeks) + '<th class="num">Average</th>'
    rows = ""
    starter_ids = {c["sid"] for c in L["chosen"].values()}
    order = sorted(o["avg"], key=lambda s: -o["avg"][s])
    shown = [sid for i, sid in enumerate(order) if i < 16 or sid in starter_ids]
    for sid in shown:
        p0 = o["by_week"][weeks[0]][sid]
        cells = ""
        for w in weeks:
            pw = o["by_week"][w][sid]
            if pw["status"] == "bye":
                cells += '<td class="bye">Bye</td>'
                continue
            if not pw["opp"]:
                cells += "<td>-</td>"
                continue
            if pw["actual_now"] is not None and pw["game_state"] in ("live", "final"):
                cells += (f'<td class="played"><span class="g">{"final" if pw["game_state"] == "final" else "live"}'
                          f'</span><b>{pw["actual_now"]:.1f}</b></td>')
                continue
            v = pw["f_match"] - 1
            alpha = min(abs(v) / 0.2, 1) * 0.30 if pw["pos"] in SKILL else 0
            tint = f"background:rgba({'43,122,75' if v >= 0 else '164,31,25'},{alpha:.2f})"
            where = "vs" if pw["home"] else "at"
            cells += (f'<td style="{tint}"><span class="g">{where} {esc(show_team(pw["opp"]))}</span>'
                      f'<b>{pw["proj"]:.1f}</b></td>')
        tag = ""
        if p0["sell"]:
            tag = ' <span class="tag warn">Sell high</span>'
        elif p0["buy"]:
            tag = ' <span class="tag in">Due for a bounce</span>'
        rows += (f'<tr><th scope="row">{esc(p0["name"])}<br><span class="sub">{esc(p0["pos"])} on '
                 f'{esc(p0["team_show"])}</span>{tag}</th>{cells}<td class="num">{o["avg"][sid]:.1f}</td></tr>')
    totals = "".join(f'<td><b>{o["totals"][w]:.1f}</b></td>' for w in weeks)
    byes = "".join(f'<td class="note">{esc(", ".join(o["byes"][w])) if o["byes"][w] else "None"}</td>' for w in weeks)
    return ('<h3 class="section-title">Looking ahead</h3>'
            '<p class="legend">Each cell shows the opponent and the projected points. Green shading is a soft '
            'matchup for that position and red is a tough one. Later weeks assume today\'s injuries fade, and '
            'they only use the model, so treat them as a guide. The weekly lineup totals use the best lineup '
            'the model can build each week.</p>'
            f'<div class="ahead-wrap"><table class="ahead"><thead><tr><th>Player</th>{head}</tr></thead>'
            f'<tbody>{rows}<tr class="sum"><th scope="row">Best lineup total</th>{totals}<td></td></tr>'
            f'<tr><th scope="row">Starters on a bye</th>{byes}<td></td></tr></tbody></table></div>')


def waiver_html(L):
    head = '<h3 class="section-title">Waiver wire: top pickups</h3>'
    picks = L["waiver"]
    if not picks:
        return head + ('<p class="note">No available free agent clearly improves your lineup over the next '
                       f'{len(L["outlook"]["weeks"])} weeks.</p>')
    weeks = L["outlook"]["weeks"]
    items = ""
    for x in picks:
        p = x["p"]
        sched = ", ".join(
            f"week {w}: " + ("bye" if x["ps"][w]["status"] == "bye" else
                             f"{'vs' if x['ps'][w]['home'] else 'at'} {show_team(x['ps'][w]['opp'])} "
                             f"{x['ps'][w]['proj']:.1f}") for w in weeks)
        ref_avg = L["outlook"]["avg"].get(x["ref"]["sid"], 0)
        why = [f"Would start over {x['ref']['name']}: about {x['gain']:+.1f} points a week "
               f"({x['avg']:.1f} vs {ref_avg:.1f})."]
        if p["season_avg"] is not None:
            why.append(f"This season {p['season_avg']:.1f} a game over {p['cur_n']} "
                       f"game{'s' if p['cur_n'] != 1 else ''}, {p['l3']:.1f} over the last 3.")
        if p["inj_text"] and p["inj_boost"] >= 1.1:
            why.append(p["inj_text"])
        if p["future_mult"] is not None:
            why.append(f"His next {len(weeks)} opponents allow {pct_label(p['future_mult'])} to "
                       f"{p['pos']}s compared with the average.")
        for r in p["sell"]:
            why.append("Caution: " + r)
        drop = (f'<p class="drop">Suggested drop: <b>{esc(x["drop"]["name"])}</b>, who projects '
                f'{L["outlook"]["avg"].get(x["drop"]["sid"], 0):.1f} a week.</p>') if x["drop"] else ""
        items += (f'<li class="pick" style="{style_tc(p)}"><div class="b-top">{face_html(p)}'
                  f'<div class="b-text"><h4>{esc(p["name"])}</h4>'
                  f'<p class="sub">{esc(p["pos"])} on {esc(p["team_show"])}, free agent</p></div>'
                  f'<div class="b-pts"><span>{x["avg"]:.1f}</span><small>a week, next {len(weeks)}</small></div></div>'
                  f'<ul class="why-list">{"".join(f"<li>{esc(t)}</li>" for t in why)}</ul>'
                  f'<p class="sub">{esc(sched)}</p>{drop}</li>')
    return (head + '<p class="legend">Free agents ranked by how much they would improve a real starter over the '
            'coming weeks, not just this one. Nothing here is added for you.</p>'
            f'<ul class="picks">{items}</ul>')


def trade_html(L):
    out = '<h3 class="section-title">Trade watch</h3>'
    sell = L["sell"]
    out += '<h4 class="sub-title">Consider selling</h4>'
    if sell:
        items = ""
        for p in sell:
            items += (f'<li><b>{esc(p["name"])}</b> ({esc(p["pos"])}, {esc(p["team_show"])}): averaging '
                      f'{p["season_avg"]:.1f}, expected about {p["expected_avg"]:.1f}.'
                      f'<ul>{"".join(f"<li>{esc(r)}</li>" for r in p["sell"])}</ul></li>')
        out += ('<p class="legend">Players whose results look hard to repeat: soft opposition, touchdown-heavy '
                'scoring, or numbers inflated by an injured teammate. Their trade value is highest now.</p>'
                f'<ul class="trade">{items}</ul>')
    else:
        out += '<p class="note">None of your players look like clear sell-high candidates right now.</p>'
    bounce = [p for p in L["outlook"]["by_week"][L["outlook"]["weeks"][0]].values() if p["buy"]]
    if bounce:
        out += ('<h4 class="sub-title">Hold, do not sell low</h4><ul class="trade">' + "".join(
            f'<li><b>{esc(p["name"])}</b> ({esc(p["pos"])}, {esc(p["team_show"])}): {esc(p["buy"][0])}</li>'
            for p in bounce[:4]) + '</ul>')
    if BUY_LOW:
        out += '<h4 class="sub-title">Underperformers to trade for</h4>'
        if L["buy_low"]:
            items = ""
            for x in L["buy_low"]:
                p = x["p"]
                items += (f'<li><b>{esc(p["name"])}</b> ({esc(p["pos"])}, {esc(p["team_show"])}), owned by '
                          f'{esc(x["owner"])}: averaging {p["season_avg"]:.1f}, expected about '
                          f'{p["expected_avg"]:.1f}, projects {x["avg"]:.1f} a week going forward.'
                          f'<ul>{"".join(f"<li>{esc(r)}</li>" for r in p["buy"])}</ul></li>')
            out += ('<p class="legend">Players on other teams whose production trails their workload or '
                    'schedule. Their owners may undervalue them right now.</p>'
                    f'<ul class="trade">{items}</ul>')
        else:
            out += '<p class="note">No clear buy-low targets on other rosters right now.</p>'
    return out


def locked_note(L):
    if not L.get("locked"):
        return ""
    names = ", ".join(
        f'{p["name"]} ({p["actual_now"]:.1f})' if p["actual_now"] is not None else p["name"] for p in L["locked"])
    return (f'<p>{len(L["locked"])} starter{"s" if len(L["locked"]) != 1 else ""} already played and '
            f'{"are" if len(L["locked"]) != 1 else "is"} locked in place: {esc(names)}.</p>')


def editor_html(L):
    ai = L.get("ai") or {}
    if L.get("decided_by") == "claude":
        items = ""
        for new, old in L.get("changes", []):
            reason = (ai.get("players", {}).get(norm_name(new["name"])) or {}).get("reason", "")
            items += (f'<li><b>Starts {esc(new["name"])} over {esc(old["name"])}</b>, the model\'s pick. '
                      f'{esc(reason)}</li>')
        body = (f'<p>{esc(ai.get("review", ""))}</p>'
                + (f'<ul>{items}</ul>' if items else
                   '<p><i>Claude agreed with the model on every slot.</i></p>'))
        return ('<aside class="editor"><h3>Claude\'s final call, with a news check</h3>' + body +
                '<p class="note">Every player below has a "Why" section showing the evidence behind his '
                'start or sit.</p></aside>')
    if L.get("ai_note"):
        msg = esc(L["ai_note"])
    elif L.get("claude_on"):
        msg = "Claude was not able to review this league this time, so the projection model made the call."
    else:
        msg = ("The projection model made this call. Add an Anthropic API key to have Claude weigh all of the "
               "evidence, check the latest news and make the final decision.")
    return (f'<aside class="editor"><h3>How this lineup was decided</h3><p>{msg}</p>'
            '<p class="note">Every player below has a "Why" section showing the evidence behind his '
            'start or sit.</p></aside>')


def played_cell(p):
    if p["actual_now"] is None or p["game_state"] not in ("live", "final"):
        return "not yet"
    return f'{p["actual_now"]:.1f} ({"final" if p["game_state"] == "final" else "live"})'


def data_check_html(L):
    a = L["audit"]
    players = sorted((p for p in L["projections"].values() if p["pos"] in ELIGIBLE), key=lambda p: -p["proj"])
    have = sum(1 for p in players if p["sleeper_proj"] is not None)
    rows = ""
    for p in players:
        f1 = lambda v: "" if v is None else f"{v:.1f}"
        rows += (f'<tr><td>{esc(p["name"])}</td><td>{esc(p["pos"])}</td><td>{f1(p["sleeper_proj"]) or "none"}</td>'
                 f'<td>{f1(p["model_proj"])}</td><td><b>{p["proj"]:.1f}</b></td><td>{played_cell(p)}</td><td>{f1(p["season_avg"])}</td>'
                 f'<td>{f1(p["season_max"])}</td><td>{esc(", ".join(f"{x:.1f}" for x in p["log"]))}</td></tr>')
    if a["n_actuals"]:
        src = (f"Actual points come from Sleeper: {a['n_actuals']} player-weeks scored with this league's settings, "
               f"{a['exact']} of them straight from your league's matchup results.")
    else:
        src = ("Sleeper's actual points could not be loaded, so past points were recomputed from raw NFL stats. "
               "Those can differ from what Sleeper shows.")
    if a["diffs"]:
        worst = "".join(f'<tr><td>{esc(n)}</td><td>{w}</td><td>{c:.1f}</td><td>{ac:.1f}</td></tr>'
                        for n, w, c, ac in a["worst"])
        audit = (f'<p>Recomputing raw stats disagreed with Sleeper on {a["diffs"]} of your player-weeks, by '
                 f'{a["mae"]:.1f} points on average. Sleeper\'s numbers are the ones used. Biggest gaps:</p>'
                 f'<table class="fx-table"><thead><tr><th>Player</th><th>Week</th><th>Recomputed</th>'
                 f'<th>Sleeper</th></tr></thead><tbody>{worst}</tbody></table>')
    else:
        audit = "<p>My recomputation of the raw stats matched Sleeper's points for your players.</p>"
    cov = (f"Sleeper projected {have} of your {len(players)} players this week." if L["outlook"]["weeks"] and have
           else "Sleeper's projections could not be loaded, so every number here is the model's own.")
    return ('<details class="method"><summary>Check the numbers: Sleeper vs. the model</summary>'
            f'<p>{esc(cov)} The final number starts from Sleeper\'s projection and the model moves it only '
            f'slightly. {esc(src)}</p>{audit}'
            '<div class="ahead-wrap"><table class="fx-table"><thead><tr><th>Player</th><th>Pos</th>'
            '<th>Sleeper</th><th>Model</th><th>Final</th><th>This week</th><th>Season avg</th><th>Best game</th>'
            f'<th>Points each game this season</th></tr></thead><tbody>{rows}</tbody></table></div></details>')


def panel_html(L, hidden):
    scale = 10 * math.ceil(max([c["ceil"] for c in L["chosen"].values()] + [20]) / 10)
    calls_by_start = {c["start"]["sid"]: c for c in L["calls"]}
    current = list(L["current"])

    rows = []
    for i, s in enumerate(L["slots"]):
        label = SLOT_LABEL.get(s, s)
        if s not in ELIGIBLE:
            cur = current[i] if i < len(current) else None
            who = L["projections"].get(cur, {}).get("name", cur) if cur not in (None, "", "0") else "Empty"
            rows.append(f'<li class="brief plain"><span class="kicker">{esc(s)}</span><h4>{esc(who)}</h4>'
                        f'<p class="sub">Defensive slots are not projected, so your current pick stays.</p></li>')
            continue
        c = L["chosen"].get(i)
        if not c:
            rows.append(f'<li class="brief plain"><span class="kicker">{esc(label)}</span><h4>No eligible player</h4>'
                        f'<p class="sub">Nobody on your roster fits this slot.</p></li>')
            continue
        tags, detail = "", ""
        if c["name"] in L["adds"]:
            tags += '<span class="tag in">Not in your lineup now</span>'
        call = calls_by_start.get(c["sid"])
        if call:
            tags += '<span class="tag flag">Close call</span>'
            detail = call_detail(call, L["enrich"].get(call["id"]))
        rows.append(brief_html(label, c, scale, tags, detail))

    if L["adds"]:
        change = (f'<p>Start <b>{esc(join_names(L["adds"]))}</b>'
                  + (f' and sit <b>{esc(join_names(L["drops"]))}</b>.' if L["drops"] else ".") + "</p>")
    else:
        change = "<p>Your current Sleeper lineup already matches this recommendation.</p>"
    n = len(L["calls"])
    close = (f"<p>{n} close call{'s' if n != 1 else ''} this week. Open the boxed notes under the flagged players for the reasoning.</p>"
             if n else "<p>No close calls this week. Every starter clears the bench by a comfortable margin.</p>")

    bench_rows = "".join(bench_row(p) for p in L["bench"])
    inactive = ""
    if L["unavailable"]:
        names = ", ".join(p["name"] for p in L["unavailable"])
        inactive = f'<p class="note">On injured reserve or the taxi squad, and not eligible to start: {esc(names)}.</p>'

    return (
        f'<section class="panel" id="lg-{esc(L["id"])}" role="tabpanel"{" hidden" if hidden else ""}>'
        f'<h2>{esc(L["name"])}</h2>'
        f'<p class="deck">{esc(L["scoring"])}'
        + (f', {esc(L["teams"])} teams' if L["teams"] else "")
        + f'. Your record: {esc(L["record"])}.</p>'
        f'<div class="lede"><p>Projected lineup total: <span class="total">{L["total"]:.1f}</span> points'
        + (f', including {L["scored"]:.1f} already scored' if L.get("scored") else "") + '.</p>'
        + locked_note(L) +
        f'{change}{close}</div>'
        f'{editor_html(L)}'
        f'<h3 class="section-title">The starting lineup</h3>'
        f'<p class="legend">Each bar is the player\'s likely range, the dark mark is the projection, and the '
        f'faint lines mark every 10 points. The small gauge shows how the opposing defense treats that position: '
        f'green to the right is a soft matchup, red to the left is a tough one.</p>'
        f'<ul class="briefs">{"".join(rows)}</ul>{data_check_html(L)}'
        f'<h3 class="section-title">Reserves</h3>{inactive}'
        f'<table class="bench-table"><thead><tr><th></th><th>Player</th><th class="hide-sm">Game</th>'
        f'<th class="hide-sm">Status</th><th style="text-align:right">Projected</th></tr></thead>'
        f'<tbody>{bench_rows}</tbody></table>{ahead_html(L)}{waiver_html(L)}{trade_html(L)}'
        f'{injury_check_html(L)}{track_html(L)}</section>'
    )


def render_page(leagues, season, week):
    now = datetime.now()
    date_line = f'{now:%A, %B} {now.day}, {now.year}'
    stamp = "Updated " + now.strftime("%I:%M %p").lstrip("0")
    tabs, panels = [], []
    for i, L in enumerate(leagues):
        pid = "lg-" + L["id"]
        tabs.append(
            f'<button class="tab" role="tab" data-target="{esc(pid)}" aria-selected="{"true" if i == 0 else "false"}">'
            f'{esc(L["name"])}<small>Record {esc(L["record"])}</small></button>')
        panels.append(panel_html(L, hidden=i != 0))
    if not leagues:
        panels.append('<section class="panel"><h2>No active leagues found</h2>'
                      '<p class="deck">Leagues without a finished draft are skipped.</p></section>')
    method = (
        '<details class="method"><summary>How the projections work</summary>'
        '<p>Each projection starts from the player\'s recent fantasy points, scored with that league\'s own '
        f'settings, with newer games counting more (each older game counts {W["decay"]:.0%} as much, and last '
        'season is discounted further). It is then multiplied by four adjustments: how many points the '
        'opposing defense has allowed to that position compared with the league average, the team\'s '
        'Vegas implied points, how the player has scored against this opponent before, and a small home '
        'or away edge. Questionable players are reduced and players ruled out project at zero. Matchup and '
        'history adjustments are pulled toward neutral when there is little data, and capped so one odd '
        'week cannot swing a pick. Kickers and defenses use Vegas lines only.</p>'
        '<p>Projections start from Sleeper\'s own projection for each player, scored with this league\'s settings. '
        'The model then adjusts that number only slightly (a limited share of the gap), using the factors below. '
        'Past points are Sleeper\'s actual scores, not numbers recomputed from raw stats. The Check the numbers '
        'section shows both side by side.</p>'
        '<p>Current form comes first. Games from this season count the most, last season about half as much, '
        'and two seasons ago about a quarter, so a veteran who is struggling now is not rated on what he did '
        'in the past. Each card shows the season average and the last three games.</p>'
        '<p>The projection also reflects how the team is playing this season (points scored and pass and run '
        'volume), where the player sits among the available players at his position (when starters ahead of him '
        'are out, he moves up and takes a bigger share of the work), and a reality check against his best and '
        'worst games this season. The script replays recent weeks to see how far its own projections missed and '
        'corrects for that. With an Anthropic key, Claude then reviews all of this evidence, checks the news, '
        'and makes the final lineup decision, which the script validates before using. Each player\'s card '
        'has a Why section with every consideration.</p>'
        '<p>Looking ahead uses the same model for the next few weeks, assuming injuries fade the further out '
        'it looks. Waiver pickups are free agents ranked by how much they would improve one of your real '
        'starters over those weeks. Sell-high and buy-low flags compare each player\'s scoring with what his '
        'workload usually produces, the defenses he has faced against the ones coming up, whether injured '
        'teammates inflated his numbers, and whether his workload is rising or falling. A small correction for '
        'those flags is built into the projection itself.</p>'
        '<p>Teammate injuries change the baseline. When a starter who sees real volume at the same position '
        'is out, the script looks at past weeks he missed and compares how this player scored then and with '
        'him healthy. It also estimates the extra touches from the vacated volume, split in proportion to the '
        'remaining players and never handed over one for one, and blends the two, trusting the history more '
        'the more recent games without the starter it has (only this season and last count). If a player is '
        'scoring well below his usual level this season, the boost is cut back, and the vacated volume is split '
        'by recent production as well as workload. If a starter is healthy again, games that were inflated '
        'by his absence are discounted. A backup quarterback with little history is projected from a share of '
        'the injured starter\'s production.</p>'
        '<p>Tune the weights in the <code>W</code> dictionary at the top of <code>fantasy.py</code>. '
        'Projections are estimates. Always check inactives before kickoff. Player photos load from Sleeper '
        'and are for personal use.</p></details>')
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>The Gridiron Gazette, week {week}</title>'
        '<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
        '<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,700;0,800;0,900;1,700'
        '&family=Source+Serif+4:ital,wght@0,400;0,600;0,700;1,400&family=UnifrakturCook:wght@700&display=swap" '
        'rel="stylesheet">'
        f'<style>{CSS}</style></head><body><div class="wrap">'
        f'<header class="masthead"><h1>The Gridiron Gazette</h1>'
        f'<div class="dateline"><span>Week {week} edition</span><span>{esc(date_line)}</span>'
        f'<span>{esc(stamp)}</span></div></header>'
        f'<p class="lede-note">Recommended starters for every active league, built from your Sleeper rosters, '
        f'nflverse stats and Vegas lines.</p>'
        f'<div class="tabs" role="tablist" aria-label="Leagues">{"".join(tabs)}</div>'
        f'{"".join(panels)}{method}</div><script>{JS}</script></body></html>'
    )


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def build_context(season=None, week=None):
    """Everything that is the same for every user: NFL data, schedule, Sleeper's player list and weekly feeds."""
    _FEED_FAILS.update({"stats": 0, "projections": 0})
    state = sleeper("/state/nfl")
    season = int(season or SEASON or state["season"])
    week = max(1, int(week or WEEK or state.get("week") or 1))
    log(f"Season {season}, week {week}")
    log("Loading NFL data (first run downloads a few files, later runs use the cache)...")
    players_db = load_sleeper_players()
    sched, avg_implied, results = load_schedule(season)
    stat_rows = load_stat_rows(season, week)
    have = {r["pid"] for r in stat_rows if r["pid"]}
    name_idx = {(norm_name(r["name"]), r["pos"]): r["pid"] for r in stat_rows if r["pid"]}
    sid2gid = {}
    for sid, meta in players_db.items():
        pos = meta.get("position")
        if pos in SKILL:
            gid = meta.get("gsis_id")
            if gid not in have:
                gid = name_idx.get((norm_name(meta_name(meta, "")), pos))
            if gid:
                sid2gid[sid] = gid
    log("Fetching Sleeper's actual points and projections...")
    now = NOW_OVERRIDE or datetime.now(timezone.utc)
    sl_stats = {w: sleeper_feed("stats", season, w, None if w < week - 1 else (12 if w == week - 1 else 0.1))
                for w in range(1, week + 1)}
    sl_proj = {}
    if USE_SLEEPER_PROJECTIONS:
        sl_proj = {w: sleeper_feed("projections", season, w, 3) for w in range(week, min(week + AHEAD_WEEKS, 19))}
    log(f"  Sleeper stat lines for weeks {sorted(w for w, f in sl_stats.items() if f) or 'none'}; "
        f"projections for weeks {sorted(w for w, f in sl_proj.items() if f) or 'none'}")
    return {"season": season, "week": week, "players_db": players_db, "sched": sched,
            "avg_implied": avg_implied, "stat_rows": stat_rows, "results": results,
            "sid2gid": sid2gid, "sl_stats": sl_stats, "sl_proj": sl_proj, "now": now, "api_key": ""}


# ---- website support: shared data is built once per warm server and reused by every visitor ----
class UserNotFound(Exception):
    pass


_CTX = {"at": 0.0, "ctx": None}
_CTX_LOCK = threading.Lock()


def get_context(ttl_seconds=900):
    """The shared (user-independent) data, rebuilt every `ttl_seconds`."""
    with _CTX_LOCK:
        if _CTX["ctx"] is None or time.time() - _CTX["at"] > ttl_seconds:
            try:
                _CTX["ctx"] = build_context()
            except SystemExit as e:  # the command-line loaders exit on missing data; a web server must not
                raise RuntimeError(str(e) or "NFL data unavailable") from None
            _CTX["at"] = time.time()
        return _CTX["ctx"]


def request_context(api_key=""):
    """A per-request view of the shared data with a fresh clock and THIS visitor's key (or none)."""
    base = get_context()
    return {**base, "now": NOW_OVERRIDE or datetime.now(timezone.utc), "api_key": api_key or ""}


def find_user(username):
    u = sleeper(f"/user/{username}")
    if not u or not u.get("user_id"):
        raise UserNotFound(username)
    return u


def user_leagues(user, season):
    return sleeper(f"/user/{user['user_id']}/leagues/nfl/{season}") or []


def analyze_one(username, league_id, ctx):
    """Analyze one league for one user. Returns the result dict, or None if the league is skipped."""
    user = find_user(username)
    lg = sleeper(f"/league/{league_id}")
    if not lg:
        return None
    return analyze_league(lg, user["user_id"], ctx)


def league_payload(res):
    return {"id": res["id"], "name": res["name"], "record": res["record"],
            "decided_by": res["decided_by"], "html": panel_html(res, hidden=True)}


def main():
    ensure_network()
    key_source = load_api_key()
    if USE_CLAUDE and key_source:
        log(f"Claude decision step: ON ({CLAUDE_MODEL}), key read from {key_source}. "
            "One call per league, with web search.")
    else:
        log("Claude decision step: OFF. No API key was found, so the projection model makes every decision.\n"
            f"  Looked for a .txt file containing a key (it starts with sk-ant-) in: {os.path.dirname(os.path.abspath(__file__))}\n"
            "  To turn Claude on, save the key on one line in a text file in that folder, e.g. anthropic_key.txt.\n"
            "  Do not share that file.")
    ctx = build_context()
    ctx["api_key"] = os.environ.get("ANTHROPIC_API_KEY", "") if USE_CLAUDE else ""
    season, week = ctx["season"], ctx["week"]
    user = find_user(SLEEPER_USERNAME)
    leagues = user_leagues(user, season)
    log(f"Found {len(leagues)} leagues for {SLEEPER_USERNAME}")

    results, failed = [], 0
    for lg in leagues:
        log(f"Analyzing {lg['name']}...")
        try:
            res = analyze_league(lg, user["user_id"], ctx)
        except requests.ConnectionError:
            failed += 1
            log(f"  could not reach Sleeper while analyzing {lg['name']}. This is a network problem, "
                "not a problem with your league.")
            continue
        except Exception as e:
            failed += 1
            log(f"  could not analyze {lg['name']}: {type(e).__name__}: {e}")
            continue
        if res:
            results.append(res)
    if not results and failed:
        log("\nNo league could be analyzed, so lineups.html was not changed.")
        if failed == len([lg for lg in leagues if lg.get("status") not in ("pre_draft", "drafting")]):
            log(NETWORK_HELP.format(hosts="Sleeper's servers (api.sleeper.app)"))
        return

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), OUTPUT_FILE)
    with open(out, "w", encoding="utf-8") as f:
        f.write(render_page(results, season, week))
    log(f"\nWrote {out} ({len(results)} leagues)")
    if OPEN_WHEN_DONE:
        webbrowser.open("file:///" + out.replace("\\", "/"))


if __name__ == "__main__":
    main()
