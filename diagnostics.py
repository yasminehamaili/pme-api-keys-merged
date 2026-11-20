"""
Diagnostic engine (descriptive -> diagnostic): anomaly detection + statistical tests.

Pure functions, no I/O. All maths is numpy/scipy, never an LLM.

STRICT CAUSAL RULE: every conclusion is a `Finding` with exactly one classification:
  confirmed_fact           arithmetic / recorded data (not a statistical claim)
  statistical_association  significant relationship or difference. NOT causation.
  probable_factor          association AND the supposed cause leads the effect in time
                           (lagged test, stronger than the reverse direction). Still not proof.
  unverified_hypothesis    plausible idea with no test run yet
  insufficient_evidence    too little data, constant series, or no significant result
This module can never output "causes": the strongest label is `probable_factor`.
"""
import math
from dataclasses import dataclass, field
from typing import Sequence

import numpy as np
from scipy import stats

CLASSIFICATIONS = ("confirmed_fact", "statistical_association", "probable_factor",
                   "unverified_hypothesis", "insufficient_evidence")
NO_CAUSATION = "Association does not prove causation; confounding factors are not excluded."


class DiagnosticsError(ValueError):
    """Bad input (length mismatch, NaN...). Message is safe to show to API clients."""


@dataclass
class Finding:
    statement: str
    classification: str
    method: str
    stats: dict = field(default_factory=dict)
    n: int = 0
    evidence_ids: list[str] = field(default_factory=list)
    limits: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        assert self.classification in CLASSIFICATIONS
        return self.__dict__.copy()


def _arr(values: Sequence[float], name: str) -> np.ndarray:
    try:
        a = np.asarray(values, dtype=float)
    except (TypeError, ValueError):
        raise DiagnosticsError(f"{name} must be numbers")
    if a.ndim != 1:
        raise DiagnosticsError(f"{name} must be a flat list")
    if not np.all(np.isfinite(a)):
        raise DiagnosticsError(f"{name} contains NaN or infinite values")
    return a


def _f(x: float, nd: int = 4) -> float:
    return round(float(x), nd)


# ------------------------------------------------------------- facts / hypotheses
def fact(statement: str, evidence_ids: list[str] | None = None) -> Finding:
    return Finding(statement, "confirmed_fact", "recorded data", evidence_ids=evidence_ids or [])


def hypothesis(statement: str, data_needed: str) -> Finding:
    return Finding(statement, "unverified_hypothesis", "none", limits=[f"Not tested. Needed: {data_needed}"])


# ----------------------------------------------------------------- anomaly detection
def assess_kpi(name: str, value: float, *, target: float | None = None,
               history: Sequence[float] | None = None, higher_is_better: bool = True,
               alert_pct: float = 5, critical_pct: float = 15,
               z_alert: float = 2, z_critical: float = 3, min_history: int = 6) -> dict:
    """Compare a KPI with its target and its own history. status = worst signal."""
    if not (0 <= alert_pct <= critical_pct) or not (0 <= z_alert <= z_critical):
        raise DiagnosticsError("thresholds must satisfy 0 <= alert <= critical")
    value = float(value)
    if not math.isfinite(value):
        raise DiagnosticsError("value must be finite")
    sign = 1 if higher_is_better else -1                  # adverse movement is positive after * -sign
    rank = {"normal": 0, "alert": 1, "critical": 2}
    status, signals, out = "normal", [], {"name": name, "value": value}

    if target is not None:
        if target == 0:
            out["vs_target"] = None
            signals.append("target is 0: percentage deviation undefined")
        else:
            dev = (value - target) / abs(target) * 100
            adverse = -dev * sign
            out["vs_target"] = {"target": target, "deviation_pct": _f(dev, 2)}
            level = "critical" if adverse >= critical_pct else "alert" if adverse >= alert_pct else "normal"
            if level != "normal":
                signals.append(f"{abs(dev):.1f}% {'below' if dev < 0 else 'above'} target ({level})")
            status = max(status, level, key=rank.get)

    if history is not None:
        h = _arr(history, "history")
        if len(h) >= min_history and h.std(ddof=1) > 0:
            mean, sd = float(h.mean()), float(h.std(ddof=1))
            z = (value - mean) / sd
            adverse_z = -z * sign
            out["vs_history"] = {"mean": _f(mean), "std": _f(sd), "z_score": _f(z, 2), "n": len(h)}
            level = "critical" if adverse_z >= z_critical else "alert" if adverse_z >= z_alert else "normal"
            if level != "normal":
                signals.append(f"z-score {z:.2f} vs {len(h)} past periods ({level})")
            status = max(status, level, key=rank.get)
        else:
            out["vs_history"] = None
            signals.append(f"history unusable (need >= {min_history} varying points)")

    out.update(status=status, signals=signals)
    return out


# ------------------------------------------------------------------ association tests
def _pair(x, y):
    a, b = _arr(x, "x"), _arr(y, "y")
    if len(a) != len(b):
        raise DiagnosticsError("x and y must have the same length")
    return a, b


