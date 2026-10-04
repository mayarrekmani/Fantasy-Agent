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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "3000"))
    print(f"Serving on http://localhost:{port}  (Ctrl+C to stop)")
    ThreadingHTTPServer(("127.0.0.1", port), Dev).serve_forever()
