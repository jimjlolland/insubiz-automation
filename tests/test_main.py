import unittest

from main import format_case_context, parse_status_ids, populate_queue


class FakeClient:
    def __init__(self) -> None:
        self.requested_status_ids: list[int] = []

    async def authenticate(self) -> None:
        pass

    async def find_incidents_by_status(
        self, page_no: int, page_size: int, incident_status_id: int
    ) -> dict:
        self.requested_status_ids.append(incident_status_id)
        return {
            "totalRows": 1,
            "data": [{"id": 12, "lastEditing": "2026-09-24T12:00:00"}],
        }

    async def get_incident(self, incident_id: int) -> dict:
        return {
            "personalInjury": {"accidentDuration": {"id": 1}},
            "status": {"id": 0},
        }

    async def get_infringing_act(self, incident_id: int, infringing_act_id: int | None = None) -> dict:
        return {"id": 44, "incident": {"id": incident_id}, "postActQ1": False, "reactionQ4": True}


class FakeWorkqueue:
    def __init__(self) -> None:
        self.added_items: list[tuple[dict, str]] = []

    def get_item_by_reference(self, reference: str, status: object) -> list:
        return []

    def add_item(self, data: dict, reference: str) -> None:
        self.added_items.append((data, reference))


class PopulateQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_eligible_incident_is_added_as_a_work_item(self) -> None:
        workqueue = FakeWorkqueue()
        client = FakeClient()

        queued = await populate_queue(
            workqueue, client, closed_status_id=9, active_incident_status_ids=(0,)
        )

        self.assertEqual(queued, 1)
        self.assertEqual(client.requested_status_ids, [0])
        self.assertEqual(
            workqueue.added_items,
            [({"incident_id": 12, "infringing_act_id": 44}, "insubiz-incident-12")],
        )


class ConfigurationTests(unittest.TestCase):
    def test_status_ids_default_to_new_incidents(self) -> None:
        self.assertEqual(parse_status_ids(None), (0,))

    def test_status_ids_can_be_configured(self) -> None:
        self.assertEqual(parse_status_ids("1, 2, 7"), (1, 2, 7))


class CaseLoggingTests(unittest.TestCase):
    def test_case_context_contains_decision_fields_without_free_text(self) -> None:
        context = format_case_context(
            {
                "incidentNumberInternal": 1234,
                "status": {"id": 1, "text": "Åben"},
                "personalInjury": {
                    "accidentDuration": {"id": 1, "text": "Under én dag"}
                },
                "incidentDescription": "Følsom sagsbeskrivelse",
            },
            {"postActQ1": False, "reactionQ4": True},
        )

        self.assertEqual(
            context,
            "skadenr.=1234, status=Åben (1), fravær=Under én dag (1), "
            "krisehjælp=nej, reaktionsscore=4",
        )
