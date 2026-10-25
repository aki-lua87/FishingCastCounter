"""ルールベースのキャスト候補検知器（試作）。

アルゴリズム:
1. 加速度合成値 |a|（重力込み）を smooth_s で平滑化する
2. |a| >= accel_peak_min の局所最大を候補とする
3. 候補の前後 [t - gyro_window_before_s, t + gyro_window_after_s] でジャイロ合成値の最大が
   gyro_peak_min 以上のものだけ残す
4. （post_gyro_mean_max > 0 のとき）ピーク後 [t + post_still_start_s, t + post_still_end_s] の
   |g| 平均が post_gyro_mean_max 以下のものだけ残す。キャスト直後はルアーが飛んでいる間
   リール側の腕がほぼ静止する一方、歩行・リトリーブ中は動き続けることを利用する
5. 前後 min_interval_s 未満に |a| がより大きい候補（同じ値なら先の候補）があるものを捨てる
   （同じキャストの多重検出防止）。判定に必要なのは前後 min_interval_s の候補だけなので、
   ウォッチ上でも一定の遅延（min_interval_s + post_still_end_s）で同じ結果を出せる

Wear OS 側の Kotlin 実装（app/.../detection/CastDetector.kt）と同じ結果になるよう保つこと。
変更したら analysis/tools/export_parity_fixtures.py で一致テスト用データを作り直す。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

import numpy as np
import pandas as pd

from .signals import Signals, moving_average


@dataclass(frozen=True)
class DetectorParams:
    """既定値は P1（params/default.json、Wear OS の DetectorParams と同じ）。"""

    accel_peak_min: float = 25.0
    """候補とする加速度合成値ピークの下限（m/s^2, 重力込み）"""
    gyro_peak_min: float = 4.0
    """候補の前後窓で必要なジャイロ合成値の最大値（rad/s）"""
    gyro_window_before_s: float = 1.5
    gyro_window_after_s: float = 0.5
    min_interval_s: float = 5.0
    """検出同士の最小間隔（秒）"""
    smooth_s: float = 0.0
    """|a| の移動平均幅（秒）。0 で平滑化なし"""
    post_gyro_mean_max: float = 1.5
    """ピーク後の静止判定: |g| 平均の上限（rad/s）。0 で無効"""
    post_still_start_s: float = 0.5
    post_still_end_s: float = 3.0

    @classmethod
    def from_dict(cls, d: dict) -> "DetectorParams":
        known = {f.name for f in fields(cls)}
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"未知のパラメータ: {sorted(unknown)}（有効: {sorted(known)}）")
        return cls(**{k: float(v) for k, v in d.items()})

    @classmethod
    def load(cls, path: str | Path) -> "DetectorParams":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def with_overrides(self, overrides: list[str]) -> "DetectorParams":
        """["accel_peak_min=28", ...] 形式の上書きを適用する。"""
        d = {}
        for item in overrides:
            key, sep, value = item.partition("=")
            if not sep:
                raise ValueError(f"--set は key=value 形式で指定してください: {item}")
            d[key.strip()] = value
        DetectorParams.from_dict(d)  # キー名の検証
        return replace(self, **{k: float(v) for k, v in d.items()})

    def to_dict(self) -> dict:
        return asdict(self)

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


DETECTION_COLUMNS = ["t", "accel_peak", "gyro_peak", "post_gyro_mean"]


def detect(sig: Signals, params: DetectorParams) -> pd.DataFrame:
    """キャスト候補を検出する。戻り値の列: t（秒）, accel_peak, gyro_peak, post_gyro_mean"""
    am = moving_average(sig.accel_mag, int(round(params.smooth_s * sig.fs)))
    gm = np.nan_to_num(sig.gyro_mag)
    am = np.nan_to_num(am)
    n = len(am)
    if n < 3:
        return pd.DataFrame(columns=DETECTION_COLUMNS)

    # 2. 閾値を超える局所最大（平坦なピークは先頭を採用）
    is_peak = np.zeros(n, dtype=bool)
    is_peak[1:-1] = (am[1:-1] >= params.accel_peak_min) & (am[1:-1] > am[:-2]) & (am[1:-1] >= am[2:])
    idx = np.flatnonzero(is_peak)

    # 3. ジャイロ条件
    before = int(round(params.gyro_window_before_s * sig.fs))
    after = int(round(params.gyro_window_after_s * sig.fs))
    post_a = int(round(params.post_still_start_s * sig.fs))
    post_b = int(round(params.post_still_end_s * sig.fs))
    cands = []
    for i in idx:
        gpk = gm[max(0, i - before) : min(n, i + after + 1)].max()
        if gpk < params.gyro_peak_min:
            continue
        # 4. ピーク後の静止（データ末尾で窓が取れない場合は判定不能 = inf）
        post = gm[i + post_a : min(n, i + post_b + 1)]
        post_mean = float(post.mean()) if len(post) else float("inf")
        if params.post_gyro_mean_max > 0 and post_mean > params.post_gyro_mean_max:
            continue
        cands.append((i, am[i], gpk, post_mean))

    # 5. 近接候補の抑制（cands は時刻順）
    min_gap = params.min_interval_s * sig.fs
    kept = []
    for k, c in enumerate(cands):
        beaten = False
        for j in range(k - 1, -1, -1):
            if c[0] - cands[j][0] >= min_gap:
                break
            if cands[j][1] >= c[1]:  # 同値なら先の候補が勝つ
                beaten = True
                break
        if not beaten:
            for j in range(k + 1, len(cands)):
                if cands[j][0] - c[0] >= min_gap:
                    break
                if cands[j][1] > c[1]:
                    beaten = True
                    break
        if not beaten:
            kept.append(c)

    return pd.DataFrame(
        {
            "t": [sig.t[c[0]] for c in kept],
            "accel_peak": [c[1] for c in kept],
            "gyro_peak": [c[2] for c in kept],
            "post_gyro_mean": [c[3] for c in kept],
        },
        columns=DETECTION_COLUMNS,
    )
