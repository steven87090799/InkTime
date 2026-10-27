from functools import lru_cache
from itertools import combinations
from random import Random

from inktime.app.domain.rendering.adaptive_layout import _maximum_pairing, select_auto_photo_frames, select_pair_candidate


def test_seasonal_pair_priority_survives_expansion_for_multiple_frames():
    candidates = [
        {"id": name, "captured_at": captured, "seasonal_priority": priority, "width": 600, "height": 900}
        for name, captured, priority in (
            ("same-2012", "2012-10-22", 0), ("same-2019", "2019-10-22", 0),
            ("near-2012", "2012-10-21", 1), ("other-2012", "2012-10-20", 3),
        )
    ]
    frames = select_auto_photo_frames(candidates, frame_orientation="landscape", count=2)
    assert [[photo["id"] for photo in frame] for frame in frames] == [
        ["same-2012", "same-2019"], ["near-2012", "other-2012"],
    ]
    # A low-scoring same-day peer loses date priority without blocking an
    # acceptable nearby peer; content freshness still comes first.
    candidates[1]["seasonal_priority"] = 1000
    assert select_pair_candidate(candidates[0], candidates[1:], frame_orientation="landscape")["id"] == "near-2012"
    candidates[2]["display_count"] = 1
    candidates[3]["display_count"] = 1
    assert select_pair_candidate(candidates[0], candidates[1:], frame_orientation="landscape")["id"] == "same-2019"


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
