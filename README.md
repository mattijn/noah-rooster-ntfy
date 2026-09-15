# noah-rooster-ntfy

Leest Noahs weekrooster uit Somtoday en pusht wijzigingen naar ntfy.

Meldt uitval, verplaatsingen, lokaalwijzigingen en nieuwe toetsen/huiswerk
voor de komende dagen. Draait als GitHub Action, doordeweeks elk uur tijdens
schooltijd plus een run om 06:30.

## Hoe het werkt

Het rooster komt uit `/rest/v1/afspraakitems/{leerlingId}/jaar/{jaar}/week/{week}`
van de niet-officiele Somtoday-API. Dat endpoint geeft `wijzigingOmschrijving`
mee ("Les vervalt", "De les is verplaatst (komt van ma 9:00u)"). Let op: het
oudere `/rest/v1/afspraken` laat uitgevallen lessen simpelweg weg en is dus
onbruikbaar hiervoor.

Toetsen en huiswerk komen uit `/rest/v1/studiewijzeritemafspraaktoekenningen`
en worden op begintijd aan een les gekoppeld. Docenten voeren een SO regelmatig
in als `HUISWERK` in plaats van `TOETS`, dus filter niet op type.

`state.json` bewaart de vorige stand; `check` meldt alleen het verschil.

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

- `TOKEN_PASS` - wachtwoord waarmee `tokens.enc` versleuteld is
- `NTFY_TOPIC` - ntfy-topic waar de meldingen heen gaan