def _corr(a: np.ndarray, b: np.ndarray, method: str) -> tuple[float, float]:
    r, p = (stats.pearsonr(a, b) if method == "pearson" else stats.spearmanr(a, b))
    return float(r), float(p)


def association(x: Sequence[float], y: Sequence[float], method: str = "pearson", *,
                alpha: float = 0.05, min_n: int = 8, min_abs_r: float = 0.5) -> Finding:
    if method not in ("pearson", "spearman"):
        raise DiagnosticsError("method must be pearson or spearman")
    a, b = _pair(x, y)
    n = len(a)
    if n < min_n:
        return Finding(f"Only {n} paired observations (need >= {min_n}).", "insufficient_evidence",
                       method, n=n, limits=[NO_CAUSATION])
    if a.std() == 0 or b.std() == 0:
        return Finding("One series is constant: correlation undefined.", "insufficient_evidence",
                       method, n=n, limits=[NO_CAUSATION])
    r, p = _corr(a, b, method)
    st = {"r": _f(r), "p_value": _f(p, 6), "alpha": alpha}
    if p < alpha and abs(r) >= min_abs_r:
        direction = "positive" if r > 0 else "negative"
        return Finding(f"Significant {direction} {method} association (r={r:.2f}, p={p:.3g}, n={n}).",
                       "statistical_association", method, st, n, limits=[NO_CAUSATION])
    return Finding(f"No significant association at the required strength (r={r:.2f}, p={p:.3g}, n={n}).",
                   "insufficient_evidence", method, st, n, limits=[NO_CAUSATION])


def lagged_association(cause: Sequence[float], effect: Sequence[float], max_lag: int = 6,
                       method: str = "pearson", *, alpha: float = 0.05, min_n: int = 8,
                       min_abs_r: float = 0.5) -> Finding:
    """Does `cause` (earlier) line up with `effect` (later) better than the reverse?
    Upgrades to probable_factor only with temporal precedence. Bonferroni-corrected."""
    a, b = _pair(cause, effect)
    if not (1 <= max_lag <= 24):
        raise DiagnosticsError("max_lag must be between 1 and 24")
    if len(a) - max_lag < min_n:
        return Finding(f"Series too short for lags up to {max_lag} (n={len(a)}).",
                       "insufficient_evidence", f"lagged {method}", n=len(a), limits=[NO_CAUSATION])
    if a.std() == 0 or b.std() == 0:
        return Finding("One series is constant: correlation undefined.", "insufficient_evidence",
                       f"lagged {method}", n=len(a), limits=[NO_CAUSATION])
    tests = 2 * max_lag + 1
    corrected = alpha / tests
    rows = {}                                              # lag -> (r, p); lag>0: cause leads
    for lag in range(0, max_lag + 1):
        rows[lag] = _corr(a[:len(a) - lag], b[lag:], method)
    for lag in range(1, max_lag + 1):                      # reverse: effect leads cause
        rows[-lag] = _corr(b[:len(b) - lag], a[lag:], method)
    sig = {k: v for k, v in rows.items() if v[1] < corrected and abs(v[0]) >= min_abs_r}
    forward = {k: v for k, v in sig.items() if k >= 1}
    reverse = {k: v for k, v in sig.items() if k < 0}
    table = {str(k): {"r": _f(v[0]), "p_value": _f(v[1], 6)} for k, v in sorted(rows.items())}
    base = {"alpha_corrected": _f(corrected, 6), "by_lag": table}
    n = len(a)
    if forward:
        lag, (r, p) = max(forward.items(), key=lambda kv: abs(kv[1][0]))
        rev_best = max((abs(v[0]) for v in reverse.values()), default=0.0)
        if abs(r) > rev_best:
            return Finding(
                f"Cause leads effect by {lag} period(s) (r={r:.2f}, p={p:.3g}); stronger than the "
                f"reverse direction.", "probable_factor", f"lagged {method}",
                {**base, "best_lag": lag, "r": _f(r), "p_value": _f(p, 6)}, n,
                limits=["Temporal precedence + association only; confounders and common causes "
                        "(e.g. seasonality) are not excluded."])
    if 0 in sig:
        r, p = sig[0]
        return Finding(f"Same-period association only (r={r:.2f}, p={p:.3g}): no time ordering shown.",
                       "statistical_association", f"lagged {method}", {**base, "r": _f(r)}, n,
                       limits=[NO_CAUSATION])
    return Finding("No significant lagged association after multiple-test correction.",
                   "insufficient_evidence", f"lagged {method}", base, n, limits=[NO_CAUSATION])


# ------------------------------------------------------------------ group comparisons
def _normal(a: np.ndarray, alpha: float) -> bool:
    return len(a) >= 3 and a.std() > 0 and stats.shapiro(a).pvalue > alpha


