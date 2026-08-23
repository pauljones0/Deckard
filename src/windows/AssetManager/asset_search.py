"""
Year: 2026

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.

Positional relevance scoring for the asset searches.

Every asset search in the window scores a candidate name against the typed
query with the ladder below, and orders the survivors by that score. The module
holds strings only: no GTK, no widget and no asset object, so a headless test
pins the whole ladder, and a caller that searches across packs reuses it.

The ladder scores one query token against one candidate name:

    100  the name is the token
     90  the name starts with the token
     80  a word of the name starts with the token
     65  the name holds the token anywhere
      0  the name does not hold the token

A query of several tokens scores as its weakest token, and a name that misses
one token scores 0 whatever the others give. Tokens are therefore an AND, and
their order does not matter: "up volume" and "volume up" both find volume_up.

Separators do not: the name and the query both collapse `_`, `-`, `.`, `/`,
`\\` and `+` to a single space before anything compares, so "media next" finds
media-next, media_next and media.next alike. A word that a pack writes with a
separator inside it is found as one word as well, because a token that misses
is tried again against the name with its separators taken out: "wifi" finds
wi-fi that way. The retry is the last rung tried, and it scores the joined
form, so a match that crosses a word boundary stands on whatever rung that
form earns: "home" reads ho-me as the whole name, rung 100, and go-home as a
word of it, rung 80.

What the ladder does not do is spelling. It matches the words that are typed,
so "batery" finds nothing where an edit ratio still offered battery.

Why positions and not an edit ratio: an edit ratio scores the whole name
against the whole query, so a long name loses for its length alone. Against
the four icon packs on a real installation, 14853 names, `fuzz.ratio` at its
threshold of 50 answered "battery" with 186 names, of which 140 hold no
"battery" at all (acer, afterpay, alacritty), while it dropped 24 of the 70
that do, every long one among them: battery-charging-outline scored 38.9 and
battery_charging_20_symbolic 40.0. The ladder answers the same query with all
70 and nothing else.

The threshold is the lowest rung a candidate may stand on, which is why the
scores above end at 65 rather than 1. Only two settings mean anything: 65
keeps a name that holds the token anywhere, and 80 demands a word boundary.
80 loses real targets, airplay and display for "play", esphome and googlehome
for "home", 25 podcast and broadcast names for "cast", and buys little,
because the ranking already floats the word-boundary hits above them. So the
threshold is 65 and a caller that wants the strict cut passes 80 itself.
"""
import re
import threading

# The rungs. A caller compares against these rather than against bare numbers,
# and a threshold names the rung it accepts.
SCORE_EXACT = 100
SCORE_PREFIX = 90
SCORE_WORD_PREFIX = 80
SCORE_CONTAINS = 65
SCORE_NO_MATCH = 0

# A candidate must reach this to stay in a grid. See the module docstring for
# the measurement behind it.
SEARCH_SCORE_THRESHOLD = SCORE_CONTAINS

# What separates the words of a file name. Icon packs write the same name as
# battery_charging_full, battery-charging-full and battery.charging.full.
_SEPARATORS = re.compile(r"[\s_\-./\\+]+")

# A ranker drops its memo when it grows past this. One pack of icons is about
# 12000 names, so a search of a whole installation still memoizes every name,
# and no ranker holds a memo without an end.
_MEMO_LIMIT = 50000

# The ranking key of a name: the negated score first, so a plain ascending sort
# puts the best match first; then where the query tokens sit in the name, so an
# earlier hit wins a tie; then the length of the name, so the shorter of two
# equal hits wins; then the normalized name, so the order never depends on the
# order the caller passed the names in. An empty query scores every name the
# same and leaves the length out, so only the name orders it.
RankKey = tuple[int, int, int, str]


def normalize(text: str) -> str:
    """The form the ladder compares: lower case, one space per separator run."""
    return _SEPARATORS.sub(" ", text.lower()).strip()


def _token_score(name: str, token: str) -> tuple[int, int]:
    """The rung one token stands on in a normalized name, and where it hits.

    The position is the index of the match the rung comes from, and -1 when
    the name does not hold the token at all.
    """
    if name == token:
        return SCORE_EXACT, 0
    if name.startswith(token):
        return SCORE_PREFIX, 0
    first = name.find(token)
    if first == -1:
        return SCORE_NO_MATCH, -1
    # A hit at index 0 is the prefix case above, so every index here is at
    # least 1 and the character before it exists.
    index = first
    while index != -1:
        if name[index - 1] == " ":
            return SCORE_WORD_PREFIX, index
        index = name.find(token, index + 1)
    return SCORE_CONTAINS, first


