"""InsuBiz case selection and processing helpers for the main workflow."""

from __future__ import annotations

import logging

from automation_server_client import WorkItemError, WorkItemStatus, Workqueue

from configuration import DEFAULT_ACTIVE_INCIDENT_STATUS_IDS
from insubiz import InsuBizClient, InsuBizError, evaluate_eligibility, reaction_score
from report_pdf import read_incident_pdf_report


PAGE_SIZE = 100


def format_list_item(value: object) -> str:
    """Render InsuBiz classification values without logging free-text case data."""
    if not isinstance(value, dict):
        return "ikke oplyst"
    text = value.get("text")
    item_id = value.get("id")
    if text and item_id is not None:
        return f"{text} ({item_id})"
    if text:
        return str(text)
    if item_id is not None:
        return str(item_id)
    return "ikke oplyst"


def format_case_context(incident: dict, infringing_act: dict | None = None) -> str:
    """Create a concise, non-sensitive summary for an individual case log."""
    personal_injury = incident.get("personalInjury") or {}
    parts = [
        f"skadenr.={incident.get('incidentNumberInternal', 'ikke oplyst')}",
        f"status={format_list_item(incident.get('status'))}",
        f"fravær={format_list_item(personal_injury.get('accidentDuration'))}",
    ]
    if infringing_act is not None:
        crisis_help = infringing_act.get("postActQ1")
        crisis_help_text = "ja" if crisis_help is True else "nej" if crisis_help is False else "ikke oplyst"
        score = reaction_score(infringing_act)
        parts.extend(
            [
                f"krisehjælp={crisis_help_text}",
                f"reaktionsscore={score if score is not None else 'mangler/flere svar'}",
            ]
        )
    return ", ".join(parts)


async def load_new_infringing_acts(client: InsuBizClient) -> dict[int, dict[int, dict]]:
    """Index all returned new-case posts by incident id and post id."""
    logger = logging.getLogger(__name__)
    posts: dict[int, dict[int, dict]] = {}
    seen: set[tuple[int, int]] = set()
    page_no = 1
    logger.info("Henter krænkelsesposter via GetIncidentInfringActsPagedAsync med incidentStatusId=0 uden datofilter")
    while True:
        result = await client.get_infringing_acts_by_status(page_no, PAGE_SIZE, 0)
        if not isinstance(result, dict) or not isinstance(result.get("data"), list):
            raise InsuBizError("Krænkelseslisten har ugyldig struktur; forventede data som liste")
        acts = result["data"]
        total = result.get("totalRows")
        if total is not None and (type(total) is not int or total < 0):
            raise InsuBizError("Krænkelseslisten har ugyldigt totalRows")
        logger.info(
            "Krænkelsesposter for status Ny: side %s indeholder %s, totalRows=%s",
            page_no, len(acts), total if total is not None else "ukendt",
        )
        previous_count = len(seen)
        for act in acts:
            linked_incident = act.get("incident") if isinstance(act, dict) else None
            incident_id = linked_incident.get("id") if isinstance(linked_incident, dict) else None
            act_id = act.get("id") if isinstance(act, dict) else None
            if any(type(value) is not int or value <= 0 for value in (incident_id, act_id)):
                raise InsuBizError("Krænkelseslisten indeholder en post uden gyldigt sags-id eller post-id")
            seen.add((incident_id, act_id))
            posts.setdefault(incident_id, {})[act_id] = act
        if total is not None and len(seen) >= total:
            return posts
        if total is None and len(acts) < PAGE_SIZE:
            return posts
        if len(seen) == previous_count:
            raise InsuBizError("Krænkelseslisten kunne ikke indlæses fuldt: ingen nye poster på næste side")
        page_no += 1


