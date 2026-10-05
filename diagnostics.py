"""Read-only inspection of case links, without logging personal case content."""

import logging

from insubiz import InsuBizClient, InsuBizError, reaction_score

logger = logging.getLogger(__name__)
PAGE_SIZE = 100


async def customer_posts(client: InsuBizClient, customer_id: int, search: bool) -> list[dict]:
    """Read every page; reject incomplete or structurally invalid results."""
    posts: dict[tuple[int, int], dict] = {}
    page_no = 1
    endpoint = "FindInfringingActsPagedAsync" if search else "GetIncidentInfringActsPagedAsync"
    while True:
        result = await client.get_customer_infringing_acts(
            page_no, PAGE_SIZE, customer_id, search=search
        )
        if not isinstance(result, dict) or not isinstance(result.get("data"), list):
            raise InsuBizError(f"{endpoint}: forventede data som liste")
        rows = result["data"]
        total = result.get("totalRows")
        if total is not None and (type(total) is not int or total < 0):
            raise InsuBizError(f"{endpoint}: ugyldigt totalRows")
        logger.info(
            "DIAGNOSE: %s?customerId=%s (uden status- og datofilter), side %s: "
            "%s poster, totalRows=%s", endpoint, customer_id, page_no, len(rows), total,
        )
        previous_count = len(posts)
        for row in rows:
            incident = row.get("incident") if isinstance(row, dict) else None
            incident_id = incident.get("id") if isinstance(incident, dict) else None
            post_id = row.get("id") if isinstance(row, dict) else None
            if any(type(value) is not int or value <= 0 for value in (incident_id, post_id)):
                raise InsuBizError(f"{endpoint}: post uden gyldigt incident.id eller id")
            posts[(incident_id, post_id)] = row
        if total is not None and len(posts) >= total:
            return list(posts.values())
        if total is None and len(rows) < PAGE_SIZE:
            return list(posts.values())
        if len(posts) == previous_count:
            raise InsuBizError(f"{endpoint}: indlæsningen er ufuldstændig; siden gav ingen nye poster")
        page_no += 1


def log_post(incident_id: int, post: dict) -> None:
    """Log rule values and selected violence categories, never descriptions."""
    selected = [n for n in range(1, 12) if post.get(f"violenceTypeQ{n}") is True]
    crisis = post.get("postActQ1")
    crisis_text = "ja" if crisis is True else "nej" if crisis is False else "mangler/ugyldig"
    logger.info(
        "DIAGNOSE: sag %s, post-id=%s, krisehjælp=%s, reaktionsscore=%s, "
        "valgte violenceTypeQ-numre=%s",
        incident_id, post["id"], crisis_text, reaction_score(post), selected,
    )


async def diagnose_incidents(client: InsuBizClient, incident_ids: list[int]) -> None:
    """Inspect only requested cases; inspect customer lists to discover their posts."""
    logger.info("DIAGNOSE: læser API-data; opretter ingen køelementer og ændrer ingen sager")
    await client.authenticate()
    logger.info("DIAGNOSE: login lykkedes, konfigureret systemOwnerId=%s", client.system_owner_id)
    cache: dict[tuple[int, bool], list[dict] | None] = {}
    for incident_id in dict.fromkeys(incident_ids):
        try:
            incident = await client.get_incident(incident_id, include_dynamic_fields=True)
            if not isinstance(incident, dict) or incident.get("id") != incident_id:
                raise InsuBizError("Sagsopslaget returnerede ikke det forventede sags-id")
        except InsuBizError as error:
            logger.error("DIAGNOSE: sag %s kunne ikke hentes: %s", incident_id, error)
            continue
        logger.info(
            "DIAGNOSE: sag %s, skadenr.=%s, status-id=%s, type-id=%s, "
            "undertype-id=%s, fraværs-id=%s",
            incident_id, incident.get("incidentNumberInternal"),
            (incident.get("status") or {}).get("id"),
            (incident.get("type") or {}).get("id"),
            (incident.get("subType") or {}).get("id"),
            ((incident.get("personalInjury") or {}).get("accidentDuration") or {}).get("id"),
        )
        dynamic_fields = incident.get("dynamicFields") or []
        logger.info("DIAGNOSE: sag %s har %s dynamiske felter; værdier logges ikke", incident_id, len(dynamic_fields))
        for field in dynamic_fields:
            if isinstance(field, dict):
                logger.info(
                    "DIAGNOSE: dynamisk felt id=%s, name=%s, datatype-id=%s",
                    field.get("id"), field.get("name"), (field.get("dataType") or {}).get("id"),
                )

        customer_id = (incident.get("customer") or {}).get("id")
        matches: dict[int, dict] = {}
        completed = 0
        if type(customer_id) is not int or customer_id <= 0:
            logger.error("DIAGNOSE: sag %s mangler kunde-id; kundeopslag kan ikke udføres", incident_id)
        else:
            for search in (False, True):
                key = (customer_id, search)
                if key not in cache:
                    try:
                        cache[key] = await customer_posts(client, customer_id, search)
                    except InsuBizError as error:
                        cache[key] = None
                        logger.error("DIAGNOSE: kundeopslag fejlede: %s", error)
                rows = cache[key]
                if rows is None:
                    continue
                completed += 1
                found = [row for row in rows if row["incident"]["id"] == incident_id]
                logger.info(
                    "DIAGNOSE: sag %s matcher %s poster i %s",
                    incident_id, len(found),
                    "FindInfringingActsPagedAsync" if search else "GetIncidentInfringActsPagedAsync",
                )
                for row in found:
                    matches[row["id"]] = row
        for post_id in matches:
            try:
                post = await client.get_infringing_act(incident_id, post_id)
                if post is None:
                    raise InsuBizError("Direkte opslag gav ingen post")
                log_post(incident_id, post)
            except InsuBizError as error:
                logger.error("DIAGNOSE: sag %s, post %s kunne ikke hentes: %s", incident_id, post_id, error)
        if not matches:
            logger.warning(
                "DIAGNOSE: sag %s har ingen fundne post-id'er; %s af 2 kundeopslag "
                "blev gennemført. Det beviser ikke, at skemaet mangler i InsuBiz.", incident_id, completed,
            )
        try:
            documents = await client.get_incident_documents(incident_id)
            document_ids = [doc.get("id") for doc in documents if isinstance(doc, dict)]
            logger.info(
                "DIAGNOSE: sag %s har %s dokumenter, dokument-id'er=%s; "
                "titler og indhold logges ikke", incident_id, len(documents), document_ids,
            )
        except InsuBizError as error:
            logger.error("DIAGNOSE: dokumentopslag for sag %s fejlede: %s", incident_id, error)
    logger.info("DIAGNOSE afsluttet; se eventuelle fejl ovenfor. Ingen sager eller køelementer er ændret.")
