#!/usr/bin/env python3
"""Teken een weekrooster als PNG, om mee te sturen als ntfy-bijlage.

Apart gehouden van somtoday_rooster.py zodat dat script stdlib-only blijft:
alleen wie een plaatje wil heeft Pillow nodig.

    python3 rooster_plaatje.py state.json 2026-38 rooster.png
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys

from PIL import Image, ImageDraw, ImageFont

# --- vormgeving -------------------------------------------------------------
BREEDTE_KOLOM = 210
HOOGTE_RIJ = 74
MARGE = 24
KOP_HOOGTE = 76
UURKOLOM = 46

ACHTERGROND = (252, 252, 250)
RASTER = (226, 226, 222)
TEKST = (28, 28, 30)
GEDEMPT = (120, 120, 124)
VERVALT_VLAK = (253, 235, 235)
VERVALT_TEKST = (186, 52, 52)
GEWIJZIGD_BALK = (232, 152, 42)
TOETS_VLAK = (234, 242, 252)
TOETS_TEKST = (34, 96, 168)

DAGEN = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag"]

FONT_PADEN = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
]
FONT_VET_PADEN = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


def _font(grootte: int, vet: bool = False):
    for pad in (FONT_VET_PADEN if vet else FONT_PADEN):
        if os.path.exists(pad):
            try:
                return ImageFont.truetype(pad, grootte)
            except OSError:
                continue
    return ImageFont.load_default()


def _kort(tekenaar, tekst: str, font, maxbreedte: int) -> str:
    """Kap tekst af op pixelbreedte, met een beletselteken."""
    if tekenaar.textlength(tekst, font=font) <= maxbreedte:
        return tekst
    while tekst and tekenaar.textlength(tekst + "…", font=font) > maxbreedte:
        tekst = tekst[:-1]
    return tekst + "…"


def _korte_wijziging(tekst: str) -> str:
    """Breng de zin van de API terug tot wat in een cel past.

    "De les is verplaatst (komt van ma 9:00u)." -> "verplaatst van 9:00"
    "Het lokaal is gewijzigd (was zh104)."      -> "was zh104"
    """
    t = tekst.strip().rstrip(".")
    if m := re.search(r"verplaatst.*?van\s+\w+\s*([0-9]{1,2}[:.][0-9]{2})", t, re.I):
        return f"verplaatst van {m.group(1).replace('.', ':')}"
    if m := re.search(r"lokaal.*?was\s+(\S+?)\)?$", t, re.I):
        return f"was {m.group(1)}"
    if "verplaatst" in t.lower():
        return "verplaatst"
    return t


def teken_week(snapshot: dict, jaarweek: str, uitvoer: str) -> str:
    """Teken een weeksnapshot (uit state.json) naar een PNG."""
    lessen = list(snapshot.get("lessen", {}).values())
    huiswerk = list(snapshot.get("huiswerk", {}).values())
    if not lessen:
        raise SystemExit(f"Geen lessen in {jaarweek}.")

    jaar, week = (int(x) for x in jaarweek.split("-"))
    maandag = dt.date.fromisocalendar(jaar, week, 1)

    # Toetsen en huiswerk per begintijd, om een badge in de cel te zetten.
    merk_op_tijd: dict[str, bool] = {}
    for h in huiswerk:
        tijd = (h.get("datumTijd") or "")[:16]
        is_toets = h.get("type") in ("TOETS", "GROTE_TOETS")
        merk_op_tijd[tijd] = merk_op_tijd.get(tijd, False) or is_toets

    uren = sorted({l.get("lesuur") for l in lessen if l.get("lesuur")})
    if not uren:
        uren = [1]
    rij_van_uur = {u: i for i, u in enumerate(uren)}

    breedte = MARGE * 2 + UURKOLOM + BREEDTE_KOLOM * 5
    hoogte = MARGE * 2 + KOP_HOOGTE + HOOGTE_RIJ * len(uren)
    afbeelding = Image.new("RGB", (breedte, hoogte), ACHTERGROND)
    t = ImageDraw.Draw(afbeelding)

    f_titel = _font(30, vet=True)
    f_dag = _font(17, vet=True)
    f_vak = _font(17, vet=True)
    f_klein = _font(14)
    f_mini = _font(12, vet=True)

    t.text((MARGE, MARGE - 2), f"Week {week}", font=f_titel, fill=TEKST)
    zondag = maandag + dt.timedelta(days=6)
    t.text((MARGE + t.textlength(f"Week {week}", font=f_titel) + 14, MARGE + 9),
           f"{maandag:%d-%m} t/m {zondag:%d-%m}", font=f_klein, fill=GEDEMPT)

    top = MARGE + KOP_HOOGTE
    links = MARGE + UURKOLOM

    for k, dag in enumerate(DAGEN):
        x = links + k * BREEDTE_KOLOM
        datum = maandag + dt.timedelta(days=k)
        t.text((x + 10, top - 30), dag.capitalize(), font=f_dag, fill=TEKST)
        t.text((x + 10 + t.textlength(dag.capitalize(), font=f_dag) + 8, top - 28),
               f"{datum:%d-%m}", font=f_klein, fill=GEDEMPT)

    # Rasterlijnen
    for i in range(len(uren) + 1):
        y = top + i * HOOGTE_RIJ
        t.line([(MARGE, y), (breedte - MARGE, y)], fill=RASTER, width=1)
    for k in range(6):
        x = links + k * BREEDTE_KOLOM
        t.line([(x, top), (x, top + HOOGTE_RIJ * len(uren))], fill=RASTER, width=1)

    for u, i in rij_van_uur.items():
        t.text((MARGE + 14, top + i * HOOGTE_RIJ + HOOGTE_RIJ // 2 - 9),
               str(u), font=f_dag, fill=GEDEMPT)

    for les in lessen:
        begin = les.get("begin") or ""
        try:
            wanneer = dt.datetime.fromisoformat(begin[:19])
        except ValueError:
            continue
        kolom = wanneer.weekday()
        if kolom > 4 or les.get("lesuur") not in rij_van_uur:
            continue
        rij = rij_van_uur[les["lesuur"]]
        x = links + kolom * BREEDTE_KOLOM
        y = top + rij * HOOGTE_RIJ

        wijziging = (les.get("wijziging") or "").strip()
        vervalt = "vervalt" in wijziging.lower()
        merk = merk_op_tijd.get(begin[:16])

        if vervalt:
            t.rectangle([x + 1, y + 1, x + BREEDTE_KOLOM - 1, y + HOOGTE_RIJ - 1],
                        fill=VERVALT_VLAK)
        elif merk is not None:
            t.rectangle([x + 1, y + 1, x + BREEDTE_KOLOM - 1, y + HOOGTE_RIJ - 1],
                        fill=TOETS_VLAK)
        if wijziging and not vervalt:
            t.rectangle([x + 1, y + 1, x + 5, y + HOOGTE_RIJ - 1], fill=GEWIJZIGD_BALK)

        kleur = VERVALT_TEKST if vervalt else TEKST
        vak = _kort(t, les.get("vak") or "?", f_vak, BREEDTE_KOLOM - 24)
        t.text((x + 12, y + 10), vak, font=f_vak, fill=kleur)
        if vervalt:
            eind = x + 12 + t.textlength(vak, font=f_vak)
            t.line([(x + 12, y + 20), (eind, y + 20)], fill=VERVALT_TEKST, width=2)

        onder = " ".join(x for x in (les.get("lokaal"), les.get("docent")) if x)
        t.text((x + 12, y + 33), _kort(t, onder, f_klein, BREEDTE_KOLOM - 24),
               font=f_klein, fill=VERVALT_TEKST if vervalt else GEDEMPT)

        if vervalt:
            t.text((x + 12, y + 51), "VERVALT", font=f_mini, fill=VERVALT_TEKST)
        elif merk is True:
            t.text((x + 12, y + 51), "TOETS", font=f_mini, fill=TOETS_TEKST)
        elif merk is False:
            t.text((x + 12, y + 51), "HUISWERK", font=f_mini, fill=TOETS_TEKST)
        elif wijziging:
            t.text((x + 12, y + 51),
                   _kort(t, _korte_wijziging(wijziging), f_mini, BREEDTE_KOLOM - 24),
                   font=f_mini, fill=GEWIJZIGD_BALK)

    afbeelding.save(uitvoer, "PNG", optimize=True)
    return uitvoer


def main() -> None:
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    statepad, jaarweek, uitvoer = sys.argv[1:]
    with open(statepad) as fh:
        weken = json.load(fh)["weken"]
    if jaarweek not in weken:
        raise SystemExit(f"{jaarweek} zit niet in {statepad}. Beschikbaar: {', '.join(sorted(weken))}")
    pad = teken_week(weken[jaarweek], jaarweek, uitvoer)
    print(f"{pad} ({os.path.getsize(pad)} bytes)")


if __name__ == "__main__":
    main()
