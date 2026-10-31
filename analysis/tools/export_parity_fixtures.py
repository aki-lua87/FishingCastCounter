"""Python 版と Kotlin 版（Wear OS）の検知器が一致するかを確かめるテストデータを作る。

    cd analysis
    .venv\\Scripts\\python.exe tools\\export_parity_fixtures.py            # app/src/test/resources/parity に小さな切り出し
    .venv\\Scripts\\python.exe tools\\export_parity_fixtures.py --full     # captures/parity に全セッション分（git 管理外）

出力（1 データにつき）:
  <name>.csv.gz          元 CSV の行をそのままの順序で切り出したもの（センサーの到着順を保つ）
  <name>.meta.txt        start_elapsed_realtime_ns（元セッションの t0。グリッド位置を元と揃える）
  <name>.<params>.expected.csv  Python 版の検知結果（grid_index, timestamp_ns, 特徴量。
                         方向条件を使うパラメータ（P2 候補）では gyro_dir_cos, post_dir_cos 列も付く）
  params_<params>.json   使ったパラメータ

Kotlin 側: app/src/test/.../detection/CastDetectorParityTest.kt
"""

from __future__ import annotations

import argparse
import gzip
import json
import shutil
import sys
import tempfile
from pathlib import Path

ANALYSIS = Path(__file__).resolve().parent.parent
ROOT = ANALYSIS.parent
sys.path.insert(0, str(ANALYSIS))

from castlab.config import load_config  # noqa: E402
from castlab.detector import DetectorParams, detect  # noqa: E402
from castlab.io import find_sessions, load_session, read_meta  # noqa: E402
from castlab.signals import resample  # noqa: E402

SESSIONS = ROOT / "captures" / "sessions"

# (名前, セッション, 開始秒, 終了秒)。キャスト・見逃し・誤検出・歩行を含む区間
SEGMENTS = [
    ("casts_150936", "session_20261025_150936", 360.0, 520.0),
    ("mixed_113449", "session_20261025_113449", 0.0, 260.0),
    ("impacts_113449", "session_20261025_113449", 770.0, 930.0),
    ("walk_085344", "session_20261025_085344", 0.0, 180.0),
    # P2 候補の方向条件: 弱いキャスト（通り抜けるもの・向きで落ちるもの）と、作業中の単発動作（P1 は誤検知、P2 は落とす）
    ("weak_202426", "session_20261031_202426", 280.0, 560.0),
    ("hook_200612", "session_20261031_200612", 100.0, 300.0),
]

PARAM_SETS = {
    "p1": ANALYSIS / "params" / "default.json",
    "baseline_v1": ANALYSIS / "params" / "baseline_v1.json",
    "p2_candidate": ANALYSIS / "params" / "p2_candidate.json",
}


def cut_csv(src: Path, dst: Path, t0_ns: int, start_s: float, end_s: float) -> int:
    lo, hi = t0_ns + int(start_s * 1e9), t0_ns + int(end_s * 1e9)
    n = 0
    with src.open(encoding="utf-8") as fin, dst.open("w", encoding="utf-8", newline="\n") as fout:
        fout.write(fin.readline())
        for line in fin:
            ts = int(line.split(",", 1)[0])
            if lo <= ts <= hi:
                fout.write(line)
                n += 1
    return n


def write_expected(csv_path: Path, params: DetectorParams, out: Path) -> int:
    s = load_session(csv_path)
    sig = resample(s)
    d = detect(sig, params)
    grid = [int(round(t * sig.fs)) for t in d["t"]]
    direction = params.uses_direction
    with out.open("w", encoding="utf-8", newline="\n") as f:
        f.write("grid_index,timestamp_ns,accel_peak,gyro_peak,post_gyro_mean")
        f.write(",gyro_dir_cos,post_dir_cos\n" if direction else "\n")
        for j, k in enumerate(grid):
            ts = s.t0_ns + round(k * 1e9 / sig.fs)
            a, g, p = (float(d[c].iloc[j]) for c in ("accel_peak", "gyro_peak", "post_gyro_mean"))
            f.write(f"{k},{ts},{a!r},{g!r},{p!r}")
            if direction:
                f.write(f",{float(d['gyro_dir_cos'].iloc[j])!r},{float(d['post_dir_cos'].iloc[j])!r}")
            f.write("\n")
    return len(d)


def export_segments(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    params = {name: DetectorParams.load(p) for name, p in PARAM_SETS.items()}
    for name, p in params.items():
        p.save(out_dir / f"params_{name}.json")
    with tempfile.TemporaryDirectory() as tmp:
        for name, session, a, b in SEGMENTS:
            src = SESSIONS / f"{session}.csv"
            meta = read_meta(src.with_name(f"{session}.meta.txt"))
            t0 = int(meta["start_elapsed_realtime_ns"])
            plain = Path(tmp) / f"{name}.csv"
            rows = cut_csv(src, plain, t0, a, b)
            meta_text = f"source={session}\nsegment_s={a},{b}\nstart_elapsed_realtime_ns={t0}\n"
            Path(tmp, f"{name}.meta.txt").write_text(meta_text, encoding="utf-8")
            (out_dir / f"{name}.meta.txt").write_text(meta_text, encoding="utf-8")
            # mtime=0: 作り直しても中身が同じならバイト列も同じになるように
            with plain.open("rb") as fin, (out_dir / f"{name}.csv.gz").open("wb") as raw,                     gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=0) as fout:
                shutil.copyfileobj(fin, fout)
            counts = {pn: write_expected(plain, p, out_dir / f"{name}.{pn}.expected.csv") for pn, p in params.items()}
            print(f"{name}: {rows} 行, 検出 {counts}")


def export_full(out_dir: Path) -> None:
    """全セッションの期待値（CSV 本体はコピーしない。Kotlin テストは captures/sessions を直接読む）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    config = load_config()
    params = {name: DetectorParams.load(p) for name, p in PARAM_SETS.items()}
    for name, p in params.items():
        p.save(out_dir / f"params_{name}.json")
    for path in find_sessions([SESSIONS]):
        if config.is_ignored(path.stem):
            continue
        counts = {pn: write_expected(path, p, out_dir / f"{path.stem}.{pn}.expected.csv") for pn, p in params.items()}
        print(f"{path.stem}: 検出 {counts}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="captures/parity に全セッション分の期待値を出す")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if args.full:
        export_full(ROOT / "captures" / "parity")
    else:
        export_segments(ROOT / "app" / "src" / "test" / "resources" / "parity")


if __name__ == "__main__":
    main()
