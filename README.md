# InsuBiz – Automatisering 2

Robotten gennemgår krænkende handlinger i InsuBiz og afslutter kun en sag, når
alle tre regler fra procesbeskrivelsen er opfyldt:

1. Fravær har værdien `Uarbejdsdygtighed mindre end 1 dag`
   (`personalInjury.accidentDuration.id = 1`).
2. Psykologisk krisehjælp (`postActQ1`) er udtrykkeligt `false`.
   Manglende eller ugyldige værdier forhindrer afslutning.
3. Den umiddelbare reaktion (`reactionQ1`–`reactionQ10`) har præcis ét svar
   og scoren er 7 eller derunder.

Den bruger InsuBiz API 1.3-endepunkterne `SignInAsync`,
`FindIncidentsPagedAsync`, `GetIncidentInfringActsPagedAsync`,
`GetIncidentInfringActByIdAsync`,
`GetIncidentByIdAsync`, `GetIncidentDocuments`, `DownloadDocumentAsync`
og `UpdateIncidentFieldsAsync` fra `swagger.json`.

## Automation Server-konfiguration

Opret en credential i Automation Server med et navn, du selv vælger. Indtast
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

Tilknyt credentialen til processens **Process credentials** (`credentials_id`).
Automatiseringen henter den valgte credential via ID for den aktuelle proces;
den bruger hverken et fast navn eller InsuBiz-miljøvariabler. Credentialen kan
omdøbes, og forskellige processer kan vælge forskellige credentials.

**Git credentials** (`target_credentials_id`) bruges af Automation Server til
at klone repositoryet. Odenses offentlige procesformular viser aktuelt kun
dette felt. Hvis din version heller ikke har **Process credentials**, kan
[formularpatchen](docs/automation-server-process-credentials.patch) tilføje
vælgeren; backend understøtter allerede `credentials_id`. Patchen anvendes i
Automation Server-kildekoden, hvorefter frontend genbygges. Den er ikke en del
af processens kørselskode og er ikke automatisk installeret på din server.
Se [Odenses procesformular](https://github.com/odense-rpa/automation-server/blob/main/frontend/src/components/ProcessForm.vue)
og [API-schema](https://github.com/odense-rpa/automation-server/blob/main/backend/app/api/v1/schemas.py).

Hvis en proces mangler credentialen, stopper automatiseringen med en forklaring.
Der er ingen automatisk søgning efter et credential-navn eller brug af Git-login.

Tilknyt også en workqueue til processen. Planlæg først processen med `--queue` for
at finde kvalificerede sager og oprette work items. Kør derefter processen uden
argumenter for at behandle køen. Hvert work item bliver automatisk markeret
som completed eller failed af Automation Server.

`incident_status_ids` begrænser opslaget hos InsuBiz. Standardværdien `0` er
**Ny** i klassifikationen `claim_status_insight`, så
robotten kun gennemgår nye sager.

Køopbygningen henter krænkelsesposter via
`GetIncidentInfringActsPagedAsync?incidentStatusId=0`, uden datofilter eller
filter på krænkelsespostens egen status. Alle sider indlæses og kobles til
de fundne sager via `incident.id`. Kun én entydig post pr. sag kan lægges i
køen. Flere forskellige poster logges som ikke vurderet.
Sagsdetaljerne hentes separat for at kontrollere status og fravær.

Når krænkelsesposten mangler i API-listen, undersøger robotten sagens
vedhæftede PDF-dokumenter. Den bruger kun ét entydigt **Skema for krænkende
handlinger**, hvor skadenummer og den konfigurerede systemejer matcher.
Reaktionsskalaens ti felter og krisehjælpsfeltet findes via tekst og
afkrydsningsfelternes geometri. Den genkendte trykte markering aflæses med
PyMuPDF og kontrolleres mod de synlige pixels. Score 7 accepteres.
Billedbaserede PDF'er, ukendte markeringer, manglende svar og flere rapporter
kræver manuel vurdering. PDF-tekster og dokumenttitler logges ikke, og
dokumenterne gemmes ikke på disk.

Ved købehandling genhentes posten via
`GetIncidentInfringActByIdAsync?incidentId=<sags-id>&id=<post-id>`.
Begge ID'er er påkrævet i klienten, og svarets ID'er kontrolleres.
PDF-baserede køelementer gemmer i stedet
`source=pdf_report` og `report_document_id`. Ved købehandling genhentes
dokumentlisten og rapporten, sagstilknytningen bekræftes, og alle regler
vurderes igen. En fjernet, erstattet eller flertydig rapport forhindrer
afslutning.

Hvis hverken API-post eller entydig PDF-rapport kan findes, vurderes sagen
ikke. Dette logges for hver sag og i optællingen til sidst; det
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
`--queue` opretter stadig køelementer ved tørkørsel; `dry_run` beskytter mod
ændringer af sagens status ved behandling af køen.

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
feltværdier, dokumenttitler og sagsbeskrivelser logges ikke. Vedhæftede PDF'er
læses også, og en entydig rapport logges med dokument-id, skadenummer,
reaktionsscore og krisehjælp. Dynamiske felter bruges ikke automatisk som
erstatning for krænkelsespostens felter.

Et opslag, der fejler, markeres i loggen og tælles ikke som en gennemført
søgning uden resultater. Loggen er grundlag for at vælge næste opslag eller
for at give InsuBiz-support konkrete eksempler; den kan ikke bevise, at et
skema ikke findes i brugerfladen.

## Test

```sh
uv run --with pytest python -m pytest
```

Ved lokal kørsel kopieres `.env.example` til `.env`, og `ATS_URL`, eventuelt
`ATS_TOKEN`, `ATS_PROCESS` og `ATS_WORKQUEUE_OVERRIDE` sættes. `ATS_PROCESS`
peger på en proces med den tilknyttede credential. På Automation Server findes
processen automatisk fra den aktuelle session.

## Kodestruktur

- `main.py`: opstart, parametre og valg af køopbygning, behandling eller diagnose.
- `configuration.py`: proces-credential og validering af credential-data.
- `workflow.py`: køopbygning, sagslogs og behandling af køelementer.
- `insubiz.py`: API-klient og de tre forretningsregler.
- `report_pdf.py`: aflæsning af vedhæftede krænkelsesrapporter.
- `diagnostics.py`: undersøgelse af udvalgte sager uden ændringer.

Opdelingen følger [Odenses proces-template](https://github.com/odense-rpa/process-template/blob/main/main.py)
med `AutomationServer.from_environment()`, proces-workqueue og særskilt
køopbygning og købehandling. Køelementer bevares ved gentagen køopbygning;
aktive referencer bruges til at undgå dubletter.
