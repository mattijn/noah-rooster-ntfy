#!/usr/bin/env python3
"""Teken roosterwijzigingen als 'patch notes'-kaart voor in de melding.

Niet het rooster zelf: dat kent hij al. Alleen wat er verandert, in de taal van
een game-update. Uitval is buit, een toets is een boss met een aftelteller, en
er zit elke dag een andere challenge in zodat het de moeite blijft om te kijken.

Opgebouwd als HTML en met headless Chrome naar PNG geschreven. Dat is makkelijker
te vormgeven dan pixels tekenen, en je krijgt kleuren-emoji gratis. Chrome staat
zowel op de Mac als op een GitHub-runner al geinstalleerd; Playwright gebruikt
die via channel="chrome" en hoeft er dus geen te downloaden.
"""

from __future__ import annotations

import datetime as dt
import html
import os
import random
import tempfile

BREEDTE = 880

# --- willekeur --------------------------------------------------------------
# Eén regel commentaar en één challenge per dag; het zaad is de datum, dus
# binnen een dag blijft het gelijk maar morgen is het weer anders.
FLAVOUR = [
    "De roostergoden hebben gesproken.",
    "Ergens huilt een docent. Niet jouw probleem.",
    "Dit stond niet in de patch notes van vorige week.",
    "Rooster.exe heeft iets doms gedaan.",
    "Kans dat dit morgen weer anders is: aanzienlijk.",
    "Zelfs de conciërge wist dit nog niet.",
    "Screenshot dit voordat het weer wijzigt.",
    "Deze update is niet getest door de school.",
]

CHALLENGES = [
    "Zeg vandaag drie keer 'uiteraard' tegen een docent.",
    "Tel hoe vaak iemand 'even snel' zegt in de les.",
    "Schrijf je aantekeningen vandaag in hoofdletters. Alles.",
    "Wees de eerste die in het lokaal zit. Zeg er niets over.",
    "Leer één woord Duits dat je nooit nodig hebt.",
    "Zoek uit wat het oudste ding in je klaslokaal is.",
    "Maak vandaag een aantekening die je morgen nog snapt.",
    "Vraag een docent wat zijn eerste baan was.",
    "Onthoud het lokaalnummer van al je lessen. Zonder kijken.",
    "Doe vandaag alsof je rugzak zwaarder is dan hij is.",
    "Bedenk een betere naam voor het vak 'samen2'.",
    "Tel je stappen tussen het eerste en het laatste lokaal.",
]

VAK_EMOJI = [
    ("wiskund", "📐"), ("nederland", "📖"), ("engel", "🇬🇧"), ("duits", "🥨"),
    ("frans", "🥐"), ("biolog", "🧬"), ("aardrijk", "🌍"), ("geschied", "🏛️"),
    ("muziek", "🎵"), ("drama", "🎭"), ("handvaardig", "✂️"), ("tekenen", "🎨"),
    ("techn", "🔧"), ("godsdienst", "🕯️"), ("lichamelijke", "🏃"), ("mentor", "🧭"),
    ("natuur", "🔬"), ("schei", "⚗️"), ("gym", "🏃"), ("zorg", "🩹"),
]


def _emoji(vak: str) -> str:
    k = (vak or "").lower()
    for sleutel, teken in VAK_EMOJI:
        if sleutel in k:
            return teken
    return "📚"


RANGEN = [(220, "LEGENDARY", "#c6f24e"), (120, "EPIC", "#f24e9e"),
          (60, "RARE", "#4ee1f2"), (0, "COMMON", "#8a92a0")]


def _rang(minuten: int) -> tuple[str, str]:
    for drempel, naam, kleur in RANGEN:
        if minuten >= drempel:
            return naam, kleur
    return RANGEN[-1][1], RANGEN[-1][2]


def _aftel(dagen: int) -> str:
    return {0: "VANDAAG", 1: "MORGEN"}.get(dagen, f"{dagen} DAGEN")


