"""
service/actions/matching.py

Matching the names a model returns against records that actually exist.

A model does not return clean strings. Shown a list formatted as

    - name: "Smoke Suite 1 run"   (application: Nethra Local, paused)

it will happily return the whole line as the name. Rather than hoping the
prompt is perfect, the matcher is tolerant: strip what we know we added,
then try progressively looser comparisons.

Returning a LIST matters. Two matches is a question for the user, not a
coin toss — you can have "Netra" and "Nethra Local", and picking one
silently is exactly the failure this is meant to prevent.
"""

import re


def clean_name(value: str) -> str:
    """
    Reduce a returned name to something comparable.

    Removes the decoration the catalogue adds: surrounding quotes, a
    trailing parenthetical, a trailing "on <application>", and a leading
    "name:" label.
    """
    v = (value or "").strip().strip('"').strip("'")
    v = re.sub(r"^name:\s*", "", v, flags=re.I)
    v = re.sub(r"\s*\([^)]*\)\s*$", "", v)
    v = re.sub(r"\s+on\s+.+$", "", v, flags=re.I)
    return v.strip()


def best_match(value: str, items: list, key: str = "name") -> list:
    """
    Find every item a name could plausibly refer to.

    Tries exact, then containment in either direction, then significant word
    overlap. Stops at the first rule that produces anything, so a precise
    match is never diluted by looser ones.

    Args:
        value: The name as returned by the model.
        items: Records to match against.
        key: Which field holds the name.

    Returns:
        Matching items. Empty means not found; more than one means ask.
    """
    v = clean_name(value).lower()
    if not v or not items:
        return []

    exact = [i for i in items if i.get(key, "").lower() == v]
    if exact:
        return exact

    contains = [i for i in items
                if v in i.get(key, "").lower() or i.get(key, "").lower() in v]
    if contains:
        return contains

    words = {w for w in re.split(r"\W+", v) if len(w) > 2}
    if not words:
        return []
    scored = []
    for i in items:
        other = {w for w in re.split(r"\W+", i.get(key, "").lower()) if len(w) > 2}
        overlap = len(words & other)
        if overlap:
            scored.append((overlap, i))
    scored.sort(key=lambda x: -x[0])
    return [i for _, i in scored[:3]]
