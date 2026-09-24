# noah-rooster-ntfy

Leest Noahs weekrooster uit Somtoday en pusht wijzigingen naar ntfy.

Meldt uitval, verplaatsingen, lokaalwijzigingen en nieuwe toetsen/huiswerk.
Draait als GitHub Action: doordeweeks elk uur tijdens schooltijd, plus een
vast bericht om 07:15 en om 17:30 (Europe/Amsterdam, dus geen gedoe met zomertijd).

## De melding

Alles staat in de tekst; een plaatje als bijlage verloopt bij ntfy na een
paar uur, en dan is de melding van gisteravond 's ochtends leeg. Bovenaan staat
waar het om draait - hoe laat je moet beginnen, en met welke les - zodat het
op het lockscherm past. Openklappen geeft de rest.

    Morgen school 10.10
    eerst wiskunde (2e uur, f101)
    - handvaardigheid vervalt (1e uur): later beginnen
    - muziek vervalt (4e uur): tussenuur 12.30-13.55
    - gym vervalt (6e uur): eerder uit 14.55
    - aardrijkskunde (3e uur): in h005, was h104
    - duits toets (SO, 3e uur)
    - wiskunde vervalt (donderdag, 2e uur)

    Toetsen
    donderdag (nog 2 dagen): nederlands - boekverslag (huiswerk)
    vrijdag (nog 3 dagen): frans - SO woorden
    volgende week woensdag (nog 8 dagen): wiskunde - toets hoofdstuk 1

Eerst die ene dag uit de titel, zonder dagnaam: de uitval met wat het betekent
(een tussenuur is iets anders dan naar huis mogen), wat er verandert en welke
toetsen er zijn. Dan **wijzigingen** op andere dagen binnen drie dagen, met de
dagnaam erbij - die wil je weten zodra ze bekend zijn. Een wijziging die het
dagoverzicht al noemt komt er niet nog eens bij. Onderaan de **toetsen** tot
veertien dagen vooruit, met de dag zoals je die zegt ("volgende week woensdag")
en hoeveel dagen het nog is - geen datum, die moet je omrekenen. Lokalen
staan er zonder de z waar ze allemaal mee beginnen.

## Hoe het werkt

Het rooster komt uit `/rest/v1/afspraakitems/{leerlingId}/jaar/{jaar}/week/{week}`
van de niet-officiele Somtoday-API. Dat endpoint geeft `wijzigingOmschrijving`
mee ("Les vervalt", "De les is verplaatst (komt van ma 9:00u)"). Let op: het
oudere `/rest/v1/afspraken` laat uitgevallen lessen simpelweg weg en is dus
onbruikbaar hiervoor.

Toetsen en huiswerk komen uit `/rest/v1/studiewijzeritemafspraaktoekenningen`
en worden op begintijd aan een les gekoppeld. Docenten voeren een SO regelmatig
in als `HUISWERK` in plaats van `TOETS`, dus filter niet op type.

`state.json` bewaart de vorige stand; `check` meldt alleen het verschil. In de
repo staat hij versleuteld als `state.enc`, met hetzelfde wachtwoord als het
token - er staan vakken, lokalen, docenten en toetsonderwerpen van een kind in,
en dat hoort niet leesbaar in een repo.

Twee dingen waar de data tegenwerkt. Uitgevallen lessen komen met een afkorting
als vaknaam ("mu" in plaats van "muziek"); die zoeken we op via docent+lokaal en
anders via weekdag+lesuur, en we vullen alleen in bij precies een kandidaat -
een verkeerde naam is erger dan een afkorting. En de runner draait op UTC, dus
alle tijdvergelijkingen lopen via `nu_nl()`, anders kiest het startblok een les
die allang bezig is.

## Commando's

    python3 somtoday_rooster.py login              # eenmalig, via de browser
    python3 somtoday_rooster.py rooster            # weekrooster in de terminal
    python3 somtoday_rooster.py check              # wat is er veranderd
    python3 somtoday_rooster.py check --notify

Bij `check`: `--dagen` is het venster voor wijzigingen (standaard 3), `--vooruit`
dat voor toetsen onderaan (standaard 14), `--altijd` pusht ook als er niets
veranderd is, en `--reset` legt de beginstand opnieuw vast.

Een run handmatig uitlokken zonder te wachten:

    gh workflow run rooster.yml --repo mattijn/noah-rooster-ntfy -f altijd=true

## Belangrijk: maar een plek tegelijk

Het refresh token roteert bij elke API-call, en hergebruik van een oud token
laat Somtoday de hele tokenfamilie intrekken. Draai dit dus **nooit** tegelijk
lokaal en in de Action. De workflow gebruikt daarom `concurrency` zonder
cancel-in-progress.

## Opnieuw inloggen na uitsluiting

Zie je in de logs `invalid_grant: Token revoked`, dan is opnieuw inloggen nodig.
Op de Mac, met SomtodayCallback.app in ~/Applications:

    python3 somtoday_rooster.py login
    TOKEN_PASS=<wachtwoord> openssl enc -aes-256-cbc -pbkdf2 -salt \
        -in tokens.json -out tokens.enc -pass env:TOKEN_PASS
    git add tokens.enc && git commit -m "opnieuw ingelogd" && git push

## Secrets

- `TOKEN_PASS` - wachtwoord waarmee `tokens.enc` en `state.enc` versleuteld zijn
- `NTFY_TOPIC` - ntfy-topic waar de meldingen heen gaan
