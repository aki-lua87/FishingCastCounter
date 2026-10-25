import numpy as np
import pandas as pd

from castlab.config import MatchingConfig, SessionConfig
from castlab.evaluate import aggregate, evaluate_session, match_one_to_one, summarize

CFG = MatchingConfig(window_before_s=2.0, window_after_s=12.0, expected_offset_s=5.0)


def _markers(*cast_t, other=()):
    rows = [(t, "CAST") for t in cast_t] + [(t, "REEL") for t in other]
    return pd.DataFrame(rows, columns=["t", "type"]).sort_values("t").reset_index(drop=True)


def _dets(*t):
    return pd.DataFrame({"t": list(t), "accel_peak": [30.0] * len(t), "gyro_peak": [8.0] * len(t)})


def test_one_detection_cannot_match_two_markers():
    # 2 つのマーカーの窓に 1 つの検出しかない → 一致は 1 件だけ
    pairs = match_one_to_one(np.array([0.0, 3.0]), np.array([6.0]), CFG)
    assert len(pairs) == 1


def test_one_marker_cannot_match_two_detections():
    pairs = match_one_to_one(np.array([0.0]), np.array([4.0, 6.0]), CFG)
    assert pairs == [(0, 0)] or pairs == [(0, 1)]
    assert len(pairs) == 1


def test_assignment_maximizes_matches_before_offset_cost():
    # マーカー0 は理想オフセットに近い検出 4.5 を好むが、それを取るとマーカー5 の窓 [3, 17] に相手が残らない。
    # 貪欲法なら 1 件、最大マッチングなら 0↔-1.0 / 5↔4.5 で 2 件。
    pairs = match_one_to_one(np.array([0.0, 5.0]), np.array([-1.0, 4.5]), CFG)
    assert sorted(pairs) == [(0, 0), (1, 1)]


def test_window_bounds():
    assert match_one_to_one(np.array([10.0]), np.array([7.9]), CFG) == []
    assert match_one_to_one(np.array([10.0]), np.array([22.1]), CFG) == []
    assert len(match_one_to_one(np.array([10.0]), np.array([8.0]), CFG)) == 1
    assert len(match_one_to_one(np.array([10.0]), np.array([22.0]), CFG)) == 1


def test_duplicate_detection_is_not_counted_twice():
    sc = SessionConfig("s", "complete", "tune")
    r = evaluate_session(sc, 100.0, _markers(10.0), _dets(14.0, 16.0), CFG)
    assert r.counts["matched"] == 1
    assert list(r.detections["status"]).count("false_positive") == 1


def test_unmatched_status_depends_on_label():
    m, d = _markers(10.0), _dets(15.0, 60.0)
    assert evaluate_session(SessionConfig("s", "complete", "eval"), 100, m, d, CFG).detections["status"].tolist() == [
        "matched", "false_positive"]
    assert evaluate_session(SessionConfig("s", "likely", "eval"), 100, m, d, CFG).detections["status"].tolist() == [
        "matched", "unconfirmed"]
    assert evaluate_session(SessionConfig("s", "uncertain", "eval"), 100, m, d, CFG).detections["status"].tolist() == [
        "matched", "unconfirmed"]
    # none: マーカーを使わない（正解として扱わない）
    r = evaluate_session(SessionConfig("s", "none", "eval"), 100, m, d, CFG)
    assert r.detections["status"].tolist() == ["unlabeled", "unlabeled"]
    assert r.counts["cast_markers"] == 0


def test_rates_only_for_trusted_labels():
    m, d = _markers(10.0, 50.0), _dets(15.0, 80.0)
    results = [
        evaluate_session(SessionConfig("a", "complete", "eval"), 100, m, d, CFG),
        evaluate_session(SessionConfig("b", "likely", "eval"), 100, m, d, CFG),
        evaluate_session(SessionConfig("c", "uncertain", "eval"), 100, m, d, CFG),
    ]
    s = summarize(results).set_index("session")
    assert s.loc["a", "precision"] == 0.5 and s.loc["a", "recall"] == 0.5
    assert np.isnan(s.loc["b", "precision"]) and s.loc["b", "recall_ref"] == 0.5
    assert np.isnan(s.loc["c", "precision"]) and np.isnan(s.loc["c", "recall"]) and np.isnan(s.loc["c", "recall_ref"])
    # 信頼度が異なるものは合算しない
    agg = aggregate(results)
    assert sorted(agg["label"]) == ["complete", "likely", "uncertain"]


def test_exclude_ranges():
    sc = SessionConfig("s", "complete", "eval", exclude=((0.0, 30.0),))
    r = evaluate_session(sc, 100.0, _markers(10.0, 50.0), _dets(15.0, 55.0), CFG)
    assert r.markers["status"].tolist() == ["excluded", "matched"]
    assert r.detections["status"].tolist() == ["excluded", "matched"]
    assert r.counts == {"cast_markers": 1, "matched": 1, "missed": 0, "detections": 1, "unmatched_detections": 0}


def test_ignore_flag(tmp_path):
    from castlab.config import load_config

    p = tmp_path / "s.toml"
    p.write_text('[sessions.a]\nignore = true\n[sessions.b]\nlabel = "likely"\n', encoding="utf-8")
    cfg = load_config(p)
    assert cfg.is_ignored("a")
    assert not cfg.is_ignored("b") and not cfg.is_ignored("unknown")


def test_reel_bound_window():
    cfg = MatchingConfig(window_before_s=2.0, window_after_s=12.0, expected_offset_s=5.0, reel_bound_max_s=30.0)
    # CAST 0 → REEL 25: 検出 20 は固定窓（+12）の外だが、REEL より前なので一致
    m = pd.DataFrame({"t": [0.0, 25.0, 60.0], "type": ["CAST", "REEL", "CAST"]})
    r = evaluate_session(SessionConfig("s", "complete", "eval"), 100, m, _dets(20.0, 64.0), cfg)
    assert r.markers["status"].tolist() == ["matched", "matched"]
    # REEL より後の検出は一致しない
    r = evaluate_session(SessionConfig("s", "complete", "eval"), 100, m, _dets(26.0), cfg)
    assert r.markers["status"].tolist() == ["missed", "missed"]
    # 無効なら従来どおり +12 まで
    r = evaluate_session(SessionConfig("s", "complete", "eval"), 100, m, _dets(20.0), CFG)
    assert r.markers["status"].tolist() == ["missed", "missed"]