# --- html -------------------------------------------------------------------
CSS = """
* { margin:0; padding:0; box-sizing:border-box; }
body {
  width:880px; background:#0d0f14; color:#f3f5f8;
  font-family:-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  -webkit-font-smoothing:antialiased;
}
.kaart { padding:40px 44px 36px; position:relative; overflow:hidden; }
.gloed { position:absolute; top:-260px; right:-160px; width:560px; height:560px;
  background:radial-gradient(circle,rgba(198,242,78,.16),transparent 65%); }
.kop { display:flex; align-items:baseline; justify-content:space-between;
  border-bottom:2px solid #202630; padding-bottom:16px; margin-bottom:26px;
  position:relative; }
.kop h1 { font-size:25px; letter-spacing:.14em; font-weight:800; }
.kop .versie { font-size:19px; color:#6e7686; font-weight:700;
  font-variant-numeric:tabular-nums; }

.held { background:linear-gradient(135deg,#1a2410,#171b23 60%);
  border:1px solid #2c3a18; border-radius:22px; padding:26px 30px 24px;
  margin-bottom:26px; position:relative; }
.badge { display:inline-block; font-size:13px; font-weight:800; letter-spacing:.12em;
  padding:6px 13px; border-radius:20px; color:#0d0f14; }
.held .cijfer { font-size:92px; font-weight:800; line-height:1.05; margin-top:10px;
  background:linear-gradient(92deg,#c6f24e,#4ee1f2); -webkit-background-clip:text;
  -webkit-text-fill-color:transparent; letter-spacing:-.03em; }
.held .cijfer small { font-size:40px; font-weight:800; letter-spacing:0; }
.held .wat { color:#9aa3b2; font-size:19px; margin-top:8px; line-height:1.5; }

h2 { font-size:13px; letter-spacing:.18em; color:#6e7686; font-weight:800;
  margin:0 0 12px 2px; }
.rij { display:flex; align-items:center; gap:16px; background:#171b23;
  border-radius:14px; padding:14px 20px; margin-bottom:10px;
  border-left:5px solid #8a92a0; }
.rij .ikoon { font-size:26px; width:32px; text-align:center; }
.rij .mid { flex:1; min-width:0; }
.rij .titel { font-size:21px; font-weight:700; }
.rij .sub { font-size:15px; color:#8a92a0; margin-top:3px;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.rij .rechts { font-size:16px; font-weight:700; text-align:right; white-space:nowrap; }

.boss .tel { font-size:30px; font-weight:800; letter-spacing:-.01em; }
.balk { height:5px; border-radius:3px; background:#232935; margin-top:8px;
  width:150px; margin-left:auto; overflow:hidden; }
.balk span { display:block; height:100%; border-radius:3px; }

.voet { border-top:2px solid #202630; margin-top:26px; padding-top:20px; }
.voet .flavour { color:#6e7686; font-size:16px; font-style:italic; }
.uitdaging { display:flex; gap:14px; align-items:center; margin-top:16px;
  background:#141821; border:1px dashed #2b3240; border-radius:14px; padding:14px 18px; }
.uitdaging .dobbel { font-size:24px; }
.uitdaging .tekst { font-size:16px; color:#c3cad6; }
.uitdaging .tekst b { display:block; font-size:12px; letter-spacing:.16em;
  color:#6e7686; margin-bottom:3px; }
"""


def _rij(ikoon: str, titel: str, sub: str, rechts: str, kleur: str,
         extra: str = "", klasse: str = "") -> str:
    return f"""<div class="rij {klasse}" style="border-left-color:{kleur}">
  <div class="ikoon">{ikoon}</div>
  <div class="mid">
    <div class="titel">{html.escape(titel)}</div>
    <div class="sub">{html.escape(sub)}</div>
  </div>
  <div class="rechts" style="color:{kleur}">{rechts}{extra}</div>
</div>"""


