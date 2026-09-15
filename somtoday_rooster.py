#!/usr/bin/env python3
"""Somtoday weekrooster ophalen via de (niet-officiele) REST API.

Alleen stdlib, geen dependencies. Twee stappen:

    python3 somtoday_rooster.py login      # eenmalig: browser-login
    python3 somtoday_rooster.py rooster    # weekrooster tonen

Het rooster komt uit /rest/v1/afspraakitems (niet /rest/v1/afspraken: dat laat
uitgevallen lessen gewoon weg). Toetsen en huiswerk komen uit de studiewijzer-
toekenningen en worden op begintijd aan de les gekoppeld.

Het wachtwoord komt nergens in dit script: je logt in je eigen browser in.
SomtodayCallback.app (~/Applications) vangt de `somtoday://` redirect op en
schrijft hem naar .callback_url; het script pikt dat vanzelf op. Staat die app
er niet, dan valt `login` terug op handmatig plakken.

Opgeslagen wordt uitsluitend het (roterende) refresh token, in tokens.json.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

# --- constanten (zelfde publieke app-client als de Somtoday-app) ------------
AUTHORIZE_URL = "https://inloggen.somtoday.nl/oauth2/authorize"
TOKEN_URL = "https://inloggen.somtoday.nl/oauth2/token"
CLIENT_ID = "somtoday-leerling-native"
REDIRECT_URI = "somtoday://nl.topicus.somtoday.leerling/oauth/callback"
SCOPE = "openid"
SESSION = "no_session"
DEFAULT_API_URL = "https://api.somtoday.nl"

PKCE_CHARSET = "abcdefghijklmnopqrstuvwxyz123456789"
_HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(_HERE, "tokens.json")
# SomtodayCallback.app registreert de somtoday:// scheme en schrijft de
# binnenkomende redirect hiernaartoe.
CALLBACK_FILE = os.path.join(_HERE, ".callback_url")
STATE_FILE = os.path.join(_HERE, "state.json")
CONFIG_FILE = os.path.join(_HERE, "config.json")
HANDLER_APP = os.path.expanduser("~/Applications/SomtodayCallback.app")
TIMEOUT = 30
# Zonder User-Agent blokkeert de edge van Somtoday met HTTP 403.
USER_AGENT = "somtoday-rooster/1.0 (python-urllib)"

_CODE_RE = re.compile(r"[?&]code=([^&\s\"'<>;,]+)")
_STATE_RE = re.compile(r"[?&]state=([^&\s\"'<>;,]+)")


# --- kleine HTTP helpers ----------------------------------------------------
def _request(url: str, *, data: dict | None = None, headers: dict | None = None):
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), err.read()


def _json_request(url: str, **kwargs) -> Any:
    status, _headers, raw = _request(url, **kwargs)
    if status >= 400:
        raise SystemExit(f"HTTP {status} bij {url}\n{raw.decode('utf-8', 'replace')[:500]}")
    return json.loads(raw)


# --- tokenopslag ------------------------------------------------------------
def load_tokens() -> dict:
    """Lees de tokens van schijf, of uit $SOMTODAY_TOKENS (voor CI).

    In CI bestaat er geen tokens.json; de inhoud komt dan uit een secret. We
    schrijven die eerst naar schijf, zodat save_tokens() verder niets hoeft te
    weten van waar ze vandaan kwamen.
    """
    uit_omgeving = os.environ.get("SOMTODAY_TOKENS")
    if uit_omgeving and not os.path.exists(TOKEN_FILE):
        save_tokens(json.loads(uit_omgeving))
    if not os.path.exists(TOKEN_FILE):
        raise SystemExit("Nog niet ingelogd. Draai eerst: python3 somtoday_rooster.py login")
    with open(TOKEN_FILE) as fh:
        return json.load(fh)


def save_tokens(tokens: dict) -> None:
    with open(TOKEN_FILE, "w") as fh:
        json.dump(tokens, fh, indent=2)
    os.chmod(TOKEN_FILE, stat.S_IRUSR | stat.S_IWUSR)  # 0600


# --- OAuth2 + PKCE ----------------------------------------------------------
def extract_code(pasted: str, expected_state: str) -> str:
    """Haal de authorization code uit een geplakte redirect-URL of losse code."""
    match = _CODE_RE.search(pasted)
    if match:
        if "session_state=" in pasted:
            raise SystemExit(
                "Dit is de tussenstap van je school-SSO, niet de eind-redirect.\n"
                "Je hebt de URL nodig die met 'somtoday://' begint."
            )
        state_match = _STATE_RE.search(pasted)
        if state_match and urllib.parse.unquote(state_match.group(1)) != expected_state:
            raise SystemExit("De state komt niet overeen; dit is een oude of vreemde redirect.")
        return urllib.parse.unquote(match.group(1))
    if pasted and not any(c in pasted for c in ":/= "):
        return pasted
    raise SystemExit("Geen bruikbare authorization code gevonden.")


def wait_for_callback(expected_state: str, timeout: int = 180) -> str:
    """Wacht tot SomtodayCallback.app de redirect heeft weggeschreven."""
    print(f"Wachten op de somtoday:// callback (max {timeout}s) ", end="", flush=True)
    for _ in range(timeout):
        if os.path.exists(CALLBACK_FILE):
            with open(CALLBACK_FILE) as fh:
                pasted = fh.read().strip()
            os.remove(CALLBACK_FILE)  # de code is eenmalig; niet laten slingeren
            print(" ontvangen.")
            return extract_code(pasted, expected_state)
        time.sleep(1)
        print(".", end="", flush=True)
    raise SystemExit(
        "\nGeen callback ontvangen. Staat SomtodayCallback.app nog in ~/Applications?"
    )


def do_login() -> None:
    verifier = "".join(secrets.choice(PKCE_CHARSET) for _ in range(128))
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    state = secrets.token_urlsafe(24)

    params = {
        "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID,
        "response_type": "code",
        "scope": SCOPE,
        "session": SESSION,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    url = f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"

    if os.path.exists(CALLBACK_FILE):
        os.remove(CALLBACK_FILE)

    if os.path.isdir(HANDLER_APP):
        # SomtodayCallback.app vangt de somtoday:// redirect op en schrijft
        # hem naar CALLBACK_FILE; we hoeven alleen te wachten.
        print("Browser wordt geopend. Log in met je ouder-account.")
        subprocess.run(["open", url], check=True)
        code = wait_for_callback(state)
    else:
        print(
            "SomtodayCallback.app niet gevonden; terugval op handmatig plakken.\n"
            "Open deze URL in je browser met DevTools open (Network, preserve log):\n"
        )
        print(url, "\n")
        pasted = input("Plak de somtoday://...?code=... URL (of de code): ").strip()
        code = extract_code(pasted, state)

    payload = _json_request(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPE,
            "session": SESSION,
        },
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )

    save_tokens(
        {
            "refresh_token": payload["refresh_token"],
            "api_url": payload.get("somtoday_api_url") or DEFAULT_API_URL,
            "tenant": payload.get("somtoday_tenant"),
        }
    )
    print(f"\nGelukt. Refresh token opgeslagen in {TOKEN_FILE}")
    print("Nu: python3 somtoday_rooster.py rooster")


def refresh_access_token(tokens: dict) -> tuple[str, str]:
    payload = _json_request(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": CLIENT_ID,
            "scope": SCOPE,
        },
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    # Het refresh token roteert: altijd het nieuwe terugschrijven.
    tokens["refresh_token"] = payload.get("refresh_token", tokens["refresh_token"])
    tokens["api_url"] = payload.get("somtoday_api_url") or tokens.get("api_url") or DEFAULT_API_URL
    save_tokens(tokens)
    return payload["access_token"], tokens["api_url"]


# --- API --------------------------------------------------------------------
def api_get(api_url: str, token: str, path: str, params=None, page=False) -> Any:
    url = f"{api_url}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    if not page:
        return _json_request(url, headers=headers)

    items: list = []
    offset, size = 0, 100
    for _ in range(50):
        status, hdrs, raw = _request(url, headers={**headers, "Range": f"items={offset}-{offset + size - 1}"})
        if status >= 400:
            raise SystemExit(f"HTTP {status} bij {url}\n{raw.decode('utf-8', 'replace')[:500]}")
        payload = json.loads(raw)
        batch = payload.get("items", payload) if isinstance(payload, dict) else payload
        items.extend(batch)
        if status == 200 or len(batch) < size:
            break
        offset += size
    return items


def week_bounds(offset_weeks: int = 0) -> tuple[dt.date, dt.date]:
    today = dt.date.today()
    monday = today - dt.timedelta(days=today.weekday()) + dt.timedelta(weeks=offset_weeks)
    return monday, monday + dt.timedelta(days=6)


def _hhmm(value: str | None) -> str:
    if not value:
        return "     "
    try:
        return dt.datetime.fromisoformat(value).strftime("%H:%M")
    except ValueError:
        return value[11:16]


def haal_huiswerk(api_url: str, token: str, leerling_id: int, jaar: int, week: int) -> list[dict]:
    """Haal de studiewijzer-toekenningen (huiswerk en toetsen) van een week op.

    Drie endpoints: aan een les gekoppeld, aan een dag, en aan de hele week.
    Alleen de eerste heeft een tijdstip waarop we kunnen koppelen.
    """
    basis = [("geenDifferentiatieOfGedifferentieerdVoorLeerling", leerling_id)]
    resultaat: list[dict] = []
    for endpoint, param, waarde in (
        ("studiewijzeritemafspraaktoekenningen", "jaarWeek", f"{jaar}~{week}"),
        ("studiewijzeritemdagtoekenningen", "jaarWeek", f"{jaar}~{week}"),
        ("studiewijzeritemweektoekenningen", "weeknummer", str(week)),
    ):
        try:
            items = api_get(
                api_url, token, f"/rest/v1/{endpoint}",
                params=basis + [(param, waarde), ("additional", "studiewijzerItem")],
                page=True,
            )
        except SystemExit:
            continue  # niet elke school gebruikt elk type toekenning
        for x in items:
            x["_bron"] = endpoint
        resultaat.extend(items)
    return resultaat


def huiswerk_label(item: dict) -> tuple[str, str]:
    """Geef (markering, tekst) voor een huiswerk- of toetsitem."""
    swi = item.get("studiewijzerItem") or {}
    soort = swi.get("huiswerkType") or "HUISWERK"
    onderwerp = (swi.get("onderwerp") or "").strip() or "(geen onderwerp)"
    # Let op: docenten voeren een SO regelmatig in als HUISWERK in plaats van
    # TOETS, dus op het type alleen kun je niet afgaan.
    markering = {"GROTE_TOETS": "TOETS", "TOETS": "TOETS"}.get(soort, "huiswerk")
    return markering, onderwerp


def show_rooster(offset_weeks: int, as_json: bool, student_filter: str | None) -> None:
    tokens = load_tokens()
    token, api_url = refresh_access_token(tokens)

    students = api_get(api_url, token, "/rest/v1/leerlingen")
    students = students.get("items", []) if isinstance(students, dict) else students
    if not students:
        raise SystemExit("Geen leerlingen gevonden op dit account.")

    def naam(s: dict) -> str:
        return " ".join(x for x in (s.get("roepnaam"), s.get("tussenvoegsel"), s.get("achternaam")) if x)

    if student_filter:
        students = [s for s in students if student_filter.lower() in naam(s).lower()]
        if not students:
            raise SystemExit(f"Geen leerling gevonden die matcht op '{student_filter}'.")
    leerling = students[0]
    leerling_id = leerling["links"][0]["id"]

    start, end = week_bounds(offset_weeks)
    jaar, week, _ = start.isocalendar()
    items = api_get(api_url, token, f"/rest/v1/afspraakitems/{leerling_id}/jaar/{jaar}/week/{week}")
    items = items.get("items", items) if isinstance(items, dict) else items
    huiswerk = haal_huiswerk(api_url, token, leerling_id, jaar, week)

    # Koppel huiswerk aan een les op begintijd (tot op de minuut).
    per_tijd: dict[str, list[dict]] = {}
    losse: list[dict] = []
    for h in huiswerk:
        tijd = (h.get("datumTijd") or "")[:16]
        if tijd:
            per_tijd.setdefault(tijd, []).append(h)
        else:
            losse.append(h)

    if as_json:
        print(json.dumps(
            {"leerling": naam(leerling), "jaar": jaar, "week": week,
             "items": items, "huiswerk": huiswerk},
            indent=2, ensure_ascii=False,
        ))
        return

    print(f"\nWeekrooster {naam(leerling)}   week {week}  ({start:%d-%m} t/m {end:%d-%m})")
    print("=" * 78)

    dagen = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]
    items.sort(key=lambda a: (a.get("beginDatumTijd") or "", a.get("beginLesuur") or 0))

    wijzigingen: list[str] = []
    huiswerkregels: list[str] = []
    for i in range(7):
        dag = start + dt.timedelta(days=i)
        vandaag = [a for a in items if (a.get("beginDatumTijd") or "")[:10] == dag.isoformat()]
        if not vandaag and i >= 5:
            continue
        print(f"\n{dagen[i].capitalize()} {dag:%d-%m}")
        if not vandaag:
            print("  (geen lessen)")
            continue
        for a in vandaag:
            vak = (a.get("vak") or {}).get("naam") or a.get("titel") or "?"
            doc = ", ".join(sorted(n.upper() for n in (a.get("docentNamen") or [])))
            lokaal = a.get("locatie") or ""
            uur, eind_uur = a.get("beginLesuur") or "", a.get("eindLesuur") or ""
            uur_txt = f"{uur}-{eind_uur}" if eind_uur and eind_uur != uur else str(uur)
            wijziging = (a.get("wijzigingOmschrijving") or "").strip()
            vervalt = "vervalt" in wijziging.lower()
            toets = a.get("afspraakItemType") == "ROOSTERTOETS"

            bij_les = per_tijd.get((a.get("beginDatumTijd") or "")[:16], [])
            labels = [huiswerk_label(h) for h in bij_les]
            toets = toets or any(m == "TOETS" for m, _ in labels)

            merk = "  !! VERVALT" if vervalt else ("  ** TOETS" if toets else "")
            print(
                f"  {uur_txt:>5}  {_hhmm(a.get('beginDatumTijd'))}-{_hhmm(a.get('eindDatumTijd'))}"
                f"  {vak[:24]:<24} {doc[:10]:<10} {lokaal[:8]:<8}{merk}"
            )
            if wijziging:
                print(f"         > {wijziging}")
                wijzigingen.append(f"{dagen[i][:2]} {dag:%d-%m} uur {uur_txt}  {vak} - {wijziging}")
            for markering, onderwerp in labels:
                print(f"         {'**' if markering == 'TOETS' else '-'} {markering}: {onderwerp}")
                huiswerkregels.append(
                    f"{dagen[i][:2]} {dag:%d-%m} uur {uur_txt}  {vak} - {markering}: {onderwerp}"
                )

    if wijzigingen or huiswerkregels or losse:
        print(f"\n{'-' * 78}")
    if wijzigingen:
        print(f"Wijzigingen deze week ({len(wijzigingen)}):")
        for w in wijzigingen:
            print(f"  {w}")
    if huiswerkregels:
        print(f"\nToetsen en huiswerk ({len(huiswerkregels)}):")
        for h in huiswerkregels:
            print(f"  {h}")
    if losse:
        print(f"\nZonder vast tijdstip ({len(losse)}):")
        for h in losse:
            markering, onderwerp = huiswerk_label(h)
            print(f"  {markering}: {onderwerp}")
    print()


# --- wijzigingsdetectie -----------------------------------------------------
def _snapshot(items: list[dict], huiswerk: list[dict]) -> dict:
    """Comprimeer een week tot de velden waarop we willen vergelijken."""
    lessen = {}
    for a in items:
        sleutel = str(a.get("uniqueIdentifier"))
        lessen[sleutel] = {
            "begin": a.get("beginDatumTijd"),
            "eind": a.get("eindDatumTijd"),
            "lesuur": a.get("beginLesuur"),
            "vak": (a.get("vak") or {}).get("naam") or a.get("titel"),
            "lokaal": a.get("locatie"),
            # De API levert docentNamen niet in een vaste volgorde; sorteren
            # voorkomt meldingen over een wijziging die er niet is.
            "docent": ", ".join(sorted(n.upper() for n in (a.get("docentNamen") or []))),
            "wijziging": (a.get("wijzigingOmschrijving") or "").strip(),
        }
    # Een huiswerkitem noemt alleen de lesgroepcode (ZTH1CHA), niet het vak.
    # Via de begintijd vinden we de bijbehorende les en daarmee de vaknaam.
    vak_op_tijd = {(l["begin"] or "")[:16]: l["vak"] for l in lessen.values()}

    werk = {}
    for h in huiswerk:
        swi = h.get("studiewijzerItem") or {}
        onderwerp = (swi.get("onderwerp") or "").strip()
        tijd = h.get("datumTijd")
        werk[f"{tijd}|{onderwerp}"] = {
            "datumTijd": tijd,
            "type": swi.get("huiswerkType"),
            "onderwerp": onderwerp,
            "vak": vak_op_tijd.get((tijd or "")[:16]) or (h.get("lesgroep") or {}).get("naam"),
        }
    return {"lessen": lessen, "huiswerk": werk}


# Velden waarvan een verandering het melden waard is, met hun label.
GEVOLGD = {
    "wijziging": "melding",
    "begin": "begintijd",
    "lesuur": "lesuur",
    "lokaal": "lokaal",
    "docent": "docent",
    "vak": "vak",
}


def _vergelijk(oud: dict, nieuw: dict, tot: dt.datetime) -> list[str]:
    """Beschrijf wat er veranderd is, beperkt tot lessen die voor 'tot' beginnen."""
    regels: list[str] = []

    def binnen(begin: str | None) -> bool:
        if not begin:
            return False
        try:
            return dt.datetime.fromisoformat(begin[:19]) <= tot
        except ValueError:
            return False

    def wanneer(rec: dict) -> str:
        begin = rec.get("begin") or rec.get("datumTijd") or ""
        try:
            d = dt.datetime.fromisoformat(begin[:19])
        except ValueError:
            return begin[:16]
        dagen = ["ma", "di", "wo", "do", "vr", "za", "zo"]
        return f"{dagen[d.weekday()]} {d:%d-%m %H:%M}"

    oude_lessen, nieuwe_lessen = oud.get("lessen", {}), nieuw.get("lessen", {})
    for sleutel, na in nieuwe_lessen.items():
        if not binnen(na.get("begin")):
            continue
        voor = oude_lessen.get(sleutel)
        if voor is None:
            regels.append(f"NIEUW  {wanneer(na)} uur {na['lesuur']} {na['vak']}"
                          + (f" - {na['wijziging']}" if na["wijziging"] else ""))
            continue
        waar = f"{wanneer(na)} uur {na['lesuur']} {na['vak']}"
        for veld, label in GEVOLGD.items():
            if voor.get(veld) == na.get(veld):
                continue
            was, wordt = voor.get(veld) or "-", na.get(veld) or "-"
            if veld == "wijziging":
                # De API schrijft hier hele zinnen ("Les vervalt", "De les is
                # verplaatst (komt van ma 9:00u)."); die lezen prima als melding.
                if not wordt or wordt == "-":
                    regels.append(f"HERSTELD  {waar} - {was.rstrip('.')} geldt niet meer")
                elif "vervalt" in wordt.lower():
                    regels.append(f"VERVALT  {waar}")
                else:
                    regels.append(f"LET OP  {waar} - {wordt.rstrip('.')}")
            elif veld == "begin":
                regels.append(f"VERZET  {waar} - was {str(was)[11:16]}, wordt {str(wordt)[11:16]}")
            else:
                regels.append(f"LET OP  {waar} - {label} {was} wordt {wordt}")

    for sleutel, voor in oude_lessen.items():
        if binnen(voor.get("begin")) and sleutel not in nieuwe_lessen:
            regels.append(f"WEG  {wanneer(voor)} uur {voor['lesuur']} {voor['vak']}"
                          " - staat niet meer in het rooster")

    oud_werk, nieuw_werk = oud.get("huiswerk", {}), nieuw.get("huiswerk", {})
    for sleutel, na in nieuw_werk.items():
        if sleutel not in oud_werk and binnen(na.get("datumTijd")):
            soort = "TOETS" if na.get("type") in ("TOETS", "GROTE_TOETS") else "huiswerk"
            regels.append(f"NIEUW {soort}  {wanneer(na)} - {na['onderwerp']}")
    return regels


def _headerwaarde(tekst: str) -> str:
    """Maak tekst geschikt voor een HTTP-header: geen regeleinden, latin-1."""
    plat = tekst.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")
    return plat.encode("utf-8").decode("latin-1", "replace")


def stuur_notificatie(titel: str, tekst: str, bijlage: str | None = None) -> None:
    """Push via ntfy. Leest server en topic uit config.json."""
    cfg = {}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as fh:
            cfg = json.load(fh)
    # In CI staat er geen config.json; dan komt het uit secrets.
    topic = os.environ.get("NTFY_TOPIC") or cfg.get("ntfy_topic")
    if not topic:
        raise SystemExit("Geen ntfy-topic: zet NTFY_TOPIC of vul config.json.")
    server = (os.environ.get("NTFY_SERVER") or cfg.get("ntfy_server") or "https://ntfy.sh").rstrip("/")
    kop = {
        "User-Agent": USER_AGENT,
        # ntfy-headers zijn latin-1; accenten gaan er anders uit met een fout.
        "Title": _headerwaarde(titel),
        "Tags": "calendar",
    }
    if bijlage and os.path.exists(bijlage):
        # Met een bestand als body moet de tekst in een header; ntfy host het
        # plaatje dan zelf en de app toont het in de melding.
        with open(bijlage, "rb") as fh:
            body = fh.read()
        kop["Filename"] = os.path.basename(bijlage)
        # Een HTTP-header mag geen echte regeleinden bevatten; ntfy verwacht
        # daar de twee tekens \n en zet die zelf weer om.
        kop["Message"] = _headerwaarde(tekst)
        req = urllib.request.Request(f"{server}/{topic}", data=body, headers=kop, method="PUT")
    else:
        req = urllib.request.Request(f"{server}/{topic}", data=tekst.encode("utf-8"), headers=kop)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        if resp.status >= 300:
            raise SystemExit(f"ntfy antwoordde met HTTP {resp.status}")


def _dagen_tekst(dagen: int) -> str:
    """'vandaag' / 'morgen' / 'nog 3 dagen'."""
    if dagen <= 0:
        return "vandaag"
    if dagen == 1:
        return "morgen"
    return f"nog {dagen} dagen"


def komende_toetsen(snapshots: dict, nu: dt.datetime, vooruit: int) -> list[str]:
    """Aftelling naar toetsen en huiswerk in de komende 'vooruit' dagen.

    Toetsen staan voorop: een SO die als HUISWERK is ingevoerd telt evengoed,
    dus we tonen alles en markeren alleen wat de school echt toets noemt.
    """
    vandaag = nu.date()
    gevonden: dict[str, tuple] = {}
    for snap in snapshots.values():
        for sleutel, rec in snap.get("huiswerk", {}).items():
            begin = rec.get("datumTijd") or ""
            try:
                wanneer = dt.datetime.fromisoformat(begin[:19]).date()
            except ValueError:
                continue
            resterend = (wanneer - vandaag).days
            if not 0 <= resterend <= vooruit:
                continue
            is_toets = rec.get("type") in ("TOETS", "GROTE_TOETS")
            gevonden[sleutel] = (wanneer, resterend, is_toets, rec)

    regels = []
    for wanneer, resterend, is_toets, rec in sorted(gevonden.values(), key=lambda x: x[0]):
        dagnamen = ["ma", "di", "wo", "do", "vr", "za", "zo"]
        merk = "TOETS   " if is_toets else "huiswerk"
        vak = rec.get("vak") or "?"
        regels.append(
            f"{_dagen_tekst(resterend):<12} {dagnamen[wanneer.weekday()]} {wanneer:%d-%m}  "
            f"{merk}  {vak} - {rec['onderwerp']}"
        )
    return regels


def kaartgegevens(snapshots: dict, nu: dt.datetime, dagen: int, vooruit: int) -> tuple:
    """Zet de komende dagen om in de drie secties van de patch notes-kaart.

    Bewust de *stand* van het venster, niet het verschil: de kaart laat zien
    wat er aan de hand is, de tekst van de melding zegt wat er net veranderde.
    """
    dagnamen = ["ma", "di", "wo", "do", "vr", "za", "zo"]
    grens = nu + dt.timedelta(days=dagen)
    winst, verschuivingen = [], []

    for snap in snapshots.values():
        for les in snap.get("lessen", {}).values():
            try:
                begin = dt.datetime.fromisoformat((les.get("begin") or "")[:19])
            except ValueError:
                continue
            if not nu <= begin <= grens:
                continue
            wijziging = (les.get("wijziging") or "").strip()
            if not wijziging:
                continue
            wanneer = f"{dagnamen[begin.weekday()]} {les.get('lesuur')}e"
            if "vervalt" in wijziging.lower():
                try:
                    eind = dt.datetime.fromisoformat((les.get("eind") or "")[:19])
                    minuten = max(0, int((eind - begin).total_seconds() // 60))
                except ValueError:
                    minuten = 50
                winst.append({"vak": les.get("vak") or "?", "wanneer": wanneer,
                              "minuten": minuten, "_sort": begin})
            else:
                verschuivingen.append({"vak": les.get("vak") or "?",
                                       "wanneer": f"{wanneer} uur",
                                       "wat": wijziging.rstrip("."), "_sort": begin})

    bosses = []
    for snap in snapshots.values():
        for rec in snap.get("huiswerk", {}).values():
            try:
                wanneer = dt.datetime.fromisoformat((rec.get("datumTijd") or "")[:19])
            except ValueError:
                continue
            resterend = (wanneer.date() - nu.date()).days
            if not 0 <= resterend <= vooruit:
                continue
            bosses.append({"vak": rec.get("vak") or "?", "onderwerp": rec.get("onderwerp") or "?",
                           "dagen": resterend, "toets": rec.get("type") in ("TOETS", "GROTE_TOETS"),
                           "_sort": wanneer})

    for lijst in (winst, verschuivingen, bosses):
        lijst.sort(key=lambda x: x["_sort"])
        for x in lijst:
            x.pop("_sort")
    return winst, verschuivingen, bosses


def kaartkop(winst: list[dict], bosses: list[dict]) -> str:
    """Een titel waar een twaalfjarige op tikt."""
    if winst:
        return f"+{sum(w['minuten'] for w in winst)} MIN VRIJ"
    dichtstbij = min((b for b in bosses if b["toets"]), key=lambda b: b["dagen"], default=None)
    if dichtstbij and dichtstbij["dagen"] <= 1:
        return "BOSS INCOMING"
    return "Rooster update"


def do_check(dagen: int, notify: bool, reset: bool, vooruit: int, altijd: bool,
             kaart: bool = False) -> None:
    tokens = load_tokens()
    token, api_url = refresh_access_token(tokens)
    students = api_get(api_url, token, "/rest/v1/leerlingen")
    students = students.get("items", students) if isinstance(students, dict) else students
    if not students:
        raise SystemExit("Geen leerlingen gevonden op dit account.")
    leerling = students[0]
    leerling_id = leerling["links"][0]["id"]
    naam = " ".join(x for x in (leerling.get("roepnaam"), leerling.get("achternaam")) if x)

    nu = dt.datetime.now()
    tot = nu + dt.timedelta(days=dagen)

    # Wijzigingen melden we alleen vlak vooruit, maar voor de aftelling naar
    # toetsen kijken we verder; daarom halen we het ruimste venster op.
    weken = {}
    dag = nu.date()
    laatste = nu.date() + dt.timedelta(days=max(dagen, vooruit))
    while dag <= laatste:
        jaar, week, _ = dag.isocalendar()
        weken[f"{jaar}-{week:02d}"] = (jaar, week)
        dag += dt.timedelta(days=1)

    verse: dict[str, dict] = {}
    for label, (jaar, week) in weken.items():
        items = api_get(api_url, token, f"/rest/v1/afspraakitems/{leerling_id}/jaar/{jaar}/week/{week}")
        items = items.get("items", items) if isinstance(items, dict) else items
        verse[label] = _snapshot(items, haal_huiswerk(api_url, token, leerling_id, jaar, week))

    oude = {}
    if os.path.exists(STATE_FILE) and not reset:
        with open(STATE_FILE) as fh:
            oude = json.load(fh).get("weken", {})

    eerste_keer = not oude
    regels: list[str] = []
    for label, snap in verse.items():
        if label in oude:  # alleen weken die we eerder al zagen
            regels += _vergelijk(oude[label], snap, tot)

    bewaard = {**oude, **verse}
    with open(STATE_FILE, "w") as fh:
        json.dump({"bijgewerkt": nu.isoformat(timespec="seconds"), "weken": bewaard}, fh, indent=2, ensure_ascii=False)
    os.chmod(STATE_FILE, stat.S_IRUSR | stat.S_IWUSR)

    agenda = komende_toetsen(verse, nu, vooruit)

    def toon_agenda() -> None:
        if agenda:
            print(f"\nOp de agenda (komende {vooruit} dagen):")
            for a in agenda:
                print(f"  {a}")
        else:
            print(f"\nGeen toetsen of huiswerk in de komende {vooruit} dagen.")

    if eerste_keer:
        print(f"Eerste run: beginstand opgeslagen ({sum(len(s['lessen']) for s in verse.values())} lessen). "
              "Vanaf nu worden wijzigingen gemeld.")
        toon_agenda()
        return

    if regels:
        kop = f"Rooster {naam}: {len(regels)} wijziging{'en' if len(regels) != 1 else ''}"
        print(kop)
        for r in regels:
            print(f"  {r}")
    else:
        kop = f"Rooster {naam}"
        print(f"Geen wijzigingen in de komende {dagen} dagen.")
    toon_agenda()

    if not notify or not (regels or altijd):
        return

    delen = list(regels)
    if agenda:
        if delen:
            delen.append("")
        delen.append("Op de agenda:")
        delen += agenda
    tekst = "\n".join(delen) or "Geen wijzigingen."

    bijlage = None
    if kaart:
        winst, verschuivingen, bosses = kaartgegevens(verse, nu, dagen, vooruit)
        if winst or verschuivingen or bosses:
            try:
                from rooster_kaart import teken_kaart
                _, week_nu, _ = nu.isocalendar()
                bijlage = teken_kaart(winst, verschuivingen, bosses, week_nu,
                                      os.path.join(_HERE, "kaart.png"))
                kop = kaartkop(winst, bosses)
                print(f"kaart: {bijlage}")
            except Exception as err:
                # Een mislukte kaart mag de melding zelf nooit tegenhouden.
                print(f"kaart overgeslagen ({type(err).__name__}: {err})")

    stuur_notificatie(kop, tekst, bijlage)
    print("\n-> notificatie verstuurd" + (" met kaart" if bijlage else ""))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="eenmalige browser-login (PKCE)")
    p = sub.add_parser("rooster", help="weekrooster tonen")
    p.add_argument("--week", type=int, default=0, help="0=deze week, 1=volgende week, -1=vorige")
    p.add_argument("--json", action="store_true", help="ruwe JSON uitvoer")
    p.add_argument("--leerling", help="filter op (deel van) de naam")

    c = sub.add_parser("check", help="wijzigingen sinds de vorige run melden")
    c.add_argument("--dagen", type=int, default=3, help="hoe ver vooruit kijken (standaard 3)")
    c.add_argument("--notify", action="store_true", help="push via ntfy (zie config.json)")
    c.add_argument("--reset", action="store_true", help="beginstand opnieuw vastleggen")
    c.add_argument("--vooruit", type=int, default=14, help="aftelling naar toetsen, in dagen (standaard 14)")
    c.add_argument("--altijd", action="store_true", help="ook pushen als er niets gewijzigd is")
    c.add_argument("--kaart", action="store_true", help="patch notes-kaart meesturen (vereist playwright)")

    args = parser.parse_args()
    if args.cmd == "login":
        do_login()
    elif args.cmd == "check":
        do_check(args.dagen, args.notify, args.reset, args.vooruit, args.altijd, args.kaart)
    else:
        show_rooster(args.week, args.json, args.leerling)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
