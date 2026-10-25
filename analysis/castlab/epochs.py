"""マーカー前後の波形切り出しと、切り出し区間ごとの特徴量。

align="accel_peak" を指定すると、各マーカーの探索窓内で |a| が最大になる時刻を基準に揃える。
これは波形の形を比べるための整列であって、その時刻を正解のキャスト時刻とみなすものではない。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .signals import Signals


@dataclass
class Epoch:
    marker_type: str
    marker_t: float
    ref_t: float
    """切り出しの基準時刻（align なしなら marker_t）"""
    rel: np.ndarray
    accel: np.ndarray
    """(N, 3)"""
    gyro: np.ndarray

    @property
    def accel_mag(self) -> np.ndarray:
        return np.linalg.norm(self.accel, axis=1)

    @property
    def gyro_mag(self) -> np.ndarray:
        return np.linalg.norm(self.gyro, axis=1)


def cut(sig: Signals, t_ref: float, pre: float, post: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """[t_ref - pre, t_ref + post] を等間隔グリッドで切り出す。範囲外は NaN。"""
    rel = np.arange(-round(pre * sig.fs), round(post * sig.fs) + 1) / sig.fs
    tt = t_ref + rel

    def take(arr: np.ndarray) -> np.ndarray:
        return np.column_stack([np.interp(tt, sig.t, arr[:, i], left=np.nan, right=np.nan) for i in range(3)])

    return rel, take(sig.accel), take(sig.gyro)


def find_accel_peak(sig: Signals, t: float, search: tuple[float, float]) -> float:
    m = (sig.t >= t + search[0]) & (sig.t <= t + search[1])
    if not m.any():
        return t
    am = np.nan_to_num(sig.accel_mag[m])
    return float(sig.t[m][int(np.argmax(am))])


def marker_epochs(
    sig: Signals,
    markers: pd.DataFrame,
    types: list[str],
    pre: float,
    post: float,
    align: str | None = None,
    search: tuple[float, float] = (-2.0, 12.0),
) -> dict[str, list[Epoch]]:
    out: dict[str, list[Epoch]] = {t: [] for t in types}
    for _, row in markers.iterrows():
        if row["type"] not in out:
            continue
        ref = find_accel_peak(sig, row["t"], search) if align == "accel_peak" else row["t"]
        rel, acc, gyr = cut(sig, ref, pre, post)
        out[row["type"]].append(Epoch(row["type"], float(row["t"]), float(ref), rel, acc, gyr))
    return out


def epoch_features(sig: Signals, markers: pd.DataFrame, search: tuple[float, float] = (-2.0, 12.0)) -> pd.DataFrame:
    """マーカーごとに探索窓 [t+search[0], t+search[1]] 内の特徴量を計算する。"""
    rows = []
    am_all = sig.accel_mag
    gm_all = sig.gyro_mag
    for _, row in markers.iterrows():
        t = row["t"]
        m = (sig.t >= t + search[0]) & (sig.t <= t + search[1])
        if not m.any():
            continue
        tt = sig.t[m]
        am = np.nan_to_num(am_all[m])
        gm = np.nan_to_num(gm_all[m])
        ia, ig = int(np.argmax(am)), int(np.argmax(gm))
        acc_at = sig.accel[m][ia]
        gyr_at = sig.gyro[m][ia]
        rows.append(
            {
                "marker_type": row["type"],
                "marker_t": round(t, 3),
                "accel_peak": round(float(am[ia]), 3),
                "accel_peak_offset_s": round(float(tt[ia] - t), 3),
                "gyro_peak": round(float(gm[ig]), 3),
                "gyro_peak_offset_s": round(float(tt[ig] - t), 3),
                "gyro_minus_accel_peak_s": round(float(tt[ig] - tt[ia]), 3),
                "accel_x_at_peak": round(float(acc_at[0]), 3),
                "accel_y_at_peak": round(float(acc_at[1]), 3),
                "accel_z_at_peak": round(float(acc_at[2]), 3),
                "gyro_x_at_accel_peak": round(float(gyr_at[0]), 3),
                "gyro_y_at_accel_peak": round(float(gyr_at[1]), 3),
                "gyro_z_at_accel_peak": round(float(gyr_at[2]), 3),
                "accel_mag_mean": round(float(am.mean()), 3),
                "gyro_mag_mean": round(float(gm.mean()), 3),
                "samples_over_20": int((am > 20).sum()),
            }
        )
    return pd.DataFrame(rows)
