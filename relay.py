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
import json, time, os, threading, signal, sys, sqlite3, hashlib
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
        # Grad/škola/šifra po kodu učionice — za "solo" nastavnike (bez dijeljene
        # škole) i kao poveznica ka instituciji (skolski_kod) za dijeljeni pogled.
        konn.execute("""
            CREATE TABLE IF NOT EXISTS ucionice (
                kod          TEXT PRIMARY KEY,
                grad         TEXT DEFAULT '',
                skola        TEXT DEFAULT '',
                sifra_hash   TEXT DEFAULT '',
                skolski_kod  TEXT DEFAULT '',
                azurirano    TEXT
            )
        """)
        # Institucija (škola) koju dijeli više nastavnika — svi njihovi kodovi
        # učionica koji se pridruže istom skolski_kod-u vide zajedničku istoriju
        # (Grad → Škola → Razred → Učenici) i dijele istu šifru za brisanje.
        konn.execute("""
            CREATE TABLE IF NOT EXISTS institucije (
                skolski_kod  TEXT PRIMARY KEY,
                grad         TEXT NOT NULL,
                skola        TEXT NOT NULL,
                sifra_hash   TEXT NOT NULL,
                kreirano     TEXT
            )
        """)
        # Ručna odluka profesora o statusu "završio" po učeniku (nadjačava
        # ono što učenikova aplikacija sama izračuna). Trajno se pamti po
        # (kod, ucenik_id) i primjenjuje se i na živi prikaz i na trajno
        # sačuvane radove tog učenika.
        konn.execute("""
            CREATE TABLE IF NOT EXISTS rucni_zavrsio (
                kod        TEXT NOT NULL,
                ucenik_id  TEXT NOT NULL,
                zavrsio    INTEGER NOT NULL,
                vrijeme    REAL,
                PRIMARY KEY (kod, ucenik_id)
            )
        """)
        # Klasifikovane greške učenika tokom rada (formativno praćenje) —
        # bez vremena, samo datum, redni broj promjene, oblast i tip. Tip
        # greške je vidljiv SAMO profesoru, nikad učeniku.
        konn.execute("""
            CREATE TABLE IF NOT EXISTS greske_log (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                kod                  TEXT NOT NULL,
                ucenik_id            TEXT NOT NULL,
                ucenik_ime           TEXT DEFAULT '',
                datum                TEXT,
                redni_broj_promjene  TEXT,
                oblast               TEXT,
                tip                  TEXT,
                vrijeme              REAL
            )
        """)
        konn.execute("""
            CREATE INDEX IF NOT EXISTS idx_greske_kod_ucenik
            ON greske_log (kod, ucenik_id)
        """)
        # Migracije za baze napravljene prije uvođenja šifre/institucija.
        for _alter in (
            "ALTER TABLE ucionice ADD COLUMN sifra_hash TEXT DEFAULT ''",
            "ALTER TABLE ucionice ADD COLUMN skolski_kod TEXT DEFAULT ''",
            "ALTER TABLE greske_log ADD COLUMN ucenik_ime TEXT DEFAULT ''",
        ):
            try:
                konn.execute(_alter)
            except Exception:
                pass  # kolona već postoji


def _upisi_hash(sol, sifra):
    return hashlib.sha256(f"{sol}:{sifra or ''}".encode("utf-8")).hexdigest()


def _generisi_skolski_kod(konn):
    import random
    alfabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    while True:
        kandidat = "".join(random.choices(alfabet, k=6))
        if not konn.execute(
                "SELECT 1 FROM institucije WHERE skolski_kod = ?", (kandidat,)).fetchone():
            return kandidat


def _kodovi_institucije(konn, skolski_kod):
    return [r[0] for r in konn.execute(
        "SELECT kod FROM ucionice WHERE skolski_kod = ?", (skolski_kod,)).fetchall()]


