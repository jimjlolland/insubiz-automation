import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from insubiz import InsuBizClient, InsuBizError, evaluate_eligibility
from main import populate_queue, process_workqueue


def case(status=0):
    return {
        "id": 12,
        "incidentNumberInternal": 9560,
        "status": {"id": status},
        "personalInjury": {"accidentDuration": {"id": 1}},
    }


def act():
    return {
        "id": 44,
        "incident": {"id": 12},
        "postActQ1": False,
        "reactionQ4": True,
        "lastEditing": "2020-01-01T00:00:00",
    }


def client_for_workflow(status=0, response=None):
    client = Mock()
    client.authenticate = AsyncMock()
    client.find_incidents_by_status = AsyncMock(
        return_value={"data": [{"id": 12}], "totalRows": 1}
    )
    client.get_incident = AsyncMock(return_value=case(status))
    client.get_infringing_act = AsyncMock(return_value=response)
    client.close_incident = AsyncMock()
    return client


@pytest.mark.parametrize("post_id", [None, 44])
def test_direct_lookup_uses_incident_id_and_only_includes_known_post_id(post_id):
    client = InsuBizClient("https://example.test", "key", "secret")
    client._request = AsyncMock(return_value=act())
    result = asyncio.run(client.get_infringing_act(12, post_id))
    query = {"incidentId": 12}
    if post_id is not None:
        query["id"] = post_id
    client._request.assert_awaited_once_with(
        "GET", "/Incident/GetIncidentInfringActByIdAsync", query=query
    )
    assert result["id"] == 44


@pytest.mark.parametrize("response", [None, {}])
def test_empty_direct_response_is_unavailable(response):
    client = InsuBizClient("https://example.test", "key", "secret")
    client._request = AsyncMock(return_value=response)
    assert asyncio.run(client.get_infringing_act(12)) is None


@pytest.mark.parametrize("response", [
    [],
    {"data": [act()]},
    {**act(), "incident": {"id": 99}},
    {**act(), "incident": None},
    {**act(), "id": 0},
    {**act(), "id": 45},
])
def test_unverifiable_or_wrong_post_is_rejected(response):
    client = InsuBizClient("https://example.test", "key", "secret")
    client._request = AsyncMock(return_value=response)
    with pytest.raises(InsuBizError):
        asyncio.run(client.get_infringing_act(12, 44))


@pytest.mark.parametrize("value", [None, 0, "false", True])
def test_crisis_help_requires_explicit_boolean_false(value):
    response = {**act(), "postActQ1": value}
    assert not evaluate_eligibility(case(), response).eligible


def test_missing_crisis_help_prevents_eligibility():
    response = act()
    del response["postActQ1"]
    assert not evaluate_eligibility(case(), response).eligible


def test_queue_finds_older_post_even_without_incident_timestamp():
    client = client_for_workflow(response=act())
    queue = Mock()
    queue.get_item_by_reference.return_value = []
    assert asyncio.run(populate_queue(queue, client, 3)) == 1
    client.get_infringing_act.assert_awaited_once_with(12)
    queue.add_item.assert_called_once_with(
        {"incident_id": 12, "infringing_act_id": 44}, reference="insubiz-incident-12"
    )


def test_queue_logs_full_case_when_post_is_unavailable(caplog):
    client = client_for_workflow()
    queue = Mock()
    assert asyncio.run(populate_queue(queue, client, 3)) == 0
    queue.add_item.assert_not_called()
    assert "skadenr.=9560" in caplog.text
    assert "fravær=1" in caplog.text
    assert "ikke vurderet" in caplog.text


@pytest.mark.parametrize("status", [1, 2, 3, None])
def test_queue_skips_incident_no_longer_new(status):
    client = client_for_workflow(status, act())
    queue = Mock()
    assert asyncio.run(populate_queue(queue, client, 3)) == 0
    queue.add_item.assert_not_called()
    client.get_infringing_act.assert_not_awaited()


class WorkItem:
    id = 7
    data = {"incident_id": 12, "infringing_act_id": 44}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.mark.parametrize("status", [1, 2, 3, None])
def test_existing_queue_item_cannot_close_case_no_longer_new(status):
    client = client_for_workflow(status, act())
    assert asyncio.run(process_workqueue([WorkItem()], client, 3, False)) == 0
    client.close_incident.assert_not_awaited()
    client.get_infringing_act.assert_not_awaited()


@pytest.mark.parametrize("dry_run", [True, False])
def test_valid_new_case_respects_dry_run(dry_run):
    client = client_for_workflow(response=act())
    assert asyncio.run(process_workqueue([WorkItem()], client, 3, dry_run)) == 1
    client.get_infringing_act.assert_awaited_once_with(12, 44)
    if dry_run:
        client.close_incident.assert_not_awaited()
    else:
        client.close_incident.assert_awaited_once_with(12, 3)


def test_unavailable_post_never_closes_queued_case(caplog):
    client = client_for_workflow()
    assert asyncio.run(process_workqueue([WorkItem()], client, 3, False)) == 0
    client.close_incident.assert_not_awaited()
    assert "krænkelsespost kunne ikke hentes" in caplog.text
