import asyncio
from unittest.mock import AsyncMock, Mock, call

import pytest

from insubiz import InsuBizClient, InsuBizError, evaluate_eligibility
from workflow import populate_queue, process_workqueue


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
    client.get_infringing_acts_by_status = AsyncMock(
        return_value={"data": [response] if response else [], "totalRows": 1 if response else 0}
    )
    client.get_infringing_act = AsyncMock(return_value=response)
    client.get_incident_documents = AsyncMock(return_value=[])
    client.system_owner_id = 141
    client.close_incident = AsyncMock()
    return client


def test_direct_lookup_uses_both_ids():
    client = InsuBizClient("https://example.test", "key", "secret")
    client._request = AsyncMock(return_value=act())
    result = asyncio.run(client.get_infringing_act(12, 44))
    query = {"incidentId": 12, "id": 44}
    client._request.assert_awaited_once_with(
        "GET", "/Incident/GetIncidentInfringActByIdAsync", query=query
    )
    assert result["id"] == 44


@pytest.mark.parametrize("post_id", [None, 0, -1, True, "44"])
def test_direct_lookup_rejects_invalid_post_id_before_request(post_id):
    client = InsuBizClient("https://example.test", "key", "secret")
    client._request = AsyncMock()
    with pytest.raises(InsuBizError):
        asyncio.run(client.get_infringing_act(12, post_id))
    client._request.assert_not_awaited()


def test_list_lookup_filters_incident_status_without_date_or_post_status():
    client = InsuBizClient("https://example.test", "key", "secret")
    client._request = AsyncMock(return_value={"data": [], "totalRows": 0})
    asyncio.run(client.get_infringing_acts_by_status(2, 100, 0))
    client._request.assert_awaited_once_with(
        "POST", "/Incident/GetIncidentInfringActsPagedAsync",
        {"pageNo": 2, "pageSize": 100}, query={"incidentStatusId": 0},
    )


@pytest.mark.parametrize("response", [None, {}])
def test_empty_direct_response_is_unavailable(response):
    client = InsuBizClient("https://example.test", "key", "secret")
    client._request = AsyncMock(return_value=response)
    assert asyncio.run(client.get_infringing_act(12, 44)) is None


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
    client.get_infringing_act.assert_not_awaited()
    client.get_infringing_acts_by_status.assert_awaited_once_with(1, 100, 0)
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
    client.get_infringing_act.assert_not_awaited()


def test_empty_list_logs_both_new_cases_without_direct_lookup(caplog):
    client = client_for_workflow()
    client.find_incidents_by_status.return_value = {
        "data": [{"id": 12}, {"id": 13}], "totalRows": 2,
    }
    client.get_incident.side_effect = [case(), {**case(), "id": 13, "incidentNumberInternal": 9577}]
    queue = Mock()
    assert asyncio.run(populate_queue(queue, client, 3)) == 0
    assert "Sag 12" in caplog.text
    assert "Sag 13" in caplog.text
    assert "skadenr.=9577" in caplog.text
    client.get_infringing_act.assert_not_awaited()
    queue.add_item.assert_not_called()


def test_list_pagination_finds_target_after_short_server_page():
    client = client_for_workflow(response=act())
    client.get_infringing_acts_by_status.side_effect = [
        {"data": [{**act(), "id": 99, "incident": {"id": 99}}], "totalRows": 2},
        {"data": [act()], "totalRows": 2},
    ]
    queue = Mock()
    queue.get_item_by_reference.return_value = []
    assert asyncio.run(populate_queue(queue, client, 3)) == 1
    assert client.get_infringing_acts_by_status.await_args_list == [call(1, 100, 0), call(2, 100, 0)]
    client.get_incident.assert_awaited_once_with(12)
    client.get_infringing_act.assert_not_awaited()


def test_multiple_distinct_posts_prevent_arbitrary_selection(caplog):
    client = client_for_workflow(response=act())
    client.get_infringing_acts_by_status.return_value = {
        "data": [act(), {**act(), "id": 45}], "totalRows": 2,
    }
    queue = Mock()
    assert asyncio.run(populate_queue(queue, client, 3)) == 0
    queue.add_item.assert_not_called()
    assert "2 forskellige krænkelsesposter" in caplog.text


@pytest.mark.parametrize("response", [
    {}, {"data": None}, {"data": [{**act(), "id": 0}], "totalRows": 1},
    {"data": [{**act(), "incident": None}], "totalRows": 1},
    {"data": [], "totalRows": 2},
])
def test_invalid_or_incomplete_list_fails_before_queue_mutation(response):
    client = client_for_workflow()
    client.get_infringing_acts_by_status.return_value = response
    queue = Mock()
    with pytest.raises(InsuBizError):
        asyncio.run(populate_queue(queue, client, 3))
    queue.add_item.assert_not_called()


def test_repeated_list_page_fails_instead_of_looping():
    client = client_for_workflow(response=act())
    client.get_infringing_acts_by_status.return_value = {"data": [act()], "totalRows": 2}
    with pytest.raises(InsuBizError):
        asyncio.run(populate_queue(Mock(), client, 3))
    assert client.get_infringing_acts_by_status.await_count == 2


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
