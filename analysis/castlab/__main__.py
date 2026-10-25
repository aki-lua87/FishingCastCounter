"""コマンドライン: python -m castlab <command> ...

  plot      波形グラフ（XYZ・合成値・マーカー）
  epochs    マーカー前後の波形切り出し・比較・特徴量 CSV
  detect    キャスト候補の検出（時刻一覧）
  evaluate  検出とマーカーの照合、集計、HTML レポート
  tune      tune セッションでパラメータをグリッド探索し、eval セッションで評価
"""

from __future__ import annotations

import argparse
import itertools
import sys
import tomllib
from pathlib import Path

import pandas as pd

from . import plots
from .config import Config, load_config
from .detector import DetectorParams
from .epochs import epoch_features, marker_epochs
from .evaluate import aggregate, summarize
from .io import MARKER_TYPES, find_sessions
from .pipeline import detections_table, load, run
from .report import write_index, write_session_report

ANALYSIS_DIR = Path(__file__).resolve().parent.parent
DEFAULT_SESSIONS = ANALYSIS_DIR.parent / "captures" / "sessions"
DEFAULT_OUTPUT = ANALYSIS_DIR / "output"
DEFAULT_PARAMS = ANALYSIS_DIR / "params" / "default.json"
DEFAULT_GRID = ANALYSIS_DIR / "params" / "grid.toml"


def _sessions(args) -> list[Path]:
    paths = find_sessions(args.sessions or [DEFAULT_SESSIONS])
    if args.include_ignored:
        return paths
    config = load_config(args.config)
    ignored = [p for p in paths if config.is_ignored(p.stem)]
    if ignored:
        print(f"ignore 指定のため除外: {', '.join(p.stem for p in ignored)}", file=sys.stderr)
    return [p for p in paths if not config.is_ignored(p.stem)]


def _params(args) -> DetectorParams:
    base = DetectorParams.load(args.params) if args.params else (
        DetectorParams.load(DEFAULT_PARAMS) if DEFAULT_PARAMS.exists() else DetectorParams())
    return base.with_overrides(args.set or [])


def _print_df(df: pd.DataFrame) -> None:
    with pd.option_context("display.max_rows", 500, "display.max_columns", 50, "display.width", 200):
        print(df.to_string(index=False, float_format=lambda v: f"{v:.3f}"))


def _add_common(p: argparse.ArgumentParser, detector: bool = False) -> None:
    p.add_argument("sessions", nargs="*", help=f"CSV ファイル / ディレクトリ / glob（既定: {DEFAULT_SESSIONS}）")
    p.add_argument("-o", "--out", type=Path, default=DEFAULT_OUTPUT, help="出力ディレクトリ")
    p.add_argument("--config", type=Path, help="セッション設定 TOML（既定: analysis/sessions.toml）")
    p.add_argument("--include-ignored", action="store_true", help="sessions.toml で ignore = true のセッションも読み込む")
    if detector:
        p.add_argument("--params", type=Path, help="検知パラメータ JSON（既定: analysis/params/default.json）")
        p.add_argument("--set", action="append", metavar="KEY=VALUE", help="パラメータを上書き（複数可）")


# --- plot ---------------------------------------------------------------------------------------

def cmd_plot(args) -> None:
    config = load_config(args.config)
    params = _params(args) if args.detect else None
    for path in _sessions(args):
        ls = load(path)
        s, sig = ls.session, ls.signals
        out = args.out / "plots" / s.name
        det = missed = None
        if params is not None:
            r = run(ls, params, config)
            det, missed = r.detections, r.markers[r.markers["status"] == "missed"]
        if args.start is not None or args.end is not None:
            t0 = args.start if args.start is not None else float(sig.t[0])
            t1 = args.end if args.end is not None else float(sig.t[-1])
            p = plots.plot_window(sig, s.markers, out / f"window_{t0:.0f}_{t1:.0f}.png", t0, t1, detections=det,
                                  missed_markers=missed, params=params, matching=config.matching, title=s.name)
            print(p)
            continue
        print(plots.plot_overview(sig, s.markers, out / "overview.png", det, params, title=s.name))
        pages = plots.plot_session_pages(sig, s.markers, out / "pages", args.page, detections=det,
                                         missed_markers=missed, params=params, matching=config.matching, title=s.name)
        print(f"{out / 'pages'}  ({len(pages)} ページ)")


