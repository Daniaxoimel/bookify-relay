# -*- coding: utf-8 -*-
"""
Bookify Server — Profesor aplikacija
Profesor vidi live kompletan Bookify prikaz svakog učenika.
"""

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, filedialog
import threading
import socket
import json
import time
import os
import webbrowser
import tempfile
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

# ══════════════════════════════════════════════════════════════════════════════
# PDF EXPORT — dijeli se s bookify.py (ista implementacija)
# ══════════════════════════════════════════════════════════════════════════════

def _generiši_logičke_redove_prof(podaci):
    """Generiše logičke redove iz podaci dict-a (za PDF export)."""
    sve_stavke = podaci.get("sve_stavke", [])
    grade      = podaci.get("grade", "2. razred")
    ime        = podaci.get("ucenik_ime", "")
    zadatak    = podaci.get("zadatak_tekst", "")
    je_3_4     = grade in ["3. razred", "4. razred"]

    logicki_redovi = []
    current_rb   = ""
    current_date = ""
    is_first     = True

    for s in sve_stavke:
        if s.get("samo_gk"):
            continue
        if s.get("zaglavlje_linija"):
            current_rb   = s.get("rb_bloka", "")
            current_date = s.get("datum", "")
            is_first = True
            continue
        if s.get("konto_display") == "------------------------------":
            logicki_redovi.append({"tip": "separator"})
            continue
        rb_d = current_rb   if is_first else ""
        dt_d = current_date if is_first else ""
        if not rb_d   and is_first: rb_d = s.get("rb_bloka", "")
        if not dt_d   and is_first: dt_d = s.get("datum", "")
        rb_d = str(rb_d) if rb_d != "" else ""
        if rb_d == "PS":    rb_d = "0."
        elif rb_d and not rb_d.endswith("."): rb_d = rb_d + "."
        konto_display = s.get("konto_display", "")
        if je_3_4:
            broj_konta   = ""
            opis_display = konto_display.strip()
            full_text    = s.get("konto_sa_nazivom") or s.get("konto", "") or ""
            if full_text and "-" in full_text:
                parts = full_text.split("-", 1)
                bk    = parts[0].strip()
                opis_display = (parts[1].strip() if len(parts) > 1 else "").strip() or opis_display
                if s.get("potrazuje", ""):
                    broj_konta   = "          " + bk
                    opis_display = "          " + opis_display
                else:
                    broj_konta = bk
            logicki_redovi.append({
                "tip": "stavka", "col1": rb_d, "col2": broj_konta,
                "opis": opis_display, "duguje": s.get("duguje", ""), "potrazuje": s.get("potrazuje", ""),
                "_rb_bloka": str(current_rb), "_opis_raw": opis_display.strip(),
            })
        else:
            logicki_redovi.append({
                "tip": "stavka", "col1": rb_d, "col2": dt_d,
                "opis": konto_display, "duguje": s.get("duguje", ""), "potrazuje": s.get("potrazuje", ""),
                "_rb_bloka": str(current_rb), "_opis_raw": konto_display.strip(),
            })
        is_first = False

    if podaci.get("is_journal_closed"):
        def _fmt(v):
            try:
                s_ = f"{float(v):,.2f}".replace(",","X").replace(".",",").replace("X",".")
                return s_[:-3]+",-" if s_.endswith(",00") else s_
            except Exception: return str(v) if v else ""
        logicki_redovi.append({
            "tip": "promet", "col1": "0", "col2": "",
            "opis": "Promet Dnevnika",
            "duguje":    _fmt(podaci.get("ukupno_duguje_promet", 0)),
            "potrazuje": _fmt(podaci.get("ukupno_potrazuje_promet", 0)),
        })
    return logicki_redovi, je_3_4, ime, grade, zadatak


def izvezi_u_pdf(podaci, putanja_pdf, oznake_profesora=None):
    """Generiše PDF dnevnika iz podaci dict-a."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import cm
    from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                     Paragraph, Spacer, HRFlowable, PageBreak)
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.enums import TA_CENTER
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    _font_regular = "Helvetica"
    _font_bold    = "Helvetica-Bold"
    for _fdir in [
        os.path.join(os.environ.get("WINDIR", ""), "Fonts"),
        "/usr/share/fonts/truetype/dejavu",
        "/usr/share/fonts/truetype/liberation",
        os.path.expanduser("~/.fonts"),
    ]:
        _cand  = os.path.join(_fdir, "DejaVuSans.ttf")
        _cand_b= os.path.join(_fdir, "DejaVuSans-Bold.ttf")
        if os.path.exists(_cand):
            try:
                pdfmetrics.registerFont(TTFont("DejaVuSans",      _cand))
                pdfmetrics.registerFont(TTFont("DejaVuSans-Bold", _cand_b))
                _font_regular = "DejaVuSans"
                _font_bold    = "DejaVuSans-Bold"
            except Exception: pass
            break

    logicki_redovi, je_3_4, ime, grade, zadatak = _generiši_logičke_redove_prof(podaci)
    oznake_map = {}
    for oz in (oznake_profesora or []):
        oznake_map[(str(oz.get("rb_bloka","")).strip(), str(oz.get("opis","")).strip())] = oz.get("komentar","")

    doc = SimpleDocTemplate(putanja_pdf, pagesize=A4,
                             leftMargin=1.5*cm, rightMargin=1.5*cm,
                             topMargin=1.8*cm,  bottomMargin=1.8*cm)

    def _st(name, **kw): return ParagraphStyle(name, fontName=_font_regular, fontSize=9, leading=12, **kw)
    def _sb(name, **kw): return ParagraphStyle(name, fontName=_font_bold,    fontSize=9, leading=12, **kw)
    s_n = _st("n"); s_b = _sb("b"); s_r = _sb("r", textColor=colors.HexColor("#1a3a6e"))
    s_g = _sb("g", textColor=colors.HexColor("#cc0000"))
    s_p = _sb("p2", textColor=colors.HexColor("#1a3a6e"))
    s_t = ParagraphStyle("tit", fontName=_font_bold, fontSize=13, leading=16, alignment=TA_CENTER)
    s_su= ParagraphStyle("sub", fontName=_font_regular, fontSize=9, leading=12, alignment=TA_CENTER, textColor=colors.HexColor("#555555"))

    story = []
    story.append(Paragraph("DNEVNIK KNJIŽENJA", s_t))
    story.append(Spacer(1, 4))
    meta = []
    if ime:   meta.append(f"Učenik/ca: {ime}")
    if grade: meta.append(f"Razred: {grade}")
    if meta:  story.append(Paragraph("  |  ".join(meta), s_su))
    story.append(Spacer(1, 2))
    story.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor("#1a3a6e"), spaceAfter=6))
    if zadatak:
        story.append(Paragraph(f"<b>Zadatak:</b> {zadatak}",
            ParagraphStyle("zad", fontName=_font_regular, fontSize=8.5, leading=12,
                           backColor=colors.HexColor("#fffbe6"), borderPadding=6)))
        story.append(Spacer(1, 6))

    col_widths = [1.0*cm, 2.2*cm, 8.8*cm, 2.5*cm, 2.5*cm]
    if je_3_4:
        col_headers = ["Br.", "Konto", "Opis", "Duguje", "Potražuje"]
    else:
        col_headers = ["Br.", "Datum", "Opis/Konto", "Duguje", "Potražuje"]

    base_styles = [
        ("BACKGROUND",  (0,0),(-1,0), colors.HexColor("#1a3a6e")),
        ("TEXTCOLOR",   (0,0),(-1,0), colors.white),
        ("FONTNAME",    (0,0),(-1,0), _font_bold),
        ("FONTSIZE",    (0,0),(-1,0), 9),
        ("ALIGN",       (0,0),(-1,0), "CENTER"),
        ("VALIGN",      (0,0),(-1,-1),"MIDDLE"),
        ("GRID",        (0,0),(-1,-1), 0.4, colors.HexColor("#cccccc")),
        ("LEFTPADDING",  (0,0),(-1,-1), 4),
        ("RIGHTPADDING", (0,0),(-1,-1), 4),
        ("TOPPADDING",   (0,0),(-1,-1), 3),
        ("BOTTOMPADDING",(0,0),(-1,-1), 3),
        ("ALIGN",       (3,0),(4,-1), "RIGHT"),
    ]

    def _fmt(v):
        try:
            s_ = f"{float(str(v).replace('.','').replace(',','.').replace('-','0')):,.2f}"
            s_ = s_.replace(",","X").replace(".",",").replace("X",".")
            return s_[:-3]+",-" if s_.endswith(",00") else s_
        except Exception: return str(v) if v else ""

    def _pi(v):
        try: return float(str(v).replace(".","").replace(",",".").replace("-","0"))
        except Exception: return 0.0

    def _flush(tdata, tstyles):
        t = Table(tdata, colWidths=col_widths, repeatRows=1)
        t.setStyle(TableStyle(tstyles))
        return t

    table_data   = [[Paragraph(h, _sb("hh")) for h in col_headers]]
    table_styles = list(base_styles)
    i_row        = 1
    kum_dug = kum_pot = 0.0
    zadnji_rb = None
    promjena  = 0
    br_strane = 1
    MAX_P     = 7

    for r in logicki_redovi:
        rb = r.get("col1","")
        if rb and rb != "0" and rb != zadnji_rb and r["tip"] == "stavka":
            zadnji_rb = rb; promjena += 1

        if r["tip"] != "promet" and promjena > MAX_P:
            # Prenos na sljedeću stranu
            pre = [Paragraph("",s_r), Paragraph("",s_r),
                   Paragraph(f"Prenos na stranu {br_strane+1}", s_r),
                   Paragraph(_fmt(kum_dug), s_r), Paragraph(_fmt(kum_pot), s_r)]
            table_data.append(pre)
            table_styles.append(("BACKGROUND",(0,i_row),(-1,i_row),colors.HexColor("#e8eef8")))
            i_row += 1
            story.append(_flush(table_data, table_styles))
            story.append(PageBreak())
            table_data   = [table_data[0]]
            table_styles = list(base_styles)
            i_row = 1; promjena = 1; zadnji_rb = rb; br_strane += 1
            pre2 = [Paragraph("",s_r), Paragraph("",s_r),
                    Paragraph(f"Prenos sa strane {br_strane-1}", s_r),
                    Paragraph(_fmt(kum_dug), s_r), Paragraph(_fmt(kum_pot), s_r)]
            table_data.append(pre2)
            table_styles.append(("BACKGROUND",(0,i_row),(-1,i_row),colors.HexColor("#e8eef8")))
            i_row += 1

        if r["tip"] == "separator":
            table_data.append([Paragraph("",s_n)]*5)
            table_styles.append(("LINEBELOW",(0,i_row),(-1,i_row),0.8,colors.HexColor("#aaaaaa")))
            i_row += 1
        elif r["tip"] == "promet":
            table_data.append([Paragraph("",s_n)]*5)
            table_styles.append(("LINEABOVE",(0,i_row),(-1,i_row),1.0,colors.HexColor("#1a3a6e")))
            i_row += 1
            table_data.append([Paragraph("",s_p), Paragraph("",s_p),
                               Paragraph(r["opis"],s_p),
                               Paragraph(r.get("duguje",""),s_p),
                               Paragraph(r.get("potrazuje",""),s_p)])
            table_styles.append(("BACKGROUND",(0,i_row),(-1,i_row),colors.HexColor("#e8eef8")))
            table_styles.append(("LINEBELOW",(0,i_row),(-1,i_row),1.5,colors.HexColor("#1a3a6e")))
            i_row += 1
            kum_dug += _pi(r.get("duguje","")); kum_pot += _pi(r.get("potrazuje",""))
        else:
            rb_r = str(r.get("_rb_bloka","")).strip()
            op_r = str(r.get("_opis_raw", r.get("opis",""))).strip()
            kom  = oznake_map.get((rb_r, op_r))
            if kom is not None:
                ktxt = f"{r['opis']}  ◄ {kom}" if kom else f"{r['opis']}  ◄ Greška!"
                row  = [Paragraph(r["col1"],s_g), Paragraph(r["col2"],s_g),
                        Paragraph(ktxt,s_g),
                        Paragraph(r.get("duguje",""),s_g), Paragraph(r.get("potrazuje",""),s_g)]
                table_data.append(row)
                table_styles.append(("BACKGROUND",(0,i_row),(-1,i_row),colors.HexColor("#ffe8e8")))
            else:
                s_rr = s_b if (r.get("duguje") or r.get("potrazuje")) else s_n
                table_data.append([Paragraph(r["col1"],s_rr), Paragraph(r["col2"],s_rr),
                                   Paragraph(r["opis"],s_rr),
                                   Paragraph(r.get("duguje",""),s_rr),
                                   Paragraph(r.get("potrazuje",""),s_rr)])
            i_row += 1
            kum_dug += _pi(r.get("duguje","")); kum_pot += _pi(r.get("potrazuje",""))

    if len(table_data) > 1:
        story.append(_flush(table_data, table_styles))

    story.append(Spacer(1, 8))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#cccccc"), spaceBefore=2))
    from datetime import datetime as _dt2
    story.append(Paragraph(
        f"Bookify — Izvezeno: {_dt2.now().strftime('%d.%m.%Y. %H:%M')}",
        ParagraphStyle("ft", fontName=_font_regular, fontSize=7,
                        textColor=colors.HexColor("#aaaaaa"), alignment=TA_CENTER)))
    doc.build(story)
    return True



def _otvori_uputstvo_profesori():
    html = """<!DOCTYPE html>
