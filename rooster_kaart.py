#!/usr/bin/env python3
"""Teken de roosterkaart voor in de melding: een compacte, lichte kaart op
telefoonbreedte.

Bovenaan staat waar het om draait: hoe laat je moet beginnen. Daaronder de
uitval (met wat het betekent: later starten, tussenuur, of eerder klaar), de
gewijzigde lessen (met het nieuwe lokaal of tijdstip voorop) en de toetsen met
een aftelling.

Opgebouwd als HTML en met headless Chrome naar PNG geschreven. Chrome staat
zowel op macOS als op een GitHub-runner al klaar; Playwright gebruikt die via
channel="chrome" en hoeft er dus geen te downloaden.
"""

from __future__ import annotations

import html
import os
import tempfile

from vakiconen import icoon_voor

BREEDTE = 400  # css-punten: ongeveer de breedte van een melding op een telefoon
SCHAAL = 3     # retina

# Zachte vlakken met een donkere lijn erop, stabiel per vak verdeeld over het
# palet (geen hash: die liet de helft op dezelfde kleur uitkomen).
PALET = [("#e8f0fb", "#2c5f9e"), ("#fdeef0", "#a83a52"), ("#eaf6ed", "#2f6b42"),
         ("#fdf3e3", "#9a6415"), ("#f1ecfb", "#5b3fa8"), ("#e6f5f5", "#186a6a"),
         ("#fbeef7", "#96336f")]

VOLGORDE = ["nederlandse taal", "engelse taal", "duitse taal", "franse taal",
            "wiskunde", "biologie", "aardrijkskunde", "geschiedenis", "godsdienst",
            "muziek", "drama", "handvaardigheid", "techniek",
            "lichamelijke opvoeding", "mentor uur", "samen2", "kwt"]


def kleur(vak: str) -> tuple[str, str]:
    k = (vak or "").lower().strip()
    i = VOLGORDE.index(k) if k in VOLGORDE else sum(map(ord, k))
    return PALET[i % len(PALET)]


CSS = """
*{margin:0;padding:0;box-sizing:border-box}
body{width:%dpx;background:#fcfcfa;color:#17191d;padding:18px 16px 20px;
 font-family:-apple-system,"Segoe UI",Roboto,Arial,sans-serif;-webkit-font-smoothing:antialiased}
.kop{display:flex;justify-content:space-between;align-items:baseline;
 padding-bottom:11px;border-bottom:1.5px solid #e7e6e1}
.kop b{font-size:18px;font-weight:800;letter-spacing:-.01em}
.kop span{font-size:12px;color:#8e8e86;font-weight:600}

.start{margin-top:14px;border:1.5px solid;border-radius:16px;padding:14px 16px 15px}
.start .label{font-size:10.5px;letter-spacing:.15em;font-weight:800}
.start .tijd{font-size:52px;font-weight:800;letter-spacing:-.03em;line-height:1.05;margin-top:2px}
.start .les{font-size:13.5px;margin-top:3px}
.start .reden{display:inline-block;margin-top:9px;font-size:12.5px;font-weight:700;
 color:#a8481f;background:#fbeee6;border-radius:8px;padding:5px 10px}
.gewoon{background:#f4f6f8;border-color:#e0e4e8}
.gewoon .label{color:#5a6673} .gewoon .tijd{color:#1a2027} .gewoon .les{color:#5f6a75}
.later{background:#f2f6ee;border-color:#dae5cd}
.later .label{color:#5f7a49} .later .tijd{color:#1d2a15} .later .les{color:#617054}
.eerder{background:#fdf2ec;border-color:#f2d7c4}
.eerder .label{color:#a8481f} .eerder .tijd{color:#3a1d0f} .eerder .les{color:#8a5c3f}

h2{font-size:10.5px;letter-spacing:.15em;font-weight:800;color:#8e8e86;margin:16px 0 4px}
.rij{display:flex;align-items:center;gap:11px;padding:9px 0;border-bottom:1px solid #f1f0ec}
.rij:last-child{border-bottom:none}
.chip{width:36px;height:36px;border-radius:11px;flex:none;display:flex;
 align-items:center;justify-content:center}
.chip svg{width:19px;height:19px}
.mid{flex:1;min-width:0}
.vak{font-size:16.5px;font-weight:650;line-height:1.2;white-space:nowrap;
 overflow:hidden;text-overflow:ellipsis}
.sub{font-size:12.5px;color:#84847c;margin-top:2px;white-space:nowrap;
 overflow:hidden;text-overflow:ellipsis}
.rechts{text-align:right;white-space:nowrap;flex:none;padding-left:6px}
.rechts .groot{font-size:15px;font-weight:800;line-height:1.15}
.rechts .was{font-size:12px;color:#a6a69e;text-decoration:line-through;display:block;margin-top:1px}
.rechts .klok{font-size:13px;color:#6b6b63;font-weight:700;display:block;margin-top:1px}
.uitval .vak{color:#8c8c84;text-decoration:line-through;
 text-decoration-color:#cf4d42;text-decoration-thickness:2px}
.tijdwinst{color:#2f6b42} .gat{color:#8a8a82}
.d0{color:#cf4d42} .d1{color:#b3720f} .dv{color:#5f5f58}
""" % BREEDTE


def _chip(vak: str) -> str:
    bg, fg = kleur(vak)
    svg = (f'<svg viewBox="0 0 24 24" fill="none" stroke="{fg}" stroke-width="2" '
           f'stroke-linecap="round" stroke-linejoin="round">{icoon_voor(vak)}</svg>')
    return f'<div class="chip" style="background:{bg}">{svg}</div>'


