# -*- coding: utf-8 -*-
"""
Bookify Relay Server v3.0
Ucenik salje podatke na relay, profesor cita sa relaya.

Promjene u odnosu na v2.0:
- Razdvojene strukture za sobe/zadatke/oznake (umjesto miješanja u jednom dict-u
  preko string-prefiksa) -> lakše za čitanje i bez bug-a u /status.
- Zadaci i oznake sada imaju svoj timestamp i čiste se zajedno sa sobom
  (prije su se gomilali zauvijek u memoriji).
- ThreadingHTTPServer -> paralelno opsluzuje vise ucenika/profesora odjednom.
- Query parametri se parsiraju preko urllib.parse (robusnije od rucnog splita).
- Periodično čuvanje stanja u JSON fajl na disku, ucitavanje pri pokretanju
  -> podaci preživljavaju restart/redeploy (npr. Render free tier).
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import json, time, os, threading, signal, sys

ISTICE_ZA = 7200        # 2 sata neaktivnosti -> soba/zadatak/oznaka istice
CISTI_SVAKIH = 600      # interval čišćenja (sekunde)
SNAPSHOT_SVAKIH = 30    # interval čuvanja na disk (sekunde)
SNAPSHOT_FILE = os.environ.get("RELAY_SNAPSHOT", "relay_state.json")

# sobe:   {kod: {ucenik_id: {podaci..., "vrijeme": ts}}}
# zadaci: {(kod, ucenik_id_ili_None): {"tekst":.., "tip":.., "vrijeme": ts}}
# oznake: {(kod, ucenik_id): {"lista": [...], "vrijeme": ts}}
sobe = {}
zadaci = {}
oznake = {}
lock = threading.Lock()


def _kljuc_zadatka(kod, ucenik_id=None):
    return f"{kod}|{ucenik_id or ''}"


def _snapshot_ucitaj():
    if not os.path.exists(SNAPSHOT_FILE):
        return
    try:
        with open(SNAPSHOT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        with lock:
            sobe.update(data.get("sobe", {}))
            zadaci.update(data.get("zadaci", {}))
            oznake.update(data.get("oznake", {}))
        print(f"Učitano stanje iz {SNAPSHOT_FILE}")
    except Exception as e:
        print(f"Nije moguće učitati snapshot: {e}")


def _snapshot_sacuvaj():
    with lock:
        data = {"sobe": sobe, "zadaci": zadaci, "oznake": oznake}
    tmp = SNAPSHOT_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, SNAPSHOT_FILE)
    except Exception as e:
        print(f"Nije moguće sačuvati snapshot: {e}")


class RelayHandler(BaseHTTPRequestHandler):
    def log_message(self, f, *a):
        pass

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # klijent je otisao prije nego smo stigli odgovoriti

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)
        kod = (params.get("kod", [""])[0] or "").strip().upper()

        if path == "/ucenik_lista":
            if not kod:
                self._json({"greska": "Nedostaje kod"}, 400)
                return
            with lock:
                soba = sobe.get(kod, {})
                aktivni = {uid: u for uid, u in soba.items()
                           if time.time() - u.get("vrijeme", 0) < ISTICE_ZA}
            self._json({"ucenici": aktivni})

        elif path == "/zadatak":
            if not kod:
                self._json({"tekst": "", "tip": "tekst"})
                return
            with lock:
                z = zadaci.get(_kljuc_zadatka(kod), {"tekst": "", "tip": "tekst"})
            self._json({"tekst": z.get("tekst", ""), "tip": z.get("tip", "tekst")})

        elif path == "/ping":
            self._json({"status": "ok", "relay": "Bookify Relay v3.0"})

        elif path == "/status":
            with lock:
                ukupno = sum(len(v) for v in sobe.values())
            self._json({"spojeni": ukupno})

        else:
            self._json({"greska": "Not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            sirovo = self.rfile.read(length).decode("utf-8") if length else "{}"
            data = json.loads(sirovo)
        except Exception:
            self._json({"greska": "Neispravan JSON"}, 400)
            return

        path = urlparse(self.path).path

        # Ucenik salje podatke: POST /update
        if path == "/update":
            kod = str(data.get("classroom_kod", "")).strip().upper()
            ucenik_id = str(data.get("ucenik_id", "")).strip()
            if not kod or not ucenik_id:
                self._json({"greska": "Nedostaje kod ili ucenik_id"}, 400)
                return
            with lock:
                sobe.setdefault(kod, {})[ucenik_id] = {
                    "ime":           data.get("ime", "Nepoznat"),
                    "razred":        data.get("razred", ""),
                    "promet_dug":    data.get("promet_dug", 0),
                    "promet_pot":    data.get("promet_pot", 0),
                    "zavrsio":       data.get("zavrsio", False),
                    "broj_gresaka":  data.get("broj_gresaka", 0),
                    "zadnji_update": data.get("zadnji_update", ""),
                    "state":         data.get("state", {}),
                    "ip":            ucenik_id,
                    "vrijeme":       time.time(),
                }
                # Individualni zadatak ima prednost nad globalnim
                z = zadaci.get(_kljuc_zadatka(kod, ucenik_id)) or \
                    zadaci.get(_kljuc_zadatka(kod)) or \
                    {"tekst": "", "tip": "tekst"}
                zadatak = {"tekst": z.get("tekst", ""), "tip": z.get("tip", "tekst")}
                o = oznake.get(f"{kod}|{ucenik_id}")
                lista_oznaka = o.get("lista", []) if o else []
            self._json({"status": "ok", "zadatak": zadatak, "oznake": lista_oznaka})

        # Profesor salje zadatak: POST /posalji_zadatak
        elif path == "/posalji_zadatak":
            kod = str(data.get("classroom_kod", "")).strip().upper()
            tekst = data.get("tekst", "")
            tip = data.get("tip", "tekst")
            ucenik_id = str(data.get("ucenik_id", "")).strip()
            if not kod:
                self._json({"greska": "Nedostaje kod"}, 400)
                return
            with lock:
                kljuc = _kljuc_zadatka(kod, ucenik_id if ucenik_id else None)
                if tekst:
                    zadaci[kljuc] = {"tekst": tekst, "tip": tip, "vrijeme": time.time()}
                else:
                    zadaci.pop(kljuc, None)  # Obriši — globalni (ako postoji) dobija prednost
            self._json({"status": "ok"})

        # Profesor salje oznake: POST /posalji_oznake
        elif path == "/posalji_oznake":
            kod = str(data.get("classroom_kod", "")).strip().upper()
            ucenik_id = str(data.get("ucenik_id", "")).strip()
            lista = data.get("oznake", [])
            if not kod or not ucenik_id:
                self._json({"greska": "Nedostaje kod ili ucenik_id"}, 400)
                return
            with lock:
                oznake[f"{kod}|{ucenik_id}"] = {"lista": lista, "vrijeme": time.time()}
            self._json({"status": "ok"})

        else:
            self._json({"greska": "Not found"}, 404)


def _cisti():
    while True:
        time.sleep(CISTI_SVAKIH)
        sada = time.time()
        with lock:
            # Neaktivni ucenici
            for kod in list(sobe.keys()):
                for uid in list(sobe[kod].keys()):
                    if sada - sobe[kod][uid].get("vrijeme", 0) > ISTICE_ZA:
                        del sobe[kod][uid]
                if not sobe[kod]:
                    del sobe[kod]

            # Istekli zadaci
            for kljuc in list(zadaci.keys()):
                if sada - zadaci[kljuc].get("vrijeme", 0) > ISTICE_ZA:
                    del zadaci[kljuc]

            # Istekle oznake
            for kljuc in list(oznake.keys()):
                if sada - oznake[kljuc].get("vrijeme", 0) > ISTICE_ZA:
                    del oznake[kljuc]


def _snapshotuj_periodicno():
    while True:
        time.sleep(SNAPSHOT_SVAKIH)
        _snapshot_sacuvaj()


def _na_gasenje(signum, frame):
    # Render i slicni hosting servisi salju SIGTERM (ne SIGINT) pri gasenju/redeployu,
    # pa moramo sacuvati snapshot i tu, ne samo na KeyboardInterrupt.
    _snapshot_sacuvaj()
    sys.exit(0)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    _snapshot_ucitaj()
    signal.signal(signal.SIGTERM, _na_gasenje)
    threading.Thread(target=_cisti, daemon=True).start()
    threading.Thread(target=_snapshotuj_periodicno, daemon=True).start()
    print(f"Bookify Relay v3.0 pokrenut na portu {port}")
    try:
        ThreadingHTTPServer(("0.0.0.0", port), RelayHandler).serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _snapshot_sacuvaj()
