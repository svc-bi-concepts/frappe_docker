"""The approval ladder of the desk assistant's actions: assistant_levels.compute_level, pure, no frappe.

Invented scores and amounts only (round figures). Each row of the ladder, the confidence rules, the caps and the
settings that move a level 0 action to level 1.

    python3 -m unittest bi_finance.test_assistant_levels
"""

import unittest
from types import SimpleNamespace

from bi_finance.assistant_levels import (
    ALWAYS_APPROVER,
    AUTO,
    NEVER,
    ONE_CLICK,
    check_score,
    compute_level,
)

AUTO_TYPE = SimpleNamespace(level_class=AUTO)
ONE_CLICK_TYPE = SimpleNamespace(level_class=ONE_CLICK)
ALWAYS_TYPE = SimpleNamespace(level_class=ALWAYS_APPROVER)

SETTINGS = {
    "auto_level_enabled": 1,
    "auto_threshold": 0.8,
    "one_click_threshold": 0.9,
    "one_click_amount_limit": 1000,
    "auto_actions_per_day": 50,
    "auto_actions_today": 0,
}


def passed(name="ok", hard=True, **kw):
    return {"check": name, "passed": 1, "hard": hard, "cap": 0, **kw}


def failed(name="bad", cap=False):
    return {"check": name, "passed": 0, "hard": 1, "cap": 1 if cap else 0}


def soft(score, ok=True):
    return {
        "check": f"evidence {score}",
        "passed": 1 if ok else 0,
        "hard": 0,
        "score": score,
    }


class Ladder(unittest.TestCase):
    def level(self, action, model=1.0, checks=(), amount=0, **settings):
        return compute_level(
            action, model, list(checks), amount, {**SETTINGS, **settings}
        )

    def test_a_level_0_type_at_the_threshold_applies_on_its_own(self):
        self.assertEqual(self.level(AUTO_TYPE, 0.80), (0.80, 0))

    def test_a_low_model_score_moves_a_level_0_type_up_to_1(self):
        self.assertEqual(self.level(AUTO_TYPE, 0.79), (0.79, 1))

    def test_a_low_check_score_moves_it_up_too(self):
        self.assertEqual(self.level(AUTO_TYPE, 1.0, [soft(0.6)]), (0.6, 1))

    def test_confidence_is_the_lower_of_the_two_scores(self):
        self.assertEqual(self.level(ONE_CLICK_TYPE, 0.95, [soft(0.9)])[0], 0.9)
        self.assertEqual(self.level(ONE_CLICK_TYPE, 0.92, [soft(1.0)])[0], 0.92)

    def test_a_one_click_type_within_the_limits_is_level_1(self):
        self.assertEqual(self.level(ONE_CLICK_TYPE, 0.9, amount=1000), (0.9, 1))

    def test_an_amount_above_the_limit_moves_a_one_click_type_to_2(self):
        self.assertEqual(self.level(ONE_CLICK_TYPE, 1.0, amount=1000.01), (1.0, 2))

    def test_a_credit_amount_counts_by_its_size(self):
        self.assertEqual(self.level(ONE_CLICK_TYPE, 1.0, amount=-2000)[1], 2)

    def test_a_confidence_below_the_one_click_threshold_is_level_2(self):
        self.assertEqual(self.level(ONE_CLICK_TYPE, 0.89, amount=10), (0.89, 2))

    def test_an_always_approver_type_stays_2_at_any_confidence(self):
        self.assertEqual(self.level(ALWAYS_TYPE, 0.99), (0.99, 2))
        self.assertEqual(self.level(ALWAYS_TYPE, 0.1)[1], 2)

    def test_the_thresholds_and_the_limit_are_the_settings(self):
        self.assertEqual(self.level(AUTO_TYPE, 0.7, auto_threshold=0.7)[1], 0)
        self.assertEqual(
            self.level(ONE_CLICK_TYPE, 0.85, one_click_threshold=0.85)[1], 1
        )
        self.assertEqual(
            self.level(ONE_CLICK_TYPE, 1.0, amount=500, one_click_amount_limit=400)[1],
            2,
        )

    def test_a_missing_setting_is_the_default(self):
        self.assertEqual(compute_level(AUTO_TYPE, 0.8, [], 0, {})[1], 0)
        self.assertEqual(compute_level(ONE_CLICK_TYPE, 0.9, [], 1000, {})[1], 1)

    def test_auto_off_moves_level_0_to_1(self):
        self.assertEqual(self.level(AUTO_TYPE, 1.0, auto_level_enabled=0)[1], 1)

    def test_the_daily_cap_moves_level_0_to_1(self):
        self.assertEqual(self.level(AUTO_TYPE, 1.0, auto_actions_today=49)[1], 0)
        self.assertEqual(self.level(AUTO_TYPE, 1.0, auto_actions_today=50)[1], 1)

    def test_a_model_score_that_is_no_number_is_no_confidence(self):
        self.assertEqual(self.level(AUTO_TYPE, None), (0.0, 1))
        self.assertEqual(self.level(AUTO_TYPE, "high"), (0.0, 1))

    def test_a_model_score_above_1_counts_as_1(self):
        self.assertEqual(self.level(AUTO_TYPE, 7)[0], 1.0)


