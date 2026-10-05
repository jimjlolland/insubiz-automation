import asyncio
import logging
from unittest.mock import AsyncMock, Mock, call

import pytest

from diagnostics import customer_posts, diagnose_incidents
from insubiz import InsuBizClient, InsuBizError
from main import parse_arguments


def incident(incident_id=12):
    return {
        "id": incident_id, "incidentNumberInternal": 9594,
        "customer": {"id": 100, "name": "HEMMELIGT_KUNDENAVN"},
        "status": {"id": 0}, "subType": {"id": 3109},
        "personalInjury": {"accidentDuration": {"id": 1}},
        "incidentDescription": "HEMMELIG_SAGSBESKRIVELSE",
        "dynamicFields": [{"id": 7, "name": "customReaction", "dataType": {"id": 1},
                           "value": "HEMMELIG_FELTVÆRDI"}],
    }


def post():
    return {"id": 44, "incident": {"id": 12}, "reactionQ4": True,
            "postActQ1": False, "violenceTypeQ3": True,
            "eventDescription": "HEMMELIG_POSTBESKRIVELSE"}


def fake_client():
    client = Mock(spec=InsuBizClient)
    client.system_owner_id = 141
    client.get_incident.return_value = incident()
    client.get_customer_infringing_acts.return_value = {"data": [], "totalRows": 0}
    client.get_infringing_act.return_value = post()
    client.get_incident_documents.return_value = [{"id": 77, "title": "HEMMELIG_DOKUMENTTITEL"}]
    return client


def test_diagnosis_finds_post_through_alternative_endpoint_and_logs_only_summary(caplog):
    caplog.set_level(logging.INFO)
    client = fake_client()
    client.get_customer_infringing_acts.side_effect = [
        {"data": [], "totalRows": 0},
        {"data": [{"id": 44, "incident": {"id": 12}}], "totalRows": 1},
    ]
    asyncio.run(diagnose_incidents(client, [12]))
    client.get_incident.assert_awaited_once_with(12, include_dynamic_fields=True)
    client.get_infringing_act.assert_awaited_once_with(12, 44)
    client.close_incident.assert_not_called()
    assert "krisehjælp=nej" in caplog.text
    assert "reaktionsscore=4" in caplog.text
    assert "violenceTypeQ-numre=[3]" in caplog.text
    assert "customReaction" in caplog.text
    assert "dokument-id'er=[77]" in caplog.text
    assert "HEMMELIG" not in caplog.text


def test_empty_results_are_logged_without_guessing_post_ids(caplog):
    caplog.set_level(logging.INFO)
    client = fake_client()
    asyncio.run(diagnose_incidents(client, [12]))
    client.get_infringing_act.assert_not_called()
    client.close_incident.assert_not_called()
    assert "2 af 2 kundeopslag blev gennemført" in caplog.text
    assert "Det beviser ikke" in caplog.text


def test_failed_endpoint_is_distinguished_from_empty_result_and_other_checks_continue(caplog):
    caplog.set_level(logging.INFO)
    client = fake_client()
    client.get_customer_infringing_acts.side_effect = [
        InsuBizError("HTTP 500"), {"data": [], "totalRows": 0},
    ]
    asyncio.run(diagnose_incidents(client, [12]))
    assert "kundeopslag fejlede" in caplog.text
    assert "1 af 2 kundeopslag blev gennemført" in caplog.text
    client.get_incident_documents.assert_awaited_once_with(12)


def test_shared_customer_lists_are_loaded_once_and_only_selected_cases_are_fetched():
    client = fake_client()
    client.get_incident.side_effect = [incident(12), incident(13)]
    client.get_customer_infringing_acts.return_value = {
        "data": [{"id": 99, "incident": {"id": 999}}], "totalRows": 1,
    }
    asyncio.run(diagnose_incidents(client, [12, 13, 12]))
    assert client.get_customer_infringing_acts.await_count == 2
    assert client.get_incident.await_args_list == [
        call(12, include_dynamic_fields=True), call(13, include_dynamic_fields=True),
    ]
    client.get_infringing_act.assert_not_called()
    client.close_incident.assert_not_called()


def test_customer_scan_reads_past_short_server_page():
    client = fake_client()
    client.get_customer_infringing_acts.side_effect = [
        {"data": [{"id": 99, "incident": {"id": 99}}], "totalRows": 2},
        {"data": [post()], "totalRows": 2},
    ]
    rows = asyncio.run(customer_posts(client, 100, True))
    assert len(rows) == 2
    assert client.get_customer_infringing_acts.await_args_list == [
        call(1, 100, 100, search=True), call(2, 100, 100, search=True),
    ]


@pytest.mark.parametrize("response", [
    {}, {"data": [], "totalRows": 2},
    {"data": [{"id": 44}], "totalRows": 1},
])
def test_incomplete_or_unlinked_lists_do_not_count_as_empty_search(response):
    client = fake_client()
    client.get_customer_infringing_acts.return_value = response
    with pytest.raises(InsuBizError):
        asyncio.run(customer_posts(client, 100, False))


def test_repeated_page_does_not_loop():
    client = fake_client()
    client.get_customer_infringing_acts.return_value = {"data": [post()], "totalRows": 2}
    with pytest.raises(InsuBizError):
        asyncio.run(customer_posts(client, 100, False))
    assert client.get_customer_infringing_acts.await_count == 2


@pytest.mark.parametrize("search,endpoint", [
    (False, "/Incident/GetIncidentInfringActsPagedAsync"),
    (True, "/Incident/FindInfringingActsPagedAsync"),
])
def test_customer_requests_omit_status_and_date_filters(search, endpoint):
    client = InsuBizClient("https://example.test", "key", "secret")
    client._request = AsyncMock(return_value={"data": [], "totalRows": 0})
    asyncio.run(client.get_customer_infringing_acts(1, 100, 100, search=search))
    client._request.assert_awaited_once_with(
        "POST", endpoint, {"pageNo": 1, "pageSize": 100}, query={"customerId": 100},
    )


def test_dynamic_fields_are_requested_explicitly():
    client = InsuBizClient("https://example.test", "key", "secret")
    client._request = AsyncMock(return_value=incident())
    asyncio.run(client.get_incident(12, include_dynamic_fields=True))
    client._request.assert_awaited_once_with(
        "GET", "/Incident/GetIncidentByIdAsync", query={"id": 12, "includeDynamicFields": "true"},
    )


def test_diagnostic_command_accepts_multiple_api_ids():
    args = parse_arguments(["--diagnose", "2494158", "2492267"])
    assert args.diagnose == [2494158, 2492267]
    assert not args.queue


@pytest.mark.parametrize("args", [
    ["--queue", "--diagnose", "12"], ["--diagnose"], ["--diagnose", "0"],
])
def test_diagnostic_command_rejects_conflicting_modes_or_invalid_ids(args):
    with pytest.raises(SystemExit):
        parse_arguments(args)
