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
`FindIncidentsPagedAsync`,
`GetIncidentInfringActByIdAsync`,
`GetIncidentByIdAsync` og `UpdateIncidentFieldsAsync` fra `swagger.json`.

## Automation Server-konfiguration

Opret en credential i Automation Server med navnet `InsuBiz API`. Indtast
InsuBiz API-nøglen i feltet **Username** og den hemmelige nøgle i **Password**.
Indtast dette i credentialens **Data**-felt:

```json
{
  "base_url": "https://deploy.insubiz.dk",
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

For hver fundet sag hentes sagsdetaljerne og krænkelsesposten direkte via
`GetIncidentInfringActByIdAsync?incidentId=<sags-id>`, uden datofilter og uden
parameteren `id`. Svaret skal indeholde et positivt post-id og henvise til
den forespurgte sag. Tomme svar logges som ikke vurderet; ugyldige svar giver
en fejl. Dette opslag med kun `incidentId` skal fortsat bekræftes ved en
tørkørsel mod InsuBiz; lokale tests bruger simulerede svar.

Kun sager, der stadig har status **Ny** (`0`), kan lægges i køen eller
afsluttes. Status og regler kontrolleres igen ved købehandling, også for
tidligere oprettede køelementer. Sagsloggen bruger de fulde sagsdetaljer.

Start altid med tørkørsel:

```sh
uv run python main.py
```

Tørkørsel er standard og logger de sager, som ville blive afsluttet. Først når
testresultatet er godkendt, ændres credential-data til `"dry_run": "false"`;
derefter ændrer robotten sagens status til credentialens `closed_status_id`.

## Test

```sh
uv run --with pytest python -m pytest
```
