"""Language support for reading titles: month names and stop words.

These are facts about English and Spanish, not any group's wording, so they
live in code. One table serves the duplicate check (pipeline) and the
newer-issue signal (checker.gather). Every word is folded: lower case, no
accents, the way both read a title.
"""

from __future__ import annotations

# Month name -> month number.
MONTHS: dict[str, int] = {
    **{name: number for number, name in enumerate(
        "january february march april may june july august september october november december".split(), 1)},
    **{name: number for number, name in enumerate(
        "enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre".split(), 1)},
    "setiembre": 9,
}

# Words too common to say two titles are the same thing.
STOPWORDS: frozenset[str] = frozenset(
    # English
    "the a an of and or in on at to for with by from is as".split()
    # Spanish
    + "el la los las un una unos unas de del en y e o por que a al con "
    "se es su sus no ni para sin sobre entre mas".split()
)