<html lang="sr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Bookify – Uputstvo za profesore</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600;700;800&display=swap');

  :root {
    --blue-dark: #1a3a6e;
    --blue-mid:  #253a5e;
    --blue-card: #1e3050;
    --gold:      #f0c040;
    --blue:      #5dade2;
    --green:     #58d68d;
    --red:       #ec7063;
    --muted:     #a8c4e8;
    --border:    #2e4a7a;
    --white:     #ffffff;
  }

  * { margin: 0; padding: 0; box-sizing: border-box; }

  body {
    font-family: 'Inter', sans-serif;
    background: #0d1f3c;
    color: var(--white);
    min-height: 100vh;
  }

  #naslovnica {
    min-height: 100vh;
    background: linear-gradient(145deg, #0d1f3c 0%, #1a3a6e 50%, #253a5e 100%);
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    position: relative;
    overflow: hidden;
    cursor: pointer;
  }
  #naslovnica::before {
    content: '';
    position: absolute;
    top: -100px; left: -100px;
    width: 400px; height: 400px;
    background: rgba(240,192,64,0.04);
    border-radius: 50%;
  }
  #naslovnica::after {
    content: '';
    position: absolute;
    bottom: -120px; right: -120px;
    width: 500px; height: 500px;
    background: rgba(93,173,226,0.05);
    border-radius: 50%;
  }

  .cover-badge {
    background: rgba(240,192,64,0.15);
    border: 1px solid rgba(240,192,64,0.3);
    color: var(--gold);
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 3px;
    text-transform: uppercase;
    padding: 6px 18px;
    border-radius: 20px;
    margin-bottom: 28px;
  }

  .cover-logo-wrap {
    background: rgba(255,255,255,0.06);
    border-radius: 28px;
    padding: 24px 40px;
    margin-bottom: 28px;
    border: 1px solid rgba(255,255,255,0.1);
  }

  .cover-logo {
    font-size: 58px;
    font-weight: 800;
    background: linear-gradient(135deg, #f0c040, #5dade2);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
    letter-spacing: -2px;
    line-height: 1;
  }

  .cover-subtitle { font-size: 22px; font-weight: 600; color: var(--gold); margin-bottom: 8px; }
  .cover-desc     { font-size: 14px; color: var(--muted); font-weight: 300; margin-bottom: 12px; }
  .cover-author   { font-size: 13px; color: rgba(168,196,232,0.5); margin-bottom: 56px; }

  .cover-btn {
    background: var(--gold);
    color: var(--blue-dark);
    font-size: 15px;
    font-weight: 800;
    padding: 16px 48px;
    border: none;
    border-radius: 50px;
    cursor: pointer;
    box-shadow: 0 8px 32px rgba(240,192,64,0.3);
    transition: all 0.2s;
    z-index: 2;
    position: relative;
  }
  .cover-btn:hover { transform: translateY(-2px); box-shadow: 0 12px 40px rgba(240,192,64,0.4); }

  .cover-dots { position: absolute; bottom: 28px; font-size: 12px; color: rgba(168,196,232,0.4); }

  #ebook { display: none; background: #0d1f3c; min-height: 100vh; }

  .ebook-header {
    background: var(--blue-dark);
    border-bottom: 3px solid var(--gold);
    padding: 14px 28px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    position: sticky;
    top: 0;
    z-index: 100;
  }
  .ebook-header h1 { font-size: 17px; font-weight: 700; color: var(--white); }

  .back-btn {
    background: rgba(255,255,255,0.1);
    color: var(--muted);
    border: none;
    padding: 6px 16px;
    border-radius: 20px;
    cursor: pointer;
    font-size: 13px;
    font-family: inherit;
    transition: background 0.2s;
  }
  .back-btn:hover { background: rgba(255,255,255,0.18); }

  .ebook-body { max-width: 100%; padding: 0 0 80px; }

  .toc { background: var(--blue-mid); border-bottom: 1px solid var(--border); padding: 28px 40px; }
  .toc h2 { font-size: 11px; font-weight: 700; letter-spacing: 3px; text-transform: uppercase; color: var(--gold); margin-bottom: 16px; }
  .toc-list { list-style: none; display: flex; flex-wrap: wrap; gap: 10px; }
  .toc-list a {
    color: var(--muted); text-decoration: none; font-size: 13px; font-weight: 500;
    display: flex; align-items: center; gap: 8px;
    background: var(--blue-card); padding: 8px 16px; border-radius: 20px;
    border: 1px solid var(--border); transition: all 0.15s;
  }
  .toc-list a:hover { color: var(--gold); border-color: var(--gold); }
  .toc-num {
    background: var(--gold); color: var(--blue-dark); font-size: 11px; font-weight: 800;
    width: 20px; height: 20px; border-radius: 50%;
    display: inline-flex; align-items: center; justify-content: center;
  }

  .section { padding: 44px 40px 36px; border-bottom: 1px solid var(--border); }
  .section-badge { display: inline-flex; align-items: center; gap: 6px; font-size: 11px; font-weight: 700; letter-spacing: 2px; text-transform: uppercase; color: var(--gold); margin-bottom: 10px; }
  .section h2 { font-size: 28px; font-weight: 800; color: var(--white); margin-bottom: 6px; letter-spacing: -0.5px; }
  .divider { height: 3px; background: linear-gradient(90deg, var(--gold), var(--blue), transparent); border-radius: 2px; margin-bottom: 24px; }
  .section p { font-size: 15px; line-height: 1.8; color: var(--muted); margin-bottom: 14px; }

  .card-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 14px; margin-top: 20px; }
  .card {
    border-radius: 12px; padding: 20px 22px;
    display: flex; align-items: flex-start; gap: 14px;
    border: 1px solid var(--border); background: var(--blue-card);
    transition: transform 0.15s, border-color 0.15s;
  }
  .card:hover { transform: translateY(-2px); border-color: var(--gold); }
  .card-full { grid-column: 1 / -1; }
  .card-icon { font-size: 26px; min-width: 42px; text-align: center; margin-top: 2px; }
  .card-info h3 { font-size: 14px; font-weight: 700; color: var(--white); margin-bottom: 5px; }
  .card-info p { font-size: 13px; color: var(--muted); line-height: 1.6; margin: 0; }

  .steps { margin-top: 20px; }
  .step { display: flex; gap: 16px; margin-bottom: 22px; align-items: flex-start; }
  .step-num {
    background: var(--gold); color: var(--blue-dark); font-size: 13px; font-weight: 800;
    width: 32px; height: 32px; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    flex-shrink: 0; margin-top: 2px;
  }
  .step-content h3 { font-size: 15px; font-weight: 700; color: var(--white); margin-bottom: 4px; }
  .step-content p { font-size: 14px; color: var(--muted); line-height: 1.65; margin: 0; }

  .info-box {
    border-radius: 10px; padding: 16px 20px; font-size: 14px; line-height: 1.65;
    margin: 18px 0; display: flex; gap: 12px; align-items: flex-start;
  }
  .info-box.tip  { background: rgba(93,173,226,0.1);  border-left: 4px solid var(--blue); }
  .info-box.warn { background: rgba(240,192,64,0.1);  border-left: 4px solid var(--gold); }
  .info-box.note { background: rgba(88,214,141,0.1);  border-left: 4px solid var(--green); }
  .info-box-icon { font-size: 18px; margin-top: 1px; }
  .info-box p { margin: 0; color: var(--muted); }
  .info-box strong { color: var(--white); }

  .prof-table { width: 100%; border-collapse: separate; border-spacing: 0; margin-top: 18px; font-size: 13px; border-radius: 10px; overflow: hidden; }
  .prof-table th { background: var(--gold); color: var(--blue-dark); padding: 12px 16px; font-weight: 700; text-align: left; }
  .prof-table td { padding: 11px 16px; border-bottom: 1px solid var(--border); color: var(--muted); background: var(--blue-card); }
  .prof-table tr:last-child td { border-bottom: none; }
  .prof-table tr:hover td { background: var(--blue-mid); }

  .badge { display: inline-block; padding: 3px 10px; border-radius: 20px; font-size: 12px; font-weight: 700; }
  .badge-gold  { background: rgba(240,192,64,0.2);  color: var(--gold); }
  .badge-blue  { background: rgba(93,173,226,0.2);  color: var(--blue); }
  .badge-green { background: rgba(88,214,141,0.2);  color: var(--green); }

  .ebook-footer { text-align: center; padding: 32px; font-size: 13px; color: rgba(168,196,232,0.4); border-top: 1px solid var(--border); }
  .ebook-footer strong { color: var(--gold); }

  @media (max-width: 600px) {
    .card-grid { grid-template-columns: 1fr; }
    .section { padding: 28px 18px; }
    .toc { padding: 20px 18px; }
    .cover-logo { font-size: 42px; }
  }
</style>
</head>
<body>

<div id="naslovnica">
  <div class="cover-badge">Profesor Edition</div>
  <div class="cover-logo-wrap">
    <div class="cover-logo">📚 BOOKIFY</div>
  </div>
  <p class="cover-subtitle">Uputstvo za profesore</p>
  <p class="cover-desc">Bookify — Profesor aplikacija</p>
  <p class="cover-author">Danijel Đukić</p>
  <button class="cover-btn" onclick="otvoriEbook()">📖 Otvori uputstvo</button>
  <p class="cover-dots">Kliknite bilo gde da otvorite ›</p>
</div>

