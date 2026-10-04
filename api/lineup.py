"""GET /api/lineup?username=NAME&league_id=ID  ->  the analyzed league as an HTML panel (this is the slow call).

Optional headers: X-Anthropic-Key (the visitor's own key, used only for this request and never stored) or
X-Owner-Code (the site owner's passcode, which makes the site use its own ANTHROPIC_API_KEY)."""
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from http.server import BaseHTTPRequestHandler  # noqa: E402

from lib import server_common as C  # noqa: E402

F = C.F


class handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        q = C.query(self)
        username, league_id = (q.get("username") or "").strip(), (q.get("league_id") or "").strip()
        if not C.USERNAME_RE.match(username) or not C.LEAGUE_RE.match(league_id):
            return C.send_json(self, 400, {"error": "A valid Sleeper username and league are required."})
        if not C.LIMITER.allow("lineup:" + C.client_ip(self), 20, 600):
            return C.send_json(self, 429, {"error": "Too many analyses in a short time. Please wait a few minutes."})
        key, mode, key_error = C.pick_api_key(self)
        if key_error:
            return C.send_json(self, key_error[0], {"error": key_error[1]})
        started = time.time()
        try:
            ctx = F.request_context(api_key=key)
            res = F.analyze_one(username, league_id, ctx)
        except F.UserNotFound:
            return C.send_json(self, 404, {"error": "That Sleeper user was not found."})
        except F.requests.RequestException:
            return C.send_json(self, 502, {"error": "Sleeper or the NFL data source did not respond. Please try again."})
        except RuntimeError as e:
            print("data unavailable:", str(e)[:200])
            return C.send_json(self, 503, {"error": "The NFL data feed is temporarily unavailable. Please try again soon."})
        except Exception as e:
            print("lineup error:", type(e).__name__, str(e)[:200])
            traceback.print_exc()
            return C.send_json(self, 500, {"error": "Something went wrong analyzing this league."})
        if not res:
            return C.send_json(self, 200, {"skipped": "This league has no players on your roster yet, or its draft has not finished."})
        payload = F.league_payload(res)
        payload["claude"] = mode
        payload["seconds"] = round(time.time() - started, 1)
        C.send_json(self, 200, payload)
