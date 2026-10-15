"""
service/actions/domain.py

Values that more than one layer needs to agree on.

Frequencies appear in three places: the CHECK constraint on the schedules
table, the tool schema shown to the model, and the validation that runs
before anything is created. When they were written out separately they
could drift — a value accepted by the model that the database then
rejected, or a new option added to the table that the model never learned
about.

They are declared once here and imported everywhere else.
"""

# Must match the CHECK constraint on schedules.frequency.
FREQUENCIES = ("once", "hourly", "daily", "weekdays", "weekly")

FREQUENCY_WORDS = {
    "once": "once",
    "hourly": "every hour",
    "daily": "every day",
    "weekdays": "every weekday",
    "weekly": "every week",
}

# Must match the CHECK constraint on test_suites.kind.
SUITE_KINDS = ("smoke", "regression", "custom")

# Recognised in an instruction; used only to help the model, never to decide.
RELATIVE_DAYS = ("today", "tomorrow")


def frequency_words(freq: str) -> str:
    """Render a frequency the way a person would say it."""
    return FREQUENCY_WORDS.get(freq, freq)
