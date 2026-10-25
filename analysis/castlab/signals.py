"""センサー信号の前処理（等間隔グリッドへのリサンプリング・合成値）。

ACCEL と GYRO はサンプル時刻が揃っていないため、共通の等間隔グリッドに線形補間して扱う。
サンプル間隔が max_gap_s を超える区間（記録抜け）は補間せず NaN にする。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .io import Session

GRAVITY = 9.80665


@dataclass
class Signals:
    """等間隔グリッド上の信号。配列はすべて同じ長さ。"""

    t: np.ndarray
    fs: float
    accel: np.ndarray
    """(N, 3) m/s^2（重力込み）"""
    gyro: np.ndarray
    """(N, 3) rad/s"""

    @property
    def accel_mag(self) -> np.ndarray:
        return np.linalg.norm(self.accel, axis=1)

    @property
    def gyro_mag(self) -> np.ndarray:
        return np.linalg.norm(self.gyro, axis=1)

    def slice(self, t_start: float, t_end: float) -> "Signals":
        m = (self.t >= t_start) & (self.t <= t_end)
        return Signals(self.t[m], self.fs, self.accel[m], self.gyro[m])


def _interp_with_gaps(t_src: np.ndarray, v_src: np.ndarray, t_dst: np.ndarray, max_gap_s: float) -> np.ndarray:
    out = np.interp(t_dst, t_src, v_src, left=np.nan, right=np.nan)
    if len(t_src) >= 2:
        gaps = np.where(np.diff(t_src) > max_gap_s)[0]
        for i in gaps:
            out[(t_dst > t_src[i]) & (t_dst < t_src[i + 1])] = np.nan
    return out


def resample(session: Session, fs: float = 50.0, max_gap_s: float = 0.2) -> Signals:
    a, g = session.accel, session.gyro
    t_start = max(a["t"].iloc[0], g["t"].iloc[0])
    t_end = min(a["t"].iloc[-1], g["t"].iloc[-1])
    t = np.arange(np.ceil(t_start * fs), np.floor(t_end * fs) + 1) / fs

    def stack(df: pd.DataFrame) -> np.ndarray:
        ts = df["t"].to_numpy()
        return np.column_stack([_interp_with_gaps(ts, df[c].to_numpy(), t, max_gap_s) for c in ("x", "y", "z")])

    return Signals(t=t, fs=fs, accel=stack(a), gyro=stack(g))


def moving_average(x: np.ndarray, n: int) -> np.ndarray:
    """中心化した移動平均。n<=1 ならそのまま返す。"""
    if n <= 1:
        return x
    kernel = np.ones(n) / n
    return np.convolve(np.nan_to_num(x), kernel, mode="same")
