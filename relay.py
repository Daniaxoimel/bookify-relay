# -*- coding: utf-8 -*-
"""
Bookify Relay Server — postaviti na Google Cloud Run
Čuva mapiranje: kod_učionice → javni URL profesora
"""

from http.server import HTTPServer, BaseHTTPRequestHandler
import json
import time
import os
import threading

# Podaci se čuvaju u memoriji — relay je samo posrednik
# {kod: {"url": "https://...", "vrijeme": 1234567890}}
sobe = {}
sobe_lock = threading.Lock()

# Soba ističe ako profesor nije aktivan 2 sata
ISTICE_ZA = 7200  # sekundi


class RelayHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Isključi logove

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        # Učenik pita: GET /spoji?kod=XK7M9R
        if self.path.startswith("/spoji"):
            kod = ""
            if "?" in self.path:
                params = self.path.split("?", 1)[1]
                for p in params.split("&"):
                    if p.startswith("kod="):
                        kod = p[4:].strip().upper()

            if not kod:
                self._json({"greska": "Nedostaje kod"}, 400)
                return

            with sobe_lock:
                soba = sobe.get(kod)

            if not soba:
                self._json({"greska": "Kod nije pronađen. Profesor možda nije spojen."}, 404)
                return

            # Provjeri da li je soba istekla
            if time.time() - soba["vrijeme"] > ISTICE_ZA:
                with sobe_lock:
                    sobe.pop(kod, None)
                self._json({"greska": "Profesor je offline."}, 404)
                return

            self._json({"url": soba["url"], "kod": kod})

        elif self.path == "/ping":
            self._json({"status": "ok", "relay": "Bookify Relay v1.0"})

        elif self.path == "/status":
            with sobe_lock:
                aktivne = len(sobe)
            self._json({"aktivne_sobe": aktivne})

        else:
            self._json({"greska": "Not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body.decode("utf-8"))
        except Exception:
            self._json({"greska": "Neispravan JSON"}, 400)
            return

        # Profesor registruje: POST /registruj {"kod": "XK7M9R", "url": "https://..."}
        if self.path == "/registruj":
            kod = data.get("kod", "").strip().upper()
            url = data.get("url", "").strip()

            if not kod or not url:
                self._json({"greska": "Nedostaju kod ili url"}, 400)
                return

            with sobe_lock:
                sobe[kod] = {
                    "url": url,
                    "vrijeme": time.time()
                }

            self._json({"status": "ok", "kod": kod, "url": url})

        # Profesor osvježava (heartbeat da ne istekne)
        elif self.path == "/heartbeat":
            kod = data.get("kod", "").strip().upper()
            if kod and kod in sobe:
                with sobe_lock:
                    if kod in sobe:
                        sobe[kod]["vrijeme"] = time.time()
                self._json({"status": "ok"})
            else:
                self._json({"greska": "Kod nije registrovan"}, 404)

        # Profesor se odjavljuje
        elif self.path == "/odjava":
            kod = data.get("kod", "").strip().upper()
            with sobe_lock:
                sobe.pop(kod, None)
            self._json({"status": "ok"})

        else:
            self._json({"greska": "Not found"}, 404)


def cisti_stare_sobe():
    """Briše istekle sobe svakih 10 minuta."""
    while True:
        time.sleep(600)
        with sobe_lock:
            istekli = [k for k, v in sobe.items()
                       if time.time() - v["vrijeme"] > ISTICE_ZA]
            for k in istekli:
                del sobe[k]


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))

    t = threading.Thread(target=cisti_stare_sobe, daemon=True)
    t.start()

    print(f"Bookify Relay pokrenut na portu {port}")
    server = HTTPServer(("0.0.0.0", port), RelayHandler)
    server.serve_forever()