def bouw_html(winst, verschuivingen, bosses, week, rnd) -> str:
    minuten = sum(w.get("minuten", 50) for w in winst)
    rang, rangkleur = _rang(minuten)
    delen = [f'<div class="kaart"><div class="gloed"></div>',
             f'<div class="kop"><h1>ROOSTER UPDATE</h1>'
             f'<div class="versie">week {week} · v{rnd.randint(1, 9)}</div></div>']

    if winst:
        vakken = " · ".join(f"{_emoji(w['vak'])} {w['vak']} {w['wanneer']}" for w in winst[:4])
        if len(winst) > 4:
            vakken += f" · +{len(winst) - 4}"
        delen.append(f"""<div class="held">
  <span class="badge" style="background:{rangkleur}">{rang} DROP</span>
  <div class="cijfer">+{minuten}<small> MIN</small></div>
  <div class="wat">{html.escape(vakken)}</div>
</div>""")

    if verschuivingen:
        delen.append("<h2>GEWIJZIGD</h2>")
        for v in verschuivingen:
            delen.append(_rij(_emoji(v["vak"]), v["vak"], v["wanneer"],
                              html.escape(v["wat"]), "#f2b04e"))

    if bosses:
        delen.append("<h2>INCOMING</h2>")
        for b in bosses:
            kleur = "#f2564e" if b["toets"] else "#4ee1f2"
            label = "🔥 BOSS" if b["toets"] else "📌 QUEST"
            # Hoe dichterbij, hoe voller de balk.
            vol = max(6, min(100, int(100 - (b["dagen"] / 14) * 100)))
            balk = (f'<div class="balk"><span style="width:{vol}%;'
                    f'background:{kleur}"></span></div>')
            delen.append(_rij(label.split()[0], b["vak"],
                              f"{label.split()[1]} · {b['onderwerp']}",
                              f'<div class="tel">{_aftel(b["dagen"])}</div>',
                              kleur, balk, "boss"))

    delen.append(f"""<div class="voet">
  <div class="flavour">{html.escape(rnd.choice(FLAVOUR))}</div>
  <div class="uitdaging"><div class="dobbel">🎲</div>
    <div class="tekst"><b>CHALLENGE VAN DE DAG</b>{html.escape(rnd.choice(CHALLENGES))}</div>
  </div>
</div></div>""")

    return (f"<!doctype html><meta charset='utf-8'><style>{CSS}</style>"
            f"<body>{''.join(delen)}</body>")


# --- naar png ---------------------------------------------------------------
def teken_kaart(winst, verschuivingen, bosses, week, uitvoer, zaad=None) -> str:
    """Render de kaart naar PNG. Geeft het pad terug.

    winst          lessen die vervallen: {"vak", "wanneer", "minuten"}
    verschuivingen overige wijzigingen:  {"vak", "wanneer", "wat"}
    bosses         toetsen/huiswerk:     {"vak", "onderwerp", "dagen", "toets"}
    """
    from playwright.sync_api import sync_playwright

    rnd = random.Random(zaad or dt.date.today().isoformat())
    doc = bouw_html(winst, verschuivingen, bosses, week, rnd)

    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(doc)
        pad = fh.name
    try:
        with sync_playwright() as pw:
            # Systeem-Chrome; valt terug op een meegeleverde chromium.
            try:
                browser = pw.chromium.launch(channel="chrome")
            except Exception:
                browser = pw.chromium.launch()
            pagina = browser.new_page(viewport={"width": BREEDTE, "height": 600},
                                      device_scale_factor=2)
            pagina.goto(f"file://{pad}")
            pagina.screenshot(path=uitvoer, full_page=True)
            browser.close()
    finally:
        os.unlink(pad)
    return uitvoer


DEMO = dict(
    winst=[{"vak": "techniek", "wanneer": "di 2e", "minuten": 60},
           {"vak": "muziek", "wanneer": "wo 5e", "minuten": 60},
           {"vak": "mentor", "wanneer": "vr 2e", "minuten": 60},
           {"vak": "lichamelijke opvoeding", "wanneer": "vr 6e", "minuten": 60}],
    verschuivingen=[{"vak": "aardrijkskunde", "wanneer": "di 3e uur", "wat": "lokaal was zh104"},
                    {"vak": "godsdienst", "wanneer": "ma 5e uur", "wat": "verplaatst van 13:55"}],
    bosses=[{"vak": "handvaardigheid", "onderwerp": "Toets theorie: Vorm", "dagen": 0, "toets": True},
            {"vak": "Duitse taal", "onderwerp": "Mini SO", "dagen": 1, "toets": False},
            {"vak": "wiskunde", "onderwerp": "Toets hoofdstuk 1", "dagen": 8, "toets": True}],
    week=38)

if __name__ == "__main__":
    import sys
    pad = teken_kaart(**DEMO, uitvoer=(sys.argv[1] if len(sys.argv) > 1 else "kaart.png"),
                      zaad="demo-3")
    print(f"{pad} ({os.path.getsize(pad)} bytes)")
