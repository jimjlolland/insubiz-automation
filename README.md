# InsuBiz – Automatisering 2

Robotten gennemgår krænkende handlinger i InsuBiz og afslutter kun en sag, når
alle tre regler fra procesbeskrivelsen er opfyldt:

1. Fravær (`personalInjury.accidentAbsence`) er under én dag.
2. Psykologisk krisehjælp (`postActQ1`) er ikke valgt.
3. Den umiddelbare reaktion (`reactionQ1`–`reactionQ10`) har præcis ét svar
   og scoren er under 7.

Den bruger InsuBiz API 1.3-endepunkterne `SignInAsync`,
`GetIncidentInfringActsPagedAsync`, `GetIncidentInfringActByIdAsync`,
`GetIncidentByIdAsync` og `UpdateIncidentFieldsAsync` fra `swagger.json`.

## Automation Server-konfiguration

Opret en credential i Automation Server med navnet `InsuBiz API`. Indtast
InsuBiz API-nøglen i feltet **Username** og den hemmelige nøgle i **Password**.
Indtast dette i credentialens **Data**-felt:

```json
{
  "base_url": "https://deploy.insubiz.dk",
  "closed_status_id": "<status-id for Afsluttet>",
  "dry_run": "true"
}
```

Tilknyt en workqueue til processen. Planlæg først processen med `--queue` for
at finde kvalificerede sager og oprette work items. Kør derefter processen uden
argumenter for at behandle køen. Hvert work item bliver automatisk markeret
som completed eller failed af Automation Server.

Start altid med tørkørsel:

```sh
uv run python main.py
```

Tørkørsel er standard og logger de sager, som ville blive afsluttet. Først når
testresultatet er godkendt, ændres credential-data til `"dry_run": "false"`;
derefter ændrer robotten sagens status til credentialens `closed_status_id`.

## Test

```sh
python3 -m unittest discover -s tests -v
```
