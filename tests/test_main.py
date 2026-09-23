import unittest

from main import populate_queue


class FakeClient:
    async def authenticate(self) -> None:
        pass

    async def get_infringing_acts(self, page_no: int, page_size: int) -> dict:
        return {"data": [{"id": 44, "incident": {"id": 12}}]}

    async def get_incident(self, incident_id: int) -> dict:
        return {"personalInjury": {"accidentAbsence": 0}, "status": {"id": 2}}

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

        queued = await populate_queue(workqueue, FakeClient(), closed_status_id=9)

        self.assertEqual(queued, 1)
        self.assertEqual(
            workqueue.added_items,
            [({"incident_id": 12, "infringing_act_id": 44}, "insubiz-incident-12")],
        )
