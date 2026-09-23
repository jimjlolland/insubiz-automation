import unittest

from main import parse_status_ids, populate_queue


class FakeClient:
    def __init__(self) -> None:
        self.requested_status_ids: list[int] = []

    async def authenticate(self) -> None:
        pass

    async def get_infringing_acts(
        self, page_no: int, page_size: int, incident_status_id: int
    ) -> dict:
        self.requested_status_ids.append(incident_status_id)
        return {
            "data": [
                {
                    "id": 44,
                    "incident": {"id": 12},
                    "postActQ1": False,
                    "reactionQ4": True,
                }
            ]
        }

    async def get_incident(self, incident_id: int) -> dict:
        return {
            "personalInjury": {"accidentDuration": {"id": 1}},
            "status": {"id": 2},
        }

    async def get_infringing_act(self, incident_id: int, infringing_act_id: int) -> dict:
        return {"postActQ1": False, "reactionQ4": True}


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
            workqueue, client, closed_status_id=9, active_incident_status_ids=(1, 2)
        )

        self.assertEqual(queued, 1)
        self.assertEqual(client.requested_status_ids, [1, 2])
        self.assertEqual(
            workqueue.added_items,
            [({"incident_id": 12, "infringing_act_id": 44}, "insubiz-incident-12")],
        )


class ConfigurationTests(unittest.TestCase):
    def test_status_ids_default_to_open_and_reopened(self) -> None:
        self.assertEqual(parse_status_ids(None), (1, 2))

    def test_status_ids_can_be_configured(self) -> None:
        self.assertEqual(parse_status_ids("1, 2, 7"), (1, 2, 7))
