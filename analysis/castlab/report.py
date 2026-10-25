"""人が目視確認するための HTML レポート（静的ファイル、ブラウザで開く）。

output/<name>/index.html          : 全体サマリー（役割 × ラベル信頼度ごとの集計、セッション一覧）
output/<name>/<session>/index.html: セッション詳細（全体図、照合表、イベント別の拡大図、時系列ページ）
"""

from __future__ import annotations

import html
import json
from pathlib import Path

import numpy as np
import pandas as pd

from . import device, plots
from .config import Config
from .detector import DetectorParams
from .evaluate import SessionResult, aggregate, summarize
from .pipeline import LoadedSession, detections_table

LABEL_DESC = {
    "complete": "全キャストにマーカーあり（精度・再現率を算出）",
    "likely": "マーカー数 ≈ 実キャスト数（再現率は参考値、精度は算出しない）",
    "uncertain": "誤タップ・漏れの可能性あり（件数のみ）",
    "none": "マーカーなし（検出一覧のみ）",
}
STATUS_JA = {
    "matched": "✓ 一致",
    "missed": "✗ 未検出",
    "false_positive": "✗ 誤検出",
    "unconfirmed": "? 誤検出候補",
    "unlabeled": "· ラベルなし",
    "excluded": "除外",
}

CSS = """
:root { --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e; --line:#e4e3df; --good:#0ca30c; --bad:#d03b3b; --warn:#9a6a00; }
@media (prefers-color-scheme: dark) { :root { --surface:#1a1a19; --ink:#f0efec; --ink2:#c3c2b7; --line:#3a3a37; --warn:#fab219; } }
body { background:var(--surface); color:var(--ink); font-family:"Yu Gothic UI","Meiryo",system-ui,sans-serif; margin:16px auto; max-width:1500px; padding:0 16px; line-height:1.5; }
h1 { font-size:1.4rem; } h2 { font-size:1.15rem; margin-top:2rem; border-bottom:1px solid var(--line); }
table { border-collapse:collapse; font-size:0.85rem; font-variant-numeric:tabular-nums; margin:8px 0; }
th, td { border-bottom:1px solid var(--line); padding:3px 10px; text-align:right; white-space:nowrap; }
th { color:var(--ink2); font-weight:600; } td:first-child, th:first-child { text-align:left; }
td.l { text-align:left; }
.matched { color:var(--good); } .missed, .false_positive { color:var(--bad); font-weight:600; } .unconfirmed { color:var(--warn); }
.note { color:var(--ink2); font-size:0.85rem; }
img { max-width:100%; border:1px solid var(--line); background:#fcfcfb; margin:6px 0; }
.wrap { overflow-x:auto; }
code { font-size:0.85rem; }
"""


def _fmt(v) -> str:
    if isinstance(v, float):
        if np.isnan(v):
            return "—"
        return f"{v:.3f}" if abs(v) < 10 else f"{v:.1f}"
    return html.escape(str(v))


