"""Turning a natural-language question into a full-text query.

PostgreSQL's ``plainto_tsquery`` / ``websearch_to_tsquery`` AND every word together. For
"What does error E-317 mean?" that requires a passage to contain *what*, *does* and *mean* as
well - and the passage that answers it does not. The evaluation measured it: full-text recall on
error-code questions was 0.

Instead, the question is reduced to its content words (function words in English, French and
German are dropped) and the terms are OR-ed; ``ts_rank_cd`` then ranks passages that match more
terms, closer together, first.
"""

from __future__ import annotations

import re

MAX_TERMS = 32

# Tokens keep inner hyphens, dots and slashes so that identifiers survive: E-317, FLT-4012,
# v2.4, /v2/robots/{robot_id}/missions.
_TOKEN = re.compile(r"\w+(?:[-./]\w+)*", re.UNICODE)

_STOPWORD_TEXT = """
    a an and are as at be been but by can could did do does doing for from had has have how i if
    in into is it its me my no not of on or our should so than that the their them then there
    these they this those to too up us was we were what when where which while who whom why will
    with would you your yours about after again all also any because before being between both
    each few further here just more most much must other over own same some such through under
    until very get got may might shall please tell know need want
    le la les un une des du de d l et ou mais donc or ni car que qui quoi quel quelle quels
    quelles est sont être avoir a ont ai as en dans sur pour par avec sans ce cet cette ces mon
    ma mes ton ta tes son sa ses notre nos votre vos leur leurs je tu il elle on nous vous ils
    elles me te se y ne pas plus comment combien pourquoi quand où
    der die das ein eine einer eines und oder aber ist sind war waren sein haben hat wie was wer
    wo wann warum welche welcher welches mit von zu im in am an auf für bei aus nach den dem des
    nicht kein keine ich du er sie es wir ihr mein dein sich
"""
STOPWORDS = frozenset(_STOPWORD_TEXT.split())


def query_terms(query: str) -> list[str]:
    """Content terms of ``query``, lower-cased, deduplicated, in order of appearance."""
    tokens = [t.lower() for t in _TOKEN.findall(query)]
    content = [t for t in tokens if t not in STOPWORDS]
    terms = content or tokens  # a question made only of stopwords still searches something
    return list(dict.fromkeys(terms))[:MAX_TERMS]