<div id="ebook">
  <div class="ebook-header">
    <h1>📚 Bookify — Uputstvo za profesore</h1>
    <button class="back-btn" onclick="nazad()">← Naslovnica</button>
  </div>

  <div class="ebook-body">
    <div class="toc">
      <h2>Sadržaj</h2>
      <ul class="toc-list">
        <li><a href="#s1"><span class="toc-num">1</span> O Profesor aplikaciji</a></li>
        <li><a href="#s2"><span class="toc-num">2</span> Pokretanje i spajanje</a></li>
        <li><a href="#s3"><span class="toc-num">3</span> Pregled učenika</a></li>
        <li><a href="#s4"><span class="toc-num">4</span> Live prikaz učenika</a></li>
        <li><a href="#s5"><span class="toc-num">5</span> Slanje zadataka</a></li>
        <li><a href="#s6"><span class="toc-num">6</span> Označavanje grešaka</a></li>
        <li><a href="#s7"><span class="toc-num">7</span> Čuvanje i učitavanje radova</a></li>
        <li><a href="#s8"><span class="toc-num">8</span> Više učionica</a></li>
      </ul>
    </div>

    <!-- S1 -->
    <div class="section" id="s1">
      <div class="section-badge">📘 Poglavlje 1</div>
      <h2>O Profesor aplikaciji</h2>
      <div class="divider"></div>
      <p>
        <strong style="color:white;">Bookify Profesor</strong> je aplikacija koja se pokreće na profesorovom računaru i omogućava praćenje rada svakog učenika u realnom vremenu, slanje zadataka direktno na učeničke računare, kao i pregled i ocenjivanje završenih radova.
      </p>
      <p>
        Sistem radi na principu lokalne mreže — svi računari u učionici moraju biti povezani na isti server. Profesor pokreće <strong style="color:white;">profesori.py</strong>, učenici se spajaju unosom IP adrese u Bookify, i od tog trenutka profesor vidi sve.
      </p>
      <div class="card-grid">
        <div class="card">
          <span class="card-icon">👁</span>
          <div class="card-info">
            <h3>Live prikaz</h3>
            <p>Kliknite na učenika i vidite tačno šta radi — dnevnik, T-konta, glavnu knjigu — u realnom vremenu.</p>
          </div>
        </div>
        <div class="card">
          <span class="card-icon">📋</span>
          <div class="card-info">
            <h3>Slanje zadataka</h3>
            <p>Unesite tekst zadatka i pošaljite ga odjednom na sve učeničke računare — pojavljuje se direktno u Bookifyu.</p>
          </div>
        </div>
        <div class="card">
          <span class="card-icon">🔴</span>
          <div class="card-info">
            <h3>Označavanje grešaka</h3>
            <p>Dvostrukim klikom na red u live prikazu označite grešku — učenik odmah vidi komentar u svom programu.</p>
          </div>
        </div>
        <div class="card">
          <span class="card-icon">💾</span>
          <div class="card-info">
            <h3>Čuvanje radova</h3>
            <p>Sačuvajte radove svih učenika jednim klikom, ili učitajte prethodno sačuvan rad za pregled i ocjenjivanje.</p>
          </div>
        </div>
      </div>
    </div>

    <!-- S2 -->
    <div class="section" id="s2">
      <div class="section-badge">🚀 Poglavlje 2</div>
      <h2>Pokretanje i spajanje</h2>
      <div class="divider"></div>
      <p>Profesor pokreće <strong style="color:white;">profesori.py</strong> na svom računaru. Učenici imaju samo <strong style="color:white;">bookify.py</strong> — profesori.py im nije potreban.</p>

      <div class="steps">
        <div class="step">
          <div class="step-num">1</div>
          <div class="step-content">
            <h3>Pokrenite profesori.py</h3>
            <p>Dvostruki klik na <code style="background:var(--blue-card);padding:2px 6px;border-radius:4px;color:var(--gold);">profesori.py</code> — server se automatski pokreće na portu 5050.</p>
          </div>
        </div>
        <div class="step">
          <div class="step-num">2</div>
          <div class="step-content">
            <h3>Zapišite IP adresu na tablu</h3>
            <p>U gornjem desnom uglu ekrana vidite IP adresu npr. <code style="background:var(--blue-card);padding:2px 6px;border-radius:4px;color:var(--gold);">192.168.1.5:5050</code> — upišite je učenicima na tablu.</p>
          </div>
        </div>
        <div class="step">
          <div class="step-num">3</div>
          <div class="step-content">
            <h3>Učenici se spajaju</h3>
            <p>Učenici otvore Bookify → <strong style="color:white;">PODEŠAVANJA</strong> → <strong style="color:white;">📡 Mreža — Spajanje na Profesor Server</strong> → unesu IP adresu → kliknu <strong style="color:white;">🔌 Spoji se</strong>. IP se pamti, sledeći put se spajaju automatski.</p>
          </div>
        </div>
        <div class="step">
          <div class="step-num">4</div>
          <div class="step-content">
            <h3>Pratite učenike</h3>
            <p>Čim se učenik spoji, pojavljuje se u listi. Podaci se automatski ažuriraju svakih nekoliko sekundi.</p>
          </div>
        </div>
      </div>

      <div class="info-box note">
        <span class="info-box-icon">💻</span>
        <p><strong>Isti računar (testiranje):</strong> Ako su profesori.py i bookify.py pokrenuti na istom računaru, bookify se automatski spaja na <strong>127.0.0.1:5050</strong> bez ikakvog unosa — ne treba ništa podešavati.</p>
      </div>

      <div class="info-box warn">
        <span class="info-box-icon">⚠️</span>
        <p><strong>Važno:</strong> Server mora biti pokrenut <strong>prije</strong> nego što se učenici počnu spajati. Ako server nije aktivan, učenici neće moći da se spoje.</p>
      </div>
    </div>

    <!-- S3 -->
    <div class="section" id="s3">
      <div class="section-badge">👥 Poglavlje 3</div>
      <h2>Pregled učenika</h2>
      <div class="divider"></div>
      <p>U glavnom prozoru nalazi se lista svih spojenih učenika sa karticama. Lista se automatski osvježava.</p>

      <table class="prof-table">
        <thead>
          <tr><th>Podatak</th><th>Šta prikazuje</th></tr>
        </thead>
       

      <div class="info-box tip" style="margin-top:20px;">
        <span class="info-box-icon">💡</span>
        <p><strong>Dvostruki klik</strong> na karticu učenika automatski otvara live prikaz tog učenika.</p>
      </div>
      <div class="info-box warn">
        <span class="info-box-icon">🗑</span>
        <p>Dugme <strong>Ukloni neaktivne</strong> (gornji lijevi ugao) briše učenike koji duže nisu slali podatke — npr. ako je učenik ugasio Bookify.</p>
      </div>
    </div>

    <!-- S4 -->
    <div class="section" id="s4">
      <div class="section-badge">👁 Poglavlje 4</div>
      <h2>Live prikaz učenika</h2>
      <div class="divider"></div>
      <p>Dvostrukim klikom na karticu učenika otvara se <strong style="color:white;">pravi Bookify prozor</strong> sa svim podacima tog učenika — dnevnik, T-konta, glavna knjiga, izvještaji, kalkulacija.</p>

      <div class="steps">
        <div class="step">
          <div class="step-num">1</div>
          <div class="step-content">
            <h3>Dvostruki klik na učenika</h3>
            <p>Kliknite dvaput na karticu učenika u listi. Otvara se novi prozor sa žutim banerom koji označava da je ovo live prikaz.</p>
          </div>
        </div>
        <div class="step">
          <div class="step-num">2</div>
          <div class="step-content">
            <h3>Pratite u realnom vremenu</h3>
            <p>Prozor prikazuje kompletan Bookify učenika i automatski se osvežava dok učenik radi. Možete pregledati dnevnik, T-konta i sve ostale sekcije.</p>
          </div>
        </div>
        <div class="step">
          <div class="step-num">3</div>
          <div class="step-content">
            <h3>Označite greške (opciono)</h3>
            <p>Dvostrukim klikom na red u dnevniku (u live prikazu) možete označiti grešku i dodati komentar — učenik to vidi odmah u svom Bookifyu.</p>
          </div>
        </div>
      </div>

      <div class="info-box note">
        <span class="info-box-icon">📌</span>
        <p><strong>Napomena:</strong> Live prikaz je samo za gledanje — ne možete mjenjati podatke učenika. Žuti baner na vrhu prozora podsjeća vas da je ovo prikaz, a ne originalni rad.</p>
      </div>
      <div class="info-box tip">
        <span class="info-box-icon">💡</span>
        <p>Možete otvoriti <strong>više live prikaza odjednom</strong> — jedan prozor po učeniku — i pratiti više učenika istovremeno.</p>
      </div>
    </div>

    <!-- S5 -->
    <div class="section" id="s5">
      <div class="section-badge">📋 Poglavlje 5</div>
      <h2>Slanje zadataka učenicima</h2>
      <div class="divider"></div>
      <p>U desnom panelu profesorske aplikacije nalazi se sekcija <strong style="color:white;">📋 POŠALJI ZADATAK</strong>. Zadatak koji pošaljete pojavljuje se automatski u Bookifyu na svim spojenim računarima.</p>

      <div class="steps">
        <div class="step">
          <div class="step-num">1</div>
          <div class="step-content">
            <h3>Unesite tekst zadatka</h3>
            <p>Upišite zadatak u polje za tekst. Može biti kratak ili detaljan — učenici će ga vidjeti u cjelosti u svom Bookifyu.</p>
          </div>
        </div>
        <div class="step">
          <div class="step-num">2</div>
          <div class="step-content">
            <h3>Kliknite „📤 Pošalji svim učenicima"</h3>
            <p>Zadatak se šalje svim trenutno spojenim učenicima. Svaki učenik dobije obavještenje i zadatak se prikazuje u panelu ZADATAK unutar Bookifya.</p>
          </div>
        </div>
        <div class="step">
          <div class="step-num">3</div>
          <div class="step-content">
            <h3>Brisanje zadatka</h3>
            <p>Kliknite <strong style="color:white;">🗑 Obriši zadatak</strong> kada želite ukloniti zadatak sa svih učeničkih računara — npr. na kraju časa.</p>
          </div>
        </div>
      </div>

      <div class="info-box warn">
        <span class="info-box-icon">⚠️</span>
        <p><strong>Napomena:</strong> Učenici koji se spoje <strong>nakon</strong> slanja zadatka automatski preuzimaju zadatak pri prvoj sinhronizaciji.</p>
      </div>
    </div>

    <!-- S6 -->
    <div class="section" id="s6">
      <div class="section-badge">🔴 Poglavlje 6</div>
      <h2>Označavanje grešaka</h2>
      <div class="divider"></div>
      <p>U live prikazu učenika možete označiti konkretne redove u dnevniku kao greške — učenik odmah vidi komentar profesora direktno u svom Bookifyu, obojen crvenom bojom.</p>

      <div class="steps">
        <div class="step">
          <div class="step-num">1</div>
          <div class="step-content">
            <h3>Otvorite live prikaz učenika</h3>
            <p>Dvostruki klik na karticu učenika u listi.</p>
          </div>
        </div>
        <div class="step">
          <div class="step-num">2</div>
          <div class="step-content">
            <h3>Dvostruki klik na red u dnevniku</h3>
            <p>Kliknite dvaput na bilo koji red u dnevniku učenika. Otvara se prozor za označavanje greške.</p>
          </div>
        </div>
        <div class="step">
          <div class="step-num">3</div>
          <div class="step-content">
            <h3>Unesite komentar i pošaljite</h3>
            <p>Unesite komentar (npr. "Pogrešna strana" ili "Iznos netačan") i kliknite <strong style="color:white;">✔ Pošalji oznaku</strong>. Učenik odmah vidi taj red obojen crvenom sa vašim komentarom.</p>
          </div>
        </div>
        <div class="step">
          <div class="step-num">4</div>
          <div class="step-content">
            <h3>Uklanjanje oznaka</h3>
            <p>Kliknite <strong style="color:white;">🗑 Ukloni sve oznake</strong> u istom prozoru da uklonite sve greške za tog učenika.</p>
          </div>
        </div>
      </div>
    </div>

    <!-- S7 -->
    <div class="section" id="s7">
      <div class="section-badge">💾 Poglavlje 7</div>
      <h2>Čuvanje i učitavanje radova</h2>
      <div class="divider"></div>
      <p>U desnom panelu, u sekciji <strong style="color:white;">📂 PREGLED RADOVA</strong>, možete sačuvati radove svih učenika ili učitati prethodno sačuvan rad.</p>

      <div class="card-grid">
        <div class="card">
          <span class="card-icon">💾</span>
          <div class="card-info">
            <h3>Sačuvaj radove svih učenika</h3>
            <p>Klikom na <strong style="color:white;">💾 Sačuvaj radove svih učenika</strong> — svaki učenikov rad se snima u poseban JSON fajl na profesorovom računaru. Fajlovi se imenuju po imenu učenika.</p>
          </div>
        </div>
        <div class="card">
          <span class="card-icon">📂</span>
          <div class="card-info">
            <h3>Učitaj rad</h3>
            <p>Klikom na <strong style="color:white;">📂 Učitaj rad</strong> možete otvoriti prethodno sačuvan JSON fajl i pregledati ga u Bookify prikazu — korisno za ocjenjivanje van časa.</p>
          </div>
        </div>
      </div>

      <div class="info-box tip" style="margin-top:20px;">
        <span class="info-box-icon">💡</span>
        <p><strong>Savjet:</strong> Sačuvajte radove na kraju svakog časa — tako imate arhivu napretka svakog učenika.</p>
      </div>
    </div>

    <!-- S8 -->
    <div class="section" id="s8">
      <div class="section-badge">🏫 Poglavlje 8</div>
      <h2>Rad sa više učionica</h2>
      <div class="divider"></div>
      <p>Ako imate dve učionice, pokrenite <strong style="color:white;">jedan profesori.py po učionici</strong> — svaki na svom računaru. Svaki server ima svoju IP adresu i svoju nezavisnu listu učenika.</p>

      <div class="card-grid">
        <div class="card">
          <span class="card-icon">🏫</span>
          <div class="card-info">
            <h3>Učionica 1</h3>
            <p>Profesor 1 pokreće profesori.py. Dobije npr. IP <code style="color:var(--gold);">192.168.1.5:5050</code>. Svih 12 učenika se spaja na tu adresu.</p>
          </div>
        </div>
        <div class="card">
          <span class="card-icon">🏫</span>
          <div class="card-info">
            <h3>Učionica 2</h3>
            <p>Profesor 2 pokreće profesori.py. Dobije npr. IP <code style="color:var(--gold);">192.168.1.8:5050</code>. Svih 12 učenika u toj učionici se spaja na tu adresu.</p>
          </div>
        </div>
        <div class="card card-full">
          <span class="card-icon">✅</span>
          <div class="card-info">
            <h3>Dve sesije — potpuno nezavisne</h3>
            <p>Svaka učionica funkcioniše potpuno nezavisno. Profesor 1 vidi samo svoje učenike, profesor 2 samo svoje. Nema mješanja podataka između učionica.</p>
          </div>
        </div>
      </div>

      <div class="info-box tip" style="margin-top:20px;">
        <span class="info-box-icon">💡</span>
        <p><strong>Savjet:</strong> Ako učenik greškom unese pogrešnu IP adresu, može je ispraviti u Bookify → <strong>PODEŠAVANJA</strong> → <strong>📡 Mreža</strong> → unese ispravnu IP → klikne <strong>🔌 Spoji se</strong>.</p>
      </div>
    </div>

    <div class="ebook-footer">
      <p>📚 <strong>Bookify</strong> — Uputstvo za profesore</p>
      <p style="margin-top:6px;">Autor: <strong>Danijel Đukić</strong></p>
    </div>
  </div>
</div>

