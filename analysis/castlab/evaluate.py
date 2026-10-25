"""検知結果と CAST マーカーの 1 対 1 照合、およびラベル信頼度に応じた集計。

照合:
- 検出 d がマーカー m の窓 [m - window_before_s, m + window_after_s] に入るとき対応可能
- 1 つのマーカーには最大 1 つの検出、1 つの検出には最大 1 つのマーカーを割り当てる
- 割り当て数が最大になる組み合わせのうち、|d - m - expected_offset_s| の合計が最小のものを選ぶ
  （ハンガリアン法。対応不可の組は巨大コストにして除外）

集計はラベル信頼度ごとに分け、信頼度の低いラベルから率（精度・再現率）を算出しない。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.optimize import linear_sum_assignment

from .config import MatchingConfig, SessionConfig

_FORBIDDEN = 1e9


def match_one_to_one(
    markers: np.ndarray, detections: np.ndarray, cfg: MatchingConfig, window_after: np.ndarray | None = None
) -> list[tuple[int, int]]:
    """(marker_index, detection_index) の組の一覧を返す。

    window_after を渡すとマーカーごとに窓の終わり（マーカーからの秒数）を上書きする。
    """
    if len(markers) == 0 or len(detections) == 0:
        return []
    after = np.full(len(markers), cfg.window_after_s) if window_after is None else window_after
    dt = detections[None, :] - markers[:, None]
    allowed = (dt >= -cfg.window_before_s) & (dt <= after[:, None])
    cost = np.where(allowed, np.abs(dt - cfg.expected_offset_s), _FORBIDDEN)
    rows, cols = linear_sum_assignment(cost)
    return [(int(r), int(c)) for r, c in zip(rows, cols) if allowed[r, c]]


def cast_window_after(all_markers: pd.DataFrame, cast_t: np.ndarray, cfg: MatchingConfig) -> np.ndarray:
    """CAST マーカーごとの照合窓の終わり（マーカーからの秒数）。

    reel_bound_max_s > 0 のとき、次の CAST より前・reel_bound_max_s 以内に REEL があればその時刻までにする。
    """
    after = np.full(len(cast_t), cfg.window_after_s)
    if cfg.reel_bound_max_s <= 0:
        return after
    reels = all_markers.loc[all_markers["type"] == "REEL", "t"].to_numpy()
    for i, t in enumerate(cast_t):
        nxt = cast_t[i + 1] if i + 1 < len(cast_t) else np.inf
        r = reels[(reels > t) & (reels < nxt) & (reels <= t + cfg.reel_bound_max_s)]
        if len(r):
            after[i] = r[0] - t
    return after


def _in_ranges(t: np.ndarray, ranges: tuple[tuple[float, float], ...]) -> np.ndarray:
    m = np.zeros(len(t), dtype=bool)
    for a, b in ranges:
        m |= (t >= a) & (t <= b)
    return m


def _nearest_marker(t: float, markers: pd.DataFrame) -> str:
    if markers.empty:
        return ""
    i = int(np.argmin(np.abs(markers["t"].to_numpy() - t)))
    row = markers.iloc[i]
    return f"{row['type']}{t - row['t']:+.1f}s"


@dataclass
class SessionResult:
    session: SessionConfig
    duration_s: float
    markers: pd.DataFrame
    """CAST マーカーの照合結果。列: marker_t, status(matched/missed/excluded), det_t, offset_s"""
    detections: pd.DataFrame
    """検出の照合結果。列: t, accel_peak, gyro_peak, status, marker_t, offset_s, nearest_marker"""

    @property
    def counts(self) -> dict[str, int]:
        m, d = self.markers, self.detections
        return {
            "cast_markers": int((m["status"] != "excluded").sum()),
            "matched": int((m["status"] == "matched").sum()),
            "missed": int((m["status"] == "missed").sum()),
            "detections": int((d["status"] != "excluded").sum()),
            "unmatched_detections": int(d["status"].isin(["false_positive", "unconfirmed", "unlabeled"]).sum()),
        }


def evaluate_session(
    session_cfg: SessionConfig,
    duration_s: float,
    all_markers: pd.DataFrame,
    detections: pd.DataFrame,
    cfg: MatchingConfig,
) -> SessionResult:
    use_markers = session_cfg.label != "none"
    cast_t = all_markers.loc[all_markers["type"] == "CAST", "t"].to_numpy() if use_markers else np.array([])
    det_t = detections["t"].to_numpy()

    m_ex = _in_ranges(cast_t, session_cfg.exclude)
    d_ex = _in_ranges(det_t, session_cfg.exclude)
    m_idx = np.flatnonzero(~m_ex)
    d_idx = np.flatnonzero(~d_ex)
    after = cast_window_after(all_markers, cast_t, cfg)
    pairs = [(m_idx[i], d_idx[j]) for i, j in match_one_to_one(cast_t[m_idx], det_t[d_idx], cfg, after[m_idx])]
    m2d = dict(pairs)
    d2m = {j: i for i, j in pairs}

    marker_rows = []
    for i, t in enumerate(cast_t):
        if m_ex[i]:
            marker_rows.append((t, "excluded", np.nan, np.nan))
        elif i in m2d:
            dt = det_t[m2d[i]]
            marker_rows.append((t, "matched", dt, dt - t))
        else:
            marker_rows.append((t, "missed", np.nan, np.nan))
    markers_df = pd.DataFrame(marker_rows, columns=["marker_t", "status", "det_t", "offset_s"])

    unmatched_status = {
        "complete": "false_positive",
        "likely": "unconfirmed",
        "uncertain": "unconfirmed",
        "none": "unlabeled",
    }[session_cfg.label]
    det = detections.copy()
    status, mt, off = [], [], []
    for j, t in enumerate(det_t):
        if d_ex[j]:
            status.append("excluded"), mt.append(np.nan), off.append(np.nan)
        elif j in d2m:
            m = cast_t[d2m[j]]
            status.append("matched"), mt.append(m), off.append(t - m)
        else:
            status.append(unmatched_status), mt.append(np.nan), off.append(np.nan)
    det["status"] = status
    det["marker_t"] = mt
    det["offset_s"] = off
    det["nearest_marker"] = [_nearest_marker(t, all_markers) for t in det_t]
    return SessionResult(session_cfg, duration_s, markers_df, det)


def _rate(n: int, d: int) -> float:
    return n / d if d else float("nan")


def summarize(results: list[SessionResult]) -> pd.DataFrame:
    """セッションごとの集計表。率はラベル信頼度が許す範囲でのみ埋める。"""
    rows = []
    for r in results:
        c = r.counts
        label = r.session.label
        row = {
            "session": r.session.name,
            "label": label,
            "role": r.session.role,
            "minutes": round(r.duration_s / 60, 1),
            **c,
            "recall": np.nan,
            "precision": np.nan,
            "f1": np.nan,
            "recall_ref": np.nan,
        }
        if label == "complete":
            p = _rate(c["matched"], c["detections"])
            rc = _rate(c["matched"], c["cast_markers"])
            row.update(recall=rc, precision=p, f1=_rate(2 * p * rc, p + rc) if p == p and rc == rc else np.nan)
        elif label == "likely":
            row["recall_ref"] = _rate(c["matched"], c["cast_markers"])
        rows.append(row)
    return pd.DataFrame(rows)


def aggregate(results: list[SessionResult]) -> pd.DataFrame:
    """ラベル信頼度 × 役割ごとの合算。異なる信頼度は混ぜない。"""
    rows = []
    for (label, role), group in _group(results).items():
        tot = {k: sum(r.counts[k] for r in group) for k in group[0].counts}
        row = {"label": label, "role": role, "sessions": len(group), **tot}
        if label == "complete":
            p = _rate(tot["matched"], tot["detections"])
            rc = _rate(tot["matched"], tot["cast_markers"])
            row.update(recall=rc, precision=p, f1=_rate(2 * p * rc, p + rc) if p == p and rc == rc else np.nan)
        elif label == "likely":
            row["recall_ref"] = _rate(tot["matched"], tot["cast_markers"])
        elif label == "none":
            minutes = sum(r.duration_s for r in group) / 60
            row["detections_per_hour"] = _rate(tot["detections"] * 60, minutes)
        rows.append(row)
    order = {lv: i for i, lv in enumerate(("complete", "likely", "uncertain", "none"))}
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(["role", "label"], key=lambda s: s.map(order) if s.name == "label" else s)
    return df.reset_index(drop=True)


def _group(results: list[SessionResult]) -> dict[tuple[str, str], list[SessionResult]]:
    groups: dict[tuple[str, str], list[SessionResult]] = {}
    for r in results:
        groups.setdefault((r.session.label, r.session.role), []).append(r)
    return groups
