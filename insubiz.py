"""Client and eligibility rules for Automatisering 2 in InsuBiz."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API_VERSION_PATH = "/api/v1.3"
CRISIS_HELP_FIELD = "postActQ1"
ABSENCE_UNDER_ONE_DAY_ID = 1


class InsuBizError(RuntimeError):
    """An unsuccessful or invalid response from the InsuBiz API."""


@dataclass(frozen=True)
class Eligibility:
    eligible: bool
    reason: str


def reaction_score(infringing_act: dict[str, Any]) -> int | None:
    """Return the one selected 1--10 reaction score, or None when invalid."""
    selected = [
        score for score in range(1, 11) if infringing_act.get(f"reactionQ{score}") is True
    ]
    return selected[0] if len(selected) == 1 else None


def evaluate_eligibility(incident: dict[str, Any], infringing_act: dict[str, Any]) -> Eligibility:
    """Apply the three business conditions from Automatisering 2."""
    absence_duration = (incident.get("personalInjury") or {}).get("accidentDuration") or {}
    if absence_duration.get("id") != ABSENCE_UNDER_ONE_DAY_ID:
        return Eligibility(False, "fravær er ikke under én dag")
    if infringing_act.get(CRISIS_HELP_FIELD) is True:
        return Eligibility(False, "psykologisk krisehjælp er registreret")
    score = reaction_score(infringing_act)
    if score is None:
        return Eligibility(False, "reaktionsskalaen mangler eller har flere svar")
    if score >= 7:
        return Eligibility(False, f"reaktionsskalaen er {score}")
    return Eligibility(True, f"fravær under én dag, ingen krisehjælp, skala {score}")


class InsuBizClient:
    """Small asynchronous wrapper around the endpoints used by this robot."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        secret_key: str,
        system_owner_id: int | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.secret_key = secret_key
        self.system_owner_id = system_owner_id
        self.token: str | None = None

    async def authenticate(self) -> None:
        payload = {"apiKey": self.api_key, "secretKey": self.secret_key}
        if self.system_owner_id is not None:
            payload["systemOwnerId"] = self.system_owner_id
        response = await self._request(
            "POST", "/Authentication/SignInAsync", payload, authenticated=False
        )
        if not response.get("isAuthenticated") or not response.get("token"):
            raise InsuBizError(response.get("message") or "InsuBiz-login mislykkedes")
        self.token = response["token"]

    async def get_infringing_acts(self, page_no: int, page_size: int) -> dict[str, Any]:
        return await self._request("POST", "/Incident/GetIncidentInfringActsPagedAsync", {"pageNo": page_no, "pageSize": page_size})

    async def get_infringing_act(self, incident_id: int, infringing_act_id: int) -> dict[str, Any]:
        return await self._request("GET", "/Incident/GetIncidentInfringActByIdAsync", query={"incidentId": incident_id, "id": infringing_act_id})

    async def get_incident(self, incident_id: int) -> dict[str, Any]:
        return await self._request("GET", "/Incident/GetIncidentByIdAsync", query={"id": incident_id})

    async def close_incident(self, incident_id: int, closed_status_id: int) -> None:
        await self._request("POST", "/Incident/UpdateIncidentFieldsAsync", {"recordId": incident_id, "fields": [{"name": "status", "value": str(closed_status_id)}]})

    async def _request(self, method: str, path: str, payload: dict[str, Any] | None = None, *, query: dict[str, Any] | None = None, authenticated: bool = True) -> dict[str, Any]:
        if authenticated and not self.token:
            raise InsuBizError("InsuBiz-klienten er ikke logget ind")
        url = f"{self.base_url}{API_VERSION_PATH}{path}"
        if query:
            url = f"{url}?{urlencode(query)}"
        headers = {"Accept": "application/json"}
        if authenticated:
            headers["Authorization"] = f"Bearer {self.token}"
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        def send() -> dict[str, Any]:
            try:
                with urlopen(Request(url, data=data, headers=headers, method=method), timeout=30) as response:
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as error:
                detail = error.read().decode("utf-8", errors="replace")
                raise InsuBizError(f"{method} {path} fejlede ({error.code}): {detail}") from error
            except URLError as error:
                raise InsuBizError(f"Kunne ikke forbinde til InsuBiz: {error.reason}") from error

        return await asyncio.to_thread(send)
