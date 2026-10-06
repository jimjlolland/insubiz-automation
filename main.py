"""Automation Server entry point for InsuBiz Automatisering 2."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os

import httpx
from automation_server_client import AutomationServer

from configuration import configuration_from_process
from diagnostics import diagnose_incidents
from insubiz import InsuBizError
from workflow import populate_queue, process_workqueue


logger = logging.getLogger(__name__)


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--queue", action="store_true", help="Opbyg køen med kvalificerede nye sager")
    mode.add_argument("--diagnose", nargs="+", type=int, metavar="INCIDENT_ID",
                      help="Undersøg disse API-sags-id'er uden at ændre data")
    args = parser.parse_args(argv)
    if args.diagnose and any(value <= 0 for value in args.diagnose):
        parser.error("--diagnose kræver positive API-sags-id'er")
    return args


def configure_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    # Keep the client's audit-log HTTP calls out of the case log.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    args = parse_arguments(argv)
    configure_logging()
    try:
        ats = AutomationServer.from_environment()
        configuration = configuration_from_process(ats)
        if args.diagnose:
            asyncio.run(diagnose_incidents(configuration.client, args.diagnose))
            return 0

        workqueue = ats.workqueue()
        if args.queue:
            logger.info(
                "Starter køopbygning (lukket status-id: %s, aktive status-id'er: %s)",
                configuration.closed_status_id,
                ", ".join(map(str, configuration.active_incident_status_ids)),
            )
            asyncio.run(populate_queue(
                workqueue, configuration.client, configuration.closed_status_id,
                configuration.active_incident_status_ids,
            ))
        else:
            logger.info("Starter købehandling (%s)",
                        "tørkørsel" if configuration.dry_run else "opdatering af sager")
            asyncio.run(process_workqueue(
                workqueue, configuration.client, configuration.closed_status_id,
                configuration.dry_run,
            ))
        return 0
    except (InsuBizError, ValueError) as error:
        logger.error("Automatiseringen stoppede: %s", error)
    except httpx.HTTPError:
        logger.error("Automatiseringen stoppede: opslag i Automation Server fejlede")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
