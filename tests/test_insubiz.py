import unittest

from insubiz import InsuBizClient, evaluate_eligibility, reaction_score


def incident(absence: int) -> dict:
    return {"personalInjury": {"accidentAbsence": absence}}


def act(*, crisis_help: bool = False, score: int | None = 4) -> dict:
    data = {"postActQ1": crisis_help}
    if score is not None:
        data[f"reactionQ{score}"] = True
    return data


class EligibilityTests(unittest.TestCase):
    def test_case_is_eligible_when_all_three_conditions_are_met(self) -> None:
        self.assertTrue(evaluate_eligibility(incident(0), act(score=6)).eligible)

    def test_case_is_not_eligible_when_crisis_help_is_selected(self) -> None:
        decision = evaluate_eligibility(incident(0), act(crisis_help=True, score=4))
        self.assertFalse(decision.eligible)
        self.assertIn("krisehjælp", decision.reason)

    def test_case_is_not_eligible_at_score_seven(self) -> None:
        self.assertFalse(evaluate_eligibility(incident(0), act(score=7)).eligible)

    def test_reaction_score_requires_exactly_one_selection(self) -> None:
        self.assertIsNone(reaction_score({"reactionQ1": True, "reactionQ2": True}))


class RecordingClient(InsuBizClient):
    def __init__(self, system_owner_id: int | None) -> None:
        super().__init__("https://example.test", "api-key", "secret-key", system_owner_id)
        self.payload: dict | None = None

    async def _request(self, method: str, path: str, payload: dict | None = None, **kwargs: object) -> dict:
        self.payload = payload
        return {"isAuthenticated": True, "token": "token"}


class AuthenticationTests(unittest.IsolatedAsyncioTestCase):
    async def test_system_owner_id_is_sent_when_configured(self) -> None:
        client = RecordingClient(system_owner_id=42)

        await client.authenticate()

        self.assertEqual(client.payload["systemOwnerId"], 42)

    async def test_system_owner_id_is_omitted_when_not_configured(self) -> None:
        client = RecordingClient(system_owner_id=None)

        await client.authenticate()

        self.assertNotIn("systemOwnerId", client.payload)