def _povezanost_koda(konn, kod):
    """Vraća red iz ucionice za dati kod (ili None)."""
    konn.row_factory = sqlite3.Row
    return konn.execute(
        "SELECT grad, skola, sifra_hash, skolski_kod FROM ucionice WHERE kod = ?", (kod,)
    ).fetchone()


def _kodovi_za_upit(konn, kod):
    """Skup kodova preko kojih treba tražiti radove za dati kod — ako je kod
    dio dijeljene institucije, to su SVI kodovi te institucije; inače samo taj
    jedan (solo način rada, kao i prije)."""
    red = _povezanost_koda(konn, kod)
    skolski_kod = (red["skolski_kod"] if red else "") or ""
    if skolski_kod:
        kodovi = _kodovi_institucije(konn, skolski_kod)
        return kodovi or [kod], skolski_kod
    return [kod], ""


def _provjeri_sifru(kod, sifra):
    """Vraća True samo ako je šifra tačna — protiv institucije (dijeljena škola)
    ako je kod pridružen jednoj, inače protiv šifre samog koda (solo način).
    Ako šifra još nije postavljena nigdje, brisanje se odbija."""
    with _db_lock, _db_konekcija() as konn:
        red = _povezanost_koda(konn, kod)
        if not red:
            return False
        skolski_kod = (red["skolski_kod"] or "")
        if skolski_kod:
            konn.row_factory = sqlite3.Row
            inst = konn.execute(
                "SELECT sifra_hash FROM institucije WHERE skolski_kod = ?", (skolski_kod,)
            ).fetchone()
            sacuvani = (inst["sifra_hash"] if inst else "") or ""
            if not sacuvani:
                return False
            return _upisi_hash(skolski_kod, sifra) == sacuvani
        sacuvani = red["sifra_hash"] or ""
        if not sacuvani:
            return False
        return _upisi_hash(kod, sifra) == sacuvani


def _db_postavi_skolu(kod, grad=None, skola=None, sifra_hash=None):
    """Djelimičan upsert nad ucionice (solo način) — samo polja koja nisu None
    se mijenjaju. Ne dira institucije (dijeljenu školu), samo lokalnu etiketu
    za kodove koji NISU pridruženi nijednoj instituciji."""
    vrijeme = datetime.now().isoformat(timespec="seconds")
    with _db_lock, _db_konekcija() as konn:
        konn.row_factory = sqlite3.Row
        red = konn.execute(
            "SELECT grad, skola, sifra_hash, skolski_kod FROM ucionice WHERE kod = ?", (kod,)
        ).fetchone()
        novi_grad  = grad  if grad  is not None else (red["grad"]       if red else "")
        novi_skola = skola if skola is not None else (red["skola"]      if red else "")
        novi_sifra = sifra_hash if sifra_hash is not None else (red["sifra_hash"] if red else "")
        skolski_kod = (red["skolski_kod"] if red else "") or ""
        konn.execute("""
            INSERT INTO ucionice (kod, grad, skola, sifra_hash, skolski_kod, azurirano)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(kod) DO UPDATE SET grad=excluded.grad, skola=excluded.skola,
                                            sifra_hash=excluded.sifra_hash,
                                            azurirano=excluded.azurirano
        """, (kod, novi_grad or "", novi_skola or "", novi_sifra or "", skolski_kod, vrijeme))


def _db_napravi_instituciju(kod, grad, skola, sifra):
    """Pravi novu dijeljenu školu (instituciju) i odmah joj pridružuje trenutni
    kod učionice. Vraća novogenerisani skolski_kod (daje se kolegama da se
    pridruže)."""
    vrijeme = datetime.now().isoformat(timespec="seconds")
    with _db_lock, _db_konekcija() as konn:
        skolski_kod = _generisi_skolski_kod(konn)
        konn.execute("""
            INSERT INTO institucije (skolski_kod, grad, skola, sifra_hash, kreirano)
            VALUES (?, ?, ?, ?, ?)
        """, (skolski_kod, grad, skola, _upisi_hash(skolski_kod, sifra), vrijeme))
        konn.execute("""
            INSERT INTO ucionice (kod, grad, skola, sifra_hash, skolski_kod, azurirano)
            VALUES (?, ?, ?, '', ?, ?)
            ON CONFLICT(kod) DO UPDATE SET grad=excluded.grad, skola=excluded.skola,
                                            skolski_kod=excluded.skolski_kod,
                                            azurirano=excluded.azurirano
        """, (kod, grad, skola, skolski_kod, vrijeme))
    return skolski_kod