async def queue_eligible_incidents(
    workqueue: Workqueue,
    client: InsuBizClient,
    closed_status_id: int,
    active_incident_status_ids: tuple[int, ...] = DEFAULT_ACTIVE_INCIDENT_STATUS_IDS,
) -> int:
    """Find eligible cases and add one auditable work item per incident."""
    logger = logging.getLogger(__name__)
    logger.info("Logger ind i InsuBiz og henter krænkelsessager")
    await client.authenticate()
    logger.info("InsuBiz-login lykkedes")

    incident_summaries: list[dict] = []
    for incident_status_id in active_incident_status_ids:
        if incident_status_id != 0:
            logger.warning("Status-id %s ignoreres: kun Ny (0) behandles", incident_status_id)
            continue
        page_no = 1
        while True:
            incident_search = await client.find_incidents_by_status(
                page_no, PAGE_SIZE, incident_status_id
            )
            incidents = incident_search.get("data") or []
            logger.info(
                "InsuBiz finder %s sager med status-id %s; side %s indeholder %s",
                incident_search.get("totalRows", "ukendt antal"),
                incident_status_id,
                page_no,
                len(incidents),
            )
            incident_summaries.extend(incidents)
            if len(incidents) < PAGE_SIZE:
                break
            page_no += 1

    acts_by_incident = await load_new_infringing_acts(client) if incident_summaries else {}
    queued_count = 0
    skipped_closed = 0
    skipped_status = 0
    skipped_missing_act = 0
    skipped_ambiguous_act = 0
    skipped_ineligible = 0
    skipped_existing = 0
    processed_incident_ids: set[int] = set()
    for incident_summary in incident_summaries:
        incident_id = incident_summary.get("id")
        if type(incident_id) is not int or incident_id <= 0:
            logger.warning("Springer søgeresultat uden gyldigt sags-id over")
            continue
        if incident_id in processed_incident_ids:
            continue
        processed_incident_ids.add(incident_id)

        incident = await client.get_incident(incident_id)
        if (incident.get("status") or {}).get("id") == closed_status_id:
            skipped_closed += 1
            logger.info(
                "Sag %s springes over: allerede afsluttet (%s)",
                incident_id,
                format_case_context(incident),
            )
            continue
        if (incident.get("status") or {}).get("id") != 0:
            skipped_status += 1
            logger.info("Sag %s springes over: status er ikke Ny (%s)",
                        incident_id, format_case_context(incident))
            continue
        acts = acts_by_incident.get(incident_id, {})
        if not acts:
            logger.info("Sag %s: ingen API-krænkelsespost; undersøger vedhæftede PDF-rapporter", incident_id)
            try:
                result = await read_incident_pdf_report(client, incident)
                if result is None:
                    raise InsuBizError("ingen genkendelig krænkelsesrapport i sagens PDF-dokumenter")
            except InsuBizError as error:
                skipped_missing_act += 1
                logger.warning("Sag %s: ikke vurderet: %s (%s)",
                               incident_id, error, format_case_context(incident))
                continue
            report, document_id = result
            act = report.as_infringing_act()
            item_data = {"incident_id": incident_id, "source": "pdf_report",
                         "report_document_id": document_id}
            logger.info("Sag %s: bruger PDF-rapport fra dokument %s (%s)",
                        incident_id, document_id, format_case_context(incident, act))
        elif len(acts) != 1:
            skipped_ambiguous_act += 1
            logger.warning(
                "Sag %s: listeopslaget returnerede %s forskellige krænkelsesposter; "
                "sagen er ikke vurderet (%s)", incident_id, len(acts), format_case_context(incident)
            )
            continue
        else:
            act = next(iter(acts.values()))
            act_id = act["id"]
            item_data = {"incident_id": incident_id, "infringing_act_id": act_id}
            logger.info("Sag %s: fundet krænkelsespost %s i listeopslaget", incident_id, act_id)
        decision = evaluate_eligibility(incident, act)
        if not decision.eligible:
            skipped_ineligible += 1
            logger.info(
                "Sag %s beholdes åben: %s (%s)",
                incident_id,
                decision.reason,
                format_case_context(incident, act),
            )
            continue

        reference = f"insubiz-incident-{incident_id}"
        active_items = workqueue.get_item_by_reference(reference, WorkItemStatus.NEW)
        active_items += workqueue.get_item_by_reference(reference, WorkItemStatus.IN_PROGRESS)
        if active_items:
            skipped_existing += 1
            logger.info(
                "Sag %s findes allerede i køen (%s)",
                incident_id,
                format_case_context(incident, act),
            )
            continue
        workqueue.add_item(
            item_data,
            reference=reference,
        )
        queued_count += 1
        logger.info(
            "Sag %s er lagt i køen: %s (%s)",
            incident_id,
            decision.reason,
            format_case_context(incident, act),
        )

    logger.info(
        "Indlæsning afsluttet: %s lagt i køen, %s allerede afsluttet, "
        "%s opfyldte ikke reglerne, %s fandtes allerede i køen, "
        "%s havde anden status end Ny, %s kunne ikke vurderes via API eller PDF, "
        "%s havde flere krænkelsesposter",
        queued_count,
        skipped_closed,
        skipped_ineligible,
        skipped_existing,
        skipped_status,
        skipped_missing_act,
        skipped_ambiguous_act,
    )
    return queued_count


async def process_incident_item(
    data: dict,
    client: InsuBizClient,
    closed_status_id: int,
    dry_run: bool,
) -> bool:
    """Recheck one queued case; return whether it qualifies for closing."""
    logger = logging.getLogger(__name__)
    incident_id = data.get("incident_id")
    act_id = data.get("infringing_act_id")
    source = data.get("source", "api")
    document_id = data.get("report_document_id")
    if (type(incident_id) is not int or incident_id <= 0
            or source not in {"api", "pdf_report"}
            or (source == "api" and (type(act_id) is not int or act_id <= 0))
            or (source == "pdf_report" and (type(document_id) is not int or document_id <= 0))):
        raise WorkItemError("Work item mangler gyldigt sags-id eller kilde-id")
    incident = await client.get_incident(incident_id)
    if (incident.get("status") or {}).get("id") == closed_status_id:
        logger.info("Sag %s er allerede afsluttet (%s)", incident_id, format_case_context(incident))
        return False
    if (incident.get("status") or {}).get("id") != 0:
        logger.info("Sag %s springes over: status er ikke Ny (%s)",
                    incident_id, format_case_context(incident))
        return False
    if source == "pdf_report":
        result = await read_incident_pdf_report(client, incident, required_document_id=document_id)
        if result is None:
            raise WorkItemError(f"Sag {incident_id}: PDF-rapport kunne ikke hentes")
        report, _ = result
        act = report.as_infringing_act()
        logger.info("Sag %s: genaflæst PDF-rapport fra dokument %s", incident_id, document_id)
    else:
        act = await client.get_infringing_act(incident_id, act_id)
    if act is None:
        raise WorkItemError(f"Sag {incident_id}: krænkelsespost kunne ikke hentes")
    decision = evaluate_eligibility(incident, act)
    if not decision.eligible:
        logger.info("Sag %s beholdes åben: %s (%s)",
                    incident_id, decision.reason, format_case_context(incident, act))
        return False
    if dry_run:
        logger.info("TØRKØRSEL: sag %s ville blive afsluttet: %s (%s)",
                    incident_id, decision.reason, format_case_context(incident, act))
    else:
        await client.close_incident(incident_id, closed_status_id)
        logger.info("Sag %s er afsluttet: %s (%s)",
                    incident_id, decision.reason, format_case_context(incident, act))
    return True
