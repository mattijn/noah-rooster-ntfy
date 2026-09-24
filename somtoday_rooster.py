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


def nu_nl() -> dt.datetime:
    """De huidige tijd in Nederland, zonder tijdzone-info.

    De API geeft lestijden zonder zone ("2026-09-15T09:00:00"), dus we
    vergelijken met een even naieve klok. Cruciaal op een GitHub-runner: die
    draait op UTC en loopt dus een of twee uur achter op de schooldag.
    """
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("Europe/Amsterdam")).replace(tzinfo=None)
    except Exception:
        return dt.datetime.now()

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


def _vergelijk(oud: dict, nieuw: dict, tot: dt.datetime,
               hoofddag: dt.date | None = None) -> list[dict]:
    """Beschrijf wat er veranderd is, in de taal van de melding zelf.

    Regels zien eruit als "wiskunde vervalt (donderdag, 2e uur)": eerst het
    vak, dan wat ermee gebeurt, en tussen haakjes waar je het terugvindt.
    Bij elke regel staan dag, vak en lesuur, zodat de melding kan zien of het
    dagoverzicht dezelfde les al noemt.
    """
    regels: list[dict] = []

    def meld(rec: dict, tekst: str) -> None:
        wanneer = _tijdstip(rec.get("begin") or rec.get("datumTijd"))
        regels.append({"tekst": tekst, "datum": wanneer.date() if wanneer else None,
                       "vak": roepnaam(rec.get("vak") or "?"), "uur": rec.get("lesuur")})

    def binnen(begin: str | None) -> bool:
        # Wijzigingen blijven over het hele venster gaan; alleen de dagnaam
        # valt weg als het toch al de dag uit de titel is.
        wanneer = _tijdstip(begin)
        return bool(wanneer) and wanneer <= tot

    def plek(rec: dict) -> str:
        wanneer = _tijdstip(rec.get("begin") or rec.get("datumTijd"))
        if not wanneer:
            return ""
        delen = []
        if wanneer.date() != hoofddag:
            delen.append(VOLLE_DAGNAMEN[wanneer.weekday()])
        if rec.get("lesuur"):
            delen.append(f"{rec['lesuur']}e uur")
        return f" ({', '.join(delen)})" if delen else ""

    oude_lessen, nieuwe_lessen = oud.get("lessen", {}), nieuw.get("lessen", {})
    for sleutel, na in nieuwe_lessen.items():
        if not binnen(na.get("begin")):
            continue
        vak = roepnaam(na.get("vak") or "?")
        voor = oude_lessen.get(sleutel)
        if voor is None:
            meld(na, f"{vak} erbij{plek(na)}")
            continue
        for veld in GEVOLGD:
            if voor.get(veld) == na.get(veld):
                continue
            if veld == "vak" and (_is_vakcode(voor.get("vak")) or _is_vakcode(na.get("vak"))):
                continue
            was, wordt = voor.get(veld) or "", na.get(veld) or ""
            if veld == "wijziging":
                if not wordt:
                    meld(na, f"{vak} gaat toch door{plek(na)}")
                elif "vervalt" in wordt.lower():
                    meld(na, f"{vak} vervalt{plek(na)}")
                else:
                    meld(na, f"{vak}: {klein(wordt.rstrip('.'))}{plek(na)}")
            elif veld == "begin":
                meld(na, f"{vak} begint om {str(wordt)[11:16]}{plek(na)}")
            elif veld == "lokaal":
                meld(na, f"{vak} in {_lokaal(wordt)}{plek(na)}")
            elif veld == "docent":
                meld(na, f"{vak} met {klein(wordt)}{plek(na)}")
            else:
                meld(na, f"{vak}: {was} wordt {wordt}{plek(na)}")

    for sleutel, voor in oude_lessen.items():
        if binnen(voor.get("begin")) and sleutel not in nieuwe_lessen:
            meld(voor, f"{roepnaam(voor.get('vak') or '?')} weg uit het rooster{plek(voor)}")

    oud_werk, nieuw_werk = oud.get("huiswerk", {}), nieuw.get("huiswerk", {})
    for sleutel, na in nieuw_werk.items():
        if sleutel in oud_werk or not binnen(na.get("datumTijd")):
            continue
        is_toets, merk = toetssoort(na.get("onderwerp"), na.get("type"))
        soort = "toets" if is_toets else "huiswerk"
        merking = f" ({merk})" if merk else ""
        meld(na, f"nieuwe {soort}{merking}: {roepnaam(na.get('vak') or '?')}"
                 f" - {klein(na.get('onderwerp') or '')}{plek(na)}")
    return regels