def _db_pridruzi_skoli(kod, skolski_kod, sifra):
    """Pridružuje kod učionice postojećoj instituciji ako je šifra ispravna.
    Vraća (True, grad, skola) ili (False, None, None) ako kod/šifra ne valjaju."""
    with _db_lock, _db_konekcija() as konn:
        konn.row_factory = sqlite3.Row
        inst = konn.execute(
            "SELECT grad, skola, sifra_hash FROM institucije WHERE skolski_kod = ?",
            (skolski_kod,)).fetchone()
        if not inst or _upisi_hash(skolski_kod, sifra) != inst["sifra_hash"]:
            return False, None, None
        vrijeme = datetime.now().isoformat(timespec="seconds")
        konn.execute("""
            INSERT INTO ucionice (kod, grad, skola, sifra_hash, skolski_kod, azurirano)
            VALUES (?, ?, ?, '', ?, ?)
            ON CONFLICT(kod) DO UPDATE SET grad=excluded.grad, skola=excluded.skola,
                                            skolski_kod=excluded.skolski_kod,
                                            azurirano=excluded.azurirano
        """, (kod, inst["grad"], inst["skola"], skolski_kod, vrijeme))
        return True, inst["grad"], inst["skola"]


def _db_napusti_skolu(kod):
    """Vraća kod učionice u solo način (bez dijeljene institucije)."""
    with _db_lock, _db_konekcija() as konn:
        konn.execute("UPDATE ucionice SET skolski_kod = '' WHERE kod = ?", (kod,))


def _db_obrisi_rad(rad_id, kod):
    with _db_lock, _db_konekcija() as konn:
        kodovi, _ = _kodovi_za_upit(konn, kod)
        upitnici = ",".join("?" * len(kodovi))
        cur = konn.execute(
            f"DELETE FROM radovi WHERE id = ? AND kod IN ({upitnici})", [rad_id] + kodovi)
        return cur.rowcount


def _db_obrisi_ucenika(kod, ucenik_ime):
    with _db_lock, _db_konekcija() as konn:
        kodovi, _ = _kodovi_za_upit(konn, kod)
        upitnici = ",".join("?" * len(kodovi))
        cur = konn.execute(
            f"DELETE FROM radovi WHERE kod IN ({upitnici}) AND ucenik_ime = ?",
            kodovi + [ucenik_ime])
        return cur.rowcount


