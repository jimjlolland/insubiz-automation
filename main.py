"""Automation Server process for InsuBiz Automatisering 2."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from dataclasses import dataclass

from automation_server_client import (
    AutomationServer,
    Credential,
    WorkItemError,
    WorkItemStatus,
    Workqueue,
)

from insubiz import InsuBizClient, InsuBizError, evaluate_eligibility, reaction_score


PAGE_SIZE = 100
DEFAULT_ACTIVE_INCIDENT_STATUS_IDS = (0,)


def configure_logging() -> None:
    """Show process events in the run output and keep HTTP client noise out."""
    root_logger = logging.getLogger()
    root_logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))
    if not any(getattr(handler, "_insubiz_console", False) for handler in root_logger.handlers):
        console_handler = logging.StreamHandler()
        console_handler._insubiz_console = True  # type: ignore[attr-defined]
        console_handler.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
        )
        root_logger.addHandler(console_handler)

    # The Automation Server client writes each log event to its audit API.  Do
    # not log those HTTP calls too, as that produces a stream of 204 responses.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


@dataclass(frozen=True)
class InsuBizConfiguration:
    client: InsuBizClient
    closed_status_id: int
    active_incident_status_ids: tuple[int, ...]
    dry_run: bool


def parse_status_ids(value: object) -> tuple[int, ...]:
    """Parse a comma-separated credential setting with a safe default."""
    if value is None or not str(value).strip():
        return DEFAULT_ACTIVE_INCIDENT_STATUS_IDS
    status_ids = tuple(int(part.strip()) for part in str(value).split(",") if part.strip())
    if not status_ids:
        raise ValueError("incident_status_ids skal indeholde mindst ét status-id")
    return status_ids


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


def insubiz_client_from_credential() -> InsuBizConfiguration:
    """Read InsuBiz secrets from Automation Server's encrypted credential store."""
    credential_name = os.getenv("INSUBIZ_CREDENTIAL_NAME", "InsuBiz API")
    credential = Credential.get_credential(credential_name)
    data = credential.data
    base_url = data.get("base_url") or os.getenv("INSUBIZ_BASE_URL")
    api_key = credential.username or data.get("api_key") or os.getenv("INSUBIZ_API_KEY")
    secret_key = credential.password or data.get("secret_key") or os.getenv("INSUBIZ_SECRET_KEY")
    closed_status_id = data.get("closed_status_id") or os.getenv("INSUBIZ_CLOSED_STATUS_ID")
    system_owner_id = data.get("system_owner_id")
    if not base_url or not api_key or not secret_key or not closed_status_id:
        raise InsuBizError(
            "Credentialen skal indeholde base_url, api_key, secret_key og closed_status_id"
        )
    dry_run = str(data.get("dry_run", "true")).lower() in {"1", "true", "yes"}
    return InsuBizConfiguration(
        client=InsuBizClient(
            base_url,
            api_key,
            secret_key,
            system_owner_id=int(system_owner_id) if system_owner_id else None,
        ),
        closed_status_id=int(closed_status_id),
        active_incident_status_ids=parse_status_ids(
            data.get("incident_status_ids") or os.getenv("INSUBIZ_INCIDENT_STATUS_IDS")
        ),
        dry_run=dry_run,
    )


