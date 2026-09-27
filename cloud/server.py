#!/usr/bin/env python3
"""Project DD — Databricks App static server.

Stdlib only (no requirements.txt → zero pip step, fast compute start). Serves the
built SPA (index.html + app.js + styles.css + data.js + hygiene_data.js +
config.js) from this directory. Binds DATABRICKS_APP_PORT (Databricks Apps injects
it), falling back to PORT then 8000 for local testing.
"""
import http.server
import os
import socketserver

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("DATABRICKS_APP_PORT") or os.environ.get("PORT") or "8000")


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=HERE, **kwargs)

    def end_headers(self):
        # data.js is regenerated on every deploy — never let a proxy or the browser
        # serve a stale blob.
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, *args):  # keep the app log quiet; Databricks captures stdout
        pass


socketserver.TCPServer.allow_reuse_address = True

if __name__ == "__main__":
    with socketserver.TCPServer(("0.0.0.0", PORT), Handler) as httpd:
        print(f"Project DD cloud mirror serving {HERE} on 0.0.0.0:{PORT}", flush=True)
        httpd.serve_forever()