def _headerwaarde(tekst: str) -> str:
    """Maak tekst geschikt voor een HTTP-header: geen regeleinden, latin-1."""
    plat = tekst.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")
    return plat.encode("utf-8").decode("latin-1", "replace")


def stuur_notificatie(titel: str, tekst: str) -> None:
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
    }
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


def _is_vakcode(naam: str | None) -> bool:
    """Herken een afkorting als vaknaam: 'tn', 'mu', 'lo', 'm'.

    Somtoday levert voor sommige lessen (waaronder de uitgevallen) alleen de
    afkorting, terwijl hetzelfde vak elders voluit staat. Echte namen zijn
    langer of beginnen met een hoofdletter; 'KWT' valt er dus buiten.
    """
    return bool(naam) and len(naam) <= 3 and naam.islower() and naam.isalpha()


def vul_vaknamen(snapshots: dict) -> None:
    """Vervang afkortingen door de volledige vaknaam.

    Dezelfde les staat in een andere week wel voluit in de data, dus we zoeken
    hem op via docent+lokaal en anders via weekdag+lesuur. Beide sleutels zijn
    soms dubbelzinnig (een docent geeft meerdere vakken, een lesuur wisselt per
    week), dus we vullen alleen in bij precies een kandidaat. Een afkorting
    laten staan is beter dan de verkeerde naam tonen.
    """
    def moment(les: dict):
        try:
            return dt.datetime.fromisoformat((les.get("begin") or "")[:19])
        except ValueError:
            return None

    op_docent_lokaal: dict[tuple, set] = {}
    op_dag_uur: dict[tuple, set] = {}
    for snap in snapshots.values():
        for les in snap.get("lessen", {}).values():
            if _is_vakcode(les.get("vak")) or not (wanneer := moment(les)):
                continue
            op_docent_lokaal.setdefault((les.get("docent"), les.get("lokaal")), set()).add(les["vak"])
            op_dag_uur.setdefault((wanneer.weekday(), les.get("lesuur")), set()).add(les["vak"])

    for snap in snapshots.values():
        for les in snap.get("lessen", {}).values():
            if not _is_vakcode(les.get("vak")) or not (wanneer := moment(les)):
                continue
            for kaart, sleutel in ((op_docent_lokaal, (les.get("docent"), les.get("lokaal"))),
                                   (op_dag_uur, (wanneer.weekday(), les.get("lesuur")))):
                kandidaten = kaart.get(sleutel, set())
                if len(kandidaten) == 1:
                    les["vak"] = next(iter(kandidaten))
                    break


# Hoe de school een vak noemt, en hoe een leerling het noemt.
ROEPNAMEN = {
    "lichamelijke opvoeding": "gym",
    "mentor uur": "mentoruur",
    "kunstzinnige vorming": "kunst",
    "verzorging": "verzorging",
}


def roepnaam(vak: str) -> str:
    """Geef de naam die een leerling gebruikt.

    "Duitse taal" is hoe Somtoday het noemt; "duits" is hoe hij het noemt.
    Talen volgen een regel (het bijvoeglijk naamwoord zonder slot-e, zonder
    "taal"), de rest staat in ROEPNAMEN.
    """
    k = (vak or "").lower().strip()
    if k in ROEPNAMEN:
        return ROEPNAMEN[k]
    if k.endswith(" taal"):
        woord = k[:-5].strip()
        return woord[:-1] if woord.endswith("e") else woord
    return k


def klein(tekst: str) -> str:
    """Alles in kleine letters, behalve afkortingen als SO, KWT of PWS.

    Een woord dat helemaal uit hoofdletters bestaat blijft staan; dat is bijna
    altijd een afkorting die je niet moet verbouwen.
    """
    return " ".join(w if (len(w) >= 2 and w.isalpha() and w.isupper()) else w.lower()
                    for w in (tekst or "").split())


DAGNAMEN = ["ma", "di", "wo", "do", "vr", "za", "zo"]
VOLLE_DAGNAMEN = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag",
                  "zaterdag", "zondag"]
MAANDEN = ["jan", "feb", "mrt", "apr", "mei", "jun",
           "jul", "aug", "sep", "okt", "nov", "dec"]

