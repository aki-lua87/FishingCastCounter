"""セッション CSV / meta の読み込み。

CSV 列: timestamp_ns,type,x,y,z
- type が ACCEL / GYRO の行はセンサーサンプル（x,y,z あり）
- type が MARK_* の行は手動マーカー（x,y,z は空）

センサー行は ACCEL と GYRO がバッチ単位で混在して書かれるため、時刻順には並んでいない。
読み込み時に種類ごとに分けてから timestamp でソートする。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

MARKER_PREFIX = "MARK_"
SESSION_CSV = re.compile(r"session_\d{8}_\d{6}\.csv")
MARKER_TYPES = ("CAST", "REEL", "WALK", "OTHER")


@dataclass
class Session:
    name: str
    csv_path: Path
    meta: dict[str, str]
    t0_ns: int
    """相対時刻 t=0 とする timestamp（ns）。meta の start_elapsed_realtime_ns、無ければ最初の行。"""
    accel: pd.DataFrame
    """列: t（秒, t0 基準）, x, y, z"""
    gyro: pd.DataFrame
    """列: t, x, y, z"""
    markers: pd.DataFrame
    """列: t, type（CAST / REEL / WALK / OTHER）。手動マーカーのみ（UNDO 済みは除く）。正解ラベルとして使うのはこれだけ"""
    events: pd.DataFrame | None = None
    """session_*.events.csv（Phase 3 以降）。列: event_id, type, t, recorded_t, ref_id, detail(dict), active。
    AUTO_CAST はウォッチ上の検知器の出力で、正解ラベルではない"""
    extra: dict = field(default_factory=dict)

    @property
    def duration_s(self) -> float:
        return float(max(self.accel["t"].iloc[-1], self.gyro["t"].iloc[-1]))

    def marker_times(self, marker_type: str) -> np.ndarray:
        return self.markers.loc[self.markers["type"] == marker_type, "t"].to_numpy()

    def device_events(self, event_type: str, active_only: bool = True) -> pd.DataFrame:
        """events.csv の指定種別（AUTO_CAST / MISS / FALSE / UNDO など）。events.csv が無ければ空。"""
        if self.events is None:
            return pd.DataFrame(columns=EVENT_COLUMNS)
        e = self.events[self.events["type"] == event_type]
        return e[e["active"]] if active_only else e


def read_meta(path: Path) -> dict[str, str]:
    meta: dict[str, str] = {}
    if not path.exists():
        return meta
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if not sep:
            continue
        # sensor= は複数行あるので連番キーにする
        if key in meta:
            i = 2
            while f"{key}#{i}" in meta:
                i += 1
            key = f"{key}#{i}"
        meta[key] = value
    return meta


def meta_path_for(csv_path: Path) -> Path:
    return csv_path.with_name(csv_path.stem + ".meta.txt")


def events_path_for(csv_path: Path) -> Path:
    return csv_path.with_name(csv_path.stem + ".events.csv")


EVENT_COLUMNS = ["event_id", "type", "timestamp_ns", "recorded_ns", "ref_id", "detail", "t", "recorded_t", "active"]


def read_events(path: Path, t0_ns: int) -> pd.DataFrame:
    """events.csv を読み、UNDO を反映した active 列を付ける（元のイベントは消さない）。

    列: event_id, type, timestamp_ns, recorded_ns, ref_id（無ければ <NA>）, detail（dict）,
        t / recorded_t（t0 からの秒）, active（UNDO されていない）
    """
    df = pd.read_csv(path, dtype={"event_id": np.int64, "type": str, "timestamp_ns": np.int64,
                                  "recorded_ns": np.int64, "ref_id": "Int64", "detail": str},
                     keep_default_na=False, na_values={"ref_id": [""]})
    df["detail"] = [dict(kv.split("=", 1) for kv in d.split(";") if kv) if d else {} for d in df["detail"]]
    df["t"] = (df["timestamp_ns"] - t0_ns) / 1e9
    df["recorded_t"] = (df["recorded_ns"] - t0_ns) / 1e9
    undone = set(df.loc[df["type"] == "UNDO", "ref_id"].dropna().astype(int))
    df["active"] = ~df["event_id"].isin(undone)
    return df.sort_values("event_id").reset_index(drop=True)


def load_session(csv_path: str | Path) -> Session:
    csv_path = Path(csv_path)
    # x,y,z はウォッチ上の値（float32）と同じにするため float32 に丸めてから float64 で扱う。
    # CSV の文字列は float32 を最短表記したものなので、float64 で読んで float32 に丸めれば元の値に戻る。
    # これで Wear OS 側の検知器（Float → Double）と入力が完全に一致する
    raw = pd.read_csv(
        csv_path,
        dtype={"timestamp_ns": np.int64, "type": str, "x": np.float64, "y": np.float64, "z": np.float64},
    )
    for c in ("x", "y", "z"):
        raw[c] = raw[c].astype(np.float32).astype(np.float64)
    meta = read_meta(meta_path_for(csv_path))

    if "start_elapsed_realtime_ns" in meta:
        t0_ns = int(meta["start_elapsed_realtime_ns"])
    else:
        t0_ns = int(raw["timestamp_ns"].min())

    def sensor(kind: str) -> pd.DataFrame:
        df = raw.loc[raw["type"] == kind, ["timestamp_ns", "x", "y", "z"]]
        df = df.sort_values("timestamp_ns", kind="stable").drop_duplicates("timestamp_ns")
        out = pd.DataFrame(
            {
                "t": (df["timestamp_ns"].to_numpy() - t0_ns) / 1e9,
                "x": df["x"].to_numpy(),
                "y": df["y"].to_numpy(),
                "z": df["z"].to_numpy(),
            }
        )
        return out.reset_index(drop=True)

    events_path = events_path_for(csv_path)
    events = read_events(events_path, t0_ns) if events_path.exists() else None
    if events is not None:
        # events.csv があればそちらが正。UNDO された手動マーカーを除く（センサー CSV の MARK_* 行は追記のみで消えない）
        mk = events[events["type"].isin(MARKER_TYPES) & events["active"]].sort_values("timestamp_ns", kind="stable")
        markers = pd.DataFrame({"t": mk["t"].to_numpy(), "type": mk["type"].to_numpy()})
    else:
        mk = raw.loc[raw["type"].str.startswith(MARKER_PREFIX), ["timestamp_ns", "type"]]
        mk = mk.sort_values("timestamp_ns", kind="stable")
        markers = pd.DataFrame(
            {
                "t": (mk["timestamp_ns"].to_numpy() - t0_ns) / 1e9,
                "type": mk["type"].str.removeprefix(MARKER_PREFIX).to_numpy(),
            }
        ).reset_index(drop=True)

    return Session(
        name=csv_path.stem,
        csv_path=csv_path,
        meta=meta,
        t0_ns=t0_ns,
        accel=sensor("ACCEL"),
        gyro=sensor("GYRO"),
        markers=markers,
        events=events,
    )


def find_sessions(paths: list[str | Path]) -> list[Path]:
    """ファイル・ディレクトリ・glob パターンの混在を CSV パスの一覧に展開する。"""
    result: list[Path] = []
    for p in paths:
        p = Path(p)
        if p.is_dir():
            # session_*.events.csv（イベント記録）はセッション本体ではないので除く
            result.extend(sorted(q for q in p.glob("session_*.csv") if SESSION_CSV.fullmatch(q.name)))
        elif p.exists():
            result.append(p)
        else:
            matched = sorted(Path().glob(str(p)))
            if not matched:
                raise FileNotFoundError(f"セッションが見つかりません: {p}")
            result.extend(matched)
    # 重複除去（順序維持）
    seen: set[Path] = set()
    unique = []
    for p in result:
        key = p.resolve()
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique
