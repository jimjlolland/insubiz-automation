import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from automation_server_client import AutomationServerConfig, Credential

from configuration import (
    configuration_from_credential,
    configuration_from_process,
    parse_dry_run,
)
from insubiz import InsuBizError


def credential_payload(credential_id=77, name="Valgfrit navn", **changes):
    timestamp = datetime.now(timezone.utc).isoformat()
    return {
        "id": credential_id, "name": name, "username": "HEMMELIG_API_KEY",
        "password": "HEMMELIG_SECRET_KEY", "deleted": False,
        "created_at": timestamp, "updated_at": timestamp,
        "data": {"base_url": "https://example.test", "closed_status_id": "3",
                 "system_owner_id": "141", "incident_status_ids": "0", "dry_run": "true"},
        **changes,
    }


def server(credential_id=77, git_credential_id=99):
    return SimpleNamespace(
        url="https://automation.test/api/",
        process=SimpleNamespace(id=12, credentials_id=credential_id,
                                target_credentials_id=git_credential_id),
    )


def response(payload, status=200):
    return httpx.Response(status, json=payload,
                          request=httpx.Request("GET", "https://automation.test/api/credentials/77"))


def test_process_selection_uses_id_and_ignores_name_and_git_credential(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setenv("INSUBIZ_CREDENTIAL_NAME", "Navn fra gammel konfiguration")
    monkeypatch.setattr(AutomationServerConfig, "token", "ATS_TEST_TOKEN")
    request = Mock(return_value=response(credential_payload()))
    monkeypatch.setattr("configuration.httpx.get", request)
    result = configuration_from_process(server())
    request.assert_called_once_with(
        "https://automation.test/api/credentials/77",
        headers={"Authorization": "Bearer ATS_TEST_TOKEN"}, timeout=30,
    )
    assert result.client.api_key == "HEMMELIG_API_KEY"
    assert result.client.secret_key == "HEMMELIG_SECRET_KEY"
    assert result.client.system_owner_id == 141
    assert result.closed_status_id == 3
    assert result.active_incident_status_ids == (0,)
    assert result.dry_run is True
    assert "credential-id 77 fra proces-id 12" in caplog.text
    assert "HEMMELIG" not in caplog.text
    assert "HEMMELIG" not in repr(result)


@pytest.mark.parametrize("name", ["InsuBiz test", "InsuBiz drift", "Omdøbt credential"])
def test_credential_rename_does_not_change_process_selection(name, monkeypatch):
    request = Mock(return_value=response(credential_payload(name=name)))
    monkeypatch.setattr("configuration.httpx.get", request)
    assert configuration_from_process(server()).dry_run is True
    assert request.call_args.args[0].endswith("/credentials/77")


def test_two_processes_can_choose_different_credentials(monkeypatch):
    request = Mock(side_effect=[response(credential_payload(77)), response(credential_payload(88))])
    monkeypatch.setattr("configuration.httpx.get", request)
    configuration_from_process(server(77))
    configuration_from_process(server(88))
    assert [call.args[0] for call in request.call_args_list] == [
        "https://automation.test/api/credentials/77", "https://automation.test/api/credentials/88",
    ]


@pytest.mark.parametrize("credential_id", [None, 0, -1, True, "invalid"])
def test_missing_process_credential_never_uses_git_or_legacy_name(credential_id, monkeypatch):
    request = Mock()
    monkeypatch.setattr("configuration.httpx.get", request)
    monkeypatch.setattr("configuration.Credential.get_credential", Mock())
    with pytest.raises(InsuBizError):
        configuration_from_process(server(credential_id))
    request.assert_not_called()
    Credential.get_credential.assert_not_called()


@pytest.mark.parametrize("status", [401, 403, 404, 410, 500])
def test_failed_lookup_does_not_leak_error_body_or_credentials(status, monkeypatch):
    monkeypatch.setattr("configuration.httpx.get", Mock(return_value=response({"detail": "HEMMELIG"}, status)))
    with pytest.raises(RuntimeError, match=f"HTTP {status}") as error:
        configuration_from_process(server())
    assert "HEMMELIG" not in str(error.value)


@pytest.mark.parametrize("payload", [
    credential_payload(88), credential_payload(deleted=True),
    {"id": 77, "username": "HEMMELIG", "password": "HEMMELIG"},
])
def test_wrong_deleted_or_invalid_credential_is_rejected_without_secret_values(payload, monkeypatch):
    monkeypatch.setattr("configuration.httpx.get", Mock(return_value=response(payload)))
    with pytest.raises(RuntimeError) as error:
        configuration_from_process(server())
    assert "HEMMELIG" not in str(error.value)


def test_network_error_has_readable_message(monkeypatch):
    monkeypatch.setattr("configuration.httpx.get", Mock(side_effect=httpx.ConnectError("HEMMELIG")))
    with pytest.raises(RuntimeError, match="Kunne ikke forbinde") as error:
        configuration_from_process(server())
    assert "HEMMELIG" not in str(error.value)


def test_local_execution_loads_the_process_selected_by_ats_process(monkeypatch):
    ats = server()
    ats.process = None
    monkeypatch.setattr(AutomationServerConfig, "process", "12")
    get_process = Mock(return_value=server().process)
    monkeypatch.setattr("configuration.Process.get_process", get_process)
    monkeypatch.setattr("configuration.httpx.get", Mock(return_value=response(credential_payload())))
    configuration_from_process(ats)
    get_process.assert_called_once_with(12)


def test_session_process_takes_precedence_over_local_process_setting(monkeypatch):
    monkeypatch.setattr(AutomationServerConfig, "process", "999")
    get_process = Mock()
    monkeypatch.setattr("configuration.Process.get_process", get_process)
    monkeypatch.setattr("configuration.httpx.get", Mock(return_value=response(credential_payload())))
    configuration_from_process(server())
    get_process.assert_not_called()


def test_local_execution_requires_process_id(monkeypatch):
    ats = server()
    ats.process = None
    monkeypatch.setattr(AutomationServerConfig, "process", None)
    with pytest.raises(RuntimeError, match="ATS_PROCESS"):
        configuration_from_process(ats)


def test_credential_data_are_not_overridden_by_environment(monkeypatch):
    monkeypatch.setenv("INSUBIZ_API_KEY", "WRONG_API_KEY")
    monkeypatch.setenv("INSUBIZ_SECRET_KEY", "WRONG_SECRET_KEY")
    monkeypatch.setenv("INSUBIZ_BASE_URL", "https://wrong.test")
    result = configuration_from_credential(Credential.model_validate(credential_payload()))
    assert result.client.api_key == "HEMMELIG_API_KEY"
    assert result.client.secret_key == "HEMMELIG_SECRET_KEY"
    assert result.client.base_url == "https://example.test"


@pytest.mark.parametrize("value,expected", [(True, True), (False, False), ("true", True),
                                         ("false", False), (" TRUE ", True), ("1", True), ("0", False)])
def test_valid_dry_run_settings(value, expected):
    assert parse_dry_run(value) is expected


@pytest.mark.parametrize("value", [None, "", "tru", "disabled", [], {}])
def test_invalid_dry_run_never_silently_enables_status_updates(value):
    with pytest.raises(RuntimeError, match="dry_run"):
        parse_dry_run(value)


def test_dry_run_defaults_to_true_when_omitted():
    payload = credential_payload()
    del payload["data"]["dry_run"]
    assert configuration_from_credential(Credential.model_validate(payload)).dry_run is True


@pytest.mark.parametrize("changes", [{"username": None}, {"password": None},
                                      {"data": {"base_url": "https://example.test"}}])
def test_missing_required_credential_fields_fail_before_insubiz_access(changes):
    with pytest.raises(RuntimeError):
        configuration_from_credential(Credential.model_validate(credential_payload(**changes)))