_WAS_LOKAAL = re.compile(r"lokaal.*?\(was\s+([^)]+)\)", re.I)
_WAS_TIJD = re.compile(r"verplaatst.*?van\s+\w*\s*(\d{1,2}[:.]\d{2})", re.I)


def dagaanduiding(doel: dt.date, vandaag: dt.date,
                  overmorgen: bool = False) -> tuple[str | None, str]:
    """Noem een dag zoals een mens dat doet.

    Geeft (voorvoegsel, dag) terug: vandaag, morgen, anders de dagnaam binnen
    deze week, en daarna "volgende week" ervoor. Een aftelling in dagen laat je
    zelf rekenen; op vrijdag is "8 dagen" niet te plaatsen en "volgende week
    zaterdag" wel.
    """
    verschil = (doel - vandaag).days
    if verschil == 0:
        return None, "vandaag"
    if verschil == 1:
        return None, "morgen"
    if overmorgen and verschil == 2:
        return None, "overmorgen"
    naam = VOLLE_DAGNAMEN[doel.weekday()]
    # Verschil in kalenderweken, via de maandag van elke week; dat werkt ook
    # over een jaargrens heen.
    weken = ((doel - dt.timedelta(days=doel.weekday()))
             - (vandaag - dt.timedelta(days=vandaag.weekday()))).days // 7
    if vandaag.weekday() >= 5:
        # In het weekend hoort de eerstvolgende schoolweek al bij "deze week";
        # op zondag zeg je "dinsdag", niet "volgende week dinsdag".
        weken -= 1
    if weken <= 0:
        # Zelfde dagnaam maar een week verder mag niet kaal: "zondag" zou dan
        # twee verschillende dagen kunnen betekenen.
        return ("volgende week", naam) if verschil >= 7 else (None, naam)
    if weken == 1:
        return "volgende week", naam
    return f"over {weken} weken", naam


def _tijdstip(waarde: str | None) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat((waarde or "")[:19])
    except ValueError:
        return None


def _vervalt(les: dict) -> bool:
    return "vervalt" in (les.get("wijziging") or "").lower()


def _lessen_per_dag(snapshots: dict) -> dict[dt.date, list[dict]]:
    """Groepeer alle lessen per datum, op tijd gesorteerd."""
    per_dag: dict[dt.date, list[dict]] = {}
    for snap in snapshots.values():
        for les in snap.get("lessen", {}).values():
            if wanneer := _tijdstip(les.get("begin")):
                per_dag.setdefault(wanneer.date(), []).append({**les, "_begin": wanneer})
    for lijst in per_dag.values():
        lijst.sort(key=lambda l: (l["_begin"], l.get("lesuur") or 0))
    return per_dag


def startblok(per_dag: dict, nu: dt.datetime) -> dict | None:
    """Beschrijf de eerstvolgende schooldag die nog niet begonnen is.

    Drie situaties, alledrie rechtstreeks uit de data af te leiden:
    gewoon (niets aan de hand), later (er vervalt iets aan het begin) en
    eerder (er is juist iets naar het eerste uur verplaatst) - dat laatste is
    het geval waarin je denkt vrij te zijn maar er staat iets anders.
    """
    kandidaten = sorted(d for d in per_dag if d >= nu.date())
    for datum in kandidaten:
        lessen = per_dag[datum]
        doorgaand = [l for l in lessen if not _vervalt(l)]
        if doorgaand and doorgaand[0]["_begin"] <= nu:
            # Deze schooldag loopt al of is voorbij; "begin je om" slaat dan
            # nergens op. Door naar de eerstvolgende dag die nog moet beginnen.
            continue
        if not doorgaand and not any(l["_begin"] > nu for l in lessen):
            continue
        voor, dag = dagaanduiding(datum, nu.date())
        dagnaam = f"{voor} {dag}" if voor else dag
        if not doorgaand:
            return {"dag": dagnaam, "geen_les": True, "datum": datum}

        eerste = doorgaand[0]
        vervallen_ervoor = [l for l in lessen
                            if _vervalt(l) and l["_begin"] < eerste["_begin"]]
        wijziging = (eerste.get("wijziging") or "").strip()

        soort, reden = "gewoon", None
        if "verplaatst" in wijziging.lower():
            soort = "eerder"
            reden = f"{eerste.get('vak')} is naar het {eerste.get('lesuur')}e uur verplaatst"
        elif vervallen_ervoor:
            soort = "later"
            uren = [str(l.get("lesuur")) for l in vervallen_ervoor]
            reden = (f"{uren[0]}e uur vervalt" if len(uren) == 1
                     else f"{' en '.join(uren)}e uur vervallen")
        return {"dag": dagnaam, "soort": soort, "reden": reden, "datum": datum,
                "tijd": eerste["_begin"].strftime("%H:%M"),
                "uur": eerste.get("lesuur"), "vak": eerste.get("vak"),
                "lokaal": eerste.get("lokaal")}
    return None


