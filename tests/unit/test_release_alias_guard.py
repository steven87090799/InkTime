import pytest

from scripts.ci.release_alias_guard import can_promote_latest


def _image(version):
    return {"config": {"Labels": {"org.opencontainers.image.version": version}}}


@pytest.mark.parametrize("candidate,current,allowed", [
    ("v1.2.0", "v1.1.9", True), ("v1.9.0", "v1.10.0", False),
    ("v1.1.0", "v2.0.0", False), ("v2.0.0", "v2.0.0", False),
])
def test_latest_alias_only_moves_to_a_newer_stable_version(candidate, current, allowed):
    assert can_promote_latest(candidate, {"linux/amd64": _image(current), "linux/arm64": _image(current)}) is allowed


def test_latest_bootstrap_is_explicit_and_unknown_versions_are_not_overwritten():
    assert can_promote_latest("v1.0.0") is True
    with pytest.raises(ValueError):
        can_promote_latest("v1.0.0", {})
    with pytest.raises(ValueError):
        can_promote_latest("v1.0.0", _image("unverified"))
    with pytest.raises(ValueError):
        can_promote_latest("v1.0.0", {"linux/amd64": _image("v1.0.0"), "linux/arm64": _image("v2.0.0")})
