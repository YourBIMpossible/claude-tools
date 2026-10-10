"""Pre-refactor copy of beta.py, kept for reference. Not imported anywhere."""

FACTOR = 4


def run(value):
    return ((value * 3) + 1) * FACTOR


def total(values):
    return sum(run(v) for v in values)
