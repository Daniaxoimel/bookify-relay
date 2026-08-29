# -*- coding: utf-8 -*-
"""
Bookify Relay Server v3.1
Ucenik salje podatke na relay, profesor cita sa relaya.

Promjene u odnosu na v3.0:
- Dodata TRAJNA baza (SQLite, relay_podaci.db) za formativno praćenje:
  radovi/rezultati učenika se čuvaju zauvijek (ne ističu kao ostalo stanje).
  Novi endpointi: POST /sacuvaj_trajno, GET /istorija, GET /istorija_detalji,
  GET /statistika.

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
import json, time, os, threading, signal, sys, sqlite3
from datetime import datetime

ISTICE_ZA = 7200        # 2 sata neaktivnosti -> soba/zadatak/oznaka istice
CISTI_SVAKIH = 600      # interval čišćenja (sekunde)
SNAPSHOT_SVAKIH = 30    # interval čuvanja na disk (sekunde)
SNAPSHOT_FILE = os.environ.get("RELAY_SNAPSHOT", "relay_state.json")

# Trajna baza (radovi/rezultati učenika) — odvojena od gornjeg efemernog stanja.
# Isti disk kao i SNAPSHOT_FILE (npr. Render persistent disk), pa preživljava restart.
DB_FILE = os.environ.get("RELAY_DB", "relay_podaci.db")

# sobe:    {kod: {ucenik_id: {podaci..., "vrijeme": ts}}}
# zadaci:  {(kod, ucenik_id_ili_None): {"tekst":.., "tip":.., "vrijeme": ts}}
# oznake:  {(kod, ucenik_id): {"lista": [...], "vrijeme": ts}}  — profesor -> ucenik (greske)
# signali: {kod: {ucenik_id: [ {rb_bloka, konto, opis, vrijeme}, ... ]}}  — ucenik -> profesor ("nisam siguran")
sobe = {}
zadaci = {}
oznake = {}
signali = {}
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
            signali.update(data.get("signali", {}))
        print(f"Učitano stanje iz {SNAPSHOT_FILE}")
    except Exception as e:
        print(f"Nije moguće učitati snapshot: {e}")


def _snapshot_sacuvaj():
    with lock:
        data = {"sobe": sobe, "zadaci": zadaci, "oznake": oznake, "signali": signali}
    tmp = SNAPSHOT_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, SNAPSHOT_FILE)
    except Exception as e:
        print(f"Nije moguće sačuvati snapshot: {e}")


# ─────────────────────────────────────────────────────────────────────────
# TRAJNA BAZA — radovi/rezultati učenika (za formativno praćenje)
# ─────────────────────────────────────────────────────────────────────────
# Odvojeno od efemernog "sobe/zadaci/oznake" stanja iznad, koje ističe nakon
# ISTICE_ZA sekundi neaktivnosti. Ovo je trajni zapis: svaki put kad učenik
# klikne "Sačuvaj rad" (ili kad profesor eksplicitno snimi njegov rad), radi
# se INSERT reda ovdje — ništa se ne briše niti prepisuje, pa se kroz vrijeme
# gradi istorija za praćenje napretka.

_db_lock = threading.Lock()


def _db_konekcija():
    konn = sqlite3.connect(DB_FILE, timeout=10)
    konn.execute("PRAGMA journal_mode=WAL")  # bolje podnosi paralelne upise
    return konn


def _db_init():
    with _db_lock, _db_konekcija() as konn:
        konn.execute("""
            CREATE TABLE IF NOT EXISTS radovi (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                kod           TEXT NOT NULL,
                ucenik_ime    TEXT NOT NULL,
                razred        TEXT,
                vrijeme       TEXT NOT NULL,
                promet_dug    REAL DEFAULT 0,
                promet_pot    REAL DEFAULT 0,
                broj_gresaka  INTEGER DEFAULT 0,
                zavrsio       INTEGER DEFAULT 0,
                podaci        TEXT NOT NULL
            )
        """)
        konn.execute("""
            CREATE INDEX IF NOT EXISTS idx_radovi_kod_ucenik
            ON radovi (kod, ucenik_ime)
        """)


def _db_sacuvaj_rad(kod, ucenik_ime, razred, promet_dug, promet_pot,
                     broj_gresaka, zavrsio, podaci_dict):
    vrijeme = datetime.now().isoformat(timespec="seconds")
    podaci_json = json.dumps(podaci_dict, ensure_ascii=False)
    with _db_lock, _db_konekcija() as konn:
        cur = konn.execute("""
            INSERT INTO radovi (kod, ucenik_ime, razred, vrijeme, promet_dug,
                                 promet_pot, broj_gresaka, zavrsio, podaci)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (kod, ucenik_ime, razred, vrijeme, promet_dug or 0, promet_pot or 0,
              broj_gresaka or 0, 1 if zavrsio else 0, podaci_json))
        return cur.lastrowid