def compare_groups(a: Sequence[float], b: Sequence[float], *, alpha: float = 0.05,
                   min_n: int = 5) -> Finding:
    """Two groups (e.g. delivery delay with vs without a stockout): Welch t-test if both look
    normal, otherwise Mann-Whitney U. Reports Cohen's d."""
    x, y = _arr(a, "a"), _arr(b, "b")
    if len(x) < min_n or len(y) < min_n:
        return Finding(f"Groups too small (n={len(x)}, {len(y)}; need >= {min_n} each).",
                       "insufficient_evidence", "group comparison", n=len(x) + len(y))
    if x.std() == 0 and y.std() == 0 and x.mean() == y.mean():
        return Finding("Both groups are identical constants.", "insufficient_evidence",
                       "group comparison", n=len(x) + len(y))
    parametric = _normal(x, alpha) and _normal(y, alpha)
    if parametric:
        res = stats.ttest_ind(x, y, equal_var=False)
        method = "Welch t-test"
    else:
        res = stats.mannwhitneyu(x, y, alternative="two-sided")
        method = "Mann-Whitney U"
    p = float(res.pvalue)
    pooled = math.sqrt(((len(x) - 1) * x.var(ddof=1) + (len(y) - 1) * y.var(ddof=1)) / (len(x) + len(y) - 2))
    d = float((x.mean() - y.mean()) / pooled) if pooled > 0 else float("nan")
    st = {"mean_a": _f(x.mean()), "mean_b": _f(y.mean()), "p_value": _f(p, 6),
          "cohens_d": None if math.isnan(d) else _f(d, 3), "alpha": alpha}
    n = len(x) + len(y)
    if p < alpha:
        return Finding(f"Groups differ significantly ({method}, p={p:.3g}; means {x.mean():.2f} vs {y.mean():.2f}).",
                       "statistical_association", method, st, n, limits=[NO_CAUSATION])
    return Finding(f"No significant difference ({method}, p={p:.3g}).", "insufficient_evidence",
                   method, st, n, limits=[NO_CAUSATION])


def compare_many(groups: dict[str, Sequence[float]], *, alpha: float = 0.05, min_n: int = 3) -> Finding:
    """3+ groups (e.g. supplier delay by supplier): one-way ANOVA, or Kruskal-Wallis if any group
    is non-normal."""
    if len(groups) < 3:
        raise DiagnosticsError("need at least 3 groups (use compare_groups for 2)")
    arrs = {k: _arr(v, k) for k, v in groups.items()}
    small = [k for k, v in arrs.items() if len(v) < min_n]
    n = sum(len(v) for v in arrs.values())
    if small:
        return Finding(f"Groups with fewer than {min_n} observations: {', '.join(small)}.",
                       "insufficient_evidence", "ANOVA", n=n)
    vals = list(arrs.values())
    if all(_normal(v, alpha) for v in vals):
        res, method = stats.f_oneway(*vals), "one-way ANOVA"
    else:
        res, method = stats.kruskal(*vals), "Kruskal-Wallis"
    p = float(res.pvalue)
    st = {"p_value": _f(p, 6), "group_means": {k: _f(v.mean()) for k, v in arrs.items()}, "alpha": alpha}
    if math.isnan(p):
        return Finding("Test undefined (identical values).", "insufficient_evidence", method, st, n)
    if p < alpha:
        return Finding(f"At least one group differs ({method}, p={p:.3g}).", "statistical_association",
                       method, st, n, limits=[NO_CAUSATION, "Post-hoc tests needed to say which groups differ."])
    return Finding(f"No significant difference between groups ({method}, p={p:.3g}).",
                   "insufficient_evidence", method, st, n, limits=[NO_CAUSATION])


def chi_square(table: Sequence[Sequence[int]], *, alpha: float = 0.05) -> Finding:
    """Independence of two categorical variables (e.g. late delivery x supplier region)."""
    t = np.asarray(table, dtype=float)
    if t.ndim != 2 or min(t.shape) < 2 or (t < 0).any() or not np.all(np.isfinite(t)):
        raise DiagnosticsError("table must be a 2D grid (>= 2x2) of non-negative counts")
    n = int(t.sum())
    if (t.sum(axis=0) == 0).any() or (t.sum(axis=1) == 0).any():
        return Finding("A row or column has no observations.", "insufficient_evidence", "chi-square", n=n)
    chi2, p, dof, expected = stats.chi2_contingency(t)
    low = float((expected < 5).mean())
    st = {"chi2": _f(chi2), "dof": int(dof), "p_value": _f(p, 6), "alpha": alpha}
    if (expected < 1).any() or low > 0.2:
        return Finding("Expected counts too small for a reliable chi-square test.",
                       "insufficient_evidence", "chi-square", st, n, limits=["Merge categories or collect more data."])
    if p < alpha:
        return Finding(f"Variables are not independent (chi2={chi2:.2f}, p={p:.3g}).",
                       "statistical_association", "chi-square", st, n, limits=[NO_CAUSATION])
    return Finding(f"No significant dependence (chi2={chi2:.2f}, p={p:.3g}).", "insufficient_evidence",
                   "chi-square", st, n, limits=[NO_CAUSATION])
