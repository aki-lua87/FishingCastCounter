"""P2 候補（方向条件つき）の検知器のテスト。

- 合成データ: 方向条件の判定（回転方向・構え姿勢）と、無効時に P1 と同じ出力になること
- 一致テスト用データ（app/src/test/resources/parity、git 管理）: Kotlin 側と共有している期待値を Python でも再現すること
- 実データ（captures/sessions、git 管理外。無ければ省略）: P1 がウォッチの AUTO_CAST と一致すること（P1 を変えていない確認）と、
  P2 候補の 10/25 での成績が 2026-10-31 フィールドレビュー §11.5（案 C）の数値と同じであること
"""

import gzip
import shutil
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from castlab.config import load_config
from castlab.detector import DIRECTION_COLUMNS, DetectorParams, _cos, detect
from castlab.evaluate import evaluate_session
from castlab.io import find_sessions, load_session
from castlab.signals import Signals, resample

ANALYSIS = Path(__file__).resolve().parent.parent
ROOT = ANALYSIS.parent
PARITY = ROOT / "app" / "src" / "test" / "resources" / "parity"
SESSIONS = ROOT / "captures" / "sessions"
P1 = DetectorParams.load(ANALYSIS / "params" / "default.json")
P2 = DetectorParams.load(ANALYSIS / "params" / "p2_candidate.json")
FS = 50.0


def _cast_like(gyro_vec, post_gravity, duration_s=40.0, t_peak=10.0):
    """|a| ピーク 30（t_peak）、ピーク時に gyro_vec の回転、ピーク後は post_gravity の向きで静止。"""
    t = np.arange(0, duration_s, 1 / FS)
    accel = np.tile(np.asarray(post_gravity, dtype=float), (len(t), 1))
    gyro = np.zeros((len(t), 3))
    i = int(t_peak * FS)
    accel[i] = [0.0, 0.0, 30.0]
    gyro[i - 3 : i + 2] = gyro_vec
    return Signals(t, FS, accel, gyro)


def test_presets():
    assert not P1.uses_direction
    assert P2.uses_direction
    assert P2.accel_peak_min == 21.0 and P2.gyro_dir_cos_min == 0.8 and P2.post_dir_cos_min == 0.85
    # |a| 閾値と方向条件以外は P1 と同じ（ジャイロ下限・静止条件・近接抑制）
    def base(p):
        return {k: v for k, v in p.to_dict().items()
                if k != "accel_peak_min" and not k.startswith(("gyro_dir", "post_dir", "dir_"))}

    assert base(P2) == base(P1)
    # テンプレートは P1 の既定値と同じ（方向条件の下限だけが違う）
    assert replace(P2, accel_peak_min=P1.accel_peak_min, gyro_dir_cos_min=-1.0, post_dir_cos_min=-1.0) == P1


def test_cos():
    assert _cos((1.0, 0.0, 0.0), (2.0, 0.0, 0.0)) == 1.0
    assert _cos((0.0, 0.0, 0.0), (1.0, 0.0, 0.0)) == 0.0
    assert _cos((1.0, 0.0, 0.0), (0.0, 1.0, 0.0)) == 0.0


def test_direction_conditions():
    g = (P2.gyro_dir_x * 10, P2.gyro_dir_y * 10, P2.gyro_dir_z * 10)
    post = (P2.post_dir_x * 9.8, P2.post_dir_y * 9.8, P2.post_dir_z * 9.8)
    # テンプレートどおりのキャスト → P1 でも P2 でも検出。P2 は方向の列が付く
    sig = _cast_like(g, post)
    d1, d2 = detect(sig, P1), detect(sig, P2)
    assert len(d1) == len(d2) == 1
    assert list(d1.columns) == ["t", "accel_peak", "gyro_peak", "post_gyro_mean"]
    assert d2["gyro_dir_cos"].iloc[0] == pytest.approx(1.0) and d2["post_dir_cos"].iloc[0] == pytest.approx(1.0)
    # 回転の向きが逆 → P2 だけ落ちる
    assert len(detect(_cast_like(tuple(-v for v in g), post), P1)) == 1
    assert detect(_cast_like(tuple(-v for v in g), post), P2).empty
    # 構え姿勢が違う（重力が z 方向）→ P2 だけ落ちる
    assert len(detect(_cast_like(g, (0.0, 0.0, 9.8)), P1)) == 1
    assert detect(_cast_like(g, (0.0, 0.0, 9.8)), P2).empty