<script>
  function otvoriEbook() {
    document.getElementById('naslovnica').style.display = 'none';
    document.getElementById('ebook').style.display = 'block';
    window.scrollTo(0, 0);
  }
  function nazad() {
    document.getElementById('ebook').style.display = 'none';
    document.getElementById('naslovnica').style.display = 'flex';
    window.scrollTo(0, 0);
  }
  document.getElementById('naslovnica').addEventListener('click', function(e) {
    if (e.target.tagName !== 'BUTTON') otvoriEbook();
  });
  document.querySelectorAll('a[href^="#"]').forEach(a => {
    a.addEventListener('click', function(e) {
      e.preventDefault();
      const t = document.querySelector(this.getAttribute('href'));
      if (t) t.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  });
</script>
</body>
</html>
"""

    tmp = tempfile.NamedTemporaryFile(
        delete=False, suffix="_bookify_uputstvo_profesori.html",
        mode="w", encoding="utf-8")
    tmp.write(html)
    tmp.close()
    webbrowser.open("file:///" + tmp.name.replace("\\", "/"))

PORT = 5050
RELAY_URL = "https://bookify-relay.onrender.com"

# ── Kod učionice — generiše se jednom pri instalaciji ────────────────────────
def _ucitaj_ili_generiraj_kod():
    import random, os, json
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    cfg_dir = os.path.join(appdata, "Bookify")
    os.makedirs(cfg_dir, exist_ok=True)
    kod_fajl = os.path.join(cfg_dir, "bookify_kod.json")
    if os.path.exists(kod_fajl):
        try:
            return json.loads(open(kod_fajl, encoding="utf-8").read()).get("kod", "")
        except Exception:
            pass
    alfabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    kod = "".join(random.choices(alfabet, k=6))
    try:
        json.dump({"kod": kod}, open(kod_fajl, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:
        pass
    return kod

CLASSROOM_CODE = _ucitaj_ili_generiraj_kod()

class _FrameAsRoot(tk.Frame):
    """Wrapper koji dozvoljava KnjigovodstvoApp da radi unutar Frame-a."""
    def title(self, t=None): return ""
    def state(self, s=None):
        if s: return
        return "normal"
    def geometry(self, g=None): return ""
    def resizable(self, *a): pass
    def attributes(self, *a, **kw): pass
    def protocol(self, *a, **kw): pass
    def iconbitmap(self, *a, **kw): pass
    def configure(self, **kw):
        # Prihvati bg/background, ignoriši ostalo što Frame ne podržava
        safe = {k: v for k, v in kw.items() if k in ("bg", "background", "cursor")}
        if safe:
            tk.Frame.configure(self, **safe)
    def config(self, **kw):
        self.configure(**kw)
    def after(self, ms, fn=None, *args):
        return tk.Frame.after(self, ms, fn, *args) if fn else tk.Frame.after(self, ms)
    def mainloop(self): pass
    def withdraw(self): pass
    def deiconify(self): pass
    def lift(self): pass
    def focus_set(self): pass
    def update(self):
        try: tk.Frame.update(self)
        except Exception: pass
    def update_idletasks(self):
        try: tk.Frame.update_idletasks(self)
        except Exception: pass
    def winfo_screenwidth(self):
        return self.winfo_toplevel().winfo_screenwidth()
    def winfo_screenheight(self):
        return self.winfo_toplevel().winfo_screenheight()
    def winfo_exists(self):
        return True
    def _get_inner_frame(self):
        """Vraća frame unutar kojeg treba prikazati sadržaj koji bi išao u Toplevel."""
        for child in self.winfo_children():
            if hasattr(child, '_is_inner'):
                return child
        inner = tk.Frame(self, bg="#f0f2f5")
        inner._is_inner = True
        inner.pack(fill="both", expand=True)
        return inner

# ── Boje ──────────────────────────────────────────────────────────────────────
BG       = "#1a3a6e"
BG_PANEL = "#253a5e"
BG_CARD  = "#1e3050"
GOLD     = "#f0c040"
BLUE     = "#5dade2"
BLUE2    = "#2471a3"
GREEN    = "#58d68d"
RED      = "#ec7063"
MUTED    = "#a8c4e8"
WHITE    = "#ffffff"
BORDER   = "#2e4a7a"

# ── Globalni podaci ───────────────────────────────────────────────────────────
ucenici      = {}
ucenici_lock = threading.Lock()
trenutni_zadatak = {"tekst": "", "tip": "tekst"}
zadaci_po_ip = {}   # ip -> {"tekst": ..., "tip": ...}  (individualni zadaci)
oznake_po_ip = {}   # ip -> lista oznaka [{rb_bloka, konto, komentar, boja}]

# ── HTTP Handler ──────────────────────────────────────────────────────────────
class BookifyHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args): pass

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/zadatak":
            self._json(trenutni_zadatak)
        elif self.path == "/ping":
            self._json({"status": "ok", "kod": CLASSROOM_CODE})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length)
        try:
            data = json.loads(body.decode("utf-8"))
        except Exception:
            self._json({"error": "bad json"}, 400)
            return

        if self.path == "/update":
            ip = self.client_address[0]
            with ucenici_lock:
                ucenici[ip] = {
                    "ime":           data.get("ime", "Nepoznat"),
                    "razred":        data.get("razred", ""),
                    "promet_dug":    data.get("promet_dug", 0),
                    "promet_pot":    data.get("promet_pot", 0),
                    "zavrsio":       data.get("zavrsio", False),
                    "broj_gresaka":  data.get("broj_gresaka", 0),
                    "zadnji_update": data.get("zadnji_update",
                                             datetime.now().strftime("%H:%M:%S")),
                    "state":         data.get("state", {}),
                    "ip":            ip,
                    "ucenik_id":     data.get("ucenik_id", ""),
                }
            ucenik_id_iz_data = data.get("ucenik_id", "")
            trenutne_oznake = oznake_po_ip.get(ucenik_id_iz_data) or oznake_po_ip.get(ip, [])
            # Individualni zadatak ima prednost nad globalnim
            # Traži po ucenik_id (MAC adresa) ili po ip — koji god postoji
            zadatak_za_ucenika = (zadaci_po_ip.get(ucenik_id_iz_data)
                                  or zadaci_po_ip.get(ip)
                                  or trenutni_zadatak)
            self._json({"status": "ok", "zadatak": zadatak_za_ucenika,
                        "oznake": trenutne_oznake})
        else:
            self._json({"error": "not found"}, 404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def pokreni_http_server():
    server = HTTPServer(("0.0.0.0", PORT), BookifyHandler)
    server.serve_forever()


# ── Live prikaz učenika ───────────────────────────────────────────────────────
def _get_bookify_path():
    """Vraća putanju do bookify.py — iz ugrađenih resursa ako je .exe, ili iz foldera."""
    import sys, tempfile, importlib.util

    # Ako je frozen (.exe) — traži bookify.py u _MEIPASS ili pored exe
    if getattr(sys, 'frozen', False):
        # Pokušaj 1: ugrađen via --add-data
        src = os.path.join(sys._MEIPASS, "bookify.py")
        if os.path.exists(src):
            return src
        # Pokušaj 2: bookify.py pored profesori.exe
        src2 = os.path.join(os.path.dirname(sys.executable), "bookify.py")
        if os.path.exists(src2):
            return src2
        messagebox.showerror("Greška", "bookify.py nije pronađen!\n\nKopirajte bookify.py u isti folder kao profesori.exe.")
        return None

    # Ako se radi kao .py — traži pored profesori.py
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bookify.py")
    if os.path.exists(path):
        return path

    messagebox.showerror("Greška", "bookify.py nije pronađen!")
    return None


def otvori_bookify_ucenik(state, ime, root_parent):
    """Live prikaz učenika — sve sekcije se renderuju jednom, pa samo prikazuju/skrivaju."""
    bookify_path = _get_bookify_path()
    if not bookify_path:
        return

    import importlib.util
    import time as _time

    grade = state.get("grade", "2. razred")
    plan  = state.get("plan", None)

    # Nađi IP učenika — proslijeđujemo ga kroz state
    ucenik_ip = state.get("_ucenik_ip")
    if not ucenik_ip:
        with ucenici_lock:
            for ip_k, u in ucenici.items():
                if u.get("ime") == ime:
                    ucenik_ip = ip_k
                    break

    je_live = bool(ucenik_ip)

    def _procitaj_signale_ucenika():
        """Obavijesti relay da je profesor pregledao učenikove oznake
        ('nisam siguran') i odmah ih ukloni lokalno — kartica prestaje
        da 'sija' bez čekanja na sljedeći poll ciklus."""
        if not ucenik_ip:
            return
        with ucenici_lock:
            if ucenik_ip in ucenici:
                ucenici[ucenik_ip]["signali"] = []

        def _salji():
            try:
                import urllib.request as _ur, json as _j
                payload = _j.dumps(
                    {"classroom_kod": CLASSROOM_CODE, "ucenik_id": ucenik_ip},
                    ensure_ascii=False).encode("utf-8")
                req = _ur.Request(f"{RELAY_URL}/procitaj_signale", data=payload,
                                  headers={"Content-Type": "application/json"})
                _ur.urlopen(req, timeout=3)
            except Exception:
                pass
        import threading as _thr4
        _thr4.Thread(target=_salji, daemon=True).start()

    proz = tk.Toplevel(root_parent)
    proz.title(f"👤 {ime}  —  {grade}  {'[LIVE PRIKAZ]' if je_live else '[UČITANI RAD]'}")
    proz.configure(bg="#f0f2f5")
    try:
        proz.state("zoomed")
    except Exception:
        proz.geometry("1400x860")

    def _zatvori_prozor():
        _procitaj_signale_ucenika()
        proz.destroy()

    proz.protocol("WM_DELETE_WINDOW", _zatvori_prozor)

    # ── Banner ─────────────────────────────────────────────────────────────
    banner = tk.Frame(proz, bg=GOLD, height=34)
    banner.pack(fill="x")
    banner.pack_propagate(False)
    tk.Label(banner, text=f"👁  LIVE:  {ime}  —  {grade}  |  Dvostruki klik na red u dnevniku = označi grešku",
             font=("Segoe UI", 10, "bold"), bg=GOLD, fg=BG).pack(side="left", padx=16)
    lbl_update = tk.Label(banner, text="", font=("Segoe UI", 9), bg=GOLD, fg="#555")
    lbl_update.pack(side="right", padx=16)

    # ── Nav traka ───────────────────────────────────────────────────────────
    nav = tk.Frame(proz, bg="#1a3a6e", height=40)
    nav.pack(fill="x")
    nav.pack_propagate(False)

    tk.Button(nav, text="← Nazad",
              command=_zatvori_prozor,
              font=("Segoe UI", 10, "bold"),
              bg=GOLD, fg=BG, relief="flat", bd=0,
              padx=14, pady=6, cursor="hand2").pack(side="left", padx=8, pady=5)

    tk.Frame(nav, bg="#2e4a7a", width=1).pack(side="left", fill="y", pady=6)

    btn_osvjezi = tk.Button(nav, text="🔄  Osvježi",
              font=("Segoe UI", 9, "bold"),
              bg=BG_CARD, fg=WHITE, relief="flat", bd=0,
              padx=12, pady=6, cursor="hand2")
    btn_osvjezi.pack(side="right", padx=8, pady=5)

    # ── Kontejner za sve sekcije ────────────────────────────────────────────
    kontejner = tk.Frame(proz, bg="#f0f2f5")
    kontejner.pack(fill="both", expand=True)

    # Pamtimo frame i app za svaku sekciju
    _sekcije   = {}   # naziv → {"frame": ..., "app": ...}
    _aktivna   = {"naziv": None}
    _tab_btns  = {}

    def _ucitaj_sekciju(naziv, akcija):
        """Kreira sekciju ako ne postoji, inače koristi postojeću."""
        if naziv in _sekcije:
            return _sekcije[naziv]

        # Sakrij kontejner dok renderujemo — bez trepćanja
        frame = tk.Frame(kontejner, bg="#f0f2f5")
        # Render se dešava dok je frame još nevidljiv (nije pack-ovan)

        import traceback as _tb

        spec = importlib.util.spec_from_file_location(
            f"bk_{naziv}_{id(frame)}_{ucenik_ip}_{_time.time_ns()}", bookify_path)
        mod  = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception as e:
            msg = f"Greška pri učitavanju bookify.py:\n{e}\n\n{_tb.format_exc()}"
            tk.Label(frame, text=msg, fg="red", bg="#f0f2f5",
                     wraplength=800, justify="left").pack(pady=20, padx=20)
            _sekcije[naziv] = {"frame": frame, "app": None}
            return _sekcije[naziv]

        with ucenici_lock:
            u  = ucenici.get(ucenik_ip, {})
            st = u.get("state", state)

        try:
            root_w = _FrameAsRoot(frame, bg="#f0f2f5")
            root_w.pack(fill="both", expand=True)
            app = mod.KnjigovodstvoApp(root_w, st.get("grade", grade),
                                        st.get("plan", plan))
            app._ucenik_signali = u.get("signali", [])
            _ucitaj_state_u_app(app, st)
            _disable_dugmad(app)
            try: app.meni_button.pack_forget()
            except Exception: pass
            try: app.app_header.pack_forget()
            except Exception: pass
            if akcija == "dnevnik":
                try: app.toggle_dnevnik_prikaz()
                except Exception: pass
            elif akcija == "knjiga":
                try: app.toggle_knjiga_prikaz()
                except Exception: pass
            elif akcija == "izvjestaji":
                try:
                    bilans_frame = tk.Frame(frame, bg="#f0f2f5")
                    bilans_frame.pack(fill="both", expand=True)
                    app.otvori_bilans_u_frame(bilans_frame)
                except Exception as e:
                    tk.Label(frame, text=f"Greška: {e}",
                             fg="red", bg="#f0f2f5").pack(pady=20)
            elif akcija == "kalkulacija":
                try:
                    from bookify_live import KalkulacijaTabela as _KT
                    inst = _KT.__new__(_KT)
                    _KT.__init__(inst, frame, grade=st.get("grade","2. razred"))
                    app._kalkulacija_inst = inst
                except Exception:
                    try:
                        # Fallback — otvori KalkulacijaTabela direktno u frame
                        spec2 = importlib.util.spec_from_file_location(
                            "bk_kal", bookify_path)
                        mod3 = importlib.util.module_from_spec(spec2)
                        spec2.loader.exec_module(mod3)
                        import tkinter as _tk2
                        _orig = _tk2.Toplevel
                        class _FakeWin(tk.Frame):
                            def __init__(s, parent=None, **kw):
                                tk.Frame.__init__(s, frame, bg="#f0f2f5")
                                s.pack(fill="both", expand=True)
                            def title(s, t=None): return ""
                            def geometry(s, g=None): return ""
                            def resizable(s, *a): pass
                            def protocol(s, *a, **kw): pass
                            def configure(s, **kw):
                                bg = kw.get("bg") or kw.get("background")
                                if bg: tk.Frame.configure(s, bg=bg)
                            def winfo_exists(s): return True
                            def withdraw(s): pass
                            def deiconify(s): pass
                            def lift(s): pass
                            def update_idletasks(s):
                                tk.Frame.update_idletasks(s)
                            def winfo_screenwidth(s):
                                return s.winfo_toplevel().winfo_screenwidth()
                            def winfo_screenheight(s):
                                return s.winfo_toplevel().winfo_screenheight()
                            def attributes(s, *a, **kw): pass
                            def state(s, val=None): return "normal"
                        _tk2.Toplevel = _FakeWin
                        mod3.KalkulacijaTabela(frame, grade=st.get("grade","2. razred"))
                        _tk2.Toplevel = _orig
                    except Exception as e2:
                        tk.Label(frame, text=f"Greška: {e2}",
                                 fg="red", bg="#f0f2f5").pack(pady=20)
        except Exception as e:
            msg = f"Greška pri prikazu:\n{e}\n\n{_tb.format_exc()}"
            tk.Label(frame, text=msg, fg="red", bg="#f0f2f5",
                     wraplength=800, justify="left").pack(pady=20, padx=20)
            app = None

        _sekcije[naziv] = {"frame": frame, "app": app}
        return _sekcije[naziv]

    def _prikazi(naziv, akcija=None):
        """Sakrij sve, prikaži samo traženu sekciju — bez trepćanja."""
        # Zamrzni update da se sve desi odjednom
        kontejner.update_idletasks()

        # Sakrij sve
        for k, v in _sekcije.items():
            v["frame"].pack_forget()

        # Ažuriraj izgled tab dugmadi
        for k, b in _tab_btns.items():
            b.config(bg=BG_CARD if k != naziv else "#2471a3")

        # Učitaj sekciju (ako nije već) i prikaži
        sek = _ucitaj_sekciju(naziv, akcija)
        sek["frame"].pack(fill="both", expand=True)
        _aktivna["naziv"] = naziv
        kontejner.update_idletasks()

    def _osvjezi():
        """Osvježi aktivnu sekciju sa najnovijim podacima."""
        naziv = _aktivna["naziv"]
        if naziv and naziv in _sekcije:
            app = _sekcije[naziv].get("app")
            if app:
                try:
                    with ucenici_lock:
                        u  = ucenici.get(ucenik_ip, {})
                        st = u.get("state", state)
                    app._ucenik_signali = u.get("signali", [])
                    _ucitaj_state_u_app(app, st)
                except Exception:
                    pass

    def _otvori_izvjestaje(app_ref, state_ref, grade_ref, ucenik_ip_ref, ime_ref, proz_parent):
        """Otvori Izvještaje u novom Toplevel prozoru."""
        with ucenici_lock:
            u  = ucenici.get(ucenik_ip_ref, {})
            st = u.get("state", state_ref)

        win = tk.Toplevel(proz_parent)
        win.title(f"📊 Izvještaji — {ime_ref}")
        win.configure(bg="#f0f2f5")
        try:
            win.state("zoomed")
        except Exception:
            win.geometry("1200x800")

        banner = tk.Frame(win, bg=GOLD, height=32)
        banner.pack(fill="x")
        banner.pack_propagate(False)
        tk.Label(banner, text=f"📊  Izvještaji  —  {ime_ref}  |  Samo za gledanje",
                 font=("Segoe UI", 10, "bold"), bg=GOLD, fg=BG).pack(side="left", padx=14)
        tk.Button(banner, text="✕ Zatvori", command=win.destroy,
                  font=("Segoe UI", 9, "bold"), bg=GOLD, fg=BG,
                  relief="flat", bd=0, cursor="hand2").pack(side="right", padx=10)

        bilans_frame = tk.Frame(win, bg="#f0f2f5")
        bilans_frame.pack(fill="both", expand=True)
        try:
            app_ref.otvori_bilans_u_frame(bilans_frame)
        except Exception as e:
            tk.Label(bilans_frame, text=f"Greška: {e}",
                     fg="red", bg="#f0f2f5").pack(pady=20)

    def _otvori_kalkulaciju(state_ref, grade_ref, ucenik_ip_ref, ime_ref, proz_parent):
        """Otvori Kalkulaciju u novom Toplevel prozoru."""
        with ucenici_lock:
            u  = ucenici.get(ucenik_ip_ref, {})
            st = u.get("state", state_ref)

        win = tk.Toplevel(proz_parent)
        win.title(f"🧮 Kalkulacija — {ime_ref}")
        win.configure(bg="#f0f2f5")
        try:
            win.state("zoomed")
        except Exception:
            win.geometry("1200x800")

        banner = tk.Frame(win, bg=GOLD, height=32)
        banner.pack(fill="x")
        banner.pack_propagate(False)
        tk.Label(banner, text=f"🧮  Kalkulacija  —  {ime_ref}  |  Samo za gledanje",
                 font=("Segoe UI", 10, "bold"), bg=GOLD, fg=BG).pack(side="left", padx=14)
        tk.Button(banner, text="✕ Zatvori", command=win.destroy,
                  font=("Segoe UI", 9, "bold"), bg=GOLD, fg=BG,
                  relief="flat", bd=0, cursor="hand2").pack(side="right", padx=10)

        kal_frame = tk.Frame(win, bg="#f0f2f5")
        kal_frame.pack(fill="both", expand=True)
        try:
            from bookify_live import KalkulacijaTabela as _KT
            _KT(kal_frame, grade=st.get("grade", grade_ref))
        except Exception:
            try:
                spec2 = importlib.util.spec_from_file_location("bk_kal2", bookify_path)
                mod3  = importlib.util.module_from_spec(spec2)
                spec2.loader.exec_module(mod3)
                mod3.KalkulacijaTabela(kal_frame, grade=st.get("grade", grade_ref))
            except Exception as e2:
                tk.Label(kal_frame, text=f"Greška: {e2}",
                         fg="red", bg="#f0f2f5").pack(pady=20)

    btn_osvjezi.config(command=_osvjezi)

    # Tab dugmad u nav traci
    for txt, ak in [
        ("📖 Dnevnik",       "dnevnik"),
        ("📚 Glavna knjiga", "knjiga"),
    ]:
        b = tk.Button(nav, text=txt,
                      command=lambda n=txt, a=ak: _prikazi(n, a),
                      font=("Segoe UI", 9, "bold"),
                      bg=BG_CARD, fg=WHITE, relief="flat", bd=0,
                      padx=10, pady=6, cursor="hand2",
                      activebackground="#2471a3")
        b.pack(side="left", padx=3, pady=5)
        _tab_btns[txt] = b

    # Izvještaji i Kalkulacija — otvaraju novi prozor
    tk.Button(nav, text="📊 Izvještaji",
              command=lambda: _otvori_izvjestaje(
                  _sekcije.get("__pregled__", {}).get("app"),
                  state, grade, ucenik_ip, ime, proz),
              font=("Segoe UI", 9, "bold"),
              bg=BG_CARD, fg=WHITE, relief="flat", bd=0,
              padx=10, pady=6, cursor="hand2",
              activebackground="#2471a3").pack(side="left", padx=3, pady=5)

    tk.Button(nav, text="🧮 Kalkulacija",
              command=lambda: _otvori_kalkulaciju(
                  state, grade, ucenik_ip, ime, proz),
              font=("Segoe UI", 9, "bold"),
              bg=BG_CARD, fg=WHITE, relief="flat", bd=0,
              padx=10, pady=6, cursor="hand2",
              activebackground="#2471a3").pack(side="left", padx=3, pady=5)

    # Renderuj live pregled odmah (dnevnik je defaultni prikaz)
    _prikazi("__pregled__", "dnevnik")

    # ── Označi red — profesor klikne red u dnevniku, pošalje komentar učeniku ─
    def _oznaci_red(event):
        """Profesor klikne na red u dnevniku → dijalog → oznaka ide učeniku."""
        app = _sekcije.get("__pregled__", {}).get("app")
        if not app or not hasattr(app, "table"):
            return
        iid = app.table.identify_row(event.y)
        if not iid:
            return
        vals = app.table.item(iid, "values")
        if not vals or len(vals) < 3:
            return
        rb   = str(vals[0]).strip().rstrip(".")
        opis = str(vals[2]).strip()
        # Preskoči linije separatora i prenos redove
        if not opis or opis.startswith("─") or opis.startswith("═") or \
           "Prenos" in opis or "prenos" in opis:
            return

        # Dijalog za komentar
        dlg = tk.Toplevel(proz)
        dlg.title("Označi red učeniku")
        dlg.configure(bg=BG)
        dlg.resizable(False, False)
        dlg.grab_set()
        # Centriraj na ekran
        dlg.geometry("420x300")
        dlg.update_idletasks()
        x = proz.winfo_x() + (proz.winfo_width()  - 420) // 2
        y = proz.winfo_y() + (proz.winfo_height() - 300) // 2
        dlg.geometry(f"420x300+{x}+{y}")

        tk.Label(dlg, text="🔴  Označi grešku učeniku",
                 font=("Segoe UI", 12, "bold"), bg=BG, fg=GOLD).pack(pady=(18, 4))

        info_frame = tk.Frame(dlg, bg=BG_CARD, padx=12, pady=8)
        info_frame.pack(fill="x", padx=16)
        rb_txt  = f"Stav {rb}  —  " if rb else ""
        opis_kr = opis[:60] + ("..." if len(opis) > 60 else "")
        tk.Label(info_frame, text=f"{rb_txt}{opis_kr}",
                 font=("Segoe UI", 10), bg=BG_CARD, fg=WHITE,
                 wraplength=380, justify="left").pack(anchor="w")

        tk.Label(dlg, text="Komentar profesora (prikazuje se učeniku):",
                 font=("Segoe UI", 9), bg=BG, fg=MUTED).pack(anchor="w", padx=16, pady=(14, 2))
        kom_entry = tk.Text(dlg, height=4, font=("Segoe UI", 10),
                            bg=BG_CARD, fg=WHITE, insertbackground=WHITE,
                            relief="flat", bd=0,
                            highlightbackground=BORDER, highlightthickness=1)
        kom_entry.pack(fill="x", padx=16)
        kom_entry.focus_set()

        btn_row = tk.Frame(dlg, bg=BG)
        btn_row.pack(fill="x", padx=16, pady=(12, 0))

        def _potvrdi():
            komentar = kom_entry.get("1.0", "end").strip()
            nova_oznaka = {
                "rb_bloka": rb,
                "opis":     opis,
                "komentar": komentar,
            }
            if ucenik_ip:
                postojece = oznake_po_ip.get(ucenik_ip, [])
                postojece = [o for o in postojece
                             if not (o.get("rb_bloka") == rb and o.get("opis") == opis)]
                postojece.append(nova_oznaka)
                oznake_po_ip[ucenik_ip] = postojece
                # Pošalji oznake na relay
                def _salji(uid, ozn):
                    try:
                        import urllib.request as _ur, json as _j
                        payload = _j.dumps({
                            "classroom_kod": CLASSROOM_CODE,
                            "ucenik_id": uid,
                            "oznake": ozn
                        }, ensure_ascii=False).encode("utf-8")
                        req = _ur.Request(f"{RELAY_URL}/posalji_oznake",
                                          data=payload,
                                          headers={"Content-Type": "application/json"})
                        _ur.urlopen(req, timeout=4)
                    except Exception:
                        pass
                import threading as _thr
                _thr.Thread(target=_salji, args=(ucenik_ip, list(postojece)), daemon=True).start()
            dlg.destroy()
            try:
                app.table.item(iid, tags=("greska_prof_tag",))
                app.table.tag_configure("greska_prof_tag",
                                        background="#ffe0e0", foreground="#cc0000",
                                        font=("Segoe UI", 11, "bold"))
            except Exception:
                pass

        def _ukloni_oznaku():
            """Ukloni sve oznake za ovog učenika."""
            if ucenik_ip:
                oznake_po_ip[ucenik_ip] = []
                def _brisi(uid):
                    try:
                        import urllib.request as _ur, json as _j
                        payload = _j.dumps({
                            "classroom_kod": CLASSROOM_CODE,
                            "ucenik_id": uid,
                            "oznake": []
                        }, ensure_ascii=False).encode("utf-8")
                        req = _ur.Request(f"{RELAY_URL}/posalji_oznake",
                                          data=payload,
                                          headers={"Content-Type": "application/json"})
                        _ur.urlopen(req, timeout=4)
                    except Exception:
                        pass
                import threading as _thr
                _thr.Thread(target=_brisi, args=(ucenik_ip,), daemon=True).start()
            dlg.destroy()

        tk.Button(btn_row, text="✔  Pošalji oznaku",
                  command=_potvrdi,
                  font=("Segoe UI", 10, "bold"),
                  bg="#cc3333", fg=WHITE, relief="flat", bd=0,
                  padx=12, pady=6, cursor="hand2").pack(side="left")
        tk.Button(btn_row, text="🗑  Ukloni sve oznake",
                  command=_ukloni_oznaku,
                  font=("Segoe UI", 9),
                  bg=BG_CARD, fg=MUTED, relief="flat", bd=0,
                  padx=10, pady=6, cursor="hand2").pack(side="left", padx=(8, 0))
        tk.Button(btn_row, text="Odustani",
                  command=dlg.destroy,
                  font=("Segoe UI", 9),
                  bg=BG_CARD, fg=MUTED, relief="flat", bd=0,
                  padx=10, pady=6, cursor="hand2").pack(side="right")

        dlg.bind("<Return>", lambda e: _potvrdi())
        dlg.bind("<Escape>", lambda e: dlg.destroy())

    # Veži klik na tabelu dnevnika — ali tek kad se sekcija kreira
    def _vezi_klik_na_tabelu():
        app = _sekcije.get("__pregled__", {}).get("app")
        if app and hasattr(app, "table"):
            app.table.bind("<Double-Button-1>", _oznaci_red)
            # Dodaj tooltip u banner
            try:
                lbl_update.config(text="Dvostruki klik na red = označi grešku")
            except Exception:
                pass
    proz.after(800, _vezi_klik_na_tabelu)

    # ── Auto-osvježavanje pregleda svake sekunde ───────────────────────────
    def _refresh():
        if not proz.winfo_exists():
            return
        try:
            with ucenici_lock:
                u = ucenici.get(ucenik_ip, {})
            if u:
                lbl_update.config(text=f"🕐 {u.get('zadnji_update', '')}")
                # Auto-osvježi aktivnu sekciju
                naziv = _aktivna["naziv"]
                if naziv and naziv in _sekcije:
                    app = _sekcije.get(naziv, {}).get("app")
                    if app:
                        app._ucenik_signali = u.get("signali", [])
                        _ucitaj_state_u_app(app, u.get("state", {}))
        except Exception:
            pass
        proz.after(1000, _refresh)

    proz.after(1000, _refresh)


def _disable_dugmad(app):
    """Onemogući SVA dugmad i unos — osim 4 pregleda."""
    import tkinter as _tk

    DOZVOLJENA = ["dnevnik", "glavna", "knjiga", "kalkulacija",
                  "izvještaji", "izvjestaji", "извјештаји", "извјештаj"]

    def _dozvoljeno(tekst):
        t = tekst.lower()
        # Ukloni emoji i specijalne znakove
        cist = ''.join(c for c in t if c.isalpha() or c.isspace())
        return any(k in cist for k in DOZVOLJENA)

    def _prodi(widget):
        try:
            cls = widget.winfo_class()
            if cls == "Button":
                txt = widget.cget("text")
                if _dozvoljeno(txt):
                    # Dozvoljeno — ostavi aktivnim
                    widget.config(cursor="hand2")
                else:
                    widget.config(state="disabled", cursor="arrow")
            elif cls in ("Entry", "TCombobox", "Text"):
                widget.config(state="disabled", cursor="arrow")
        except Exception:
            pass
        for child in widget.winfo_children():
            _prodi(child)

    _prodi(app.root)


def _ucitaj_state_u_app(app, state):
    """Ažuriraj podatke u app."""
    try:
        ime = state.get("ucenik_ime", "")
        if hasattr(app, 'ucenik_ime_var'):
            app.ucenik_ime_var.set(ime)
        app.sve_stavke              = list(state.get("sve_stavke", []))
        app.is_initial_state_mode   = state.get("is_initial_state_mode", False)
        app.is_journal_closed       = state.get("is_journal_closed", False)
        app.ukupno_duguje_promet    = state.get("ukupno_duguje_promet", 0.0)
        app.ukupno_potrazuje_promet = state.get("ukupno_potrazuje_promet", 0.0)
        app.redni_broj              = state.get("redni_broj", 0)
        app.blok_broj               = state.get("blok_broj", 0)
        app.saldo_mode              = state.get("saldo_mode", False)
        app._zadatak_tekst          = state.get("zadatak_tekst", "")

        grade = state.get("grade", app.grade)
        if grade in ["3. razred", "4. razred"] and hasattr(app, 'klasa_var'):
            sacuvana = state.get("odabrana_klasa")
            if sacuvana and hasattr(app, '_klasa_values'):
                match = next(
                    (v for v in app._klasa_values if v == sacuvana), None)
                if match:
                    app.klasa_var.set(match)

        app.recalculate_t_konti()
        app.prikazi_dnevnik()
    except Exception:
        pass


# ── GUI Profesora ─────────────────────────────────────────────────────────────
class ProfesorApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Bookify — Profesor Server")
        self.root.configure(bg=BG)
        try:
            self.root.state("zoomed")
        except Exception:
            self.root.geometry("1100x700")
        self._izgradnja_gui()
        self._osvjezi_loop()

    def _izgradnja_gui(self):
        # ── Header ────────────────────────────────────────────────────────────
        hdr = tk.Frame(self.root, bg=BG, height=80)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        tk.Label(hdr, text="📚  BOOKIFY",
                 font=("Segoe UI", 18, "bold"),
                 bg=BG, fg=WHITE).pack(side="left", padx=20, pady=10)
        tk.Label(hdr, text="Profesor Server",
                 font=("Segoe UI", 11), bg=BG, fg=MUTED).pack(side="left", pady=14)
        tk.Button(hdr, text="📖  Uputstvo",
                  command=_otvori_uputstvo_profesori,
                  font=("Segoe UI", 9, "bold"),
                  bg=BG_CARD, fg=GOLD, relief="flat", bd=0,
                  padx=12, pady=5, cursor="hand2").pack(side="left", padx=(16, 0), pady=16)
        tk.Button(hdr, text="🗑  Ukloni neaktivne",
                  command=self._ukloni_neaktivne,
                  font=("Segoe UI", 9),
                  bg=BG_CARD, fg=MUTED, relief="flat", bd=0,
                  padx=10, pady=5, cursor="hand2").pack(side="left", padx=(8, 0), pady=16)

        ip = get_local_ip()
        ip_frame = tk.Frame(hdr, bg=BG_CARD, padx=16, pady=8)
        ip_frame.pack(side="right", padx=20, pady=10)
        tk.Label(ip_frame, text="Kod učionice (unesite učenicima jednom):",
                 font=("Segoe UI", 9), bg=BG_CARD, fg=MUTED).pack(anchor="w")
        tk.Label(ip_frame, text=CLASSROOM_CODE,
                 font=("Segoe UI", 26, "bold"),
                 bg=BG_CARD, fg=GOLD).pack(anchor="w")
        tk.Label(ip_frame, text=f"(IP: {ip}:{PORT})",
                 font=("Segoe UI", 8), bg=BG_CARD, fg=MUTED).pack(anchor="w")

        tk.Frame(self.root, bg=GOLD, height=3).pack(fill="x")

        # ── Status bar ────────────────────────────────────────────────────────
        sbar = tk.Frame(self.root, bg=BG_PANEL, height=34)
        sbar.pack(fill="x")
        sbar.pack_propagate(False)
        self.lbl_broj = tk.Label(sbar, text="Spojeni učenici: 0",
                                  font=("Segoe UI", 10, "bold"), bg=BG_PANEL, fg=GOLD)
        self.lbl_broj.pack(side="left", padx=16, pady=6)
        self.lbl_status = tk.Label(sbar, text="⏳ Čekam učenike...",
                                    font=("Segoe UI", 9), bg=BG_PANEL, fg=MUTED)
        self.lbl_status.pack(side="left", padx=8, pady=6)

        # ── Glavni layout: grid učenika lijevo, zadatak desno ─────────────────
        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True)

        # Lijevo — scrollabilni grid kartica učenika
        lijevo = tk.Frame(body, bg=BG)
        lijevo.pack(side="left", fill="both", expand=True)

        # Canvas + scrollbar za grid
        canvas_outer = tk.Frame(lijevo, bg=BG)
        canvas_outer.pack(fill="both", expand=True)
        vsb_grid = tk.Scrollbar(canvas_outer, orient="vertical")
        vsb_grid.pack(side="right", fill="y")
        self.grid_canvas = tk.Canvas(canvas_outer, bg=BG, highlightthickness=0,
                                      yscrollcommand=vsb_grid.set)
        self.grid_canvas.pack(side="left", fill="both", expand=True)
        vsb_grid.config(command=self.grid_canvas.yview)
        self.grid_frame = tk.Frame(self.grid_canvas, bg=BG)
        self._grid_win = self.grid_canvas.create_window((0,0), window=self.grid_frame, anchor="nw")
        self.grid_frame.bind("<Configure>", lambda e: self.grid_canvas.configure(
            scrollregion=self.grid_canvas.bbox("all")))
        self.grid_canvas.bind("<Configure>", lambda e: self.grid_canvas.itemconfig(
            self._grid_win, width=e.width))

        # Mousewheel scroll — preskače text widgete da ne remeti pisanje
        def _scroll(event):
            if event.widget.winfo_class() in ("Text",):
                return
            self.grid_canvas.yview_scroll(int(-1*(event.delta/120)), "units")
        self.grid_canvas.bind_all("<MouseWheel>", _scroll)

        # Dict: ip → kartica frame
        self._kartice = {}

        # ── Desno: slanje zadatka ──────────────────────────────────────────────
        desno = tk.Frame(body, bg=BG_PANEL, width=320)
        desno.pack(side="right", fill="y")
        desno.pack_propagate(False)
        tk.Frame(desno, bg=BORDER, width=2).pack(side="left", fill="y")

        # Scrollabilni desni panel — da dugmad ne budu odrezana na manjim ekranima
        _zp_vsb = tk.Scrollbar(desno, orient="vertical")
        _zp_vsb.pack(side="right", fill="y")
        _zp_canvas = tk.Canvas(desno, bg=BG_PANEL, highlightthickness=0,
                               yscrollcommand=_zp_vsb.set)
        _zp_canvas.pack(side="left", fill="both", expand=True)
        _zp_vsb.config(command=_zp_canvas.yview)

        zp = tk.Frame(_zp_canvas, bg=BG_PANEL, padx=16, pady=16)
        _zp_win = _zp_canvas.create_window((0, 0), window=zp, anchor="nw")

        def _zp_configure(e):
            _zp_canvas.configure(scrollregion=_zp_canvas.bbox("all"))
        def _zp_canvas_resize(e):
            _zp_canvas.itemconfig(_zp_win, width=e.width)
        zp.bind("<Configure>", _zp_configure)
        _zp_canvas.bind("<Configure>", _zp_canvas_resize)

        def _zp_scroll(e):
            _zp_canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        _zp_canvas.bind("<MouseWheel>", _zp_scroll)
        zp.bind("<MouseWheel>", _zp_scroll)

        tk.Label(zp, text="📋  POŠALJI ZADATAK",
                 font=("Segoe UI", 12, "bold"), bg=BG_PANEL, fg=GOLD).pack(anchor="w")
        tk.Frame(zp, bg=GOLD, height=2).pack(fill="x", pady=(4, 12))

        # ── Dropdown: Svi učenici / odabrani učenik ───────────────────────────
        tk.Label(zp, text="Pošalji zadatak:",
                 font=("Segoe UI", 9), bg=BG_PANEL, fg=MUTED).pack(anchor="w", pady=(0, 4))

        _OPT_SVI = "👥  Svi učenici"
        self._odabrani_ucenik_var = tk.StringVar(value=_OPT_SVI)
        self._OPT_SVI = _OPT_SVI
        self._ucenik_ip_mapa = {}  # ime_prikaz -> ip

        # Wrapper za OptionMenu (da ga možemo lako rebuild-ati)
        self._ucenik_opt_frame = tk.Frame(zp, bg=BG_PANEL)
        self._ucenik_opt_frame.pack(fill="x", pady=(0, 10))

        self._ucenik_menu = tk.OptionMenu(
            self._ucenik_opt_frame,
            self._odabrani_ucenik_var,
            _OPT_SVI)
        self._ucenik_menu.config(
            font=("Segoe UI", 10, "bold"),
            bg=BG_CARD, fg=WHITE,
            activebackground=BORDER, activeforeground=GOLD,
            relief="flat", bd=0,
            highlightthickness=1, highlightbackground=BORDER,
            indicatoron=True, anchor="w",
            cursor="hand2", padx=10, pady=8)
        self._ucenik_menu["menu"].config(
            font=("Segoe UI", 10),
            bg=BG_CARD, fg=WHITE,
            activebackground=GOLD, activeforeground=BG,
            relief="flat", bd=0)
        self._ucenik_menu.pack(fill="x")

        tk.Label(zp, text="Tekst zadatka:",
                 font=("Segoe UI", 9), bg=BG_PANEL, fg=MUTED).pack(anchor="w", pady=(0, 2))
        self.zadatak_text = scrolledtext.ScrolledText(
            zp, height=9, font=("Segoe UI", 10),
            bg=BG_CARD, fg=WHITE, insertbackground=WHITE,
            relief="flat", bd=0, highlightbackground=BORDER, highlightthickness=1)
        self.zadatak_text.pack(fill="x", pady=(0, 10))
        # Klik na textarea odmah daje fokus i ne propagira scroll na grid
        self.zadatak_text.bind("<Button-1>", lambda e: self.zadatak_text.focus_set())
        self.zadatak_text.bind("<MouseWheel>", lambda e: "break")

        self._btn_posalji = tk.Button(zp, text="📤  Pošalji zadatak",
                  command=self._posalji_zadatak,
                  font=("Segoe UI", 11, "bold"),
                  bg=GOLD, fg=BG, relief="flat", bd=0,
                  padx=10, pady=10, cursor="hand2",
                  activebackground="#e0a800")
        self._btn_posalji.pack(fill="x")
        tk.Button(zp, text="🗑  Obriši zadatak",
                  command=self._obrisi_zadatak,
                  font=("Segoe UI", 9),
                  bg=BG_CARD, fg=MUTED, relief="flat", bd=0,
                  padx=10, pady=6, cursor="hand2").pack(fill="x", pady=(6, 0))

        self.lbl_zadatak_status = tk.Label(
            zp, text="", font=("Segoe UI", 9),
            bg=BG_PANEL, fg=GREEN, wraplength=280, justify="left")
        self.lbl_zadatak_status.pack(anchor="w", pady=(8, 0))

        # ── Pregled radova učenika ─────────────────────────────────────────────
        tk.Frame(zp, bg=BORDER, height=1).pack(fill="x", pady=(14, 10))
        tk.Label(zp, text="📂  PREGLED RADOVA",
                 font=("Segoe UI", 10, "bold"), bg=BG_PANEL, fg=GOLD).pack(anchor="w")
        tk.Frame(zp, bg=GOLD, height=2).pack(fill="x", pady=(4, 10))

        tk.Button(zp, text="💾  Sačuvaj radove svih učenika",
                  command=self._sacuvaj_radove_svih,
                  font=("Segoe UI", 10, "bold"),
                  bg=BLUE2, fg=WHITE, relief="flat", bd=0,
                  padx=10, pady=10, cursor="hand2",
                  activebackground="#1a5a8a").pack(fill="x")

        tk.Button(zp, text="📂  Učitaj rad",
                  command=self._ucitaj_rad_iz_fajla,
                  font=("Segoe UI", 10, "bold"),
                  bg=BG_CARD, fg=MUTED, relief="flat", bd=0,
                  padx=10, pady=10, cursor="hand2",
                  activebackground=BORDER).pack(fill="x", pady=(6, 0))

        self.lbl_sacuvaj_status = tk.Label(
            zp, text="", font=("Segoe UI", 9),
            bg=BG_PANEL, fg=GREEN, wraplength=280, justify="left")
        self.lbl_sacuvaj_status.pack(anchor="w", pady=(6, 0))
        self._pregled_lista_frame = tk.Frame(zp, bg=BG_PANEL)
        self._pregled_lista_frame.pack(fill="x")

        tk.Frame(zp, bg=BORDER, height=1).pack(fill="x", pady=(10, 6))
        tk.Label(zp, text="📡  Log aktivnosti:",
                 font=("Segoe UI", 9, "bold"), bg=BG_PANEL, fg=MUTED).pack(anchor="w")
        self.log_box = scrolledtext.ScrolledText(
            zp, height=7, font=("Courier New", 8),
            bg=BG_CARD, fg=MUTED, insertbackground=MUTED,
            relief="flat", bd=0, state="disabled",
            highlightbackground=BORDER, highlightthickness=1)
        self.log_box.pack(fill="x", pady=(4, 0))

    def _napravi_karticu(self, ip, u):
        """Kreira karticu za učenika u grid-u."""
        kartica = tk.Frame(self.grid_frame, bg=BG_CARD,
                           highlightbackground=BORDER, highlightthickness=1,
                           cursor="hand2")

        ime_txt   = u.get("ime") or "(bez imena)"
        razred_txt = u.get("razred", "")
        br_stavki = len([s for s in u.get("state", {}).get("sve_stavke", []) if s.get("konto")])
        zavrsio   = u.get("zavrsio", False)
        update    = u.get("zadnji_update", "")

        # Traka upozorenja — prikazuje se samo kad je učenik poslao oznaku
        # profesoru ("nisam siguran u ovaj red"). Kreirana odmah, ali se
        # pakuje/uklanja dinamički u _azuriraj_karticu.
        signal_traka = tk.Frame(kartica, bg="#e67e22", height=22)
        signal_traka.pack_propagate(False)
        tk.Label(signal_traka, text="🚩  Učenik nije siguran — pogledaj detalje!",
                 font=("Segoe UI", 8, "bold"), bg="#e67e22", fg="white").pack(
            fill="both", expand=True)

        # Header kartice
        hdr_bg = "#1a7a4a" if zavrsio else "#2471a3"
        khdr = tk.Frame(kartica, bg=hdr_bg, height=32)
        khdr.pack(fill="x")
        khdr.pack_propagate(False)
        tk.Label(khdr, text=f"🖥  {ip}", font=("Segoe UI", 8),
                 bg=hdr_bg, fg=WHITE).pack(side="left", padx=8)
        lbl_status = tk.Label(khdr,
                 text="✅ Završio" if zavrsio else "🔄 Radi...",
                 font=("Segoe UI", 8, "bold"),
                 bg=hdr_bg, fg=GREEN if zavrsio else GOLD)
        lbl_status.pack(side="right", padx=8)

        # Tijelo kartice
        body_k = tk.Frame(kartica, bg=BG_CARD, pady=12)
        body_k.pack(fill="x", padx=12)

        lbl_ime = tk.Label(body_k, text=ime_txt,
                 font=("Segoe UI", 13, "bold"), bg=BG_CARD, fg=WHITE,
                 anchor="center")
        lbl_ime.pack(fill="x")
        lbl_razred = tk.Label(body_k, text=razred_txt,
                 font=("Segoe UI", 10), bg=BG_CARD, fg=MUTED, anchor="center")
        lbl_razred.pack(fill="x", pady=(2, 8))

        # Info red
        info = tk.Frame(body_k, bg=BG_CARD)
        info.pack(fill="x")
        info_lbls = {}
        br_gresaka = u.get("broj_gresaka", 0)
        boja_gresaka = RED if br_gresaka > 0 else GREEN
        col_f = tk.Frame(info, bg=BG_CARD)
        col_f.pack(expand=True)
        lv = tk.Label(col_f, text=str(br_gresaka),
                      font=("Segoe UI", 22, "bold"),
                      bg=BG_CARD, fg=boja_gresaka)
        lv.pack()
        tk.Label(col_f, text="Greške",
                 font=("Segoe UI", 9), bg=BG_CARD, fg=MUTED).pack()
        info_lbls["greske"] = lv

        lbl_update = tk.Label(body_k, text=f"🕐 {update}",
                 font=("Segoe UI", 8), bg=BG_CARD, fg=MUTED)
        lbl_update.pack(pady=(8, 0))

        # Dugme Detalji
        tk.Button(kartica, text="👁  Otvori detalje",
                  command=lambda i=ip: self._otvori_live_ip(i),
                  font=("Segoe UI", 9, "bold"),
                  bg=GOLD, fg=BG, relief="flat", bd=0,
                  padx=8, pady=6, cursor="hand2").pack(
            fill="x", padx=12, pady=(0, 12))

        for w in [kartica, khdr, body_k, info]:
            w.bind("<Button-1>", lambda e, i=ip: self._otvori_live_ip(i))

        # Sačuvaj reference za ažuriranje bez uništavanja
        kartica._refs = {
            "khdr": khdr, "lbl_status": lbl_status,
            "lbl_ime": lbl_ime, "lbl_razred": lbl_razred,
            "info_lbls": info_lbls, "lbl_update": lbl_update,
            "signal_traka": signal_traka,
        }
        self._primijeni_signal_izgled(kartica, u)
        return kartica

    def _azuriraj_karticu(self, kartica, u):
        """Ažurira labele kartice bez uništavanja — nema trepćanja."""
        try:
            refs      = kartica._refs
            ime_txt   = u.get("ime") or "(bez imena)"
            razred_txt = u.get("razred", "")
            zavrsio   = u.get("zavrsio", False)
            update    = u.get("zadnji_update", "")
            br_gresaka = u.get("broj_gresaka", 0)

            hdr_bg = "#1a7a4a" if zavrsio else "#2471a3"
            refs["khdr"].config(bg=hdr_bg)
            refs["lbl_status"].config(
                bg=hdr_bg,
                text="✅ Završio" if zavrsio else "🔄 Radi...",
                fg=GREEN if zavrsio else GOLD)
            refs["lbl_ime"].config(text=ime_txt)
            refs["lbl_razred"].config(text=razred_txt)
            boja_gresaka = RED if br_gresaka > 0 else GREEN
            refs["info_lbls"]["greske"].config(
                text=str(br_gresaka), fg=boja_gresaka)
            refs["lbl_update"].config(text=f"🕐 {update}")
            self._primijeni_signal_izgled(kartica, u)
        except Exception:
            pass

    def _primijeni_signal_izgled(self, kartica, u):
        """Prikaži/sakrij traku upozorenja i istakni okvir kartice ako je
        učenik poslao oznaku profesoru ("nisam siguran u ovaj red")."""
        try:
            refs = kartica._refs
            traka = refs.get("signal_traka")
            ima_signal = bool(u.get("signali"))
            if ima_signal:
                kartica.config(highlightbackground="#e67e22", highlightthickness=3)
                if traka is not None and not traka.winfo_ismapped():
                    traka.pack(fill="x", before=refs["khdr"])
            else:
                kartica.config(highlightbackground=BORDER, highlightthickness=1)
                if traka is not None and traka.winfo_ismapped():
                    traka.pack_forget()
        except Exception:
            pass

    def _relayout_kartice(self, snap):
        """Raspoređuje kartice 3 po redu — ažurira bez uništavanja."""
        # Ukloni kartice učenika koji su se odspojili
        for ip in list(self._kartice.keys()):
            if ip not in snap:
                self._kartice[ip].destroy()
                del self._kartice[ip]

        # Ažuriraj postojeće ili kreiraj nove
        for ip, u in snap.items():
            if ip in self._kartice:
                self._azuriraj_karticu(self._kartice[ip], u)
            else:
                self._kartice[ip] = self._napravi_karticu(ip, u)

        # Grid raspored — 3 po redu
        for i, (ip, kartica) in enumerate(self._kartice.items()):
            kartica.grid(row=i // 3, column=i % 3,
                         padx=10, pady=10, sticky="nsew")

        for c in range(3):
            self.grid_frame.columnconfigure(c, weight=1)

        # Osvježi dropdown učenika za individualne zadatke
        self._osvjezi_ucenik_combo()

    def _log(self, tekst):
        self.log_box.config(state="normal")
        self.log_box.insert(
            "end", f"[{datetime.now().strftime('%H:%M:%S')}] {tekst}\n")
        self.log_box.see("end")
        self.log_box.config(state="disabled")

    def _osvjezi_loop(self):
        self._osvjezi_listu()
        self.root.after(2000, self._osvjezi_loop)

    def _osvjezi_listu(self):
        """Čita učenike sa relay servera."""
        def _citaj():
            try:
                import urllib.request as _ur, json as _j
                r = _ur.urlopen(
                    f"{RELAY_URL}/ucenik_lista?kod={CLASSROOM_CODE}",
                    timeout=4)
                podaci = _j.loads(r.read().decode("utf-8"))
                snap = podaci.get("ucenici", {})
                # Ukloni test ping unose
                snap = {k: v for k, v in snap.items()
                        if v.get("ime") or v.get("razred")}
                self.root.after(0, lambda s=snap: self._primijeni_snap(s))
            except Exception:
                pass

        import threading as _thr
        _thr.Thread(target=_citaj, daemon=True).start()

    def _primijeni_snap(self, snap):
        """Ažurira UI sa novim podacima sa relaya."""
        with ucenici_lock:
            # Dodaj nove učenike u lokalni dict
            for uid, u in snap.items():
                if uid not in ucenici:
                    self._log(f"Spojio se: {u.get('ime') or 'Nepoznat'} ({uid[:8]}...)")
                ucenici[uid] = u

            # Ukloni učenike kojih više nema u snapu
            za_brisanje = [k for k in ucenici if k not in snap]
            for k in za_brisanje:
                del ucenici[k]

            lokalni_snap = dict(ucenici)

        self._relayout_kartice(lokalni_snap)
        self._osvjezi_pregled_radova()
        self.lbl_broj.config(text=f"Spojeni učenici: {len(lokalni_snap)}")
        if lokalni_snap:
            self.lbl_status.config(
                text=f"🟢 Online — {len(lokalni_snap)} učenika spojeno", fg=GREEN)
        else:
            self.lbl_status.config(text="⏳ Čekam da se učenici spoje...", fg=MUTED)

    def _otvori_live_ip(self, ip):
        try:
            with ucenici_lock:
                u = ucenici.get(ip)
            if not u:
                messagebox.showinfo("Info", f"Učenik sa IP {ip} nije pronađen.")
                return
            state = dict(u.get("state", {}))
            state["_ucenik_ip"] = ip
            ime = u.get("ime", "Nepoznat")
            if not u.get("state"):
                messagebox.showinfo("Info",
                    "Učenik još nije poslao podatke. Sačekajte nekoliko sekundi.")
                return
            self._log(f"Otvaram live prikaz: {ime} ({ip})")
            otvori_bookify_ucenik(state, ime, self.root)
        except Exception as e:
            messagebox.showerror("Greška", f"Greška pri otvaranju prikaza:\n{e}")

    def _otvori_live(self):
        pass  # Zadržano za kompatibilnost

    def _ukloni_neaktivne(self):
        with ucenici_lock:
            now = datetime.now()
            za_brisanje = []
            for ip, u in ucenici.items():
                try:
                    t = datetime.strptime(u["zadnji_update"], "%H:%M:%S")
                    t = t.replace(
                        year=now.year, month=now.month, day=now.day)
                    if (now - t).total_seconds() > 120:
                        za_brisanje.append(ip)
                except Exception:
                    pass
            for ip in za_brisanje:
                self._log(
                    f"Uklonjen: {ucenici[ip]['ime']} ({ip})")
                del ucenici[ip]
        self._osvjezi_listu()

    def _on_zadatak_mod_promjena(self):
        pass  # Nije potrebno — OptionMenu se sam osvježava

    def _osvjezi_ucenik_combo(self):
        """Osvježava OptionMenu listu učenika kad se neko spoji/odspoji."""
        try:
            with ucenici_lock:
                snap = {ip: u.get("ime", "?") for ip, u in ucenici.items() if u.get("ime")}

            # Gradi mapu ime_prikaz -> ip (dodaj broj ako postoje dupla imena)
            nova_mapa = {}
            brojac = {}
            for ip, ime in snap.items():
                if ime in brojac:
                    brojac[ime] += 1
                    kljuc = f"{ime} ({brojac[ime]})"
                else:
                    brojac[ime] = 1
                    kljuc = ime
                nova_mapa[kljuc] = ip
            self._ucenik_ip_mapa = nova_mapa

            menu = self._ucenik_menu["menu"]
            menu.delete(0, "end")
            menu.add_command(
                label=self._OPT_SVI,
                command=lambda: self._odabrani_ucenik_var.set(self._OPT_SVI))
            for naziv in nova_mapa:
                menu.add_command(
                    label=naziv,
                    command=lambda v=naziv: self._odabrani_ucenik_var.set(v))

            # Reset ako odabrani više nije u listi
            if self._odabrani_ucenik_var.get() not in ([self._OPT_SVI] + list(nova_mapa.keys())):
                self._odabrani_ucenik_var.set(self._OPT_SVI)
        except Exception:
            pass

    def _posalji_zadatak(self):
        tekst = self.zadatak_text.get("1.0", "end").strip()
        if not tekst:
            messagebox.showwarning("Upozorenje", "Unesite tekst zadatka.")
            return

        odabir = self._odabrani_ucenik_var.get()

        if odabir != self._OPT_SVI:
            # ── Pošalji pojedinačnom učeniku ──────────────────────────────────
            # ciljni_ip je zapravo ucenik_id (MAC adresa) — ključ iz relay dict-a
            ucenik_id = self._ucenik_ip_mapa.get(odabir)
            if not ucenik_id:
                messagebox.showwarning("Upozorenje", "Učenik nije pronađen. Osvježite listu.")
                return
            # Sačuvaj lokalno (za učenike na lokalnoj mreži)
            zadaci_po_ip[ucenik_id] = {"tekst": tekst, "tip": "tekst"}
            # Pošalji na relay s ucenik_id
            def _salji_relay(uid, t):
                try:
                    import urllib.request as _ur, json as _j
                    payload = _j.dumps({
                        "classroom_kod": CLASSROOM_CODE,
                        "ucenik_id": uid,
                        "tekst": t,
                        "tip": "tekst"
                    }, ensure_ascii=False).encode("utf-8")
                    req = _ur.Request(f"{RELAY_URL}/posalji_zadatak",
                                      data=payload,
                                      headers={"Content-Type": "application/json"})
                    _ur.urlopen(req, timeout=4)
                except Exception:
                    pass
            import threading as _thr
            _thr.Thread(target=_salji_relay, args=(ucenik_id, tekst), daemon=True).start()
            self.lbl_zadatak_status.config(
                text=f"✅ Zadatak poslan: {odabir}", fg=GREEN)
            self._log(f"Individualni zadatak → {odabir} ({ucenik_id[:8]}...)")
            return

        # ── Pošalji svima ─────────────────────────────────────────────────────
        def _salji():
            try:
                import urllib.request as _ur, json as _j
                # Pošalji globalni zadatak
                payload = _j.dumps({
                    "classroom_kod": CLASSROOM_CODE,
                    "tekst": tekst,
                    "tip": "tekst"
                }, ensure_ascii=False).encode("utf-8")
                req = _ur.Request(f"{RELAY_URL}/posalji_zadatak",
                                  data=payload,
                                  headers={"Content-Type": "application/json"})
                _ur.urlopen(req, timeout=4)
                global trenutni_zadatak
                trenutni_zadatak = {"tekst": tekst, "tip": "tekst"}
                # Obriši sve individualne zadatke da globalni ima prednost
                with ucenici_lock:
                    svi_id = list(ucenici.keys())
                    n = len(ucenici)
                zadaci_po_ip.clear()
                for uid in svi_id:
                    try:
                        p = _j.dumps({
                            "classroom_kod": CLASSROOM_CODE,
                            "ucenik_id": uid,
                            "tekst": "", "tip": "tekst"
                        }, ensure_ascii=False).encode("utf-8")
                        r2 = _ur.Request(f"{RELAY_URL}/posalji_zadatak",
                                         data=p,
                                         headers={"Content-Type": "application/json"})
                        _ur.urlopen(r2, timeout=4)
                    except Exception:
                        pass
                self.root.after(0, lambda: self.lbl_zadatak_status.config(
                    text=f"✅ Zadatak poslan! Prikazuje se na {n} računara.", fg=GREEN))
                self.root.after(0, lambda: self._log(f"Zadatak poslan ({n} učenika)"))
            except Exception:
                self.root.after(0, lambda: self.lbl_zadatak_status.config(
                    text="❌ Greška pri slanju zadatka.", fg=RED))

        import threading as _thr
        _thr.Thread(target=_salji, daemon=True).start()

    def _obrisi_zadatak(self):
        odabir = self._odabrani_ucenik_var.get()

        if odabir != self._OPT_SVI:
            ucenik_id = self._ucenik_ip_mapa.get(odabir)
            if ucenik_id:
                zadaci_po_ip.pop(ucenik_id, None)
                def _brisi_relay(uid):
                    try:
                        import urllib.request as _ur, json as _j
                        payload = _j.dumps({
                            "classroom_kod": CLASSROOM_CODE,
                            "ucenik_id": uid,
                            "tekst": "", "tip": "tekst"
                        }, ensure_ascii=False).encode("utf-8")
                        req = _ur.Request(f"{RELAY_URL}/posalji_zadatak",
                                          data=payload,
                                          headers={"Content-Type": "application/json"})
                        _ur.urlopen(req, timeout=4)
                    except Exception:
                        pass
                import threading as _thr
                _thr.Thread(target=_brisi_relay, args=(ucenik_id,), daemon=True).start()
                self.zadatak_text.delete("1.0", "end")
                self.lbl_zadatak_status.config(
                    text=f"🗑 Zadatak obrisan za: {odabir}", fg=MUTED)
                self._log(f"Individualni zadatak obrisan → {odabir}")
            return

        def _brisi():
            try:
                import urllib.request as _ur, json as _j
                payload = _j.dumps({
                    "classroom_kod": CLASSROOM_CODE,
                    "tekst": "", "tip": "tekst"
                }, ensure_ascii=False).encode("utf-8")
                req = _ur.Request(f"{RELAY_URL}/posalji_zadatak",
                                  data=payload,
                                  headers={"Content-Type": "application/json"})
                _ur.urlopen(req, timeout=4)
            except Exception:
                pass

        import threading as _thr
        _thr.Thread(target=_brisi, daemon=True).start()
        global trenutni_zadatak
        trenutni_zadatak = {"tekst": "", "tip": "tekst"}
        self.zadatak_text.delete("1.0", "end")
        self.lbl_zadatak_status.config(
            text="🗑 Zadatak obrisan sa svih računara.", fg=MUTED)
        self._log("Zadatak obrisan")

    def _osvjezi_pregled_radova(self):
        pass  # Lista se više ne prikazuje

    def _sacuvaj_radove_svih(self):
        """Sačuvaj radove svih spojenih učenika — pita format (PDF ili JSON) jednom za sve."""
        with ucenici_lock:
            snap = dict(ucenici)
        if not snap:
            messagebox.showwarning("Upozorenje", "Nema spojenih učenika.")
            return

        # Pitaj format jednom za sve
        import tkinter as _tk
        izbor = [None]
        dial = _tk.Toplevel(self.root)
        dial.title("Sačuvaj radove")
        dial.resizable(False, False)
        dial.configure(bg="#1a3a6e")
        dial.grab_set()
        dial.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        dw, dh = 360, 185
        dial.geometry(f"{dw}x{dh}+{(sw-dw)//2}+{(sh-dh)//2}")
        _tk.Label(dial, text=f"Odaberi format za {len(snap)} učenika:",
                  font=("Segoe UI", 11, "bold"), bg="#1a3a6e", fg="white").pack(pady=(20, 8))
        btn_f = _tk.Frame(dial, bg="#1a3a6e")
        btn_f.pack()
        def _odaberi(fmt): izbor[0] = fmt; dial.destroy()
        _tk.Button(btn_f, text="📄  PDF", width=12, height=2,
                   font=("Segoe UI", 10, "bold"), bg="#f0c040", fg="#1a3a6e",
                   relief="flat", cursor="hand2", command=lambda: _odaberi("pdf")).pack(side="left", padx=10)
        _tk.Button(btn_f, text="🗂  JSON", width=12, height=2,
                   font=("Segoe UI", 10, "bold"), bg="#5dade2", fg="#1a3a6e",
                   relief="flat", cursor="hand2", command=lambda: _odaberi("json")).pack(side="left", padx=10)
        _tk.Button(dial, text="Odustani", font=("Segoe UI", 9),
                   bg="#2e4a7a", fg="#a8c4e8", relief="flat", cursor="hand2",
                   command=dial.destroy).pack(pady=(12, 0))
        dial.wait_window()
        if izbor[0] is None:
            return

        ext    = ".pdf"  if izbor[0] == "pdf" else ".json"
        folder = filedialog.askdirectory(title=f"Odaberi mapu za čuvanje radova ({ext})")
        if not folder:
            return

        sacuvano = 0
        greske   = []

        for ip, u in snap.items():
            try:
                ime    = u.get("ime") or "Nepoznat"
                razred = u.get("razred", "")
                state  = u.get("state", {})
                if not state:
                    greske.append(f"{ime}: nema podataka")
                    continue
                sigurno_ime    = ime.replace(" ", "_").replace("/", "_").replace("\\", "_")
                sigurno_razred = razred.replace(" ", "_").replace(".", "").replace("/", "_")
                naziv_fajla    = f"{sigurno_ime}_{sigurno_razred}{ext}"
                putanja        = os.path.join(folder, naziv_fajla)
                podaci = {
                    "grade":                   state.get("grade", razred),
                    "ucenik_ime":              ime,
                    "sve_stavke":              state.get("sve_stavke", []),
                    "is_initial_state_mode":   state.get("is_initial_state_mode", False),
                    "is_journal_closed":       state.get("is_journal_closed", False),
                    "ukupno_duguje_promet":    state.get("ukupno_duguje_promet", 0.0),
                    "ukupno_potrazuje_promet": state.get("ukupno_potrazuje_promet", 0.0),
                    "redni_broj":              state.get("redni_broj", 0),
                    "blok_broj":               state.get("blok_broj", 0),
                    "saldo_mode":              state.get("saldo_mode", False),
                    "odabrana_klasa":          state.get("odabrana_klasa", None),
                    "plan":                    state.get("plan", None),
                    "kalkulacije":             state.get("kalkulacije", []),
                    "zadatak_tekst":           state.get("zadatak_tekst", ""),
                }
                if izbor[0] == "json":
                    with open(putanja, "w", encoding="utf-8") as _f:
                        json.dump(podaci, _f, ensure_ascii=False, indent=4)
                else:
                    # Uvezi iz bookify modula (isti fajl) ili lokalno defini
                    try:
                        from bookify import izvezi_u_pdf as _izvezi
                    except ImportError:
                        # Ako se zove direktno — koristi globalnu funkciju (kopirana u profesori.py)
                        _izvezi = izvezi_u_pdf  # noqa: F821
                    oznake = []  # profesor nema lokalne oznake ovdje — one su na relay-u
                    _izvezi(podaci, putanja, oznake_profesora=oznake)
                sacuvano += 1
                self._log(f"Sačuvan: {ime} → {naziv_fajla}")
            except Exception as e:
                greske.append(f"{u.get('ime', ip)}: {e}")

        poruka = f"✅ Sačuvano {sacuvano} radova ({ext}) u:\n{folder}"
        if greske:
            poruka += "\n\n⚠ Greške:\n" + "\n".join(greske)
        self.lbl_sacuvaj_status.config(
            text=f"✅ Sačuvano {sacuvano} radova", fg=GREEN if not greske else GOLD)
        self._log(f"Sačuvanje završeno: {sacuvano} radova")
        messagebox.showinfo("Sačuvano", poruka)

    def _ucitaj_rad_iz_fajla(self):
        """Učitaj sačuvani JSON rad i otvori u novom prozoru."""
        putanja = filedialog.askopenfilename(
            title="Učitaj sačuvani rad",
            filetypes=[("JSON datoteke", "*.json"), ("Sve datoteke", "*.*")])
        if not putanja:
            return
        try:
            with open(putanja, "r", encoding="utf-8") as f:
                podaci = json.load(f)
            ime = podaci.get("ucenik_ime") or os.path.basename(putanja)
            podaci["_ucenik_ip"] = None
            otvori_bookify_ucenik(podaci, ime, self.root)
            self._log(f"Učitan rad: {os.path.basename(putanja)}")
        except Exception as e:
            messagebox.showerror("Greška", f"Nije moguće učitati fajl:\n{e}")



# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Pokreni lokalni HTTP server (za učenike na istoj mreži)
    t = threading.Thread(target=pokreni_http_server, daemon=True)
    t.start()
    time.sleep(0.5)

    root = tk.Tk()
    app  = ProfesorApp(root)

    ip = get_local_ip()
    messagebox.showinfo(
        "✅ Server pokrenut!",
        f"Bookify Server je aktivan!\n\n"
        f"Kod učionice:\n"
        f"► {CLASSROOM_CODE}\n\n"
        f"Unesite ovaj kod učenicima JEDNOM.\n"
        f"Nakon toga se automatski spajaju — i iz škole i od kuće!"
    )
    # Vrati fokus na textarea nakon što se messagebox zatvori
    app.zadatak_text.focus_set()
    root.mainloop()