def _db_sacuvaj_rad(kod, ucenik_ime, razred, promet_dug, promet_pot,
                     broj_gresaka, zavrsio, podaci_dict, ucenik_id=None):
    vrijeme = datetime.now().isoformat(timespec="seconds")
    podaci_json = json.dumps(podaci_dict, ensure_ascii=False)
    with _db_lock, _db_konekcija() as konn:
        # Ako profesor ima ručno postavljen status za ovog učenika, on ima
        # prednost nad onim što učenikova aplikacija sama izračuna.
        if ucenik_id:
            red = konn.execute(
                "SELECT zavrsio FROM rucni_zavrsio WHERE kod = ? AND ucenik_id = ?",
                (kod, ucenik_id)).fetchone()
            if red is not None:
                zavrsio = bool(red[0])
        cur = konn.execute("""
            INSERT INTO radovi (kod, ucenik_ime, razred, vrijeme, promet_dug,
                                 promet_pot, broj_gresaka, zavrsio, podaci)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (kod, ucenik_ime, razred, vrijeme, promet_dug or 0, promet_pot or 0,
              broj_gresaka or 0, 1 if zavrsio else 0, podaci_json))
        return cur.lastrowid


def _db_postavi_rucni_zavrsio(kod, ucenik_id, zavrsio):
    """Profesor ručno postavlja da li je učenik završio rad ili ne."""
    with _db_lock, _db_konekcija() as konn:
        konn.execute("""
            INSERT INTO rucni_zavrsio (kod, ucenik_id, zavrsio, vrijeme)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(kod, ucenik_id) DO UPDATE SET
                zavrsio = excluded.zavrsio, vrijeme = excluded.vrijeme
        """, (kod, ucenik_id, 1 if zavrsio else 0, time.time()))


def _db_obrisi_rucni_zavrsio(kod, ucenik_id):
    """Vrati status na automatski (ukloni profesorovo ručno postavljanje)."""
    with _db_lock, _db_konekcija() as konn:
        konn.execute(
            "DELETE FROM rucni_zavrsio WHERE kod = ? AND ucenik_id = ?",
            (kod, ucenik_id))


def _db_svi_rucni_zavrsio(kod):
    """Svi ručno postavljeni statusi za jedan kod učionice: {ucenik_id: bool}."""
    with _db_lock, _db_konekcija() as konn:
        redovi = konn.execute(
            "SELECT ucenik_id, zavrsio FROM rucni_zavrsio WHERE kod = ?",
            (kod,)).fetchall()
        return {r[0]: bool(r[1]) for r in redovi}


def _db_azuriraj_zadnji_rad_zavrsio(kod, ucenik_ime, zavrsio):
    """Kad profesor ručno postavi status, ažuriraj i POSLJEDNJI već sačuvani
    rad tog učenika u istoriji (da se ne vidi zastarjeli 'ne' u istoriji dok
    kartica pokazuje 'da')."""
    with _db_lock, _db_konekcija() as konn:
        red = konn.execute("""
            SELECT id FROM radovi WHERE kod = ? AND ucenik_ime = ?
            ORDER BY id DESC LIMIT 1
        """, (kod, ucenik_ime)).fetchone()
        if red:
            konn.execute("UPDATE radovi SET zavrsio = ? WHERE id = ?",
                        (1 if zavrsio else 0, red[0]))


def _db_prijavi_gresku(kod, ucenik_id, datum, redni_broj_promjene, oblast, tip, ucenik_ime=""):
    """Ucenikova aplikacija prijavljuje klasifikovanu grešku — bez vremena
    tačnog trenutka, samo datum rada koji šalje učenik."""
    with _db_lock, _db_konekcija() as konn:
        konn.execute("""
            INSERT INTO greske_log (kod, ucenik_id, ucenik_ime, datum, redni_broj_promjene,
                                     oblast, tip, vrijeme)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (kod, ucenik_id, ucenik_ime or "", datum or "", str(redni_broj_promjene or ""),
              oblast or "", tip or "ostalo", time.time()))


def _db_greske(kod, ucenik_id=None, ucenik_ime=None):
    """Klasifikovane greške za profesora — po jednom učeniku (preko ucenik_id
    ili ucenik_ime) ili cijelom kodu učionice, najnovije prve."""
    with _db_lock, _db_konekcija() as konn:
        if ucenik_id:
            redovi = konn.execute("""
                SELECT datum, redni_broj_promjene, oblast, tip
                FROM greske_log WHERE kod = ? AND ucenik_id = ?
                ORDER BY id DESC
            """, (kod, ucenik_id)).fetchall()
            return [{"datum": r[0], "redni_broj_promjene": r[1],
                     "oblast": r[2], "tip": r[3]} for r in redovi]
        if ucenik_ime:
            redovi = konn.execute("""
                SELECT datum, redni_broj_promjene, oblast, tip
                FROM greske_log WHERE kod = ? AND ucenik_ime = ?
                ORDER BY id DESC
            """, (kod, ucenik_ime)).fetchall()
            return [{"datum": r[0], "redni_broj_promjene": r[1],
                     "oblast": r[2], "tip": r[3]} for r in redovi]
        redovi = konn.execute("""
            SELECT ucenik_id, datum, redni_broj_promjene, oblast, tip
            FROM greske_log WHERE kod = ?
            ORDER BY id DESC
        """, (kod,)).fetchall()
        return [{"ucenik_id": r[0], "datum": r[1], "redni_broj_promjene": r[2],
                 "oblast": r[3], "tip": r[4]} for r in redovi]


