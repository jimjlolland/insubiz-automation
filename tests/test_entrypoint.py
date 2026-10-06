from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from insubiz import InsuBizError
from main import main


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