def _table(df: pd.DataFrame, status_col: str | None = None, link_col: str | None = None) -> str:
    if df.empty:
        return '<p class="note">（なし）</p>'
    head = "".join(f"<th>{html.escape(str(c))}</th>" for c in df.columns)
    body = []
    for _, row in df.iterrows():
        cells = []
        for c in df.columns:
            v = row[c]
            if c == status_col:
                cells.append(f'<td class="l {html.escape(str(v))}">{STATUS_JA.get(v, html.escape(str(v)))}</td>')
            elif c == link_col:
                cells.append(f'<td class="l"><a href="{html.escape(str(v))}/index.html">{html.escape(str(v))}</a></td>')
            elif isinstance(v, str):
                cells.append(f'<td class="l">{html.escape(v)}</td>')
            else:
                cells.append(f"<td>{_fmt(v)}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return f'<div class="wrap"><table><tr>{head}</tr>{"".join(body)}</table></div>'


def _page(title: str, body: str) -> str:
    return (f'<!doctype html><html lang="ja"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{html.escape(title)}</title><style>{CSS}</style></head><body>{body}</body></html>")


def write_session_report(loaded: LoadedSession, result: SessionResult, params: DetectorParams, config: Config,
                         out_dir: Path, page_s: float | None, gallery_pre: float = 4.0, gallery_post: float = 12.0) -> None:
    s, sig = loaded.session, loaded.signals
    out_dir.mkdir(parents=True, exist_ok=True)
    det = result.detections
    mk = result.markers
    title = f"{s.name}  [label={result.session.label}, role={result.session.role}]"

    det_table = detections_table(loaded, result)
    det_table.to_csv(out_dir / "detections.csv", index=False)
    mk.round(3).to_csv(out_dir / "cast_markers.csv", index=False)

    plots.plot_overview(sig, s.markers, out_dir / "overview.png", det, params, title=title)

    # イベント別の拡大図: CAST マーカー（一致 / 未検出）、未照合の検出
    galleries = []
    if not mk.empty:
        ev = mk[mk["status"] != "excluded"].copy()
        ev["t"] = ev["marker_t"]
        ev["label"] = [
            f"CAST {r.marker_t:.1f}s  " + (f"✓ 検出 {r.offset_s:+.1f}s" if r.status == "matched" else "✗ 未検出")
            for r in ev.itertuples()
        ]
        p = plots.plot_event_gallery(sig, ev, s.markers, out_dir / "gallery_cast_markers.png", gallery_pre,
                                     gallery_post, det, params,
                                     title="CAST マーカー前後（0 = マーカー時刻、薄線 = 他のマーカー）")
        galleries.append(("CAST マーカーごとの波形", p))
    um = det[det["status"].isin(["false_positive", "unconfirmed", "unlabeled"])].copy()
    if not um.empty:
        um["label"] = [f"{STATUS_JA[r.status]} {r.t:.1f}s\n|a|={r.accel_peak:.0f}  近傍マーカー {r.nearest_marker or '—'}"
                       for r in um.itertuples()]
        name = {"complete": "誤検出", "likely": "誤検出候補", "uncertain": "誤検出候補", "none": "検出"}[
            result.session.label]
        p = plots.plot_event_gallery(sig, um[["t", "label"]], s.markers, out_dir / "gallery_unmatched.png", 6.0, 6.0,
                                     det, params, title=f"マーカーと対応しない{name}（0 = 検出時刻）")
        galleries.append((f"マーカーと対応しない{name}", p))

    pages = []
    if page_s:
        pages = plots.plot_session_pages(sig, s.markers, out_dir / "pages", page_s, detections=det,
                                         missed_markers=mk[mk["status"] == "missed"], params=params,
                                         matching=config.matching, title=s.name)

    summary = summarize([result]).drop(columns=["session"])
    marker_counts = s.markers["type"].value_counts().reindex(["CAST", "REEL", "WALK", "OTHER"], fill_value=0)
    body = [
        f'<p><a href="../index.html">← 一覧へ</a></p><h1>{html.escape(s.name)}</h1>',
        f'<p>ラベル信頼度 <b>{result.session.label}</b>: {LABEL_DESC[result.session.label]} / 役割 <b>{result.session.role}</b>'
        f'{"（設定ファイルに記載なし → 既定値）" if not result.session.explicit else ""}</p>',
        f'<p class="note">{html.escape(result.session.note)}</p>' if result.session.note else "",
        f'<p class="note">開始 {html.escape(s.meta.get("start_wall_clock", "?"))} / '
        f'マーカー: {", ".join(f"{k} {v}" for k, v in marker_counts.items())}</p>',
        _table(summary),
        '<h2>全体図</h2><img src="overview.png">',
    ]
    for heading, p in galleries:
        body.append(f'<h2>{html.escape(heading)}</h2><img src="{p.name}">')
    if s.events is not None:
        body.append(_device_section(loaded, out_dir))
    body.append("<h2>CAST マーカーの照合</h2>")
    body.append(_table(mk.round(2), status_col="status"))
    body.append("<h2>検出一覧</h2>")
    body.append('<p class="note">nearest_marker: 最も近いマーカーの種類と、マーカーから見た検出の時間差</p>')
    body.append(_table(det.drop(columns=[]).round(2), status_col="status"))
    if pages:
        body.append(f"<h2>時系列（{page_s:g} 秒ごと）</h2>")
        body.extend(f'<img src="pages/{p.name}" loading="lazy">' for p in pages)
    (out_dir / "index.html").write_text(_page(s.name, "".join(body)), encoding="utf-8")


def write_index(results: list[tuple[LoadedSession, SessionResult]], params: DetectorParams, config: Config,
                out_dir: Path, extra_note: str = "") -> Path:
    res = [r for _, r in results]
    summary = summarize(res)
    agg = aggregate(res)
    summary.to_csv(out_dir / "summary.csv", index=False)
    agg.to_csv(out_dir / "aggregate.csv", index=False)
    pd.concat([detections_table(l, r) for l, r in results]).to_csv(out_dir / "detections_all.csv", index=False)
    (out_dir / "params.json").write_text(json.dumps(params.to_dict(), indent=2) + "\n", encoding="utf-8")

    body = [
        "<h1>キャスト検知 評価レポート</h1>",
        f'<p class="note">{html.escape(extra_note)}</p>' if extra_note else "",
        "<h2>集計（役割 × ラベル信頼度。信頼度の異なるセッションは合算しない）</h2>",
        '<p class="note">precision / recall / f1 は complete のみ。recall_ref は likely の参考値。'
        "uncertain / none は件数のみ。tune セッションの数値は調整に使ったデータでの成績（汎化性能ではない）。</p>",
        _table(agg),
        "<h2>セッション一覧</h2>",
        _table(summary, link_col="session"),
        "<h2>検知パラメータ</h2>",
        f"<pre>{html.escape(json.dumps(params.to_dict(), indent=2))}</pre>",
        "<h2>照合設定</h2>",
        f"<pre>{html.escape(json.dumps(config.matching.__dict__, indent=2))}</pre>",
        "<h2>ラベル信頼度</h2><ul>" + "".join(f"<li><b>{k}</b>: {v}</li>" for k, v in LABEL_DESC.items()) + "</ul>",
    ]
    path = out_dir / "index.html"
    path.write_text(_page("キャスト検知 評価レポート", "".join(body)), encoding="utf-8")
    return path


def _device_section(loaded: LoadedSession, out_dir: Path) -> str:
    """ウォッチ上のイベント（AUTO_CAST / MISS / FALSE / UNDO）。AUTO_CAST は正解ラベルではない。"""
    s, sig = loaded.session, loaded.signals
    c = device.counts(s)
    auto = device.auto_cast_table(s)
    misses = device.miss_windows(s)
    pc, params = device.compare_with_pc(s, sig)
    parts = [
        "<h2>ウォッチ上の自動検知と申告</h2>",
        '<p class="note">AUTO_CAST はウォッチ上の検知器の出力で、正解ラベルとしては使わない（評価は手動マーカーのみ）。'
        "MISS / FALSE はユーザーの申告。UNDO で取り消されたものは除いて数える。</p>",
        _table(pd.DataFrame([{"自動検知": c["auto_cast"], "MISS": c["miss"], "FALSE": c["false"], "UNDO": c["undo"],
                              "補正後": c["corrected"],
                              "meta の補正後": s.meta.get("corrected_cast_count", "—")}])),
    ]
    if params is not None and len(pc):
        n = pc["source"].value_counts()
        parts.append(f'<p>PC で同じパラメータ（meta の detector_*）で再検知: 一致 {n.get("both", 0)} / '
                     f'ウォッチのみ {n.get("watch_only", 0)} / PC のみ {n.get("pc_only", 0)}'
                     "（実装が一致していれば不一致は 0。記録停止直前の判定は窓が足りず差が出ることがある）</p>")
    plots.plot_device_overview(sig, s.markers, auto, misses, pc, out_dir / "device_overview.png",
                               title=f"{s.name} ウォッチのイベント")
    parts.append('<img src="device_overview.png">')
    flagged = auto[auto["false_event"].notna()] if len(auto) else auto
    if len(flagged):
        ev = pd.DataFrame({"t": flagged["t"], "label": [
            f"FALSE 申告: AUTO #{r.event_id} {r.t:.1f}s\n|a|={r.accel_peak:.0f} post|g|={r.post_gyro_mean:.2f}"
            for r in flagged.itertuples()]})
        p = plots.plot_event_gallery(sig, ev, s.markers, out_dir / "gallery_false.png", 6.0, 6.0,
                                     title="FALSE 申告された AUTO_CAST（0 = ピーク時刻）")
        parts.append(f'<h3>FALSE 申告された自動検知</h3><img src="{p.name}">')
    if len(misses):
        ev = pd.DataFrame({"t": misses["pressed_t"], "label": [
            f"MISS #{r.event_id} 押下 {r.pressed_t:.1f}s" + (" 判定中あり" if r.detector_pending else "")
            for r in misses.itertuples()]})
        p = plots.plot_event_gallery(sig, ev, s.markers, out_dir / "gallery_miss.png", 30.0, 3.0,
                                     title="MISS 押下前 30 秒（0 = 押した時刻。この中に検知されなかったキャストがあるはず）", cols=3)
        parts.append(f'<h3>MISS（未検知の申告）</h3><img src="{p.name}">')
        parts.append(_table(misses))
    parts.append("<h3>AUTO_CAST 一覧</h3>")
    parts.append(_table(auto))
    if len(pc):
        parts.append("<h3>PC 再検知との突き合わせ</h3>")
        parts.append(_table(pc[pc["source"] != "both"]) if (pc["source"] != "both").any() else "<p>全件一致</p>")
    ev_all = s.events.copy()
    ev_all["detail"] = [";".join(f"{k}={v}" for k, v in d.items()) for d in ev_all["detail"]]
    parts.append("<h3>イベント（events.csv、取り消し済みを含む）</h3>")
    parts.append(_table(ev_all[["event_id", "type", "t", "recorded_t", "ref_id", "active", "detail"]].round(2)))
    return "".join(parts)
