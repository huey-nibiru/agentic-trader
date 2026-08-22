"""
Local live trade-log viewer.

Cursor will not live-reload a file tab. This serves a small page at
http://127.0.0.1:<port>/ that polls trade_log.csv and redraws the table.
"""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from executor import read_trade_log
from state import STATE_PATH

MASCOT_PATH = "pnl_mascot.html"

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
    html, body {
      margin: 0; height: 100%;
      background: #0B0E14; color: #E8ECF1;
      font: 13px/1.4 "IBM Plex Mono", ui-monospace, Menlo, sans-serif;
    }
    .shell { display: flex; height: 100%; }
    .mascot-col {
      width: 364px; flex-shrink: 0;
      border-right: 1px solid #1E2530;
      background: #0B0E14;
    }
    .mascot-col iframe {
      width: 100%; height: 100%; border: 0; display: block;
    }
    .log-col {
      flex: 1; min-width: 0; overflow: auto;
      padding: 16px 20px 24px;
    }
    #status { color: #6B7480; margin: 0 0 12px; }
    table { border-collapse: collapse; width: 100%; }
    th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #1E2530;
             white-space: nowrap; }
    th { position: sticky; top: 0; background: #0B0E14; color: #6B7480;
         font-weight: 500; letter-spacing: .04em; font-size: 11px;
         text-transform: uppercase; }
    tr.buy td { color: #fff; }
    tr.sell.win td { color: #26D07C; }
    tr.sell.loss td { color: #F2554F; }
    td.reason { white-space: normal; max-width: 420px; }
  </style>
</head>
<body>
  <div class="shell">
    <aside class="mascot-col">
      <iframe src="/mascot?embed=1" title="PnL mascot"></iframe>
    </aside>
    <main class="log-col">
      <p id="status">connecting…</p>
      <table>
        <thead><tr id="head"></tr></thead>
        <tbody id="body"></tbody>
      </table>
    </main>
  </div>
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

    function rowClass(row) {
      const side = String(row.side || "").toLowerCase();
      if (side === "buy") return "buy";
      if (side !== "sell") return side;
      const pnl = Number(row.pnl);
      if (pnl > 0) return "sell win";
      if (pnl < 0) return "sell loss";
      return "sell";
    }

    async function refresh() {
      try {
        const r = await fetch("/trades?t=" + Date.now(), { cache: "no-store" });
        if (!r.ok) throw new Error("HTTP " + r.status);
        const rows = await r.json();
        body.innerHTML = rows.map(row => {
          return "<tr class=\\"" + rowClass(row) + "\\">" +
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
        if path in ("/mascot", "/pnl_mascot.html"):
            if not os.path.exists(MASCOT_PATH):
                self._send(404, "pnl_mascot.html not found", "text/plain; charset=utf-8")
                return
            with open(MASCOT_PATH, encoding="utf-8") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
            return
        if path in ("/bot_state.json", "/state"):
            if not os.path.exists(STATE_PATH):
                self._send(404, json.dumps({"error": "bot_state.json not found"}),
                           "application/json")
                return
            with open(STATE_PATH, encoding="utf-8") as f:
                self._send(200, f.read(), "application/json")
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
