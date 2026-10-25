"""Phase 3 の変更前後の評価（変更を 1 つずつ積み上げて影響を分ける）。

    cd analysis
    .venv\\Scripts\\python.exe studies\\phase3_before_after.py

A. Phase 2 当初: baseline_v1 + 近接抑制は全体を |a| 順に貪欲選択 + 固定照合窓（−2〜+12 s）
B. A の近接抑制を「前後 min_interval 内の比較」に変更（ストリーミングで再現できる規則）
C. B + 静止フィルタ（post_gyro_mean_max = 1.5）＝ レビュー時の P1
D. C + 検知間隔 5 秒 ＝ 現在の default.json（Wear OS と同じ）
E. D + 照合で REEL マーカーを上限に使う（reel_bound_max_s = 30）

ラベル信頼度ごとに分けて集計し、率は complete（tune）のみ算出する。
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd

ANALYSIS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ANALYSIS))

from castlab.config import Config, load_config  # noqa: E402
from castlab.detector import DetectorParams, detect  # noqa: E402
from castlab.evaluate import evaluate_session  # noqa: E402
from castlab.io import find_sessions  # noqa: E402
from castlab.pipeline import load  # noqa: E402

TUNE = "session_20261025_150936"
COUNT_KNOWN = "session_20261025_113449"  # 実キャスト 2 回（ユーザー申告）


def detect_greedy(sig, params: DetectorParams) -> pd.DataFrame:
    """Phase 2 当初の近接抑制（|a| の大きい順に採用し、既採用から min_interval 未満を捨てる）。"""
    cands = detect(sig, replace(params, min_interval_s=0.0))  # 抑制なしの全候補
    min_gap = params.min_interval_s * sig.fs
    kept = []
    for idx in cands.sort_values("accel_peak", ascending=False, kind="stable").index:
        i = round(cands.loc[idx, "t"] * sig.fs)
        if all(abs(i - round(cands.loc[k, "t"] * sig.fs)) >= min_gap for k in kept):
            kept.append(idx)
    return cands.loc[sorted(kept)].reset_index(drop=True)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    config = load_config()
    fixed = replace(config.matching, reel_bound_max_s=0.0)
    reel = replace(config.matching, reel_bound_max_s=30.0)
    baseline = DetectorParams.load(ANALYSIS / "params" / "baseline_v1.json")
    p1_review = replace(baseline, post_gyro_mean_max=1.5)
    current = DetectorParams.load(ANALYSIS / "params" / "default.json")
    stages = [
        ("A Phase 2 当初", baseline, detect_greedy, fixed),
        ("B 抑制規則を変更", baseline, detect, fixed),
        ("C + 静止フィルタ", p1_review, detect, fixed),
        ("D + 間隔 5 秒（現行）", current, detect, fixed),
        ("E + REEL 上限照合", current, detect, reel),
    ]
    sessions = [p for p in find_sessions([ANALYSIS.parent / "captures" / "sessions"]) if not config.is_ignored(p.stem)]
    rows = []
    for name, params, fn, matching in stages:
        cfg = Config(matching, config.sessions, config.path)
        agg: dict[str, dict] = {}
        n113 = None
        for path in sessions:
            ls = load(path)
            s = ls.session
            sc = cfg.session(s.name, bool((s.markers["type"] == "CAST").any()))
            r = evaluate_session(sc, s.duration_s, s.markers, fn(ls.signals, params), cfg.matching)
            group = "tune" if s.name == TUNE else sc.label
            a = agg.setdefault(group, {"n": 0, "cast": 0, "matched": 0, "det": 0, "unmatched": 0, "hours": 0.0})
            c = r.counts
            a["n"] += 1
            a["cast"] += c["cast_markers"]
            a["matched"] += c["matched"]
            a["det"] += c["detections"]
            a["unmatched"] += c["unmatched_detections"]
            a["hours"] += s.duration_s / 3600
            if s.name == COUNT_KNOWN:
                n113 = (c["detections"], c["matched"])
        t, lk, un, no = (agg.get(k) for k in ("tune", "likely", "uncertain", "none"))
        rows.append({
            "段階": name,
            "tune 一致/30": t["matched"],
            "tune 誤検出": t["det"] - t["matched"],
            "tune P": round(t["matched"] / t["det"], 3),
            "tune R": round(t["matched"] / t["cast"], 3),
            "likely 一致/15": lk["matched"],
            "likely 誤検出候補": lk["unmatched"],
            "uncertain 一致/43": un["matched"],
            "uncertain 未照合": un["unmatched"],
            "none 検出/時": round(no["det"] / no["hours"], 1),
            "11:34 検出（実 2）": n113[0],
            "11:34 一致": n113[1],
        })
    df = pd.DataFrame(rows)
    out = ANALYSIS / "reports" / "2026-10-25_detector_review" / "phase3_before_after.csv"
    df.to_csv(out, index=False)
    with pd.option_context("display.width", 250, "display.max_columns", 30):
        print(df.to_string(index=False))
    print(f"\n→ {out}")


if __name__ == "__main__":
    main()
