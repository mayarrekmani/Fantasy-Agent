"""GET /api/leagues?username=NAME  ->  the user's NFL leagues for the current season (fast; no analysis)."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from http.server import BaseHTTPRequestHandler  # noqa: E402

import requests  # noqa: E402

from lib import server_common as C  # noqa: E402

F = C.F
_STATE = {"at": 0.0, "value": None}


def nfl_state():
    if _STATE["value"] is None or time.time() - _STATE["at"] > 120:
        _STATE["value"], _STATE["at"] = F.sleeper("/state/nfl"), time.time()
    return _STATE["value"]


class handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep request lines (and anything in them) out of the logs
        pass

    def do_GET(self):
        q = C.query(self)
        username = (q.get("username") or "").strip()
        if not C.USERNAME_RE.match(username):
            return C.send_json(self, 400, {"error": "Enter your Sleeper username (letters, numbers, dots, dashes, underscores)."})
        if not C.LIMITER.allow("leagues:" + C.client_ip(self), 40, 600):
            return C.send_json(self, 429, {"error": "Too many requests. Please wait a few minutes and try again."})
        try:
            state = nfl_state()
            season = int(F.SEASON or state["season"])
            user = F.find_user(username)
            leagues = F.user_leagues(user, season)
        except F.UserNotFound:
            return C.send_json(self, 404, {"error": f"No Sleeper user named \"{username}\" was found. Check the spelling."})
        except F.requests.RequestException:
            return C.send_json(self, 502, {"error": "Sleeper did not respond. Please try again in a moment."})
        except Exception as e:  # never leak internals to visitors
            print(f"leagues error: {type(e).__name__}: {e}")
            return C.send_json(self, 500, {"error": "Something went wrong looking up your leagues."})
        items = []
        for lg in leagues:
            done = lg.get("status") not in ("pre_draft", "drafting")
            items.append({"id": str(lg["league_id"]), "name": lg.get("name") or "League",
                          "teams": lg.get("total_rosters"), "active": done,
                          "skip": None if done else "The draft has not finished yet."})
        C.send_json(self, 200, {
            "user": {"username": user.get("username") or username, "display_name": user.get("display_name") or username},
            "season": season, "week": int(F.WEEK or state.get("week") or 1),
            "season_type": state.get("season_type"), "leagues": items})
