from functools import lru_cache
from itertools import combinations
from random import Random

from inktime.app.domain.rendering.adaptive_layout import _maximum_pairing


def test_pair_matching_matches_exhaustive_oracle_including_odd_cycles():
    rng = Random(161)  # noqa: S311 -- reproducible graph fixtures, not security randomness.
    graphs = []
    edges = list(combinations(range(5), 2))
    for mask in range(1 << len(edges)):
        graphs.append((5, [edge for bit, edge in enumerate(edges) if mask & (1 << bit)]))
    for size in range(6, 11):
        for _ in range(100):
            graphs.append((size, [edge for edge in combinations(range(size), 2) if rng.random() < 0.4]))
    for size, pairs in graphs:
        adjacent = [set() for _ in range(size)]
        for first, second in pairs:
            adjacent[first].add(second)
            adjacent[second].add(first)

        @lru_cache(None)
        def optimal(remaining, adjacent=adjacent):
            if not remaining:
                return 0
            first, *tail = remaining
            return max([optimal(tuple(tail))] + [
                1 + optimal(tuple(peer for peer in tail if peer != second))
                for second in tail if second in adjacent[first]
            ])

        expected = optimal(tuple(range(size)))
        for limit in (1, size // 2):
            matching = _maximum_pairing([sorted(peers) for peers in adjacent], limit)
            assert sum(peer != -1 for peer in matching) == 2 * min(expected, limit), pairs
            for first, second in enumerate(matching):
                if second != -1:
                    assert second in adjacent[first] and matching[second] == first
