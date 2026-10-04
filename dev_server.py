#!/usr/bin/env python3
"""Run the whole site on your own computer (no Vercel needed):

    python dev_server.py            then open  http://localhost:3000
"""
import importlib.util
import mimetypes
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(ROOT, "public")
sys.path.insert(0, ROOT)


def _load(name):
    spec = importlib.util.spec_from_file_location(f"api_{name}", os.path.join(ROOT, "api", f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.handler


ROUTES = {"/api/leagues": _load("leagues"), "/api/lineup": _load("lineup")}


class Dev(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ROUTES:
            return ROUTES[path].do_GET(self)  # the same code Vercel runs
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        full = os.path.normpath(os.path.join(PUBLIC, rel))
        if not full.startswith(PUBLIC + os.sep) or not os.path.isfile(full):
            self.send_response(404)
            self.end_headers()
            return
        body = open(full, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", (mimetypes.guess_type(full)[0] or "text/plain") + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def enable_local_claude():
    """Local convenience only (this file is never deployed): use a key file next to this script, if there is one,
    so Claude's final call works on your own computer without pasting the key into the page each time."""
    import fantasy
    source = fantasy.load_api_key()
    if source:
        os.environ["ENABLE_SERVER_CLAUDE"] = "1"
        os.environ.setdefault("SERVER_CLAUDE_PER_DAY", "100")
        print(f"Claude's final call: ON for this local server (key read from {source}).")
    else:
        print("Claude's final call: OFF. Paste a key in the box on the page, or save your key in a .txt file "
              "in this folder (next to dev_server.py).")


if __name__ == "__main__":
    enable_local_claude()
    port = int(os.environ.get("PORT", "3000"))
    print(f"Serving on http://localhost:{port}  (Ctrl+C to stop)")
    ThreadingHTTPServer(("127.0.0.1", port), Dev).serve_forever()