def _db_istorija(kod, ucenik_ime=None):
    with _db_lock, _db_konekcija() as konn:
        konn.row_factory = sqlite3.Row
        kodovi, _ = _kodovi_za_upit(konn, kod)
        upitnici = ",".join("?" * len(kodovi))
        if ucenik_ime:
            redovi = konn.execute(f"""
                SELECT id, kod, ucenik_ime, razred, vrijeme, promet_dug,
                       promet_pot, broj_gresaka, zavrsio
                FROM radovi WHERE kod IN ({upitnici}) AND ucenik_ime = ?
                ORDER BY vrijeme DESC
            """, kodovi + [ucenik_ime]).fetchall()
        else:
            redovi = konn.execute(f"""
                SELECT id, kod, ucenik_ime, razred, vrijeme, promet_dug,
                       promet_pot, broj_gresaka, zavrsio
                FROM radovi WHERE kod IN ({upitnici})
                ORDER BY vrijeme DESC
            """, kodovi).fetchall()
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
        red = _povezanost_koda(konn, kod)
        skolski_kod = (red["skolski_kod"] if red else "") or ""

        if skolski_kod:
            inst = konn.execute(
                "SELECT grad, skola, sifra_hash FROM institucije WHERE skolski_kod = ?",
                (skolski_kod,)).fetchone()
            grad  = inst["grad"]  if inst else (red["grad"] if red else "")
            skola = inst["skola"] if inst else (red["skola"] if red else "")
            sifra_postavljena = bool(inst["sifra_hash"]) if inst else False
            kodovi = _kodovi_institucije(konn, skolski_kod) or [kod]
            broj_nastavnika = len(kodovi)
        else:
            grad  = red["grad"]  if red else ""
            skola = red["skola"] if red else ""
            sifra_postavljena = bool(red["sifra_hash"]) if red else False
            kodovi = [kod]
            broj_nastavnika = 1

        upitnici = ",".join("?" * len(kodovi))
        ukupno = konn.execute(
            f"SELECT COUNT(*) AS n FROM radovi WHERE kod IN ({upitnici})", kodovi
        ).fetchone()["n"]
        redovi = konn.execute(f"""
            SELECT COALESCE(NULLIF(TRIM(razred), ''), '(bez razreda)') AS razred,
                   ucenik_ime,
                   COUNT(*)              AS broj_radova,
                   AVG(broj_gresaka)     AS prosjek_gresaka,
                   MAX(vrijeme)          AS poslednji_put,
                   SUM(zavrsio)          AS broj_zavrsenih
            FROM radovi WHERE kod IN ({upitnici})
            GROUP BY razred, ucenik_ime
            ORDER BY razred ASC, poslednji_put DESC
        """, kodovi).fetchall()
        po_razredu_map = {}
        redoslijed = []
        for r in redovi:
            rz = r["razred"]
            if rz not in po_razredu_map:
                po_razredu_map[rz] = []
                redoslijed.append(rz)
            po_razredu_map[rz].append(dict(r))
        return {
            "kod": kod,
            "grad": grad,
            "skola": skola,
            "skolski_kod": skolski_kod,
            "nacin": "skola" if skolski_kod else "solo",
            "broj_nastavnika": broj_nastavnika,
            "sifra_postavljena": sifra_postavljena,
            "ukupno_radova": ukupno,
            "po_razredu": [{"razred": rz, "ucenici": po_razredu_map[rz]} for rz in redoslijed],
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
            # Ručna odluka profesora o "završio" ima prednost nad onim što
            # učenikova aplikacija sama izračuna.
            try:
                rucni = _db_svi_rucni_zavrsio(kod)
            except Exception:
                rucni = {}
            for uid, vrijednost in rucni.items():
                if uid in aktivni:
                    aktivni[uid]["zavrsio"] = vrijednost
                    aktivni[uid]["zavrsio_rucno"] = True
            self._json({"ucenici": aktivni})

        elif path == "/zadatak":
            if not kod:
                self._json({"tekst": "", "tip": "tekst"})
                return
            with lock:
                z = zadaci.get(_kljuc_zadatka(kod), {"tekst": "", "tip": "tekst"})
            self._json({"tekst": z.get("tekst", ""), "tip": z.get("tip", "tekst"),
                        "oblast": z.get("oblast", ""), "broj_promjena": z.get("broj_promjena")})

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

        # Klasifikovane greške po učeniku (formativno praćenje, samo za
        # profesora — tip greške se NIKAD ne šalje/prikazuje učeniku).
        elif path == "/greske":
            if not kod:
                self._json({"greska": "Nedostaje kod"}, 400)
                return
            ucenik_id = (params.get("ucenik_id", [""])[0] or "").strip()
            ucenik_ime = (params.get("ucenik_ime", [""])[0] or "").strip()
            try:
                self._json({"greske": _db_greske(kod, ucenik_id or None, ucenik_ime or None)})
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
                    "oblast":        data.get("oblast", ""),
                    "planirani_broj_promjena": data.get("planirani_broj_promjena"),
                    "preskocene_promjene": data.get("preskocene_promjene", []),
                    "zadnji_update": data.get("zadnji_update", ""),
                    "state":         data.get("state", {}),
                    "ip":            ucenik_id,
                    "vrijeme":       time.time(),
                }
                # Individualni zadatak ima prednost nad globalnim
                z = zadaci.get(_kljuc_zadatka(kod, ucenik_id)) or \
                    zadaci.get(_kljuc_zadatka(kod)) or \
                    {"tekst": "", "tip": "tekst"}
                zadatak = {"tekst": z.get("tekst", ""), "tip": z.get("tip", "tekst"),
                           "oblast": z.get("oblast", ""), "broj_promjena": z.get("broj_promjena")}
                o = oznake.get(f"{kod}|{ucenik_id}")
                lista_oznaka = o.get("lista", []) if o else []
            self._json({"status": "ok", "zadatak": zadatak, "oznake": lista_oznaka})

        # Profesor salje zadatak: POST /posalji_zadatak
        elif path == "/posalji_zadatak":
            kod = str(data.get("classroom_kod", "")).strip().upper()
            tekst = data.get("tekst", "")
            tip = data.get("tip", "tekst")
            oblast = data.get("oblast", "")
            broj_promjena = data.get("broj_promjena")
            ucenik_id = str(data.get("ucenik_id", "")).strip()
            if not kod:
                self._json({"greska": "Nedostaje kod"}, 400)
                return
            with lock:
                kljuc = _kljuc_zadatka(kod, ucenik_id if ucenik_id else None)
                if tekst:
                    zadaci[kljuc] = {"tekst": tekst, "tip": tip, "oblast": oblast,
                                      "broj_promjena": broj_promjena, "vrijeme": time.time()}
                else:
                    zadaci.pop(kljuc, None)  # Obriši — globalni (ako postoji) dobija prednost
            self._json({"status": "ok"})

        # Ucenik prijavljuje klasifikovanu grešku (formativno praćenje):
        # POST /prijavi_gresku — bez vremena, samo datum + redni broj
        # promjene + oblast + tip. Tip se NIKAD ne vraća/prikazuje učeniku.
        elif path == "/prijavi_gresku":
            kod = str(data.get("classroom_kod", "")).strip().upper()
            ucenik_id = str(data.get("ucenik_id", "")).strip()
            if not kod or not ucenik_id:
                self._json({"greska": "Nedostaje kod ili ucenik_id"}, 400)
                return
            try:
                _db_prijavi_gresku(
                    kod=kod, ucenik_id=ucenik_id,
                    ucenik_ime=str(data.get("ucenik_ime", "")).strip(),
                    datum=data.get("datum", ""),
                    redni_broj_promjene=data.get("redni_broj_promjene", ""),
                    oblast=data.get("oblast", ""),
                    tip=data.get("tip", "ostalo"))
                self._json({"status": "ok"})
            except Exception as e:
                self._json({"greska": f"Greška baze: {e}"}, 500)

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

        # Profesor ručno postavlja da li je učenik završio rad: POST /postavi_zavrsio
        # {classroom_kod, ucenik_id, zavrsio} — ako je "zavrsio" None/izostavljen,
        # ukida se ručno postavljanje i status se vraća na automatski.
        elif path == "/postavi_zavrsio":
            kod = str(data.get("classroom_kod", "")).strip().upper()
            ucenik_id = str(data.get("ucenik_id", "")).strip()
            ucenik_ime = str(data.get("ucenik_ime", "")).strip()
            if not kod or not ucenik_id:
                self._json({"greska": "Nedostaje kod ili ucenik_id"}, 400)
                return
            try:
                if "zavrsio" in data and data.get("zavrsio") is not None:
                    zavrsio_novi = bool(data.get("zavrsio"))
                    _db_postavi_rucni_zavrsio(kod, ucenik_id, zavrsio_novi)
                    # Ažuriraj i posljednji već sačuvani rad ovog učenika u
                    # istoriji, da se ručna odluka odmah vidi i tamo — ne
                    # samo na živoj kartici.
                    if ucenik_ime:
                        _db_azuriraj_zadnji_rad_zavrsio(kod, ucenik_ime, zavrsio_novi)
                else:
                    _db_obrisi_rucni_zavrsio(kod, ucenik_id)
                with lock:
                    u = sobe.get(kod, {}).get(ucenik_id)
                    if u is not None and "zavrsio" in data and data.get("zavrsio") is not None:
                        u["zavrsio"] = bool(data.get("zavrsio"))
                self._json({"status": "ok"})
            except Exception as e:
                self._json({"greska": f"Greška baze: {e}"}, 500)

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
                    ucenik_id=str(data.get("ucenik_id", "")).strip() or None,
                )
                self._json({"status": "ok", "id": novi_id})
            except Exception as e:
                self._json({"greska": f"Greška baze: {e}"}, 500)

        # Profesor postavlja/ažurira grad i školu SAMO kao lokalnu etiketu (solo
        # način, bez dijeljenja s kolegama) — za dijeljenu školu koristi se
        # /napravi_skolu ili /pridruzi_skoli.
        elif path == "/postavi_skolu":
            kod = str(data.get("classroom_kod", "")).strip().upper()
            if not kod:
                self._json({"greska": "Nedostaje kod"}, 400)
                return
            try:
                _db_postavi_skolu(kod, grad=str(data.get("grad", "")).strip(),
                                   skola=str(data.get("skola", "")).strip())
                self._json({"status": "ok"})
            except Exception as e:
                self._json({"greska": f"Greška baze: {e}"}, 500)

        # Profesor postavlja/mijenja šifru za brisanje podataka iz istorije —
        # bez ove šifre niko (pa ni neko ko sazna kod učionice) ne može brisati.
        # (Solo način — kod koji NIJE pridružen dijeljenoj školi.)
        elif path == "/postavi_sifru":
            kod = str(data.get("classroom_kod", "")).strip().upper()
            sifra = str(data.get("sifra", ""))
            if not kod or not sifra:
                self._json({"greska": "Nedostaje kod ili šifra"}, 400)
                return
            try:
                _db_postavi_skolu(kod, sifra_hash=_upisi_hash(kod, sifra))
                self._json({"status": "ok"})
            except Exception as e:
                self._json({"greska": f"Greška baze: {e}"}, 500)

        # Nastavnik pravi NOVU dijeljenu školu (instituciju) — generiše se
        # skolski_kod koji se daje kolegama da se pridruže istoj istoriji.
        elif path == "/napravi_skolu":
            kod   = str(data.get("classroom_kod", "")).strip().upper()
            grad  = str(data.get("grad", "")).strip()
            skola = str(data.get("skola", "")).strip()
            sifra = str(data.get("sifra", ""))
            if not kod or not grad or not skola or not sifra:
                self._json({"greska": "Nedostaje kod, grad, škola ili šifra"}, 400)
                return
            try:
                skolski_kod = _db_napravi_instituciju(kod, grad, skola, sifra)
                self._json({"status": "ok", "skolski_kod": skolski_kod})
            except Exception as e:
                self._json({"greska": f"Greška baze: {e}"}, 500)

        # Nastavnik se pridružuje POSTOJEĆOJ dijeljenoj školi — treba školski_kod
        # i šifru koje mu je dao kolega koji je školu napravio.
        elif path == "/pridruzi_skoli":
            kod         = str(data.get("classroom_kod", "")).strip().upper()
            skolski_kod = str(data.get("skolski_kod", "")).strip().upper()
            sifra       = str(data.get("sifra", ""))
            if not kod or not skolski_kod:
                self._json({"greska": "Nedostaje kod ili školski kod"}, 400)
                return
            try:
                uspjeh, grad, skola = _db_pridruzi_skoli(kod, skolski_kod, sifra)
                if not uspjeh:
                    self._json({"greska": "Školski kod ili šifra nisu ispravni."}, 403)
                    return
                self._json({"status": "ok", "grad": grad, "skola": skola})
            except Exception as e:
                self._json({"greska": f"Greška baze: {e}"}, 500)

        # Nastavnik napušta dijeljenu školu — vraća se u solo način rada.
        elif path == "/napusti_skolu":
            kod = str(data.get("classroom_kod", "")).strip().upper()
            if not kod:
                self._json({"greska": "Nedostaje kod"}, 400)
                return
            try:
                _db_napusti_skolu(kod)
                self._json({"status": "ok"})
            except Exception as e:
                self._json({"greska": f"Greška baze: {e}"}, 500)

        # Brisanje jednog sačuvanog rada — zahtijeva ispravnu šifru učionice.
        elif path == "/obrisi_rad":
            kod = str(data.get("classroom_kod", "")).strip().upper()
            sifra = str(data.get("sifra", ""))
            rad_id = data.get("rad_id")
            if not kod or not rad_id:
                self._json({"greska": "Nedostaje kod ili rad_id"}, 400)
                return
            if not _provjeri_sifru(kod, sifra):
                self._json({"greska": "Pogrešna šifra ili šifra još nije postavljena."}, 403)
                return
            try:
                obrisano = _db_obrisi_rad(int(rad_id), kod)
                self._json({"status": "ok", "obrisano": obrisano})
            except Exception as e:
                self._json({"greska": f"Greška baze: {e}"}, 500)

        # Brisanje svih sačuvanih radova jednog učenika — zahtijeva šifru.
        elif path == "/obrisi_ucenika":
            kod = str(data.get("classroom_kod", "")).strip().upper()
            sifra = str(data.get("sifra", ""))
            ucenik_ime = str(data.get("ucenik_ime", "")).strip()
            if not kod or not ucenik_ime:
                self._json({"greska": "Nedostaje kod ili ucenik_ime"}, 400)
                return
            if not _provjeri_sifru(kod, sifra):
                self._json({"greska": "Pogrešna šifra ili šifra još nije postavljena."}, 403)
                return
            try:
                obrisano = _db_obrisi_ucenika(kod, ucenik_ime)
                self._json({"status": "ok", "obrisano": obrisano})
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
