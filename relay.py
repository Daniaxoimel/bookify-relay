# -*- coding: utf-8 -*-
"""
Bookify Relay Server
Ucenik salje podatke na relay, profesor cita sa relaya.
"""
from http.server import HTTPServer, BaseHTTPRequestHandler
import json, time, os, threading

# {classroom_kod: {ucenik_id: {podaci}}}
sobe = {}
sobe_lock = threading.Lock()
ISTICE_ZA = 7200  # 2 sata neaktivnosti

class RelayHandler(BaseHTTPRequestHandler):
    def log_message(self, f, *a): pass

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
        # Profesor cita listu ucenika: GET /ucenik_lista?kod=S93D4R
        if self.path.startswith("/ucenik_lista"):
            kod = self._get_param("kod")
            if not kod:
                self._json({"greska": "Nedostaje kod"}, 400)
                return
            with sobe_lock:
                soba = sobe.get(kod, {})
                # Filtriraj neaktivne
                aktivni = {uid: u for uid, u in soba.items()
                           if time.time() - u.get("vrijeme", 0) < ISTICE_ZA}
            self._json({"ucenici": aktivni})

        # Profesor cita zadatak koji je poslao: GET /zadatak?kod=S93D4R
        elif self.path.startswith("/zadatak"):
            kod = self._get_param("kod")
            if not kod:
                self._json({"tekst": "", "tip": "tekst"})
                return
            with sobe_lock:
                zadatak = sobe.get(f"zadatak_{kod}", {"tekst": "", "tip": "tekst"})
            self._json(zadatak)

        elif self.path == "/ping":
            self._json({"status": "ok", "relay": "Bookify Relay v2.0"})

        elif self.path == "/status":
            with sobe_lock:
                ukupno = sum(len(v) for k, v in sobe.items() if not k.startswith("zadatak_"))
            self._json({"spojeni": ukupno})

        else:
            self._json({"greska": "Not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self._json({"greska": "Neispravan JSON"}, 400)
            return

        # Ucenik salje podatke: POST /update
        if self.path == "/update":
            kod = data.get("classroom_kod", "").strip().upper()
            ucenik_id = data.get("ucenik_id", "").strip()
            if not kod or not ucenik_id:
                self._json({"greska": "Nedostaje kod ili ucenik_id"}, 400)
                return
            with sobe_lock:
                if kod not in sobe:
                    sobe[kod] = {}
                sobe[kod][ucenik_id] = {
                    "ime":           data.get("ime", "Nepoznat"),
                    "razred":        data.get("razred", ""),
                    "promet_dug":    data.get("promet_dug", 0),
                    "promet_pot":    data.get("promet_pot", 0),
                    "zavrsio":       data.get("zavrsio", False),
                    "zadnji_update": data.get("zadnji_update", ""),
                    "state":         data.get("state", {}),
                    "ip":            ucenik_id,
                    "vrijeme":       time.time(),
                }
                # Vrati zadatak za ovaj kod
                zadatak = sobe.get(f"zadatak_{kod}", {"tekst": "", "tip": "tekst"})
                # Vrati oznake za ovog ucenika
                oznake = sobe.get(f"oznake_{kod}_{ucenik_id}", [])
            self._json({"status": "ok", "zadatak": zadatak, "oznake": oznake})

        # Profesor salje zadatak: POST /posalji_zadatak
        elif self.path == "/posalji_zadatak":
            kod = data.get("classroom_kod", "").strip().upper()
            tekst = data.get("tekst", "")
            tip = data.get("tip", "tekst")
            if not kod:
                self._json({"greska": "Nedostaje kod"}, 400)
                return
            with sobe_lock:
                sobe[f"zadatak_{kod}"] = {"tekst": tekst, "tip": tip}
            self._json({"status": "ok"})

        # Profesor salje oznake: POST /posalji_oznake
        elif self.path == "/posalji_oznake":
            kod = data.get("classroom_kod", "").strip().upper()
            ucenik_id = data.get("ucenik_id", "").strip()
            oznake = data.get("oznake", [])
            if not kod or not ucenik_id:
                self._json({"greska": "Nedostaje kod ili ucenik_id"}, 400)
                return
            with sobe_lock:
                sobe[f"oznake_{kod}_{ucenik_id}"] = oznake
            self._json({"status": "ok"})

        else:
            self._json({"greska": "Not found"}, 404)

    def _get_param(self, name):
        if "?" not in self.path:
            return ""
        for p in self.path.split("?", 1)[1].split("&"):
            if p.startswith(f"{name}="):
                return p[len(name)+1:].strip().upper()
        return ""


def _cisti():
    while True:
        time.sleep(600)
        with sobe_lock:
            for kod in list(sobe.keys()):
                if kod.startswith("zadatak_") or kod.startswith("oznake_"):
                    continue
                for uid in list(sobe[kod].keys()):
                    if time.time() - sobe[kod][uid].get("vrijeme", 0) > ISTICE_ZA:
                        del sobe[kod][uid]


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    threading.Thread(target=_cisti, daemon=True).start()
    print(f"Bookify Relay v2.0 pokrenut na portu {port}")
    HTTPServer(("0.0.0.0", port), RelayHandler).serve_forever()