# --- epochs -------------------------------------------------------------------------------------

def cmd_epochs(args) -> None:
    types = [t.strip().upper() for t in args.types.split(",")]
    labels = {lv.strip() for lv in args.labels.split(",")} if args.labels else None
    config = load_config(args.config)
    search = (-args.search_before, args.search_after)
    combined: dict[str, list] = {t: [] for t in types}
    combined_axes = []
    features = []
    out_root = args.out / "epochs"
    for path in _sessions(args):
        ls = load(path)
        s, sig = ls.session, ls.signals
        scfg = config.session(s.name, has_cast_markers=bool((s.markers["type"] == "CAST").any()))
        if labels is not None and scfg.label not in labels:
            continue
        eps = marker_epochs(sig, s.markers, types, args.pre, args.post, align=args.align, search=search)
        if not any(eps.values()):
            continue
        xlabel = "|a| ピークからの時間 [s]" if args.align else "マーカーからの時間 [s]"
        simple = {t: [(e.rel, e.accel_mag, e.gyro_mag) for e in v] for t, v in eps.items()}
        out = out_root / s.name
        if not args.combined_only:
            plots.plot_epochs(simple, out / f"epochs{'_aligned' if args.align else ''}.png",
                              title=f"{s.name} マーカー前後の波形", xlabel=xlabel)
            if eps.get("CAST"):
                plots.plot_axis_epochs([(e.rel, e.accel, e.gyro) for e in eps["CAST"]],
                                       out / f"cast_axes{'_aligned' if args.align else ''}.png",
                                       title=f"{s.name} CAST 前後（軸別）", xlabel=xlabel)
        for t in types:
            combined[t].extend(simple[t])
        combined_axes.extend((e.rel, e.accel, e.gyro) for e in eps.get("CAST", []))
        f = epoch_features(sig, s.markers[s.markers["type"].isin(types)], search)
        f.insert(0, "session", s.name)
        f.insert(1, "label", scfg.label)
        features.append(f)

    if not features:
        print("対象マーカーがありません", file=sys.stderr)
        return
    suffix = "_aligned" if args.align else ""
    xlabel = "|a| ピークからの時間 [s]" if args.align else "マーカーからの時間 [s]"
    out_root.mkdir(parents=True, exist_ok=True)
    scope = f"label={','.join(sorted(labels))}" if labels else "全セッション"
    print(plots.plot_epochs(combined, out_root / f"all_epochs{suffix}.png", title=f"{scope} マーカー前後の波形",
                            xlabel=xlabel))
    if combined_axes:
        print(plots.plot_axis_epochs(combined_axes, out_root / f"all_cast_axes{suffix}.png",
                                     title=f"{scope} CAST 前後（軸別）", xlabel=xlabel))
    feat = pd.concat(features, ignore_index=True)
    feat.to_csv(out_root / "features.csv", index=False)
    print(out_root / "features.csv")
    cols = ["accel_peak", "accel_peak_offset_s", "gyro_peak", "gyro_peak_offset_s"]
    print(f"\n探索窓 [{search[0]:+g}, {search[1]:+g}] s 内の特徴量（マーカー種別ごとの中央値 / 件数）")
    _print_df(feat.groupby("marker_type")[cols].median().join(feat.groupby("marker_type").size().rename("n"))
              .reset_index())


# --- detect -------------------------------------------------------------------------------------

def cmd_detect(args) -> None:
    config = load_config(args.config)
    params = _params(args)
    tables = []
    for path in _sessions(args):
        ls = load(path)
        r = run(ls, params, config)
        d = detections_table(ls, r)[["session", "t", "timestamp_ns", "accel_peak", "gyro_peak", "nearest_marker"]]
        tables.append(d)
        print(f"\n== {ls.session.name}  候補 {len(d)} 件 / CAST マーカー {(ls.session.markers['type'] == 'CAST').sum()} 件")
        if not args.quiet:
            _print_df(d.drop(columns=["session"]))
    out = args.out / "detect"
    out.mkdir(parents=True, exist_ok=True)
    pd.concat(tables).to_csv(out / "detections.csv", index=False)
    params.save(out / "params.json")
    print(f"\n{out / 'detections.csv'}")