def dagoverzicht(snapshots: dict, nu: dt.datetime, vooruit: int) -> tuple:
    """Zet de eerstvolgende schooldag en de toetsen erna om in de delen van de melding."""
    per_dag = _lessen_per_dag(snapshots)
    blok = startblok(per_dag, nu)
    uitval, gewijzigd = [], []

    # Uitval en wijzigingen gaan alleen over de dag die het startblok toont:
    # 's ochtends vandaag, 's avonds morgen. Verder vooruit is ruis - daarvoor
    # is de tekst van de melding.
    dagen_in_beeld = [blok["datum"]] if blok and blok.get("datum") else []
    for datum in dagen_in_beeld:
        lessen = per_dag.get(datum, [])
        doorgaand = [l for l in lessen if not _vervalt(l)]
        for les in lessen:
            basis = {"vak": les.get("vak") or "?", "dag": f'{les.get("lesuur")}e uur',
                     "uur": les.get("lesuur"), "tijd": les["_begin"].strftime("%H:%M")}

            if _vervalt(les):
                ervoor = [l for l in doorgaand if l["_begin"] < les["_begin"]]
                erna = [l for l in doorgaand if l["_begin"] > les["_begin"]]
                if not ervoor and erna:
                    gevolg, klok = "later beginnen", erna[0]["_begin"].strftime("%H:%M")
                elif ervoor and not erna:
                    eind = _tijdstip(ervoor[-1].get("eind"))
                    gevolg, klok = "eerder uit", eind.strftime("%H:%M") if eind else ""
                elif not ervoor and not erna:
                    gevolg, klok = "geen les", ""
                else:
                    # Het gat loopt van het einde van de vorige les tot het
                    # begin van de volgende; de pauzes ertussen zijn ook vrij.
                    eind = _tijdstip(ervoor[-1].get("eind"))
                    gevolg = "tussenuur"
                    klok = (f"{eind:%H:%M}-{erna[0]['_begin']:%H:%M}"
                            if eind else erna[0]["_begin"].strftime("tot %H:%M"))
                positie = "midden" if gevolg == "tussenuur" else "rand"
                uitval.append({**basis, "gevolg": gevolg, "klok": klok, "positie": positie})
                continue

            wijziging = (les.get("wijziging") or "").strip()
            if not wijziging:
                continue
            if m := _WAS_LOKAAL.search(wijziging):
                gewijzigd.append({**basis, "label": "ander lokaal",
                                  "nu": les.get("lokaal") or "?", "was": m.group(1)})
            elif m := _WAS_TIJD.search(wijziging):
                gewijzigd.append({**basis, "label": "verplaatst",
                                  "nu": basis["tijd"], "was": m.group(1).replace(".", ":")})
            else:
                gewijzigd.append({**basis, "label": wijziging.rstrip("."), "nu": "", "was": ""})

    # Volgorde van uitval: eerst wat de dag later laat beginnen, dan de
    # tussenuren, en als laatste wat je eerder naar huis stuurt.
    rang = {"later beginnen": 0, "geen les": 0, "tussenuur": 1, "eerder uit": 2}
    uitval.sort(key=lambda u: (rang.get(u["gevolg"], 1), u["tijd"]))

    toetsen = []
    for snap in snapshots.values():
        for rec in snap.get("huiswerk", {}).values():
            wanneer = _tijdstip(rec.get("datumTijd"))
            if not wanneer:
                continue
            resterend = (wanneer.date() - nu.date()).days
            # Een toets die al geweest is heeft geen aftelling meer nodig; om
            # 20:00 nog "VANDAAG" tonen voor iets van vanochtend is onzin.
            if wanneer > nu and 0 <= resterend <= vooruit:
                voor, dag = dagaanduiding(wanneer.date(), nu.date())
                # Het lesuur staat niet in de toekenning; we vinden het via de
                # les die op hetzelfde tijdstip begint.
                les = next((l for l in per_dag.get(wanneer.date(), [])
                            if l["_begin"] == wanneer), None)
                is_toets, merk = toetssoort(rec.get("onderwerp"), rec.get("type"))
                toetsen.append({"vak": rec.get("vak") or "?", "wat": rec.get("onderwerp") or "?",
                                "dagen": resterend, "voor": voor, "dag": dag,
                                "toets": is_toets, "merk": merk,
                                "uur": les.get("lesuur") if les else None,
                                "_datum": wanneer.date(), "_s": wanneer})
    toetsen.sort(key=lambda t: t["_s"])
    for t in toetsen:
        t.pop("_s")

    return blok, uitval, gewijzigd, toetsen


