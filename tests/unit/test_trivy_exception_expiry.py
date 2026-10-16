from datetime import date

import pytest

from scripts.ci.validate_trivy_exceptions import validate_exceptions


def test_trivy_exception_expiry_is_per_entry_and_expired_entries_are_inactive():
    assert validate_exceptions("CVE-2026-12345 exp:2026-09-30\nCVE-2026-54321 exp:2026-11-01\n",
                               today=date(2026, 10, 13)) == {"active": 1, "expired": 1}


@pytest.mark.parametrize("text", ["CVE-2026-12345", "CVE-2026-12345 exp:never",
                                  "CVE-2026-12345 exp:2026-99-99",
                                  "CVE-2026-12345 exp:2026-11-01\nCVE-2026-12345 exp:2026-12-01"])
def test_unbounded_invalid_and_duplicate_suppressions_fail_validation(text):
    with pytest.raises(ValueError):
        validate_exceptions(text, today=date(2026, 10, 13))