# --- evaluate -----------------------------------------------------------------------------------

def _evaluate_and_report(paths: list[Path], params: DetectorParams, config: Config, out: Path, page_s: float | None,
                         note: str = "", report: bool = True):
    results = []
    for path in paths:
        ls = load(path)
        r = run(ls, params, config)
        results.append((ls, r))
        if report:
            write_session_report(ls, r, params, config, out / ls.session.name, page_s)
    out.mkdir(parents=True, exist_ok=True)
    index = write_index(results, params, config, out, note) if report else None
    return results, index


def _print_results(results) -> None:
    res = [r for _, r in results]
    print("\n■ セッション別")
    _print_df(summarize(res))
    print("\n■ 役割 × ラベル信頼度ごとの集計（信頼度の異なるものは合算しない）")
    _print_df(aggregate(res))
    for ls, r in results:
        missed = r.markers[r.markers["status"] == "missed"]
        bad = r.detections[r.detections["status"].isin(["false_positive", "unconfirmed"])]
        if len(missed) or len(bad):
            print(f"\n- {ls.session.name} [{r.session.label}]")
            for t in missed["marker_t"]:
                print(f"    ✗ 未検出 CAST  {t:8.1f}s")
            for row in bad.itertuples():
                tag = "✗ 誤検出    " if row.status == "false_positive" else "? 誤検出候補"
                print(f"    {tag} {row.t:8.1f}s  |a|={row.accel_peak:.1f} |g|={row.gyro_peak:.1f}  近傍 {row.nearest_marker}")


def cmd_evaluate(args) -> None:
    config = load_config(args.config)
    params = _params(args)
    out = args.out / "evaluate"
    results, index = _evaluate_and_report(_sessions(args), params, config, out, None if args.no_pages else args.page,
                                          report=not args.no_report)
    _print_results(results)
    if index:
        print(f"\nレポート: {index}")


# --- tune ---------------------------------------------------------------------------------------

def _load_grid(path: Path) -> dict[str, list[float]]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    grid = data.get("grid", data)
    for k, v in grid.items():
        if not isinstance(v, list):
            grid[k] = [v]
    DetectorParams.from_dict({k: v[0] for k, v in grid.items()})  # キー名の検証
    return grid


