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

from insubiz import InsuBizClient, InsuBizError, evaluate_eligibility


PAGE_SIZE = 100


@dataclass(frozen=True)
class InsuBizConfiguration:
    client: InsuBizClient
    closed_status_id: int
    dry_run: bool


def insubiz_client_from_credential() -> InsuBizConfiguration:
    """Read InsuBiz secrets from Automation Server's encrypted credential store."""
    credential_name = os.getenv("INSUBIZ_CREDENTIAL_NAME", "InsuBiz API")
    credential = Credential.get_credential(credential_name)
    data = credential.data
    base_url = data.get("base_url") or os.getenv("INSUBIZ_BASE_URL")
    api_key = credential.username or data.get("api_key") or os.getenv("INSUBIZ_API_KEY")
    secret_key = credential.password or data.get("secret_key") or os.getenv("INSUBIZ_SECRET_KEY")
    closed_status_id = data.get("closed_status_id") or os.getenv("INSUBIZ_CLOSED_STATUS_ID")
    if not base_url or not api_key or not secret_key or not closed_status_id:
        raise InsuBizError(
            "Credentialen skal indeholde base_url, api_key, secret_key og closed_status_id"
        )
    dry_run = str(data.get("dry_run", "true")).lower() in {"1", "true", "yes"}
    return InsuBizConfiguration(
        client=InsuBizClient(base_url, api_key, secret_key),
        closed_status_id=int(closed_status_id),
        dry_run=dry_run,
    )


async def populate_queue(workqueue: Workqueue, client: InsuBizClient, closed_status_id: int) -> int:
    """Find eligible cases and add one auditable work item per incident."""
    logger = logging.getLogger(__name__)
    await client.authenticate()

    page_no = 1
    processed_incident_ids: set[int] = set()
    queued_count = 0
    while True:
        result = await client.get_infringing_acts(page_no, PAGE_SIZE)
        acts = result.get("data") or []
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
                continue
            act = await client.get_infringing_act(incident_id, act_id)
            decision = evaluate_eligibility(incident, act)
            if not decision.eligible:
                logger.info("Sag %s beholdes åben: %s", incident_id, decision.reason)
                continue

            reference = f"insubiz-incident-{incident_id}"
            active_items = workqueue.get_item_by_reference(reference, WorkItemStatus.NEW)
            active_items += workqueue.get_item_by_reference(
                reference, WorkItemStatus.IN_PROGRESS
            )
            if active_items:
                logger.info("Sag %s findes allerede i køen", incident_id)
                continue
            workqueue.add_item(
                {"incident_id": incident_id, "infringing_act_id": act_id},
                reference=reference,
            )
            queued_count += 1
            logger.info("Sag %s er lagt i køen (%s)", incident_id, decision.reason)

        if len(acts) < PAGE_SIZE:
            break
        page_no += 1

    logger.info("Indlæsning afsluttet: %s sager lagt i køen", queued_count)
    return queued_count


async def process_workqueue(
    workqueue: Workqueue,
    client: InsuBizClient,
    closed_status_id: int,
    dry_run: bool,
) -> int:
    """Recheck and process each queued case in an Automation Server work-item context."""
    logger = logging.getLogger(__name__)
    await client.authenticate()
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
                    logger.info("Sag %s er allerede afsluttet", incident_id)
                    continue
                act = await client.get_infringing_act(incident_id, act_id)
                decision = evaluate_eligibility(incident, act)
                if not decision.eligible:
                    logger.info("Sag %s beholdes åben: %s", incident_id, decision.reason)
                    continue
                if dry_run:
                    logger.info(
                        "TØRKØRSEL: sag %s ville blive afsluttet (%s)",
                        incident_id,
                        decision.reason,
                    )
                else:
                    await client.close_incident(incident_id, closed_status_id)
                    logger.info("Sag %s er afsluttet (%s)", incident_id, decision.reason)
                processed_count += 1
        except Exception:
            logger.exception("Work item %s fejlede", item.id)
    return processed_count


if __name__ == "__main__":
    ats = AutomationServer.from_environment()
    workqueue = ats.workqueue()
    logging.getLogger().setLevel(os.getenv("LOG_LEVEL", "INFO"))
    try:
        configuration = insubiz_client_from_credential()
        if "--queue" in sys.argv:
            asyncio.run(
                populate_queue(
                    workqueue,
                    configuration.client,
                    configuration.closed_status_id,
                )
            )
        else:
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
