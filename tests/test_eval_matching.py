"""Tier 3: ranking-quality floors for the heuristic matcher.

The rest of the suite proves the scorer runs; this proves it still ranks well. Floors
are set at measured reality (with a little headroom), not aspiration — a change that
degrades ranking fails here even if every unit test still passes.

Known miss, deliberately documented rather than hidden: the tag-flooding trap
("Senior Graphic Designer" with a full-stack tag set) still cracks the top 10, which
is why the P@10 floor is 0.9 and not 1.0. Raising it is the tuning goal.
"""

from jobagent.matching.evalset import EXAMPLES, evaluate


def _metrics():
    return evaluate()


def test_dataset_is_balanced_enough_to_mean_something():
    n_pos = sum(1 for e in EXAMPLES if e.relevant)
    n_neg = len(EXAMPLES) - n_pos
    assert n_pos >= 10 and n_neg >= 12


def test_precision_at_5_is_perfect():
    """The top of the list is what the digest shows — it must be clean."""
    assert _metrics()["precision_at_5"] == 1.0


def test_precision_and_recall_at_10_floors():
    m = _metrics()
    assert m["precision_at_10"] >= 0.9
    assert m["recall_at_10"] >= 0.9


def test_separation_floor():
    """Mean positive score must sit clearly above mean negative — measured 0.48."""
    assert _metrics()["separation"] >= 0.42


def _score_of(note_fragment: str) -> float:
    """Look examples up by their note, which is unique — titles are not (the on-site
    exclusion trap is deliberately titled 'AI Engineer', same as a genuine positive)."""
    rows = [r for r in _metrics()["rows"] if note_fragment in r["note"]]
    assert len(rows) == 1, f"note fragment {note_fragment!r} matched {len(rows)} examples"
    return rows[0]["score"]


def test_every_exclusion_trap_is_capped():
    """The excluded ceiling is 0.15 — all three exclusion classes must sit at/under it."""
    assert _score_of("on-site only") <= 0.16
    assert _score_of("citizenship/clearance") <= 0.16
    assert _score_of("unpaid") <= 0.16


def test_substring_traps_stay_buried():
    """'RAG' in fragment, 'Go' in ongoing, 'cto' in Semiconductors."""
    assert _score_of("'Go' in ongoing") < 0.25
    assert _score_of("'cto' inside Semiconductors") < 0.25


def test_management_trap_scores_near_zero():
    assert _score_of("management trap") < 0.10


def test_level_mismatch_ranks_below_every_genuine_positive():
    """The trap that motivated role-signal dampening: a junior/intern posting with
    perfect skill hits used to outrank a genuine positive (0.58 vs 0.41)."""
    m = _metrics()
    weakest_positive = min(r["score"] for r in m["rows"] if r["relevant"])
    assert _score_of("seniority trap") < weakest_positive


def test_boilerplate_body_does_not_make_a_role():
    """Buzzwords in the body of a non-engineering posting must stay far down."""
    assert _score_of("buzzword body") < 0.20
    assert _score_of("requirement-mirroring boilerplate") < 0.20
