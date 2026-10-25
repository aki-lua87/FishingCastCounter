"""ウォッチ上のイベント（Phase 3: AUTO_CAST / MISS / FALSE / UNDO）の解析。

- AUTO_CAST はウォッチ上の検知器の出力で、正解ラベルとしては扱わない
- MISS / FALSE はユーザーの申告。誤検知・未検知の原因を調べる手がかりとして表示する
- PC で同じパラメータ（meta の detector_*）を使って再検知し、ウォッチの AUTO_CAST と
  グリッド番号で突き合わせる（実装が一致していれば全件一致するはず）
"""

from __future__ import annotations

from dataclasses import fields

import numpy as np
import pandas as pd

from .detector import DetectorParams, detect
from .io import Session
from .signals import Signals


def device_params(meta: dict[str, str]) -> DetectorParams | None:
    """meta の detector_* 行から、ウォッチで使ったパラメータを復元する。"""
    names = {f.name for f in fields(DetectorParams)}
    d = {k.removeprefix("detector_"): v for k, v in meta.items() if k.startswith("detector_")}
    d = {k: v for k, v in d.items() if k in names}
    return DetectorParams.from_dict(d) if d else None


def counts(session: Session) -> dict[str, int]:
    auto = session.device_events("AUTO_CAST")
    miss = session.device_events("MISS")
    false = session.device_events("FALSE")
    undo = session.device_events("UNDO")
    return {
        "auto_cast": len(auto),
        "miss": len(miss),
        "false": len(false),
        "undo": len(undo),
        "corrected": len(auto) + len(miss) - len(false),
    }


def auto_cast_table(session: Session) -> pd.DataFrame:
    """AUTO_CAST ごとの特徴量と、FALSE 申告の有無。"""
    auto = session.device_events("AUTO_CAST")
    false = session.device_events("FALSE")
    false_by_target = dict(zip(false["ref_id"].astype(int), false["event_id"]))
    rows = []
    for r in auto.itertuples():
        d = r.detail
        rows.append(
            {
                "event_id": r.event_id,
                "t": round(r.t, 2),
                "confirmed_t": round(r.recorded_t, 2),
                "grid": int(d.get("grid", -1)),
                "accel_peak": float(d.get("accel_peak", np.nan)),
                "gyro_peak": float(d.get("gyro_peak", np.nan)),
                "post_gyro_mean": float(d.get("post_gyro_mean", np.nan)),
                "false_event": false_by_target.get(r.event_id, pd.NA),
                "final_flush": d.get("final_flush", "") == "1",
            }
        )
    return pd.DataFrame(rows)


def compare_with_pc(session: Session, sig: Signals) -> tuple[pd.DataFrame, DetectorParams | None]:
    """ウォッチの AUTO_CAST と PC 再検知をグリッド番号で突き合わせる。

    戻り値の列: grid, t, source（both / watch_only / pc_only）, watch_event_id, accel_peak
    """
    params = device_params(session.meta)
    if params is None:
        return pd.DataFrame(), None
    pc = detect(sig, params)
    pc_grid = {int(round(t * sig.fs)): (t, a) for t, a in zip(pc["t"], pc["accel_peak"])}
    watch = auto_cast_table(session)
    watch_grid = dict(zip(watch["grid"], zip(watch["event_id"], watch["t"], watch["accel_peak"]))) if len(watch) else {}
    rows = []
    for g in sorted(set(pc_grid) | set(watch_grid)):
        if g in pc_grid and g in watch_grid:
            src = "both"
        elif g in watch_grid:
            src = "watch_only"
        else:
            src = "pc_only"
        t, a = pc_grid[g] if g in pc_grid else watch_grid[g][1:]
        rows.append({"grid": g, "t": round(t, 2), "source": src,
                     "watch_event_id": watch_grid[g][0] if g in watch_grid else pd.NA, "accel_peak": round(a, 2)})
    return pd.DataFrame(rows), params


def miss_windows(session: Session) -> pd.DataFrame:
    """MISS ごとの探索範囲（押した時刻からさかのぼる）と、押した時点の状況。"""
    miss = session.device_events("MISS")
    rows = []
    for r in miss.itertuples():
        d = r.detail
        lookback = float(d.get("lookback_s", 30))
        rows.append(
            {
                "event_id": r.event_id,
                "pressed_t": round(r.recorded_t, 2),
                "estimated_t": round(r.t, 2),
                "window_start_t": round(r.recorded_t - lookback, 2),
                "detector_pending": d.get("detector_pending", "") == "1",
                "since_last_auto_s": d.get("since_last_auto_s", ""),
            }
        )
    return pd.DataFrame(rows)
