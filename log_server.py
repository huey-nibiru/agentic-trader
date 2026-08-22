"""
Local live trade-log viewer.

Cursor will not live-reload a file tab. This serves a small page at
http://127.0.0.1:<port>/ that polls trade_log.csv and redraws the table.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from executor import read_trade_log

DISPLAY_FIELDS = [
    "ts_readable",
    "symbol",
    "side",
    "state",
    "usd",
    "price",
    "pnl",
    "portfolio_balance",
    "exit_reason",
    "mode",
]

PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>trade log</title>
  <style>
    body { margin: 16px; font: 13px/1.4 -apple-system, BlinkMacSystemFont, sans-serif;
           background: #111; color: #eee; }
    #status { color: #888; margin-bottom: 12px; }
    table { border-collapse: collapse; width: 100%; }
    th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #333;
             white-space: nowrap; }
    th { position: sticky; top: 0; background: #111; }
    tr.buy td { color: #8fd19e; }
    tr.sell td { color: #f0b7b7; }
    td.reason { white-space: normal; max-width: 420px; }
  </style>
</head>
<body>
  <p id="status">connecting…</p>
  <table>
    <thead><tr id="head"></tr></thead>
    <tbody id="body"></tbody>
  </table>
  <script>
    const fields = FIELDS_JSON;
    const head = document.getElementById("head");
    const body = document.getElementById("body");
    const status = document.getElementById("status");
    head.innerHTML = fields.map(f => "<th>" + f + "</th>").join("");

    function cell(field, value) {
      const cls = field === "exit_reason" ? " class=\\"reason\\"" : "";
      return "<td" + cls + ">" + String(value ?? "") + "</td>";
    }

    async function refresh() {
      try {
        const r = await fetch("/trades?t=" + Date.now(), { cache: "no-store" });
        if (!r.ok) throw new Error("HTTP " + r.status);
        const rows = await r.json();
        body.innerHTML = rows.map(row => {
          const side = String(row.side || "").toLowerCase();
          return "<tr class=\\"" + side + "\\">" +
            fields.map(f => cell(f, row[f])).join("") + "</tr>";
        }).join("");
        const now = new Date().toLocaleTimeString();
        status.textContent = rows.length + " fills · updated " + now;
      } catch (err) {
        status.textContent = "waiting for bot… " + err.message;
      }
    }

    refresh();
    setInterval(refresh, 1000);
  </script>
</body>
</html>
"""

PAGE = PAGE.replace("FIELDS_JSON", json.dumps(DISPLAY_FIELDS))


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def _send(self, code, body, content_type):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html", "/trade_log.html"):
            self._send(200, PAGE, "text/html; charset=utf-8")
            return
        if path == "/trades":
            rows = read_trade_log()
            rows.sort(key=lambda r: float(r.get("ts") or 0))
            self._send(200, json.dumps(rows), "application/json")
            return
        self._send(404, "not found", "text/plain; charset=utf-8")


def start_log_server(port: int = 8765, open_browser: bool = True):
    last_err = None
    httpd = None
    for candidate in range(port, port + 10):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", candidate), _Handler)
            port = candidate
            break
        except OSError as e:
            last_err = e
            httpd = None
    if httpd is None:
        print(f"[log] could not start live viewer: {last_err}", flush=True)
        return None

    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    print(f"[log] live trade log → {url}", flush=True)
    if open_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass
    return httpd