class Checks(unittest.TestCase):
    def level(self, action, checks, model=1.0, amount=0):
        return compute_level(action, model, checks, amount, SETTINGS)

    def test_a_structural_hard_failure_refuses(self):
        self.assertEqual(
            self.level(ONE_CLICK_TYPE, [passed(), failed("amount")]), (0.0, NEVER)
        )
        self.assertEqual(self.level(AUTO_TYPE, [failed("target open")]), (0.0, NEVER))
        self.assertEqual(self.level(ALWAYS_TYPE, [failed("direction")]), (0.0, NEVER))

    def test_a_capped_failure_caps_the_confidence_at_half(self):
        self.assertEqual(
            self.level(ONE_CLICK_TYPE, [failed("ledger date", cap=True)]), (0.5, 2)
        )

    def test_a_capped_failure_moves_a_level_0_type_to_1(self):
        self.assertEqual(
            self.level(AUTO_TYPE, [failed("duplicate", cap=True)]), (0.5, 1)
        )

    def test_a_structural_failure_wins_over_a_capped_one(self):
        checks = [failed("duplicate", cap=True), failed("currency")]
        self.assertEqual(self.level(ONE_CLICK_TYPE, checks), (0.0, NEVER))

    def test_a_soft_check_that_failed_is_not_a_hard_failure(self):
        self.assertEqual(
            self.level(ONE_CLICK_TYPE, [soft(0.9), soft(0.6, ok=False)])[0], 0.9
        )

    def test_the_check_score_is_the_strongest_passed_evidence(self):
        self.assertEqual(
            check_score([soft(0.6), soft(0.95), soft(1.0, ok=False)]), 0.95
        )

    def test_no_soft_evidence_scores_one(self):
        self.assertEqual(check_score([passed()]), 1.0)
        self.assertEqual(check_score([]), 1.0)

    def test_evidence_that_all_failed_scores_zero(self):
        self.assertEqual(check_score([soft(0.9, ok=False)]), 0.0)
        self.assertEqual(self.level(ONE_CLICK_TYPE, [soft(0.9, ok=False)])[1], 2)

    def test_an_unknown_level_class_raises(self):
        with self.assertRaises(ValueError):
            compute_level(SimpleNamespace(level_class="x"), 1.0, [], 0, SETTINGS)


class Purity(unittest.TestCase):
    def test_the_module_does_not_import_frappe(self):
        import bi_finance.assistant_levels as levels

        self.assertNotIn("frappe", vars(levels))


if __name__ == "__main__":
    unittest.main()
