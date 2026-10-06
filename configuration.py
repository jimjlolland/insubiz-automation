"""Load InsuBiz settings from the credential assigned to the current process."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx
from automation_server_client import (
    AutomationServer,
    AutomationServerConfig,
    Credential,
    Process,
)

from insubiz import InsuBizClient, InsuBizError


DEFAULT_ACTIVE_INCIDENT_STATUS_IDS = (0,)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InsuBizConfiguration:
    client: InsuBizClient = field(repr=False)
    closed_status_id: int
    active_incident_status_ids: tuple[int, ...]
    dry_run: bool


def positive_id(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise InsuBizError(f"{name} skal være et positivt heltal")
    try:
        result = int(value)
    except ValueError:
        raise InsuBizError(f"{name} skal være et positivt heltal") from None
    if result <= 0:
        raise InsuBizError(f"{name} skal være et positivt heltal")
    return result


def parse_status_ids(value: object) -> tuple[int, ...]:
    if value is None or not str(value).strip():
        return DEFAULT_ACTIVE_INCIDENT_STATUS_IDS
    try:
        result = tuple(int(part.strip()) for part in str(value).split(",") if part.strip())
    except ValueError:
        raise InsuBizError("incident_status_ids skal være kommaseparerede status-id'er") from None
    if not result or any(status < 0 for status in result):
        raise InsuBizError("incident_status_ids skal indeholde mindst ét gyldigt status-id")
    return result


def parse_dry_run(value: object) -> bool:
    """Reject typos instead of silently enabling updates to InsuBiz."""
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    raise InsuBizError("dry_run skal være true eller false")


def configuration_from_credential(credential: Credential) -> InsuBizConfiguration:
    data = credential.data
    if not isinstance(data, dict):
        raise InsuBizError("Credentialens Data-felt skal være et JSON-objekt")
    base_url = data.get("base_url")
    if not isinstance(base_url, str) or not base_url.strip():
        raise InsuBizError("Credentialens Data-felt mangler base_url")
    if not credential.username or not credential.password:
        raise InsuBizError("Credentialen skal have API-nøglen i Username og secret key i Password")
    owner_id = data.get("system_owner_id")
    return InsuBizConfiguration(
        client=InsuBizClient(
            base_url.strip(), credential.username, credential.password,
            system_owner_id=positive_id(owner_id, "system_owner_id") if owner_id is not None else None,
        ),
        closed_status_id=positive_id(data.get("closed_status_id"), "closed_status_id"),
        active_incident_status_ids=parse_status_ids(data.get("incident_status_ids")),
        dry_run=parse_dry_run(data.get("dry_run", True)),
    )


def configuration_from_process(ats: AutomationServer) -> InsuBizConfiguration:
    """Use credentials_id; target_credentials_id belongs to repository access."""
    process = ats.process
    if process is None:
        if not AutomationServerConfig.process:
            raise InsuBizError("Kør via Automation Server eller angiv ATS_PROCESS ved lokal kørsel")
        process = Process.get_process(positive_id(AutomationServerConfig.process, "ATS_PROCESS"))
    credential_id = process.credentials_id
    if credential_id is None:
        raise InsuBizError(
            "Processen mangler en InsuBiz-credential i credentials_id. "
            "Git credentials bruges til repositoryet; tilknyt en separat proces-credential."
        )
    credential_id = positive_id(credential_id, "Processens credentials_id")
    # automation-server-client 0.3.0 exposes lookup by name only. The server
    # supports lookup by id, preserving the selection even after a rename.
    try:
        response = httpx.get(
            f"{ats.url.rstrip('/')}/credentials/{credential_id}",
            headers=AutomationServerConfig.auth_headers(),
            timeout=30,
        )
        response.raise_for_status()
        credential = Credential.model_validate(response.json())
    except httpx.HTTPStatusError as error:
        raise InsuBizError(
            f"Processens credential {credential_id} kunne ikke hentes (HTTP {error.response.status_code})"
        ) from None
    except httpx.RequestError:
        raise InsuBizError("Kunne ikke forbinde til Automation Servers credential-opslag") from None
    except ValueError:
        # Model validation errors may include secret values from the response.
        raise InsuBizError("Automation Server returnerede en ugyldig credential") from None
    if credential.id != credential_id or credential.deleted:
        raise InsuBizError("Processens credential er slettet eller har et forkert ID")
    configuration = configuration_from_credential(credential)
    logger.info("Bruger credential-id %s fra proces-id %s", credential_id, process.id)
    return configuration
