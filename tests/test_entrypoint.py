import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from automation_server_client import WorkItem, WorkItemError

from insubiz import InsuBizError
from main import main, process_workqueue


@pytest.mark.parametrize("arguments,mode", [([], "process"), (["--queue"], "queue"),
                                         (["--diagnose", "12", "13"], "diagnose")])
def test_each_mode_loads_process_configuration_once(arguments, mode, monkeypatch):
    ats = Mock()
    configuration = SimpleNamespace(client=object(), closed_status_id=3,
                                    active_incident_status_ids=(0,), dry_run=True)
    initialize = Mock(return_value=ats)
    load = Mock(return_value=configuration)
    queue = AsyncMock()
    process = AsyncMock()
    diagnose = AsyncMock()
    monkeypatch.setattr("main.AutomationServer.from_environment", initialize)
    monkeypatch.setattr("main.configuration_from_process", load)
    monkeypatch.setattr("main.populate_queue", queue)
    monkeypatch.setattr("main.process_workqueue", process)
    monkeypatch.setattr("main.diagnose_incidents", diagnose)

    assert main(arguments) == 0
    initialize.assert_called_once_with()
    load.assert_called_once_with(ats)
    if mode == "diagnose":
        ats.workqueue.assert_not_called()
        diagnose.assert_awaited_once_with(configuration.client, [12, 13])
    elif mode == "queue":
        queue.assert_awaited_once_with(ats.workqueue.return_value, configuration.client, 3, (0,))
    else:
        process.assert_awaited_once_with(ats.workqueue.return_value, configuration.client, 3, True)
    assert queue.await_count + process.await_count + diagnose.await_count == 1


def test_missing_process_credential_fails_before_queue_access(monkeypatch, caplog):
    ats = Mock()
    monkeypatch.setattr("main.AutomationServer.from_environment", Mock(return_value=ats))
    monkeypatch.setattr("main.configuration_from_process", Mock(side_effect=InsuBizError("Processen mangler credential")))
    assert main(["--queue"]) == 1
    ats.workqueue.assert_not_called()
    assert "Processen mangler credential" in caplog.text


def test_server_initialization_failure_is_reported_without_http_error_body(monkeypatch, caplog):
    request = httpx.Request("GET", "https://automation.test/api/sessions/1")
    response = httpx.Response(500, json={"error": "HEMMELIG"}, request=request)
    error = httpx.HTTPStatusError("HEMMELIG", request=request, response=response)
    monkeypatch.setattr("main.AutomationServer.from_environment", Mock(side_effect=error))
    assert main(["--queue"]) == 1
    assert "opslag i Automation Server fejlede" in caplog.text
    assert "HEMMELIG" not in caplog.text


@pytest.mark.parametrize("error", [WorkItemError("Manuel vurdering"), RuntimeError("API-fejl")])
def test_failed_item_is_marked_once_and_next_item_completes(error, monkeypatch):
    timestamp = datetime.now(timezone.utc)
    items = [WorkItem(id=item_id, data={"incident_id": item_id}, locked=True,
                      status="in progress", message="", workqueue_id=1,
                      created_at=timestamp, updated_at=timestamp) for item_id in (12, 13)]
    client = SimpleNamespace(authenticate=AsyncMock())
    process_item = AsyncMock(side_effect=[error, True])
    monkeypatch.setattr("main.process_incident_item", process_item)
    monkeypatch.setattr("automation_server_client._models.ats_logging_handler", Mock())
    update = Mock(return_value=httpx.Response(
        200, request=httpx.Request("PUT", "https://automation.test/api/workitems/12/status"),
    ))
    monkeypatch.setattr("automation_server_client._models.httpx.put", update)

    assert asyncio.run(process_workqueue(items, client, 3, True)) == 1

    client.authenticate.assert_awaited_once_with()
    assert process_item.await_count == 2
    assert items[0].status == "failed"
    assert items[0].message == str(error)
    assert items[1].status == "completed"
    assert [call.kwargs["json"]["status"] for call in update.call_args_list] == ["failed", "completed"]