def toetssoort(onderwerp: str, api_type: str | None) -> tuple[bool, str | None]:
    """Is dit een toets, en hoe noemt de docent het?

    Het type uit Somtoday is onbetrouwbaar: een SO wordt geregeld als HUISWERK
    ingevoerd. Wat de docent in het onderwerp schrijft is leidend, want dat is
    ook wat de leerling leest.
    """
    woorden = re.findall(r"[a-zA-Z]+", (onderwerp or "").lower())
    for woord, merk in (("so", "SO"), ("repetitie", "repetitie"),
                        ("proefwerk", "proefwerk"), ("pw", "PW"),
                        ("overhoring", "overhoring"), ("tentamen", "tentamen")):
        if woord in woorden:
            return True, merk
    if "toets" in woorden:
        return True, None
    return api_type in ("TOETS", "GROTE_TOETS"), None


def gesproken_tijd(tijd: str) -> str:
    """09:00 wordt 9.00, 10:10 wordt 10.10."""
    uur, _, minuut = tijd.partition(":")
    return f"{int(uur)}.{minuut}"


def _uur(uur) -> str:
    return f" ({uur}e uur)" if uur else ""


def _lokaal(lokaal: str | None) -> str:
    """zf101 wordt f101: elk lokaal begint met een z, dus die zegt niets."""
    lokaal = (lokaal or "").strip()
    return lokaal[1:] if lokaal[:1].lower() == "z" and len(lokaal) > 1 else lokaal


def _toetsdag(datum: dt.date, vandaag: dt.date) -> str:
    """Vrijdag, dinsdag (nog 3 dagen), volgende week woensdag (nog 8 dagen).

    Geen datum: die moet je omrekenen. De dagnaam zegt wanneer, de aftelling
    hoe dichtbij. Nooit "vandaag" of "morgen": de Action loopt soms uren
    achter, en wie de melding later leest weet niet meer wanneer hij kwam.
    """
    voor, dag = dagaanduiding(datum, vandaag)
    if dag in ("vandaag", "morgen"):
        dag = VOLLE_DAGNAMEN[datum.weekday()]
    dagen = (datum - vandaag).days
    naam = f"{voor} {dag}" if voor else dag
    return f"{naam} (nog {dagen} dagen)" if dagen >= 2 else naam


