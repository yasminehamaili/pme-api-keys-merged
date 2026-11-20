"""Diagnostic engine: pure tests (`pytest --noconftest tests/test_diagnostics.py`)."""
import numpy as np
import pytest

import diagnostics as dx


def test_anomaly_vs_target_and_history():
    r = dx.assess_kpi("sales", 80, target=100, history=[100, 102, 98, 101, 99, 100, 103])
    assert r["status"] == "critical" and r["vs_target"]["deviation_pct"] == -20.0
    assert r["vs_history"]["z_score"] < -3
    assert dx.assess_kpi("sales", 101, target=100)["status"] == "normal"
    # lower-is-better KPI (defect rate): going UP is adverse
    assert dx.assess_kpi("defects", 8, target=5, higher_is_better=False)["status"] == "critical"
    assert dx.assess_kpi("defects", 3, target=5, higher_is_better=False)["status"] == "normal"


def test_anomaly_short_history_is_flagged_not_trusted():
    r = dx.assess_kpi("x", 1, history=[1, 2])
    assert r["vs_history"] is None and r["status"] == "normal"


def test_association_never_claims_causation():
    rng = np.random.default_rng(0)
    x = rng.normal(size=60)
    f = dx.association(x, 2 * x + rng.normal(scale=0.3, size=60))
    assert f.classification == "statistical_association"
    assert "causation" in f.limits[0]
    assert dx.association(x, rng.normal(size=60)).classification == "insufficient_evidence"
    assert dx.association([1, 2, 3], [1, 2, 3]).classification == "insufficient_evidence"   # n too small
    assert dx.association([1] * 10, list(range(10))).classification == "insufficient_evidence"  # constant


def test_lagged_requires_time_ordering():
    rng = np.random.default_rng(1)
    cause = rng.normal(size=80)
    effect = np.roll(cause, 3) + rng.normal(scale=0.2, size=80)       # effect follows cause by 3
    f = dx.lagged_association(cause, effect, max_lag=6)
    assert f.classification == "probable_factor" and f.stats["best_lag"] == 3
    # reversed roles: the "cause" actually follows -> must NOT be called a probable factor
    g = dx.lagged_association(effect, cause, max_lag=6)
    assert g.classification != "probable_factor"


def test_group_tests():
    rng = np.random.default_rng(2)
    a, b = rng.normal(10, 1, 30), rng.normal(13, 1, 30)
    assert dx.compare_groups(a, b).classification == "statistical_association"
    assert dx.compare_groups(a, rng.normal(10, 1, 30)).classification == "insufficient_evidence"
    assert dx.compare_groups([1, 2], [3, 4]).classification == "insufficient_evidence"
    groups = {"A": rng.normal(5, 1, 20), "B": rng.normal(5, 1, 20), "C": rng.normal(9, 1, 20)}
    assert dx.compare_many(groups).classification == "statistical_association"


def test_chi_square():
    assert dx.chi_square([[40, 10], [10, 40]]).classification == "statistical_association"
    assert dx.chi_square([[25, 25], [24, 26]]).classification == "insufficient_evidence"
    assert dx.chi_square([[1, 0], [0, 2]]).classification == "insufficient_evidence"   # tiny counts
    with pytest.raises(dx.DiagnosticsError):
        dx.chi_square([[1, 2]])


def test_bad_input():
    with pytest.raises(dx.DiagnosticsError):
        dx.association([1, 2, float("nan")] * 4, [1, 2, 3] * 4)
    with pytest.raises(dx.DiagnosticsError):
        dx.association([1] * 10, [1] * 9)


def test_only_allowed_classifications():
    for f in (dx.fact("x"), dx.hypothesis("y", "z")):
        assert f.to_dict()["classification"] in dx.CLASSIFICATIONS