def _rij(vak: str, sub: str, rechts: str, klasse: str = "") -> str:
    return (f'<div class="rij {klasse}">{_chip(vak)}<div class="mid">'
            f'<div class="vak">{html.escape(vak)}</div>'
            f'<div class="sub">{html.escape(sub)}</div></div>'
            f'<div class="rechts">{rechts}</div></div>')


def bouw_html(start: dict | None, uitval: list, gewijzigd: list, toetsen: list,
              vandaag: str) -> str:
    d = [f'<div class="kop"><b>Rooster</b><span>{html.escape(vandaag)}</span></div>']

    if start:
        if start.get("geen_les"):
            d.append(f'<div class="start later"><div class="label">'
                     f'{start["dag"].upper()}</div><div class="tijd">geen les</div></div>')
        else:
            reden = (f'<div class="reden">{html.escape(start["reden"])}</div>'
                     if start.get("reden") else "")
            les = f'{start["uur"]}e uur · {start["vak"]}'
            if start.get("lokaal"):
                les += f' · {start["lokaal"]}'
            d.append(f'<div class="start {start.get("soort", "gewoon")}">'
                     f'<div class="label">{start["dag"].upper()} BEGIN JE OM</div>'
                     f'<div class="tijd">{start["tijd"]}</div>'
                     f'<div class="les">{html.escape(les)}</div>{reden}</div>')

    if uitval:
        d.append("<h2>UITVAL</h2>")
        for v in uitval:
            # Een tussenuur betekent niet naar huis: alleen het eerste en het
            # laatste uur veranderen wanneer hij komt of gaat.
            kl = "gat" if v["positie"] == "midden" else "tijdwinst"
            klok = (f'<span class="klok">{html.escape(v["klok"])}</span>'
                    if v.get("klok") else "")
            d.append(_rij(v["vak"], f'{v["dag"]} · {v["tijd"]}',
                          f'<span class="groot {kl}">{html.escape(v["gevolg"])}</span>{klok}',
                          "uitval"))

    if gewijzigd:
        d.append("<h2>GEWIJZIGD</h2>")
        for v in gewijzigd:
            was = f'<span class="was">{html.escape(v["was"])}</span>' if v.get("was") else ""
            d.append(_rij(v["vak"], f'{v["label"]} · {v["dag"]}',
                          f'<span class="groot">{html.escape(v["nu"])}</span>{was}'))

    if toetsen:
        d.append("<h2>TOETSEN</h2>")
        for t in toetsen:
            k = "d0" if t["dagen"] == 0 else ("d1" if t["dagen"] == 1 else "dv")
            d.append(_rij(t["vak"], t["wat"],
                          f'<span class="groot {k}">{aftel(t["dagen"])}</span>'))

    return (f"<!doctype html><meta charset='utf-8'><style>{CSS}</style>"
            f"<body>{''.join(d)}</body>")


def aftel(dagen: int) -> str:
    return {0: "VANDAAG", 1: "MORGEN"}.get(dagen, f"{dagen} DAGEN")


def teken_kaart(start, uitval, gewijzigd, toetsen, vandaag, uitvoer) -> str:
    """Render de kaart naar PNG en geef het pad terug."""
    from playwright.sync_api import sync_playwright

    doc = bouw_html(start, uitval, gewijzigd, toetsen, vandaag)
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(doc)
        pad = fh.name
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(channel="chrome")
            except Exception:
                browser = pw.chromium.launch()
            pagina = browser.new_page(viewport={"width": BREEDTE, "height": 200},
                                      device_scale_factor=SCHAAL)
            pagina.goto(f"file://{pad}")
            # Op het body-element knippen; full_page zou het venster volgen en
            # een lap wit onder de kaart laten staan.
            pagina.locator("body").screenshot(path=uitvoer)
            browser.close()
    finally:
        os.unlink(pad)
    return uitvoer


# Demo-data, zodat de opmaak los van de API te renderen is (zie kaart-test.yml).
DEMO = dict(
    start={"dag": "morgen", "soort": "later", "reden": "1e uur vervalt",
           "tijd": "10:10", "uur": 2, "vak": "wiskunde", "lokaal": "zf101"},
    uitval=[{"vak": "handvaardigheid", "dag": "do", "uur": 1, "tijd": "09:00",
             "gevolg": "later beginnen", "klok": "10:10", "positie": "rand"},
            {"vak": "muziek", "dag": "wo", "uur": 5, "tijd": "13:55",
             "gevolg": "tussenuur", "klok": "", "positie": "midden"},
            {"vak": "lichamelijke opvoeding", "dag": "vr", "uur": 6, "tijd": "15:00",
             "gevolg": "eerder uit", "klok": "14:55", "positie": "rand"}],
    gewijzigd=[{"vak": "aardrijkskunde", "dag": "di", "uur": 3, "tijd": "11:30",
                "label": "ander lokaal", "nu": "zh005", "was": "zh104"},
               {"vak": "godsdienst", "dag": "ma", "uur": 5, "tijd": "13:15",
                "label": "verplaatst", "nu": "13:15", "was": "13:55"}],
    toetsen=[{"vak": "handvaardigheid", "wat": "Toets theorie: Vorm", "dagen": 0},
             {"vak": "Duitse taal", "wat": "Mini SO", "dagen": 1},
             {"vak": "wiskunde", "wat": "Toets hoofdstuk 1", "dagen": 8}],
    vandaag="di 15 sep")

if __name__ == "__main__":
    import sys
    pad = teken_kaart(**DEMO, uitvoer=(sys.argv[1] if len(sys.argv) > 1 else "kaart.png"))
    print(f"{pad} ({os.path.getsize(pad)} bytes)")