class QueryRanker:
    """One query, scored against as many names as the caller has.

    It memoizes the key of each name, because a comparator asks for the same
    name once per comparison, which is O(n log n) times over a grid of
    thousands. Build one per query and throw it away when the query changes.

    Two threads may score against one ranker. They can duplicate the work of a
    name that neither has memoized yet, and that is the whole cost: each call
    computes its own key, and a dict write is atomic.
    """

    def __init__(self, query: str) -> None:
        self.query = query
        self._query_normalized = normalize(query)
        self._tokens = self._query_normalized.split(" ") if self._query_normalized else []
        self._keys: dict[str, RankKey] = {}

    @property
    def is_empty(self) -> bool:
        """An empty query, which every name matches. A query of separators
        alone normalizes to nothing and counts as empty too."""
        return not self._tokens

    def rank_key(self, name: str) -> RankKey:
        """The sort key of name under this query. Best match sorts first."""
        key = self._keys.get(name)
        if key is None:
            key = self._compute(name)
            if len(self._keys) >= _MEMO_LIMIT:
                self._keys.clear()
            self._keys[name] = key
        return key

    def score(self, name: str) -> int:
        """0 for no match, else the rung the whole query stands on."""
        return -self.rank_key(name)[0]

    def matches(self, name: str, threshold: int = SEARCH_SCORE_THRESHOLD) -> bool:
        """Whether name earns a place in the grid.

        A caller that wants only the names holding a whole word of the query
        passes SCORE_WORD_PREFIX as the threshold.
        """
        if self.is_empty:
            # The resting state of every grid. Answering it here costs no
            # normalize and leaves no memo behind, which matters because this
            # runs once per name in the pack on every pass.
            return True
        return self.score(name) >= threshold

    def compare(self, name1: str, name2: str) -> int:
        """A GTK sort comparator: -1 when name1 comes first, 1 when it comes
        last, 0 when the two rank alike."""
        key1 = self.rank_key(name1)
        key2 = self.rank_key(name2)
        if key1 < key2:
            return -1
        if key1 > key2:
            return 1
        return 0

    def _compute(self, name: str) -> RankKey:
        normalized = normalize(name)
        if self.is_empty:
            # Every name answers an empty query alike, so the score and the
            # position are the same for all of them and the length is left
            # out. Only the name then orders the grid. Carrying the length
            # here would order it by how long its names are, which reads as no
            # order at all.
            return (-SCORE_EXACT, 0, 0, normalized)
        if normalized == self._query_normalized:
            return (-SCORE_EXACT, 0, len(normalized), normalized)

        # Built on the first token that misses, and only for a name that
        # holds a separator, because it is the same string otherwise.
        stripped: str | None = None

        worst = SCORE_EXACT
        positions = 0
        for token in self._tokens:
            rung, position = _token_score(normalized, token)
            if rung == SCORE_NO_MATCH and " " in normalized:
                # A pack can write one word with a separator inside it, such
                # as wi-fi for wifi. Try the name without its separators
                # before giving the token up.
                if stripped is None:
                    stripped = normalized.replace(" ", "")
                rung, position = _token_score(stripped, token)
            if rung == SCORE_NO_MATCH:
                # One missed token drops the candidate, whatever the rest of
                # the query scores.
                return (-SCORE_NO_MATCH, 0, len(normalized), normalized)
            worst = min(worst, rung)
            positions += position
        return (-worst, positions, len(normalized), normalized)


# The ranker of the query that was scored last. Every call below goes through
# it, so a filter pass and the sort pass that follows share one memo, and a
# keystroke that changes the query drops it.
_cache_lock = threading.Lock()
_cached_ranker: QueryRanker | None = None


def ranker(query: str) -> QueryRanker:
    """The ranker for query, reusing the last one while the query holds.

    A caller that scores a batch of names should keep the returned object for
    the batch. The lock guards the one-entry cache only; the ranker itself
    needs none.
    """
    global _cached_ranker
    with _cache_lock:
        cached = _cached_ranker
        if cached is not None and cached.query == query:
            return cached
        fresh = QueryRanker(query)
        _cached_ranker = fresh
        return fresh


def release_cache() -> None:
    """Drop the cached ranker and the memo it holds.

    A ranker that scored a whole pack holds a key per name of it. The window
    that searched is the one to say when that is spent, because nothing in
    here knows that the grid has gone.
    """
    global _cached_ranker
    with _cache_lock:
        _cached_ranker = None


def score(name: str, query: str) -> int:
    """The rung name stands on for query. 0 means no match."""
    return ranker(query).score(name)


def rank_key(name: str, query: str) -> RankKey:
    """The sort key of name under query. Best match sorts first."""
    return ranker(query).rank_key(name)


def matches(name: str, query: str, threshold: int = SEARCH_SCORE_THRESHOLD) -> bool:
    """Whether name earns a place in a grid searched for query."""
    return ranker(query).matches(name, threshold)


def compare(name1: str, name2: str, query: str) -> int:
    """A GTK sort comparator over two names under one query."""
    return ranker(query).compare(name1, name2)


def is_empty_query(query: str) -> bool:
    """Whether query asks for nothing, which every name matches."""
    return normalize(query) == ""