def cmd_tune(args) -> None:
    config = load_config(args.config)
    base = _params(args)
    all_paths = _sessions(args)
    infos = []
    for p in all_paths:
        ls = load(p)
        has_cast = bool((ls.session.markers["type"] == "CAST").any())
        infos.append((p, config.session(ls.session.name, has_cast)))

    if args.tune_sessions:
        tune_names = {Path(n).stem for n in args.tune_sessions}
    else:
        tune_names = {sc.name for _, sc in infos if sc.role == "tune"}
    tune = [(p, sc) for p, sc in infos if sc.name in tune_names]
    evals = [(p, sc) for p, sc in infos if sc.name not in tune_names and sc.role != "tune"]
    if not tune:
        sys.exit("tune セッションがありません（sessions.toml で role = \"tune\" を指定するか --tune-sessions を使う）")
    scored = [(p, sc) for p, sc in tune if sc.label == "complete"]
    if not scored:
        sys.exit("tune セッションに label = \"complete\" のものがありません。"
                 "不完全なラベルでは誤検出を確定できないため、調整の目的関数を計算できません。")
    for _, sc in tune:
        if sc.label != "complete":
            print(f"注意: {sc.name} は label={sc.label} のため目的関数に使いません", file=sys.stderr)

    grid = _load_grid(args.grid)
    keys = list(grid)
    combos = list(itertools.product(*(grid[k] for k in keys)))
    print(f"tune: {[sc.name for _, sc in scored]}  /  eval: {len(evals)} セッション  /  {len(combos)} 通り")

    rows = []
    for combo in combos:
        params = base.with_overrides([f"{k}={v}" for k, v in zip(keys, combo)])
        tp = det = n = 0
        for p, _ in scored:
            c = run(load(p), params, config).counts
            tp += c["matched"]
            det += c["detections"]
            n += c["cast_markers"]
        prec = tp / det if det else 0.0
        rec = tp / n if n else 0.0
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        rows.append({**dict(zip(keys, combo)), "matched": tp, "detections": det, "cast_markers": n,
                     "fp": det - tp, "fn": n - tp, "precision": prec, "recall": rec, "f1": f1})
    ranking = pd.DataFrame(rows).sort_values(["f1", "precision"], ascending=False).reset_index(drop=True)
    out = args.out / "tune"
    out.mkdir(parents=True, exist_ok=True)
    ranking.to_csv(out / "ranking.csv", index=False)
    print("\n■ tune セッションでの上位（調整に使ったデータでの成績）")
    _print_df(ranking.head(args.top))

    best = base.with_overrides([f"{k}={ranking.loc[0, k]}" for k in keys])
    best.save(out / "best_params.json")
    print(f"\n最良パラメータ: {out / 'best_params.json'}")
    _print_df(pd.DataFrame([best.to_dict()]))

    note = (f"パラメータは {', '.join(sorted(tune_names))} で調整（グリッド {len(combos)} 通り）。"
            f"eval セッションは調整に使っていない。")
    paths = [p for p, _ in tune] + [p for p, _ in evals]
    tune_cfg = Config(config.matching,
                      {**config.sessions, **{n: {**config.sessions.get(n, {}), "role": "tune"} for n in tune_names}},
                      config.path)
    results, index = _evaluate_and_report(paths, best, tune_cfg, out / "report", None if args.no_pages else args.page,
                                          note, report=not args.no_report)
    _print_results(results)
    if index:
        print(f"\nレポート: {index}")


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="castlab", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("plot", help="波形グラフを生成")
    _add_common(p, detector=True)
    p.add_argument("--start", type=float, help="切り出し開始 [s]（指定時は 1 枚だけ出力）")
    p.add_argument("--end", type=float, help="切り出し終了 [s]")
    p.add_argument("--page", type=float, default=120.0, help="1 ページの秒数（既定 120）")
    p.add_argument("--detect", action="store_true", help="検出結果も重ねる")
    p.set_defaults(func=cmd_plot)

    p = sub.add_parser("epochs", help="マーカー前後の波形を切り出して比較")
    _add_common(p)
    p.add_argument("--types", default=",".join(MARKER_TYPES), help="対象マーカー種別（カンマ区切り）")
    p.add_argument("--pre", type=float, default=3.0, help="基準時刻より前の秒数")
    p.add_argument("--post", type=float, default=12.0, help="基準時刻より後の秒数")
    p.add_argument("--align", choices=["accel_peak"], help="探索窓内の |a| 最大で揃える（形の比較用）")
    p.add_argument("--search-before", type=float, default=2.0, help="特徴量・整列の探索窓（マーカー前）[s]")
    p.add_argument("--search-after", type=float, default=12.0, help="特徴量・整列の探索窓（マーカー後）[s]")
    p.add_argument("--labels", help="対象のラベル信頼度（カンマ区切り。例: complete,likely）。既定は全て")
    p.add_argument("--combined-only", action="store_true", help="セッション別の図を出さない")
    p.set_defaults(func=cmd_epochs)

    p = sub.add_parser("detect", help="キャスト候補の時刻一覧を出力")
    _add_common(p, detector=True)
    p.add_argument("-q", "--quiet", action="store_true", help="一覧を表示せず件数のみ")
    p.set_defaults(func=cmd_detect)

    for name, func, helptext in (("evaluate", cmd_evaluate, "マーカーとの照合・集計・HTML レポート"),
                                 ("tune", cmd_tune, "グリッド探索（tune）→ 評価（eval）")):
        p = sub.add_parser(name, help=helptext)
        _add_common(p, detector=True)
        p.add_argument("--page", type=float, default=120.0, help="レポートの時系列 1 ページの秒数")
        p.add_argument("--no-pages", action="store_true", help="時系列ページを生成しない（高速）")
        p.add_argument("--no-report", action="store_true", help="HTML レポートを生成しない")
        p.set_defaults(func=func)
        if name == "tune":
            p.add_argument("--grid", type=Path, default=DEFAULT_GRID, help="探索グリッド TOML")
            p.add_argument("--tune-sessions", nargs="+", help="調整に使うセッション名（sessions.toml の role より優先）")
            p.add_argument("--top", type=int, default=15, help="表示する上位件数")

    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
