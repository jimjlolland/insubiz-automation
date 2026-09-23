import unittest

from insubiz import evaluate_eligibility, reaction_score


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
