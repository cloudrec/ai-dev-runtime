#!/usr/bin/env python3
"""GAIKA owner capture inbox: accepts PUT /owner/inbox/<name>.zip behind nginx basic auth.
Listens on 127.0.0.1 only. Stores inert files in /var/lib/gaika-owner-inbox; never serves or runs them."""
import http.server, os, re, time

DIR = "/var/lib/gaika-owner-inbox"
MAX = 50 * 1024 * 1024
DISK = 2 * 1024**3
NAME = re.compile(r"^gaika-owner-capture-[0-9]{8}-[0-9]{4}\.zip$")


class H(http.server.BaseHTTPRequestHandler):
    def do_PUT(self):
        name = self.path.rsplit("/", 1)[-1]
        n = int(self.headers.get("Content-Length") or 0)
        used = sum(os.path.getsize(os.path.join(DIR, f)) for f in os.listdir(DIR))
        if not NAME.match(name) or n <= 0 or n > MAX or used + n > DISK:
            self.send_response(400); self.end_headers(); return
        body = self.rfile.read(n)
        if body[:2] != b"PK":
            self.send_response(400); self.end_headers(); return
        dst = os.path.join(DIR, f"{int(time.time())}-{name}")
        with open(dst + ".part", "wb") as f:
            f.write(body)
        os.replace(dst + ".part", dst)
        self.send_response(201); self.end_headers(); self.wfile.write(b"stored\n")

    def log_message(self, fmt, *a):
        pass


os.makedirs(DIR, exist_ok=True)
http.server.ThreadingHTTPServer(("127.0.0.1", 8932), H).serve_forever()