def meldtekst(blok: dict | None, uitval: list, gewijzigd: list, toetsen: list,
              wijzigingen: list[dict], nu: dt.datetime) -> tuple[str, str]:
    """Bouw titel en body van de melding.

    De titel zegt wanneer school begint. Daaronder eerst die ene dag: met welke
    les je begint, wat er uitvalt en wat dat betekent, wat er verandert en welke
    toetsen er zijn. Dan wijzigingen op andere dagen (met dagnaam), en tot slot
    de toetsen verderop. Op het lockscherm zie je de eerste regels; de rest
    als je hem openklapt.
    """
    # De dagnaam in de titel, niet "morgen": een run kan uren te laat zijn,
    # en dan is niet meer te zien vanaf wanneer "morgen" gerekend is.
    hoofddag = blok.get("datum") if blok else None
    dag = VOLLE_DAGNAMEN[hoofddag.weekday()].capitalize() if hoofddag else ""
    if not blok:
        titel = "Rooster bijgewerkt"
    elif blok.get("geen_les"):
        titel = f"{dag} geen school"
    else:
        titel = f"{dag} school {gesproken_tijd(blok['tijd'])}"

    regels: list[str] = []
    if blok and not blok.get("geen_les"):
        waar = ", ".join(x for x in (f"{blok['uur']}e uur" if blok.get("uur") else None,
                                     _lokaal(blok.get("lokaal"))) if x)
        regels.append(f"eerst {roepnaam(blok.get('vak') or '?')}" + (f" ({waar})" if waar else ""))

    # Wat het dagoverzicht al noemt, hoeft niet nog eens als wijziging.
    gedekt: set = set()
    for u in uitval:
        gevolg = u["gevolg"]
        # Later beginnen staat al in de titel; alleen de rest krijgt een tijd.
        if u.get("klok") and gevolg not in ("later beginnen", "geen les"):
            gevolg += " " + u["klok"].replace(":", ".")
        regels.append(f"- {roepnaam(u['vak'])} vervalt{_uur(u['uur'])}: {gevolg}")
        gedekt.add((roepnaam(u["vak"]), u["uur"]))
    for g in gewijzigd:
        if g["label"] == "ander lokaal":
            wat = f"in {_lokaal(g['nu'])}, was {_lokaal(g['was'])}"
        elif g["label"] == "verplaatst":
            wat = f"om {gesproken_tijd(g['nu'])}, was {gesproken_tijd(g['was'])}"
        else:
            wat = klein(g["label"])
        regels.append(f"- {roepnaam(g['vak'])}{_uur(g['uur'])}: {wat}")
        gedekt.add((roepnaam(g["vak"]), g["uur"]))

    later = []
    for t in toetsen:
        soort = "toets" if t["toets"] else "huiswerk"
        if t["_datum"] == hoofddag:
            tussen = [x for x in (t.get("merk"),
                                  f"{t['uur']}e uur" if t.get("uur") else None) if x]
            haakjes = f" ({', '.join(tussen)})" if tussen else ""
            regels.append(f"- {roepnaam(t['vak'])} {soort}{haakjes}")
            # Een nieuwe toets heeft in de wijziging geen lesuur; op vak alleen
            # herkennen is genoeg, het gaat om dezelfde dag.
            gedekt.update({(roepnaam(t["vak"]), t.get("uur")), (roepnaam(t["vak"]), None)})
        else:
            wat = klein(t["wat"])
            if not t["toets"]:
                wat = f"{wat} (huiswerk)"
            later.append(f"{_toetsdag(t['_datum'], nu.date())}: {roepnaam(t['vak'])} - {wat}")

    regels += [f"- {w['tekst']}" for w in wijzigingen
               if w["datum"] != hoofddag or (w["vak"], w["uur"]) not in gedekt]

    if later:
        regels += ["", "Toetsen", *later]
    return titel, "\n".join(regels).strip()


def do_check(dagen: int, notify: bool, reset: bool, vooruit: int, altijd: bool) -> None:
    tokens = load_tokens()
    token, api_url = refresh_access_token(tokens)
    students = api_get(api_url, token, "/rest/v1/leerlingen")
    students = students.get("items", students) if isinstance(students, dict) else students
    if not students:
        raise SystemExit("Geen leerlingen gevonden op dit account.")
    leerling = students[0]
    leerling_id = leerling["links"][0]["id"]
    naam = " ".join(x for x in (leerling.get("roepnaam"), leerling.get("achternaam")) if x)

    nu = nu_nl()
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
    vul_vaknamen(verse)

    oude = {}
    if os.path.exists(STATE_FILE) and not reset:
        with open(STATE_FILE) as fh:
            oude = json.load(fh).get("weken", {})

    eerste_keer = not oude
    # Het dagoverzicht eerst: daar komt de dag uit waar deze melding over gaat,
    # en die bepaalt of een wijzigingsregel zijn dagnaam nodig heeft.
    blok, uitval, gewijzigd_l, toetsen = dagoverzicht(verse, nu, vooruit)
    hoofddag = blok.get("datum") if blok else None

    regels: list[dict] = []
    for label, snap in verse.items():
        if label in oude:  # alleen weken die we eerder al zagen
            regels += _vergelijk(oude[label], snap, tot, hoofddag)

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
            print(f"  {r['tekst']}")
    else:
        kop = f"Rooster {naam}"
        print(f"Geen wijzigingen in de komende {dagen} dagen.")
    toon_agenda()

    if not notify or not (regels or altijd):
        return

    kop, tekst = meldtekst(blok, uitval, gewijzigd_l, toetsen, regels, nu)
    stuur_notificatie(kop, tekst)
    print("\n-> notificatie verstuurd")


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

    args = parser.parse_args()
    if args.cmd == "login":
        do_login()
    elif args.cmd == "check":
        do_check(args.dagen, args.notify, args.reset, args.vooruit, args.altijd)
    else:
        show_rooster(args.week, args.json, args.leerling)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
