"""セッション読み込み → 検出 → 照合 をまとめて行う処理。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from .config import Config
from .detector import DetectorParams, detect
from .evaluate import SessionResult, evaluate_session
from .io import Session, load_session
from .signals import Signals, resample


@dataclass
class LoadedSession:
    session: Session
    signals: Signals


_cache: dict[Path, LoadedSession] = {}


def load(path: Path) -> LoadedSession:
    key = path.resolve()
    if key not in _cache:
        s = load_session(path)
        _cache[key] = LoadedSession(s, resample(s))
    return _cache[key]


def run(loaded: LoadedSession, params: DetectorParams, config: Config) -> SessionResult:
    s = loaded.session
    scfg = config.session(s.name, has_cast_markers=bool((s.markers["type"] == "CAST").any()))
    det = detect(loaded.signals, params)
    return evaluate_session(scfg, s.duration_s, s.markers, det, config.matching)


def detections_table(loaded: LoadedSession, result: SessionResult) -> pd.DataFrame:
    """CSV 出力用。相対秒に加え、元 CSV と同じ時間基準の timestamp_ns も付ける。"""
    d = result.detections.copy()
    d.insert(0, "session", loaded.session.name)
    d.insert(2, "timestamp_ns", (d["t"] * 1e9).round().astype("int64") + loaded.session.t0_ns)
    return d.round(3)
