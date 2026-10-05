# InsuBiz – Automatisering 2

Robotten gennemgår krænkende handlinger i InsuBiz og afslutter kun en sag, når
alle tre regler fra procesbeskrivelsen er opfyldt:

1. Fravær har værdien `Uarbejdsdygtighed mindre end 1 dag`
   (`personalInjury.accidentDuration.id = 1`).
2. Psykologisk krisehjælp (`postActQ1`) er udtrykkeligt `false`.
   Manglende eller ugyldige værdier forhindrer afslutning.
3. Den umiddelbare reaktion (`reactionQ1`–`reactionQ10`) har præcis ét svar
   og scoren er under 7.

Den bruger InsuBiz API 1.3-endepunkterne `SignInAsync`,
`FindIncidentsPagedAsync`, `GetIncidentInfringActsPagedAsync`,
`GetIncidentInfringActByIdAsync`,
`GetIncidentByIdAsync` og `UpdateIncidentFieldsAsync` fra `swagger.json`.

## Automation Server-konfiguration

Opret en credential i Automation Server med navnet `InsuBiz API`. Indtast
InsuBiz API-nøglen i feltet **Username** og den hemmelige nøgle i **Password**.
Indtast dette i credentialens **Data**-felt:

```json
{
  "base_url": "https://api.insubiz.dk",
  "closed_status_id": "3",
  "incident_status_ids": "0",
  "system_owner_id": "<systemOwnerId fra InsuBiz>",
  "dry_run": "true"
}
```

Tilknyt en workqueue til processen. Planlæg først processen med `--queue` for
at finde kvalificerede sager og oprette work items. Kør derefter processen uden
argumenter for at behandle køen. Hvert work item bliver automatisk markeret
som completed eller failed af Automation Server.

`incident_status_ids` begrænser opslaget hos InsuBiz. Standardværdien `0` er
**Ny** i Insight-visningen (klassifikationen kalder den `Xnet indbakke`), så
robotten kun gennemgår nye sager.

Køopbygningen henter krænkelsesposter via
`GetIncidentInfringActsPagedAsync?incidentStatusId=0`, uden datofilter eller
filter på krænkelsespostens egen status. Alle sider indlæses og kobles til
de fundne sager via `incident.id`. Kun én entydig post pr. sag kan lægges i
køen; manglende eller flere forskellige poster logges som ikke vurderet.
Sagsdetaljerne hentes separat for at kontrollere status og fravær.

Ved købehandling genhentes posten via
`GetIncidentInfringActByIdAsync?incidentId=<sags-id>&id=<post-id>`.
Begge ID'er er påkrævet i klienten, og svarets ID'er kontrolleres.
Der foretages aldrig direkte opslag med kun `incidentId`, da denne form
gav HTTP 500 i testen mod InsuBiz.

Hvis API'et fortsat returnerer en tom liste for Ny, kan robotten ikke vurdere
de nye sager. Dette logges for hver sag og i optællingen til sidst; det
betyder ikke, at sagerne er vurderet og afvist af forretningsreglerne.
Lokale tests bruger simulerede API-svar.

Kun sager, der stadig har status **Ny** (`0`), kan lægges i køen eller
afsluttes. Status og regler kontrolleres igen ved købehandling, også for
tidligere oprettede køelementer. Sagsloggen bruger de fulde sagsdetaljer.
Andre konfigurerede status-id'er ignoreres med en advarsel.

Start altid med tørkørsel:

```sh
uv run python main.py
```

Tørkørsel er standard og logger de sager, som ville blive afsluttet. Først når
testresultatet er godkendt, ændres credential-data til `"dry_run": "false"`;
derefter ændrer robotten sagens status til credentialens `closed_status_id`.

## Diagnose af manglende krænkelsesposter

Kør processen i Automation Server med disse parametre for at undersøge konkrete
sager med den eksisterende credential:

```text
--diagnose 2494158 2492267
```

Tallene er API-sags-id'er, ikke skadenumre. Diagnosekørslen kræver ingen
workqueue og ændrer hverken sager eller køelementer, uanset `dry_run`.
`--diagnose` og `--queue` kan ikke bruges samtidig.

For hver sag hentes detaljer med `includeDynamicFields=true`. Begge
krænkelsesliste-endpoints undersøges for sagens kunde uden status- eller
datofilter, med alle sider og uden regelvurdering af kundens andre sager.
Fundne poster til de valgte sager genhentes med begge ID'er. Loggen viser
ID'er, fravær, reaktionsscore, krisehjælp og valgte `violenceTypeQ`-numre.
Derudover logges dynamiske felters tekniske navne og dokumenters ID'er;
feltværdier, dokumenttitler og sagsbeskrivelser logges ikke. Dokumenternes
indhold læses ikke, og dynamiske felter bruges ikke automatisk som erstatning
for krænkelsespostens felter.

Et opslag, der fejler, markeres i loggen og tælles ikke som en gennemført
søgning uden resultater. Loggen er grundlag for at vælge næste opslag eller
for at give InsuBiz-support konkrete eksempler; den kan ikke bevise, at et
skema ikke findes i brugerfladen.

## Test

```sh
uv run --with pytest python -m pytest
```