def _db_istorija(kod, ucenik_ime=None):
    with _db_lock, _db_konekcija() as konn:
        konn.row_factory = sqlite3.Row
        if ucenik_ime:
            redovi = konn.execute("""
                SELECT id, kod, ucenik_ime, razred, vrijeme, promet_dug,
                       promet_pot, broj_gresaka, zavrsio
                FROM radovi WHERE kod = ? AND ucenik_ime = ?
                ORDER BY vrijeme DESC
            """, (kod, ucenik_ime)).fetchall()
        else:
            redovi = konn.execute("""
                SELECT id, kod, ucenik_ime, razred, vrijeme, promet_dug,
                       promet_pot, broj_gresaka, zavrsio
                FROM radovi WHERE kod = ?
                ORDER BY vrijeme DESC
            """, (kod,)).fetchall()
        return [dict(r) for r in redovi]


def _db_detalji(rad_id):
    with _db_lock, _db_konekcija() as konn:
        konn.row_factory = sqlite3.Row
        red = konn.execute("SELECT * FROM radovi WHERE id = ?", (rad_id,)).fetchone()
        if not red:
            return None
        d = dict(red)
        try:
            d["podaci"] = json.loads(d["podaci"])
        except Exception:
            pass
        return d


def _db_statistika(kod):
    with _db_lock, _db_konekcija() as konn:
        konn.row_factory = sqlite3.Row
        ukupno = konn.execute(
            "SELECT COUNT(*) AS n FROM radovi WHERE kod = ?", (kod,)
        ).fetchone()["n"]
        po_ucenika = konn.execute("""
            SELECT ucenik_ime,
                   COUNT(*)              AS broj_radova,
                   AVG(broj_gresaka)     AS prosjek_gresaka,
                   MAX(vrijeme)          AS poslednji_put,
                   SUM(zavrsio)          AS broj_zavrsenih
            FROM radovi WHERE kod = ?
            GROUP BY ucenik_ime
            ORDER BY poslednji_put DESC
        """, (kod,)).fetchall()
        return {
            "kod": kod,
            "ukupno_radova": ukupno,
            "po_ucenika": [dict(r) for r in po_ucenika],
        }


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
                aktivni = {}
                for uid, u in soba.items():
                    if time.time() - u.get("vrijeme", 0) < ISTICE_ZA:
                        u2 = dict(u)
                        u2["signali"] = signali.get(kod, {}).get(uid, [])
                        aktivni[uid] = u2
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

        # Trajna istorija radova za jedan kod učionice (svi učenici, ili
        # samo jedan ako je zadan ucenik_ime) — za formativno praćenje.
        elif path == "/istorija":
            if not kod:
                self._json({"greska": "Nedostaje kod"}, 400)
                return
            ucenik_ime = (params.get("ucenik_ime", [""])[0] or "").strip()
            try:
                redovi = _db_istorija(kod, ucenik_ime or None)
                self._json({"radovi": redovi})
            except Exception as e:
                self._json({"greska": f"Greška baze: {e}"}, 500)

        # Puni sadržaj jednog sačuvanog rada (za pregled/učitavanje kod profesora)
        elif path == "/istorija_detalji":
            rad_id = params.get("id", [""])[0]
            if not rad_id:
                self._json({"greska": "Nedostaje id"}, 400)
                return
            try:
                detalji = _db_detalji(int(rad_id))
                if detalji is None:
                    self._json({"greska": "Rad nije pronađen"}, 404)
                else:
                    self._json({"rad": detalji})
            except Exception as e:
                self._json({"greska": f"Greška baze: {e}"}, 500)

        # Agregirana statistika po učionici (broj radova, prosjek grešaka po
        # učeniku, itd.) — za formativno praćenje napretka kroz vrijeme.
        elif path == "/statistika":
            if not kod:
                self._json({"greska": "Nedostaje kod"}, 400)
                return
            try:
                self._json(_db_statistika(kod))
            except Exception as e:
                self._json({"greska": f"Greška baze: {e}"}, 500)

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

        # Ucenik salje signal profesoru ("nisam siguran u ovaj red"): POST /posalji_signal
        elif path == "/posalji_signal":
            kod = str(data.get("classroom_kod", "")).strip().upper()
            ucenik_id = str(data.get("ucenik_id", "")).strip()
            if not kod or not ucenik_id:
                self._json({"greska": "Nedostaje kod ili ucenik_id"}, 400)
                return
            unos = {
                "rb_bloka": data.get("rb_bloka", ""),
                "konto":    data.get("konto", ""),
                "opis":     data.get("opis", ""),
                "vrijeme":  time.time(),
            }
            with lock:
                signali.setdefault(kod, {}).setdefault(ucenik_id, []).append(unos)
            self._json({"status": "ok"})

        # Ucenik trajno cuva svoj rad (za formativno pracenje): POST /sacuvaj_trajno
        # Za razliku od /update (efemerno, briše se nakon ISTICE_ZA), ovo se
        # NIKAD ne briše — svaki poziv dodaje novi red u istoriju.
        elif path == "/sacuvaj_trajno":
            kod = str(data.get("classroom_kod", "")).strip().upper()
            ucenik_ime = str(data.get("ucenik_ime", "")).strip()
            if not kod or not ucenik_ime:
                self._json({"greska": "Nedostaje kod ili ucenik_ime"}, 400)
                return
            try:
                novi_id = _db_sacuvaj_rad(
                    kod=kod,
                    ucenik_ime=ucenik_ime,
                    razred=data.get("razred", ""),
                    promet_dug=data.get("promet_dug", 0),
                    promet_pot=data.get("promet_pot", 0),
                    broj_gresaka=data.get("broj_gresaka", 0),
                    zavrsio=data.get("zavrsio", False),
                    podaci_dict=data.get("podaci", {}),
                )
                self._json({"status": "ok", "id": novi_id})
            except Exception as e:
                self._json({"greska": f"Greška baze: {e}"}, 500)

        # Profesor potvrdjuje da je pregledao signale ucenika: POST /procitaj_signale
        elif path == "/procitaj_signale":
            kod = str(data.get("classroom_kod", "")).strip().upper()
            ucenik_id = str(data.get("ucenik_id", "")).strip()
            if not kod or not ucenik_id:
                self._json({"greska": "Nedostaje kod ili ucenik_id"}, 400)
                return
            with lock:
                if kod in signali:
                    signali[kod].pop(ucenik_id, None)
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

            # Signali ucenika ciji ucenik vise nije u sobi (odjavio se/istekao)
            for kod in list(signali.keys()):
                for uid in list(signali[kod].keys()):
                    if kod not in sobe or uid not in sobe[kod]:
                        del signali[kod][uid]
                if not signali[kod]:
                    del signali[kod]


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
    _db_init()
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
