"""Receive DTS Entry alerts from TradingView and hand each one to the executor.

TradingView posts the alert's message as the request body to
https://<tunnel>/dts/<secret>. Every message is appended to
state/dts_alert_inbox.jsonl as received, then the executor is started for it
in the background; the executor's lock makes alerts run one at a time in
arrival order. The reply goes back to TradingView immediately.

  python -m day_trading_setup.executor.receiver
"""
import json
import logging
import os
import secrets
import subprocess
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

from day_trading_setup.executor import settings

NY = ZoneInfo("America/New_York")
log = logging.getLogger("dts.receiver")
MAX_BODY = 4096


def load_secret():
    f = settings.SECRET_FILE
    if not f.exists():
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(secrets.token_urlsafe(24))
        os.chmod(f, 0o600)
    return f.read_text().strip()


SECRET = load_secret()


class Handler(BaseHTTPRequestHandler):
    def _reply(self, code, text):
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self._reply(200, f"ok ({'PAPER orders' if settings.EXECUTE else 'dry run'})")
        self._reply(404, "not found")

    def do_POST(self):
        if self.path != f"/dts/{SECRET}":
            log.warning("rejected POST to %s", self.path[:40])
            return self._reply(404, "not found")
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > MAX_BODY:
            return self._reply(400, "bad length")
        raw = self.rfile.read(n).decode("utf-8", "replace").strip()
        message = raw
        if raw.startswith("{"):                       # tolerate a JSON body {"message": ...}
            try:
                message = json.loads(raw).get("message", raw)
            except ValueError:
                pass
        received = datetime.now(NY).isoformat()
        settings.STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(settings.INBOX_FILE, "a") as f:
            f.write(json.dumps({"received": received, "message": message}) + "\n")
        log.info("alert: %s", message)
        subprocess.Popen([sys.executable, "-m", "day_trading_setup.executor.executor",
                          "--message", message, "--received", received],
                         cwd=str(settings.REPO), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        self._reply(200, "ok")

    def log_message(self, fmt, *args):   # route http.server's own logging through ours
        log.info("%s %s", self.address_string(), fmt % args)


def main():
    settings.LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(settings.LOG_DIR / "dts_receiver.log"), logging.StreamHandler()])
    server = ThreadingHTTPServer((settings.RECEIVER_HOST, settings.RECEIVER_PORT), Handler)
    log.info("listening on %s:%s (%s)", settings.RECEIVER_HOST, settings.RECEIVER_PORT,
             "PAPER orders" if settings.EXECUTE else "dry run")
    server.serve_forever()


if __name__ == "__main__":
    main()
