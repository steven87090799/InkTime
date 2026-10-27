"""Reproducible daily quality lottery using confirmed displays only."""
import hashlib
import math
from datetime import date, datetime

from .rotation import exposure_count, last_exposure_at


def seasonal_order(rows, target: date, minimum_score: float = 60, repeat_high_score: float = 75):
    def key(row):
        score = float(row.get("selection_score") or row.get("combined_score") or 0)
        weight = max(1.0, score - minimum_score + 1) ** 2
        if exposure_count(row):
            weight *= 0.6 if score >= repeat_high_score else 0.2
        try:
            seen = datetime.fromisoformat(last_exposure_at(row).replace("Z", "+00:00")).date()
            if 0 <= (target - seen).days < 30:
                weight *= 0.01
        except ValueError:
            pass
        weight /= 1 + max(0, int(row.get("day_distance") or 0)) * 0.2
        digest = hashlib.sha256(f"{target.isoformat()}:{row['id']}".encode()).digest()
        uniform = (int.from_bytes(digest[:8], "big") + 1) / (2**64 + 1)
        return (score < minimum_score, -math.log(uniform) / weight, str(row["id"]))
    return sorted(rows, key=key)
