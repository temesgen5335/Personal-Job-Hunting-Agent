"""The agent may tune the search; it may not touch identity or the CV.

Identity is what gets typed into employer forms; the CV is R1's ground truth and an
agent that can "improve" it has a fabrication path. Both stay human-edited in Settings.
"""

import json

import pytest

from jobagent.assistant.profile_policy import (
    PROFILE_FROZEN, PROFILE_WRITABLE, ProfileRefused, apply_profile, preview_profile,
)


def _paths(tmp_path):
    return {"local_path": str(tmp_path / "none.toml"), "overlay_path": str(tmp_path / "profile.json")}


def test_search_fields_are_writable_and_identity_and_cv_are_frozen():
    assert {"target_roles", "core_skills", "remote_scope", "seniority"} <= PROFILE_WRITABLE
    assert {"name", "email", "phone", "cv_path", "links"} <= PROFILE_FROZEN
    assert PROFILE_WRITABLE & PROFILE_FROZEN == frozenset()


def test_a_frozen_field_is_refused_with_a_reason(tmp_path):
    with pytest.raises(ProfileRefused) as exc:
        preview_profile("name", "Someone Else", **_paths(tmp_path))
    assert "identity" in str(exc.value).lower()
    with pytest.raises(ProfileRefused):
        preview_profile("cv_path", "/tmp/x.pdf", **_paths(tmp_path))


def test_an_unknown_field_is_refused(tmp_path):
    with pytest.raises(ProfileRefused):
        preview_profile("not_a_field", "x", **_paths(tmp_path))


def test_a_list_field_is_previewed_as_the_parsed_list(tmp_path):
    impact = preview_profile("target_roles", "AI Engineer, ML Engineer", **_paths(tmp_path))
    text = impact.render()
    assert "target_roles" in text and "AI Engineer" in text and "ML Engineer" in text
    assert impact.parsed == ["AI Engineer", "ML Engineer"]


def test_a_scalar_field_keeps_its_type(tmp_path):
    assert preview_profile("remote_scope", "global", **_paths(tmp_path)).parsed == "global"


def test_apply_writes_only_the_named_field_into_the_overlay(tmp_path):
    paths = _paths(tmp_path)
    apply_profile("target_roles", "AI Engineer, ML Engineer", **paths)
    apply_profile("seniority", "senior", **paths)
    overlay = json.loads((tmp_path / "profile.json").read_text())
    assert overlay["profile"]["target_roles"] == ["AI Engineer", "ML Engineer"]
    assert overlay["profile"]["seniority"] == "senior"
    assert set(overlay["profile"]) == {"target_roles", "seniority"}   # nothing else touched


def test_apply_refuses_a_frozen_field_and_writes_nothing(tmp_path):
    paths = _paths(tmp_path)
    with pytest.raises(ProfileRefused):
        apply_profile("email", "x@y.com", **paths)
    assert not (tmp_path / "profile.json").exists()