async def populate_queue(
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

    processed_incident_ids: set[int] = set()
    queued_count = 0
    skipped_closed = 0
    skipped_ineligible = 0
    skipped_existing = 0
    for incident_status_id in active_incident_status_ids:
        page_no = 1
        logger.info("Henter kun sager med status-id %s", incident_status_id)
        while True:
            result = await client.get_infringing_acts(page_no, PAGE_SIZE, incident_status_id)
            acts = result.get("data") or []
            logger.info(
                "Status %s, side %s: modtog %s krænkelsessager",
                incident_status_id,
                page_no,
                len(acts),
            )
            for act_summary in acts:
                incident_id = (act_summary.get("incident") or {}).get("id")
                act_id = act_summary.get("id")
                if not isinstance(incident_id, int) or not isinstance(act_id, int):
                    logger.warning("Springer post uden gyldige id'er over: %s", act_summary)
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
                decision = evaluate_eligibility(incident, act_summary)
                if not decision.eligible:
                    skipped_ineligible += 1
                    logger.info(
                        "Sag %s beholdes åben: %s (%s)",
                        incident_id,
                        decision.reason,
                        format_case_context(incident, act_summary),
                    )
                    continue

                reference = f"insubiz-incident-{incident_id}"
                active_items = workqueue.get_item_by_reference(reference, WorkItemStatus.NEW)
                active_items += workqueue.get_item_by_reference(
                    reference, WorkItemStatus.IN_PROGRESS
                )
                if active_items:
                    skipped_existing += 1
                    logger.info(
                        "Sag %s findes allerede i køen (%s)",
                        incident_id,
                        format_case_context(incident, act_summary),
                    )
                    continue
                workqueue.add_item(
                    {"incident_id": incident_id, "infringing_act_id": act_id},
                    reference=reference,
                )
                queued_count += 1
                logger.info(
                    "Sag %s er lagt i køen: %s (%s)",
                    incident_id,
                    decision.reason,
                    format_case_context(incident, act_summary),
                )

            if len(acts) < PAGE_SIZE:
                break
            page_no += 1

    logger.info(
        "Indlæsning afsluttet: %s lagt i køen, %s allerede afsluttet, %s opfyldte ikke reglerne, %s fandtes allerede i køen",
        queued_count,
        skipped_closed,
        skipped_ineligible,
        skipped_existing,
    )
    return queued_count


async def process_workqueue(
    workqueue: Workqueue,
    client: InsuBizClient,
    closed_status_id: int,
    dry_run: bool,
) -> int:
    """Recheck and process each queued case in an Automation Server work-item context."""
    logger = logging.getLogger(__name__)
    logger.info("Logger ind i InsuBiz for at behandle køen")
    await client.authenticate()
    logger.info("InsuBiz-login lykkedes")
    processed_count = 0
    for item in workqueue:
        try:
            with item:
                incident_id = item.data.get("incident_id")
                act_id = item.data.get("infringing_act_id")
                if not isinstance(incident_id, int) or not isinstance(act_id, int):
                    raise WorkItemError(
                        "Work item mangler gyldigt incident_id eller infringing_act_id"
                    )
                incident = await client.get_incident(incident_id)
                if (incident.get("status") or {}).get("id") == closed_status_id:
                    logger.info(
                        "Sag %s er allerede afsluttet (%s)",
                        incident_id,
                        format_case_context(incident),
                    )
                    continue
                act = await client.get_infringing_act(incident_id, act_id)
                decision = evaluate_eligibility(incident, act)
                if not decision.eligible:
                    logger.info(
                        "Sag %s beholdes åben: %s (%s)",
                        incident_id,
                        decision.reason,
                        format_case_context(incident, act),
                    )
                    continue
                if dry_run:
                    logger.info(
                        "TØRKØRSEL: sag %s ville blive afsluttet: %s (%s)",
                        incident_id,
                        decision.reason,
                        format_case_context(incident, act),
                    )
                else:
                    await client.close_incident(incident_id, closed_status_id)
                    logger.info(
                        "Sag %s er afsluttet: %s (%s)",
                        incident_id,
                        decision.reason,
                        format_case_context(incident, act),
                    )
                processed_count += 1
        except Exception:
            logger.exception("Work item %s fejlede", item.id)
    return processed_count


if __name__ == "__main__":
    ats = AutomationServer.from_environment()
    workqueue = ats.workqueue()
    configure_logging()
    try:
        configuration = insubiz_client_from_credential()
        if "--queue" in sys.argv:
            logging.getLogger(__name__).info(
                "Starter køopbygning (lukket status-id: %s, aktive status-id'er: %s)",
                configuration.closed_status_id,
                ", ".join(map(str, configuration.active_incident_status_ids)),
            )
            asyncio.run(
                populate_queue(
                    workqueue,
                    configuration.client,
                    configuration.closed_status_id,
                    configuration.active_incident_status_ids,
                )
            )
        else:
            logging.getLogger(__name__).info(
                "Starter købehandling (%s)",
                "tørkørsel" if configuration.dry_run else "opdatering af sager",
            )
            asyncio.run(
                process_workqueue(
                    workqueue,
                    configuration.client,
                    configuration.closed_status_id,
                    configuration.dry_run,
                )
            )
    except (InsuBizError, ValueError) as error:
        logging.getLogger(__name__).error("Automatiseringen stoppede: %s", error)
        sys.exit(1)
