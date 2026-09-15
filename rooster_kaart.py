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

from vakiconen import icoon_voor, klein, roepnaam

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
body{width:%dpx;background:#fff;color:#17191d;padding:18px 16px 20px;
 font-family:-apple-system,"Segoe UI",Roboto,Arial,sans-serif;-webkit-font-smoothing:antialiased}
/* Geen titel, en de datum niet rechtsboven: daar zet de ntfy-app zijn eigen
   icoon overheen. Hij staat nu rechts in het startblok. */
.datum{font-size:12px;color:#9a9a92;font-weight:600;margin-left:auto;
 align-self:center;flex:none}

.start{margin-top:0;border:1.5px solid #e6e6e6;background:#fff;
 border-radius:16px;padding:13px 16px 14px}
.start .label{font-size:10.5px;letter-spacing:.15em;font-weight:800;color:#6a7280}
/* Twee kolommen: de tijd links, de details ernaast. Onder elkaar bleef er
   een halve kaart wit over. */
.start .rij2{display:flex;align-items:center;gap:14px;margin-top:3px}
.start .tijd{font-size:46px;font-weight:800;letter-spacing:-.03em;line-height:1;
 color:#17191d;flex:none}
.start .les{font-size:13px;color:#5f6a75;line-height:1.45;min-width:0}
.start .les b{display:block;font-weight:700;color:#3a4049;font-size:14px}
.start .reden{display:inline-block;margin-top:10px;font-size:12.5px;font-weight:700;
 color:#a8481f;background:#fbeee6;border-radius:8px;padding:5px 10px}

h2{font-size:10.5px;letter-spacing:.15em;font-weight:800;color:#8e8e86;margin:16px 0 4px}
.rij{display:flex;align-items:center;gap:11px;padding:9px 0;border-bottom:1px solid #eeeeee}
.rij:last-child{border-bottom:none}
.chip{width:36px;height:36px;border-radius:11px;flex:none;display:flex;
 align-items:center;justify-content:center;position:relative;overflow:hidden}
.chip .vulling{position:absolute;inset:0;display:flex;align-items:center;
 justify-content:center}
.chip svg{width:19px;height:19px}
.chip{box-shadow:inset 0 0 0 1px rgba(0,0,0,.10)}
.mid{flex:1;min-width:0}
.vak{font-size:16.5px;font-weight:650;line-height:1.2;white-space:nowrap;
 overflow:hidden;text-overflow:ellipsis}
.sub{font-size:12.5px;color:#84847c;margin-top:2px;white-space:nowrap;
 overflow:hidden;text-overflow:ellipsis}
.rechts{text-align:right;white-space:nowrap;flex:none;padding-left:6px}
.rechts .groot{font-size:15px;font-weight:800;line-height:1.15}
.rechts .was{font-size:12px;color:#a6a69e;text-decoration:line-through;display:block;margin-top:1px}
.rechts .klok{font-size:13px;color:#6b6b63;font-weight:700;display:block;
 margin-top:1px;font-variant-numeric:tabular-nums}
.rechts .voor{font-size:11.5px;color:#9a9a92;font-weight:700;display:block;
 letter-spacing:.02em;margin-bottom:1px}
.rechts .tel{font-size:11.5px;color:#9a9a92;font-weight:700;display:block;margin-top:1px}
.uitval .vak{color:#8c8c84;text-decoration:line-through;
 text-decoration-color:#cf4d42;text-decoration-thickness:2px}
.tijdwinst{color:#17191d} .gat{color:#8a8a82}
.d0{color:#17191d} .d1{color:#3f3f3a} .dv{color:#8a8a82}
""" % BREEDTE


# Talen krijgen geen icoon maar een vlag: bij Lucide is er niets dat duits van
# frans onderscheidt, en een vlag herken je zonder nadenken. Opgebouwd uit
# gradients, dus geen plaatjes van buiten.
VLAGGEN = {
    "nederlands": "linear-gradient(#ae1c28 33.3%,#fff 33.3%,#fff 66.6%,#21468b 66.6%)",
    "duits": "linear-gradient(#000 33.3%,#dd0000 33.3%,#dd0000 66.6%,#ffce00 66.6%)",
    "frans": "linear-gradient(to right,#002395 33.3%,#fff 33.3%,#fff 66.6%,#ed2939 66.6%)",
    "engels": ("linear-gradient(transparent 40%,#c8102e 40%,#c8102e 60%,transparent 60%),"
               "linear-gradient(to right,transparent 40%,#c8102e 40%,#c8102e 60%,transparent 60%),"
               "linear-gradient(transparent 28%,#fff 28%,#fff 72%,transparent 72%),"
               "linear-gradient(to right,transparent 28%,#fff 28%,#fff 72%,transparent 72%),"
               "#012169"),
    "spaans": "linear-gradient(#aa151b 25%,#f1bf00 25%,#f1bf00 75%,#aa151b 75%)",
}


def _chip(vak: str) -> str:
    if vlag := VLAGGEN.get(roepnaam(vak)):
        return f'<div class="chip vlag" style="background:{vlag}"></div>'
    # Een vlag vult het hele blokje; een klein icoon op een kleurvlak deed dat
    # niet. Daarom een grote, vervaagde versie van hetzelfde icoon als vulling,
    # met het scherpe icoon eroverheen zodat je nog ziet wat het is.
    _, fg = kleur(vak)
    vorm = icoon_voor(vak)

    def teken(grootte: float, dikte: float, dekking: float) -> str:
        return (f'<svg viewBox="0 0 24 24" fill="none" stroke="#fff" '
                f'stroke-width="{dikte}" stroke-linecap="round" stroke-linejoin="round" '
                f'style="width:{grootte}px;height:{grootte}px;opacity:{dekking}">{vorm}</svg>')

    return (f'<div class="chip" style="background:{fg}">'
            f'<div class="vulling">{teken(56, 1.4, .32)}</div>'
            f'{teken(16, 2.2, 1)}</div>')


def _rij(vak: str, sub: str, rechts: str, klasse: str = "") -> str:
    return (f'<div class="rij {klasse}">{_chip(vak)}<div class="mid">'
            f'<div class="vak">{html.escape(klein(roepnaam(vak)))}</div>'
            f'<div class="sub">{html.escape(klein(sub))}</div></div>'
            f'<div class="rechts">{rechts}</div></div>')


def bouw_html(start: dict | None, uitval: list, gewijzigd: list, toetsen: list,
              vandaag: str) -> str:
    d = []
    datum = f'<div class="datum">{html.escape(klein(vandaag))}</div>' 

    if start:
        if start.get("geen_les"):
            d.append(f'<div class="start"><div class="label">'
                     f'{klein(start["dag"])}</div>'
                     f'<div class="rij2"><div class="tijd">geen les</div>'
                     f'{datum}</div></div>')
        else:
            reden = (f'<div class="reden">{html.escape(klein(start["reden"]))}</div>'
                     if start.get("reden") else "")
            lokaal = f' · {start["lokaal"]}' if start.get("lokaal") else ""
            d.append(f'<div class="start">'
                     f'<div class="label">{klein(start["dag"])} begin je om</div>'
                     f'<div class="rij2"><div class="tijd">{start["tijd"]}</div>'
                     f'<div class="les"><b>{html.escape(klein(roepnaam(start["vak"] or "")))}</b>'
                     f'{start["uur"]}e uur{html.escape(lokaal)}</div>'
                     f'{datum}</div>{reden}</div>')

    if uitval:
        d.append("<h2>uitval</h2>")
        for v in uitval:
            # Een tussenuur betekent niet naar huis: alleen het eerste en het
            # laatste uur veranderen wanneer hij komt of gaat.
            kl = "gat" if v["positie"] == "midden" else "tijdwinst"
            klok = (f'<span class="klok">{html.escape(v["klok"])}</span>'
                    if v.get("klok") else "")
            # Staat er rechts een tijd, dan is dat wanneer hij komt of gaat.
            # De begintijd van de vervallen les erbij zetten leest als een les
            # van tien minuten; het lesuur alleen is genoeg.
            sub = v["dag"] if v.get("klok") else f'{v["dag"]} · {v["tijd"]}'
            d.append(_rij(v["vak"], sub,
                          f'<span class="groot {kl}">{html.escape(klein(v["gevolg"]))}</span>{klok}',
                          "uitval"))

    if gewijzigd:
        d.append("<h2>gewijzigd</h2>")
        for v in gewijzigd:
            was = f'<span class="was">{html.escape(v["was"])}</span>' if v.get("was") else ""
            d.append(_rij(v["vak"], f'{v["label"]} · {v["dag"]}',
                          f'<span class="groot">{html.escape(klein(v["nu"]))}</span>{was}'))

    if toetsen:
        d.append("<h2>toetsen</h2>")
        for t in toetsen:
            k = "d0" if t["dagen"] == 0 else ("d1" if t["dagen"] == 1 else "dv")
            voor = (f'<span class="voor">{html.escape(klein(t["voor"]))}</span>'
                    if t.get("voor") else "")
            # De dagnaam zegt wanneer, de aftelling hoe dichtbij. Bij vandaag en
            # morgen is aftellen overbodig.
            tel = (f'<span class="tel">nog {t["dagen"]} dagen</span>'
                   if t["dagen"] >= 2 else "")
            d.append(_rij(t["vak"], t["wat"],
                          f'{voor}<span class="groot {k}">{html.escape(klein(t["dag"]))}</span>{tel}'))

    return (f"<!doctype html><meta charset='utf-8'><style>{CSS}</style>"
            f"<body>{''.join(d)}</body>")


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


# Demo-data om de opmaak los van de API te renderen (zie kaart-test.yml).
# Let op: alles hoort bij een dag, want dat is wat de kaart toont.
DEMO = dict(
    start={"dag": "morgen", "reden": "1e uur vervalt",
           "tijd": "10:10", "uur": 2, "vak": "wiskunde", "lokaal": "zf101"},
    uitval=[{"vak": "handvaardigheid", "dag": "1e uur", "tijd": "09:00",
             "gevolg": "later beginnen", "klok": "10:10", "positie": "rand"},
            {"vak": "muziek", "dag": "4e uur", "tijd": "12:45",
             "gevolg": "tussenuur", "klok": "12:30-13:55", "positie": "midden"},
            {"vak": "lichamelijke opvoeding", "dag": "6e uur", "tijd": "15:00",
             "gevolg": "eerder uit", "klok": "14:55", "positie": "rand"}],
    gewijzigd=[{"vak": "aardrijkskunde", "dag": "3e uur", "tijd": "11:30",
                "label": "ander lokaal", "nu": "zh005", "was": "zh104"}],
    toetsen=[{"vak": "handvaardigheid", "wat": "Toets theorie: Vorm", "dagen": 0,
              "voor": None, "dag": "vandaag"},
             {"vak": "Duitse taal", "wat": "Mini SO", "dagen": 1,
              "voor": None, "dag": "morgen"},
             {"vak": "Nederlandse taal", "wat": "boekverslag", "dagen": 2,
              "voor": None, "dag": "donderdag"},
             {"vak": "Franse taal", "wat": "SO woorden", "dagen": 3,
              "voor": None, "dag": "vrijdag"},
             {"vak": "Engelse taal", "wat": "toets unit 1", "dagen": 4,
              "voor": None, "dag": "zaterdag"},
             {"vak": "wiskunde", "wat": "Toets hoofdstuk 1", "dagen": 8,
              "voor": "volgende week", "dag": "woensdag"}],
    vandaag="di 15 sep")

if __name__ == "__main__":
    import sys
    pad = teken_kaart(**DEMO, uitvoer=(sys.argv[1] if len(sys.argv) > 1 else "kaart.png"))
    print(f"{pad} ({os.path.getsize(pad)} bytes)")
