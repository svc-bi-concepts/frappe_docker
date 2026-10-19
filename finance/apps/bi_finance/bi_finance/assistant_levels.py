"""The approval ladder of the desk assistant's actions, as a pure function: no frappe, so the host tests run it.

An action type belongs to a level class (what the bookkeeper's plan, erp-00z0, allows it to reach); the ladder
turns a proposal's scores into the level it needs:

    0 auto        no ledger effect, reversible; logged with Undo. Only at confidence >= auto_threshold.
    1 one click   the user who asked approves on the proposal card. Confidence >= one_click_threshold and the amount
                  within one_click_amount_limit.
    2 approver    a user with the role AI Approver approves on the AI Action form, typing the amount.
    never         refused and logged.

confidence = min(model score, check score). The check score is the strongest passed soft evidence among the checks
(a check row with a "score"); a type with no soft evidence scores 1.0. A failed hard check refuses the action when it
is structural (wrong currency, direction or amount; target not open) and caps the confidence at 0.5 when it carries
"cap" (a duplicate, the ledger date), so the proposal lands in level 2, or in level 1 for a level 0 type.
"""

import datetime

AUTO = "auto"  # level class: level 0 when the confidence allows, else 1
ONE_CLICK = "one_click"  # level class: level 1 within the limits, else 2
ALWAYS_APPROVER = "always_approver"  # level class: level 2 whatever the confidence

NEVER = "never"

# the ladder's defaults, the ones AI Assistant Settings carries
AUTO_THRESHOLD = 0.80
ONE_CLICK_THRESHOLD = 0.90
ONE_CLICK_AMOUNT_LIMIT = 1000.0
AUTO_ACTIONS_PER_DAY = 50
CAP_CONFIDENCE = 0.5

# bexio leads until this day (the bookkeeper's D1); the setting ledger_actions_from replaces it
LEDGER_ACTIONS_FROM_DEFAULT = datetime.date(2027, 1, 1)


def check_score(checks):
    """The strongest soft evidence among the passed checks; 1.0 when no check is soft, 0.0 when none of them passed."""
    soft = [check for check in checks if check.get("score") is not None]
    if not soft:
        return 1.0
    return max((check["score"] for check in soft if check.get("passed")), default=0.0)


def failed_hard(checks):
    """The hard checks that did not pass."""
    return [check for check in checks if check.get("hard") and not check.get("passed")]


def compute_level(action, model_score, checks, amount, settings):
    """(confidence, level) of one proposal; level is 0, 1, 2 or "never".

    action: the registry entry, read for its level_class. checks: rows with check, passed, hard, cap and score.
    settings: auto_level_enabled, auto_threshold, one_click_threshold, one_click_amount_limit, auto_actions_per_day and
    auto_actions_today (what the user's level 0 actions of today already are); a missing value is the default above.
    """
    failed = failed_hard(checks)
    if any(not check.get("cap") for check in failed):
        return 0.0, NEVER
    confidence = min(unit_score(model_score), check_score(checks))
    if failed:
        confidence = min(confidence, CAP_CONFIDENCE)
    level_class = action.level_class
    if level_class == ALWAYS_APPROVER:
        return confidence, 2
    if level_class == AUTO:
        auto_on = bool(_value(settings, "auto_level_enabled", True))
        today_n = int(_value(settings, "auto_actions_today", 0))
        within_cap = today_n < int(
            _value(settings, "auto_actions_per_day", AUTO_ACTIONS_PER_DAY)
        )
        if (
            auto_on
            and within_cap
            and confidence >= float(_value(settings, "auto_threshold", AUTO_THRESHOLD))
        ):
            return confidence, 0
        return confidence, 1
    if level_class == ONE_CLICK:
        limit = float(
            _value(settings, "one_click_amount_limit", ONE_CLICK_AMOUNT_LIMIT)
        )
        threshold = float(_value(settings, "one_click_threshold", ONE_CLICK_THRESHOLD))
        if confidence >= threshold and abs(float(amount or 0)) <= limit:
            return confidence, 1
        return confidence, 2
    raise ValueError(f"unknown level class {level_class!r}")


def unit_score(score):
    """A model score as a number from 0 to 1; anything that is not a number counts as no confidence."""
    try:
        return min(max(float(score), 0.0), 1.0)
    except (TypeError, ValueError):
        return 0.0


def _value(settings, key, default):
    """settings[key], the default when the setting is missing or empty (a doctype field not yet saved)."""
    value = settings.get(key)
    return default if value is None or value == "" else value