def test_direction_disabled_matches_p1():
    """方向条件の下限が -1（既定）なら、テンプレートの値によらず P1 と同じ結果・同じ列。"""
    sig = _cast_like((1.0, 2.0, 3.0), (0.0, 0.0, 9.8))
    other = replace(P1, gyro_dir_x=1.0, post_dir_z=-1.0)
    pd.testing.assert_frame_equal(detect(sig, P1), detect(sig, other))


@pytest.mark.parametrize("name", ["casts_150936", "mixed_113449", "impacts_113449", "walk_085344",
                                  "weak_202426", "hook_200612"])
@pytest.mark.parametrize("params_name", ["p1", "baseline_v1", "p2_candidate"])
def test_parity_fixtures(tmp_path, name, params_name):
    """Kotlin の CastDetectorParityTest と同じ期待値を、Python でも再現する（期待値の作り直し忘れの検出）。"""
    csv = tmp_path / f"{name}.csv"
    with gzip.open(PARITY / f"{name}.csv.gz", "rb") as fin, csv.open("wb") as fout:
        shutil.copyfileobj(fin, fout)
    shutil.copy(PARITY / f"{name}.meta.txt", tmp_path / f"{name}.meta.txt")
    params = DetectorParams.load(PARITY / f"params_{params_name}.json")
    s = load_session(csv)
    d = detect(resample(s), params)
    exp = pd.read_csv(PARITY / f"{name}.{params_name}.expected.csv")
    assert [int(round(t * FS)) for t in d["t"]] == exp["grid_index"].tolist()
    for c in ["accel_peak", "gyro_peak", "post_gyro_mean"] + (DIRECTION_COLUMNS if params.uses_direction else []):
        assert d[c].tolist() == exp[c].tolist()


# --- 実データ（git 管理外） -------------------------------------------------------------------------------

needs_data = pytest.mark.skipif(not (SESSIONS / "session_20261031_212035.csv").exists(),
                                reason="captures/sessions が無い")
FIELD_1031 = ["135912", "193927", "195220", "200612", "202426", "210209", "211905", "212035"]


@needs_data
@pytest.mark.parametrize("hhmmss", FIELD_1031)
def test_p1_matches_watch_auto_cast(hhmmss):
    """P1（default.json）の PC 検知が、ウォッチ上の AUTO_CAST とグリッド番号まで一致する（P1 を変えていない）。"""
    s = load_session(SESSIONS / f"session_20261031_{hhmmss}.csv")
    d = detect(resample(s), P1)
    watch = s.device_events("AUTO_CAST")
    assert [int(round(t * FS)) for t in d["t"]] == [int(x["grid"]) for x in watch["detail"]]


@needs_data
def test_p2_regression_1025():
    """2026-10-31 フィールドレビュー §11.5 の案 C（10/25 列）と同じ成績。"""
    config = load_config(ANALYSIS / "sessions.toml")
    agg: dict[str, list[int]] = {}
    walk = s113449 = 0
    none_det, none_h = 0, 0.0
    for p in find_sessions([SESSIONS]):
        if not p.stem.startswith("session_20261025") or config.is_ignored(p.stem):
            continue
        s = load_session(p)
        scfg = config.session(p.stem, has_cast_markers=bool((s.markers["type"] == "CAST").any()))
        res = evaluate_session(scfg, s.duration_s, s.markers, detect(resample(s), P2), config.matching)
        c = res.counts
        key = "tune" if p.stem.endswith("150936") else scfg.label
        a = agg.setdefault(key, [0, 0, 0])
        a[0] += c["cast_markers"]
        a[1] += c["matched"]
        a[2] += c["unmatched_detections"]
        if p.stem.endswith("085344"):
            walk = c["detections"]
        if p.stem.endswith("113449"):
            s113449 = c["detections"]
        if scfg.label == "none":
            none_det += c["detections"]
            none_h += s.duration_s / 3600
    assert agg["tune"] == [30, 29, 0]
    assert agg["likely"] == [15, 15, 0]
    assert agg["uncertain"][2] == 5
    assert s113449 == 4
    assert walk == 0
    assert none_det / none_h == pytest.approx(28.3, abs=0.05)


@needs_data
def test_p2_detection_counts_1031():
    """10/31 の 8 セッションでの P2 候補の検出数（§11.5 の案 C と同じ入力・同じ検知器）。"""
    counts = [len(detect(resample(load_session(SESSIONS / f"session_20261031_{h}.csv")), P2)) for h in FIELD_1031]
    assert counts == [10, 11, 13, 10, 3, 3, 0, 46]
