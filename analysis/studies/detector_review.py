"""検知アルゴリズムの実用性レビュー（2026-10-25 データ）。

    cd analysis
    .venv\\Scripts\\python.exe studies\\detector_review.py [--out reports/2026-10-25_detector_review]

項目:
  1. CAST マーカー → 検出の時間差分布と照合窓（−2〜+12 秒）の妥当性
  2. 見逃したキャストの波形・特徴
  3. REEL マーカー後（リトリーブ中）の誤検知
  4. OTHER / WALK マーカー周辺の動作
  5. 11:34 セッション（実キャスト 2 回とユーザー申告）の重点確認
  6. 調整用（tune）と評価用（eval）を分離した成績
  7. 検知間隔（min_interval_s）が短い間隔の連続キャストに与える影響

ラベルの扱い:
- complete（15:09）以外の未照合検出は誤検出と確定しない
- マーカーなし区間・ラベルなしセッションを非キャストとみなさない
- 11:34 は「実キャストは 2 回」というユーザー申告があるため「件数既知」として、
  検出数が 2 を超えた分（下限）だけを誤検出の目安にする（どれが誤りかは確定しない）
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from castlab import plots  # noqa: E402
from castlab.config import Config, MatchingConfig, load_config  # noqa: E402
from castlab.detector import DetectorParams, detect  # noqa: E402
from castlab.evaluate import evaluate_session, match_one_to_one  # noqa: E402
from castlab.io import find_sessions  # noqa: E402
from castlab.pipeline import LoadedSession, load  # noqa: E402
from castlab.signals import Signals  # noqa: E402

plt = plots.plt

ANALYSIS = Path(__file__).resolve().parent.parent
TUNE = "session_20261025_150936"
COUNT_KNOWN = {"session_20261025_113449": 2}  # ユーザー申告の実キャスト数

BASELINE = DetectorParams.load(ANALYSIS / "params" / "baseline_v1.json")  # Phase 2 当初の検知器
# P1: tune セッションだけで選んだもの（F1 が下がらない範囲で最も厳しい静止フィルタ）。レビュー時点は間隔 8 秒
P1 = replace(BASELINE, post_gyro_mean_max=1.5)
# P2: 参考。|a| 閾値を下げて静止フィルタを強めた案（eval では選んでいない）
P2 = replace(BASELINE, accel_peak_min=20.0, post_gyro_mean_max=1.0)
VARIANTS = {"baseline": BASELINE, "P1": P1, "P2": P2}

# 特徴量を見るための緩い候補抽出（静止フィルタなし、近接 3 秒で間引き）
POOL = DetectorParams(accel_peak_min=15.0, gyro_peak_min=0.0, min_interval_s=3.0)

C_CAST, C_OTHER, C_THIRD = "#2a78d6", "#eb6834", "#1baf7a"


# ------------------------------------------------------------------------------------------------
# 共通

class Ctx:
    def __init__(self, sessions_dir: Path, out: Path):
        self.config = load_config()
        self.out = out
        self.fig = out / "figures"
        self.fig.mkdir(parents=True, exist_ok=True)
        self.sessions: dict[str, LoadedSession] = {}
        self.label: dict[str, str] = {}
        for p in find_sessions([sessions_dir]):
            if self.config.is_ignored(p.stem):
                continue
            ls = load(p)
            sc = self.config.session(ls.session.name, bool((ls.session.markers["type"] == "CAST").any()))
            self.sessions[ls.session.name] = ls
            self.label[ls.session.name] = sc.label
        self.md: list[str] = []

    def by_label(self, *labels: str) -> list[str]:
        return [n for n in self.sessions if self.label[n] in labels]

    def h(self, text: str) -> None:
        self.md.append(f"\n## {text}\n")
        print(f"\n=== {text}")

    def p(self, text: str = "") -> None:
        self.md.append(text)
        print(text)

    def table(self, df: pd.DataFrame, floatfmt: str = ".2f") -> None:
        if df.empty:
            self.p("（なし）")
            return
        cols = list(df.columns)
        lines = ["| " + " | ".join(map(str, cols)) + " |", "|" + "---|" * len(cols)]
        for _, r in df.iterrows():
            cells = []
            for c in cols:
                v = r[c]
                if isinstance(v, (float, np.floating)):
                    cells.append("—" if np.isnan(v) else format(v, floatfmt))
                else:
                    cells.append(str(v))
            lines.append("| " + " | ".join(cells) + " |")
        self.p("\n".join(lines) + "\n")

    def save(self, fig, name: str) -> str:
        fig.savefig(self.fig / name, dpi=90, bbox_inches="tight")
        plt.close(fig)
        self.p(f"![{name}](figures/{name})\n")
        return name


def short(name: str) -> str:
    s = name.removeprefix("session_2026")
    return f"{s[:2]}/{s[2:4]} {s[5:7]}:{s[7:9]}"


def gwin(sig: Signals, arr: np.ndarray, t: float, a: float, b: float) -> np.ndarray:
    m = (sig.t >= t + a) & (sig.t <= t + b)
    return np.nan_to_num(arr[m])


def markers_of(ls: LoadedSession, typ: str) -> np.ndarray:
    return ls.session.marker_times(typ)


def next_after(arr: np.ndarray, t: float) -> float:
    later = arr[arr > t]
    return float(later[0]) if len(later) else np.inf


def run_variant(ctx: Ctx, name: str, params: DetectorParams, matching: MatchingConfig | None = None):
    cfg = Config(matching or ctx.config.matching, ctx.config.sessions, ctx.config.path)
    out = {}
    for n, ls in ctx.sessions.items():
        s = ls.session
        sc = cfg.session(n, bool((s.markers["type"] == "CAST").any()))
        det = detect(ls.signals, params)
        out[n] = evaluate_session(sc, s.duration_s, s.markers, det, cfg.matching)
    return out


# ------------------------------------------------------------------------------------------------
# 1. 時間差分布

def item1_offsets(ctx: Ctx) -> None:
    ctx.h("1. CAST マーカーと検知時刻の時間差分布（照合窓 −2〜+12 秒の妥当性）")
    wide = MatchingConfig(window_before_s=10.0, window_after_s=30.0, expected_offset_s=5.0)
    rows = []
    for n in ctx.by_label("complete", "likely", "uncertain"):
        ls = ctx.sessions[n]
        casts = markers_of(ls, "CAST")
        reels = markers_of(ls, "REEL")
        det = detect(ls.signals, P1)
        # 広い窓でも隣の CAST をまたがないよう、窓の終わりを次の CAST − 2 秒で打ち切る
        after = np.array([min(wide.window_after_s, next_after(casts, t) - t - 2.0) for t in casts])
        for i, j in match_one_to_one(casts, det["t"].to_numpy(), wide, after):
            t, d = casts[i], det["t"].iloc[j]
            nr = next_after(reels, t)
            nc = next_after(casts, t)
            rows.append(dict(session=short(n), label=ctx.label[n], marker_t=round(t, 1), offset=d - t,
                             to_reel=(nr - d) if nr < nc else np.nan, accel=det["accel_peak"].iloc[j]))
    df = pd.DataFrame(rows)
    df.to_csv(ctx.out / "item1_offsets.csv", index=False)

    ctx.p("P1 検知器（静止フィルタあり）の検出を、広い窓（−10〜+30 秒、ただし次の CAST の 2 秒前まで）で 1 対 1 照合したときの "
          "「検出時刻 − CAST マーカー時刻」。広い窓は分布を見るためだけに使い、評価には使っていない。\n")
    summ = df.groupby("label")["offset"].describe(percentiles=[0.05, 0.5, 0.95])[
        ["count", "min", "5%", "50%", "95%", "max"]].reset_index()
    out_of = df.assign(out=(df.offset < -2) | (df.offset > 12)).groupby("label")["out"].sum().rename("窓外(−2〜+12)")
    ctx.table(summ.merge(out_of, on="label"))

    reel = df.dropna(subset=["to_reel"])
    if len(reel):
        ctx.p(f"直後の REEL マーカーまでの時間（検出 → REEL, n={len(reel)}）: 中央値 {reel.to_reel.median():.1f} 秒、"
              f"範囲 {reel.to_reel.min():.1f}〜{reel.to_reel.max():.1f} 秒\n")
    outside = df[(df.offset < -2) | (df.offset > 12)]
    ctx.p("固定窓（−2〜+12 秒）の外にあった対応:\n")
    ctx.table(outside[["session", "label", "marker_t", "offset", "to_reel", "accel"]].round(2))

    # CAST 間隔（窓をどこまで広げられるか）
    gaps = []
    for n in ctx.by_label("complete", "likely"):
        c = markers_of(ctx.sessions[n], "CAST")
        gaps.extend(np.diff(c))
    ctx.p(f"complete / likely の CAST マーカー間隔: 最小 {min(gaps):.1f} 秒、中央値 {np.median(gaps):.1f} 秒\n")

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 3.8), gridspec_kw=dict(width_ratios=[3, 2], wspace=0.25))
    order = ["complete", "likely", "uncertain"]
    for k, lab in enumerate(order):
        v = df[df.label == lab].offset.to_numpy()
        jitter = (np.random.default_rng(k).random(len(v)) - 0.5) * 0.35
        ax1.scatter(v, np.full(len(v), k) + jitter, s=28, color=[C_CAST, C_THIRD, C_OTHER][k],
                    edgecolors=plots.SURFACE, linewidths=0.8, zorder=3)
    ax1.axvspan(-2, 12, color="#cde2fb", alpha=0.5, lw=0, zorder=0)
    ax1.text(5, 2.55, "現在の照合窓 −2〜+12 s", ha="center", fontsize=8, color=plots.INK_2)
    ax1.set_yticks(range(3), [f"{lab} (n={int((df.label == lab).sum())})" for lab in order])
    ax1.set_ylim(-0.6, 2.8)
    ax1.set_xlabel("検出時刻 − CAST マーカー時刻 [s]")
    ax1.set_title("マーカー → 検出の時間差（P1 検知器、広い窓で照合）", loc="left")
    ax2.hist(reel.to_reel, bins=np.arange(0, 26, 1), color=C_CAST, edgecolor=plots.SURFACE)
    ax2.set_xlabel("検出 → 直後の REEL マーカー [s]")
    ax2.set_ylabel("件数")
    ax2.set_title("検出から REEL マーカーまで", loc="left")
    ctx.save(fig, "item1_offsets.png")

    # REEL を上限にした照合の効果
    rows = []
    for mname, m in (("固定窓 −2〜+12", ctx.config.matching),
                     ("REEL で上限（最大 30 s）", replace(ctx.config.matching, reel_bound_max_s=30.0))):
        res = run_variant(ctx, "P1", P1, m)
        for lab in ("complete", "likely", "uncertain"):
            rs = [res[n] for n in ctx.by_label(lab)]
            c = {k: sum(r.counts[k] for r in rs) for k in rs[0].counts}
            rows.append({"照合": mname, "label": lab, "CAST": c["cast_markers"], "一致": c["matched"],
                         "未検出": c["missed"], "未照合の検出": c["unmatched_detections"]})
    ctx.p("REEL マーカーを窓の上限に使う照合（`reel_bound_max_s`）との比較（P1 検知器）:\n")
    ctx.table(pd.DataFrame(rows))


# ------------------------------------------------------------------------------------------------
# 2. 見逃し

def candidate_pool(ctx: Ctx, names: list[str]) -> pd.DataFrame:
    rows = []
    for n in names:
        ls = ctx.sessions[n]
        d = detect(ls.signals, POOL)
        d["session"] = n
        d["label"] = ctx.label[n]
        rows.append(d)
    return pd.concat(rows, ignore_index=True)


def label_pool_by_cast(ctx: Ctx, pool: pd.DataFrame) -> pd.DataFrame:
    """各 CAST マーカーの照合窓（−2〜+12）内で |a| 最大の候補を「キャスト」とする（complete / likely のみ）。"""
    pool = pool.copy()
    pool["is_cast"] = False
    m = ctx.config.matching
    for n, g in pool.groupby("session"):
        for t in markers_of(ctx.sessions[n], "CAST"):
            w = g[(g.t >= t - m.window_before_s) & (g.t <= t + m.window_after_s)]
            if len(w):
                pool.loc[w.accel_peak.idxmax(), "is_cast"] = True
    return pool


def item2_missed(ctx: Ctx) -> None:
    ctx.h("2. 見逃したキャストの特徴")
    base = run_variant(ctx, "baseline", BASELINE)
    missed = []
    for n in ctx.by_label("complete", "likely"):
        r = base[n]
        for t in r.markers.loc[r.markers.status == "missed", "marker_t"]:
            missed.append((n, t))

    pool = label_pool_by_cast(ctx, candidate_pool(ctx, ctx.by_label("complete", "likely")))
    pool.to_csv(ctx.out / "item2_candidate_pool.csv", index=False)
    casts = pool[pool.is_cast]
    rows = []
    for n, t in missed:
        c = casts[(casts.session == n) & (casts.t >= t - 2) & (casts.t <= t + 12)]
        if len(c):
            r = c.iloc[0]
            rows.append(dict(session=short(n), label=ctx.label[n], marker_t=round(t, 1), peak_t=round(r.t, 1),
                             offset=r.t - t, accel_peak=r.accel_peak, gyro_peak=r.gyro_peak,
                             post_gyro_mean=r.post_gyro_mean))
    n_markers = sum(len(markers_of(ctx.sessions[n], "CAST")) for n in ctx.by_label("complete", "likely"))
    ctx.p(f"baseline（|a|≥25）で complete / likely の CAST マーカー {n_markers} 本中 {len(missed)} 本を見逃し。"
          "照合窓内で |a| が最大の候補の特徴:\n")
    ctx.table(pd.DataFrame(rows))
    ctx.p("一致したキャスト（complete / likely、見逃し分を除く）の分布:\n")
    is_missed = np.zeros(len(casts), dtype=bool)
    for n, t in missed:
        is_missed |= ((casts.session == n) & (casts.t >= t - 2) & (casts.t <= t + 12)).to_numpy()
    hit = casts[~is_missed]
    ctx.table(hit[["accel_peak", "gyro_peak", "post_gyro_mean"]].describe(percentiles=[0.05, 0.5, 0.95])
              .loc[["count", "min", "5%", "50%", "95%", "max"]].reset_index().rename(columns={"index": "stat"}))

    # 散布図: |a| ピーク vs ピーク後の |g| 平均
    fig, axes = plt.subplots(1, 2, figsize=(13, 4.6), sharey=True, gridspec_kw=dict(wspace=0.06))
    for ax, lab in zip(axes, ("complete", "likely")):
        g = pool[pool.label == lab]
        nc = g[~g.is_cast]
        cc = g[g.is_cast]
        ax.scatter(nc.post_gyro_mean, nc.accel_peak, s=16, color=C_OTHER, alpha=0.55, label="照合窓外 / 窓内の 2 番手以下",
                   edgecolors="none")
        ax.scatter(cc.post_gyro_mean, cc.accel_peak, s=36, color=C_CAST, label="CAST 照合窓内の最大ピーク",
                   edgecolors=plots.SURFACE, linewidths=0.8)
        for n, t in missed:
            if ctx.label[n] != lab:
                continue
            c = cc[(cc.session == n) & (cc.t >= t - 2) & (cc.t <= t + 12)]
            for r in c.itertuples():
                ax.annotate(f"見逃し {short(n)} {t:.0f}s", (r.post_gyro_mean, r.accel_peak), xytext=(8, -12),
                            textcoords="offset points", fontsize=8, color=plots.INK)
        ax.axhline(25, color=plots.INK_MUTED, ls="--", lw=0.8)
        ax.axvline(1.5, color=plots.INK_MUTED, ls=":", lw=0.8)
        ax.text(1.55, 44, "P1 静止閾値 1.5", fontsize=8, color=plots.INK_2)
        ax.text(4.3, 25.4, "|a| 閾値 25", fontsize=8, color=plots.INK_2, ha="right")
        ax.set_xlabel("ピーク後 0.5〜3.0 s の |g| 平均 [rad/s]")
        ax.set_title(f"{lab}（{'tune: 15:09' if lab == 'complete' else 'eval: 09:50 / 10:44 / 11:15'}）", loc="left")
        ax.set_xlim(0, 4.5)
    axes[0].set_ylabel("|a| ピーク [m/s²]")
    axes[0].legend(loc="upper right")
    ctx.save(fig, "item2_feature_scatter.png")
    ctx.p("「照合窓外」の点は complete では非キャスト確定、likely では非キャストの可能性が高いもの。\n")

    # 見逃し事例の波形（0 = 照合窓内の |a| 最大）
    ev = pd.DataFrame([dict(t=r["peak_t"], label=f"見逃し {r['session']} CAST {r['marker_t']}s\n|a|={r['accel_peak']:.1f} "
                                                   f"post|g|={r['post_gyro_mean']:.2f}") for r in rows])
    # 一致したキャストから比較用に 3 件（|a| が中央値付近）
    ref = hit[hit.label == "complete"].iloc[(hit[hit.label == "complete"].accel_peak - hit.accel_peak.median()).abs()
                                             .argsort()[:2]]
    ev = pd.concat([ev, pd.DataFrame([dict(t=r.t, label=f"参考: 一致 {short(r.session)} {r.t:.0f}s\n|a|={r.accel_peak:.1f} "
                                                        f"post|g|={r.post_gyro_mean:.2f}") for r in ref.itertuples()])])
    # 見逃し・参考は複数セッションにまたがるのでセッションごとに描画して横に並べる代わりに 1 セッションずつ出す
    for n in sorted({m[0] for m in missed} | set(ref.session)):
        ls = ctx.sessions[n]
        sub = ev[ev.label.str.contains(short(n))]
        if sub.empty:
            continue
        name = f"item2_missed_{n[-6:]}.png"
        plots.plot_event_gallery(ls.signals, sub, ls.session.markers, ctx.fig / name, 6.0, 8.0,
                                 detections=None, params=BASELINE,
                                 title=f"{short(n)} 見逃し事例（0 = CAST 照合窓内の |a| 最大）", cols=4)
        ctx.p(f"![{name}](figures/{name})\n")


# ------------------------------------------------------------------------------------------------
# 3. REEL 後のリトリーブ

def item3_reel(ctx: Ctx) -> None:
    ctx.h("3. REEL マーカー後（リトリーブ中）の誤検知")
    ctx.p("区間: REEL マーカー +0.5 秒 〜 次の CAST マーカー −2 秒（次が無ければ +60 秒）。"
          "この区間に CAST は無い想定だが、likely / uncertain ではマーカー漏れの可能性があるため「誤検知候補」として数える。\n")
    rows, peaks = [], []
    variants = {k: {n: detect(ls.signals, p) for n, ls in ctx.sessions.items()} for k, p in VARIANTS.items()}
    for n in ctx.by_label("likely", "uncertain"):
        ls = ctx.sessions[n]
        reels = markers_of(ls, "REEL")
        casts = markers_of(ls, "CAST")
        am = ls.signals.accel_mag
        for r in reels:
            a, b = r + 0.5, min(next_after(casts, r) - 2.0, r + 60.0)
            if b <= a:
                continue
            m = (ls.signals.t >= a) & (ls.signals.t <= b)
            row = dict(session=short(n), label=ctx.label[n], reel_t=round(r, 1), len_s=round(b - a, 1),
                       max_accel=float(np.nanmax(am[m])))
            for k, d in variants.items():
                row[k] = int(((d[n].t >= a) & (d[n].t <= b)).sum())
            rows.append(row)
            pool = detect(ls.signals, POOL)
            pp = pool[(pool.t >= a) & (pool.t <= b)]
            for p in pp.itertuples():
                peaks.append(dict(session=n, t=p.t, accel_peak=p.accel_peak, post=p.post_gyro_mean, label=ctx.label[n]))
    df = pd.DataFrame(rows)
    df.to_csv(ctx.out / "item3_reel_phases.csv", index=False)
    agg = df.groupby("label").agg(区間数=("reel_t", "size"), 合計秒=("len_s", "sum"),
                                  baseline=("baseline", "sum"), P1=("P1", "sum"), P2=("P2", "sum"),
                                  最大a_中央値=("max_accel", "median")).reset_index()
    ctx.table(agg)
    ctx.p("baseline で検出があった区間:\n")
    ctx.table(df[df.baseline > 0])

    pk = pd.DataFrame(peaks)
    fig, ax = plt.subplots(figsize=(8, 4.4))
    for lab, col in (("likely", C_THIRD), ("uncertain", C_OTHER)):
        g = pk[pk.label == lab]
        ax.scatter(g.post, g.accel_peak, s=18, color=col, alpha=0.7, label=f"{lab}（n={len(g)}）", edgecolors="none")
    ax.axhline(25, color=plots.INK_MUTED, ls="--", lw=0.8)
    ax.axvline(1.5, color=plots.INK_MUTED, ls=":", lw=0.8)
    ax.set_xlabel("ピーク後 0.5〜3.0 s の |g| 平均 [rad/s]")
    ax.set_ylabel("|a| ピーク [m/s²]")
    ax.set_title("リトリーブ区間（REEL 後）の |a| ピーク", loc="left")
    ax.legend(loc="upper right")
    ax.set_xlim(0, 4.5)
    ctx.save(fig, "item3_reel_peaks.png")

    # baseline で検出された区間の波形
    ev = []
    for n in ctx.by_label("likely", "uncertain"):
        d = variants["baseline"][n]
        for r in df[(df.session == short(n)) & (df.baseline > 0)].itertuples():
            a, b = r.reel_t + 0.5, r.reel_t + 0.5 + r.len_s
            for t in d[(d.t >= a) & (d.t <= b)].t:
                ev.append((n, t, r.reel_t))
    for n in sorted({e[0] for e in ev}):
        ls = ctx.sessions[n]
        sub = pd.DataFrame([dict(t=t, label=f"REEL {rt:.0f}s → {t:.1f}s (+{t - rt:.0f}s)") for nn, t, rt in ev if nn == n])
        name = f"item3_reel_{n[-6:]}.png"
        plots.plot_event_gallery(ls.signals, sub, ls.session.markers, ctx.fig / name, 6.0, 6.0,
                                 detections=variants["P1"][n], params=BASELINE,
                                 title=f"{short(n)} REEL 後の baseline 検出（0 = 検出時刻、◆=P1 でも残る検出）", cols=5)
        ctx.p(f"![{name}](figures/{name})\n")


# ------------------------------------------------------------------------------------------------
# 4. OTHER / WALK

def item4_other(ctx: Ctx) -> None:
    ctx.h("4. OTHER / WALK マーカー後の動作")
    variants = {k: {n: detect(ls.signals, p) for n, ls in ctx.sessions.items()} for k, p in VARIANTS.items()}
    rows = []
    for n, ls in ctx.sessions.items():
        for typ in ("OTHER", "WALK"):
            for t in markers_of(ls, typ):
                a, b = t - 2.0, t + 30.0
                m = (ls.signals.t >= a) & (ls.signals.t <= b)
                row = dict(session=short(n), label=ctx.label[n], type=typ, t=round(t, 1),
                           max_accel=float(np.nanmax(ls.signals.accel_mag[m])) if m.any() else np.nan)
                for k, d in variants.items():
                    row[k] = int(((d[n].t >= a) & (d[n].t <= b)).sum())
                row["near_cast"] = bool(np.any(np.abs(markers_of(ls, "CAST") - t) <= 15))
                rows.append(row)
    df = pd.DataFrame(rows)
    df.to_csv(ctx.out / "item4_other_walk.csv", index=False)
    ctx.p("各マーカーの −2〜+30 秒にある検出数（near_cast: ±15 秒以内に CAST マーカーあり）:\n")
    ctx.table(df)

    # 歩行セッション 08:53（WALK 2 回 + OTHER 1 回、CAST なし → label none）
    n = "session_20261025_085344"
    if n in ctx.sessions:
        ls = ctx.sessions[n]
        pool = detect(ls.signals, POOL)
        ctx.p(f"08:53（WALK マーカー付き・CAST なし、{ls.session.duration_s / 60:.1f} 分）の検出数: " + ", ".join(
            f"{k} {len(variants[k][n])} 件" for k in VARIANTS) +
              f"。|a|≥20 の候補 {int((pool.accel_peak >= 20).sum())} 件のうち、ピーク後 |g| 平均 ≤1.5 は "
              f"{int(((pool.accel_peak >= 20) & (pool.post_gyro_mean <= 1.5)).sum())} 件。"
              "このセッションは label=none なので誤検出とは確定しないが、歩行中の |a| ピークは 4 秒前後の周期で続き、"
              "ピーク後も |g| が 3〜4 rad/s のまま（腕振り）。\n")
        d = variants["baseline"][n]
        sub = pd.DataFrame([dict(t=t, label=f"baseline 検出 {t:.1f}s\npost|g|={p:.2f}") for t, p in
                            zip(d.t, d.post_gyro_mean)])
        name = "item4_walk_085344.png"
        plots.plot_event_gallery(ls.signals, sub, ls.session.markers, ctx.fig / name, 6.0, 6.0,
                                 detections=variants["P1"][n], params=BASELINE,
                                 title="08:53（歩行中心）の baseline 検出（0 = 検出時刻）", cols=6)
        ctx.p(f"![{name}](figures/{name})\n")

    ev = df[df.type == "OTHER"]
    for n in sorted(set(ev.session)):
        full = [k for k in ctx.sessions if short(k) == n][0]
        ls = ctx.sessions[full]
        sub = pd.DataFrame([dict(t=t, label=f"OTHER {t:.0f}s") for t in ev[ev.session == n].t])
        name = f"item4_other_{full[-6:]}.png"
        plots.plot_event_gallery(ls.signals, sub, ls.session.markers, ctx.fig / name, 3.0, 30.0,
                                 detections=variants["baseline"][full], params=BASELINE,
                                 title=f"{n} OTHER マーカー後 30 秒（0 = マーカー、▼◆ = baseline 検出）", cols=3)
        ctx.p(f"![{name}](figures/{name})\n")


# ------------------------------------------------------------------------------------------------
# 5. 11:34

def item5_113449(ctx: Ctx) -> None:
    n = "session_20261025_113449"
    ctx.h("5. 11:34 セッション（実キャスト 2 回）の重点確認")
    ls = ctx.sessions[n]
    sig = ls.signals
    mk = ls.session.markers
    ctx.p("マーカー: " + ", ".join(f"{r.type} {r.t:.1f}s" for r in mk.itertuples()) +
          f"（セッション {ls.session.duration_s / 60:.1f} 分）\n")
    det = {k: detect(sig, p) for k, p in VARIANTS.items()}
    b = det["baseline"].copy()
    b["P1 で残る"] = b.t.isin(det["P1"].t)
    b["P2 で残る"] = b.t.isin(det["P2"].t)
    reels = markers_of(ls, "REEL")
    b["直後の REEL まで"] = [next_after(reels, t) - t for t in b.t]
    b["直後の REEL まで"] = b["直後の REEL まで"].where(b["直後の REEL まで"] < 30)
    ctx.p("baseline の検出と特徴（post_gyro_mean = ピーク後 0.5〜3.0 秒の |g| 平均）:\n")
    ctx.table(b.round(2))
    for k in VARIANTS:
        ctx.p(f"- {k}: {len(det[k])} 件 → " + ", ".join(f"{t:.1f}s" for t in det[k].t) +
              f"（実キャスト 2 回とすると、少なくとも {max(0, len(det[k]) - 2)} 件は誤検出）")
    ctx.p()

    fig = plt.figure(figsize=(16, 5))
    gs = fig.add_gridspec(2, 1, hspace=0.1)
    ax1 = fig.add_subplot(gs[0])
    ax2 = fig.add_subplot(gs[1], sharex=ax1)
    ax1.plot(sig.t, sig.accel_mag, color=plots.MAG_COLOR, lw=0.5)
    ax2.plot(sig.t, sig.gyro_mag, color=plots.MAG_COLOR, lw=0.5)
    for ax in (ax1, ax2):
        plots._draw_markers(ax, mk, sig.t[0], sig.t[-1], label=(ax is ax1), matching=None)
    ax1.scatter(det["baseline"].t, det["baseline"].accel_peak, marker="v", s=60, color="#fab219",
                edgecolors="#8a5a00", zorder=5, label="baseline 検出")
    ax1.scatter(det["P1"].t, det["P1"].accel_peak + 4, marker="D", s=40, color=C_CAST, edgecolors=plots.SURFACE,
                zorder=6, label="P1 検出")
    ax1.axhline(25, color=plots.INK_MUTED, ls="--", lw=0.8)
    ax1.set_ylabel("|a| [m/s²]")
    ax2.set_ylabel("|g| [rad/s]")
    ax2.set_xlabel("セッション開始からの時間 [s]")
    ax1.legend(loc="upper right")
    ax1.tick_params(labelbottom=False)
    fig.suptitle("11:34 全体（CAST 22 s / 130 s、REEL 50 s / 150 s）", x=0.01, ha="left", fontsize=12)
    ctx.save(fig, "item5_113449_overview.png")

    ev = pd.DataFrame([dict(t=t, label=f"{t:.1f}s |a|={a:.0f}\npost|g|={pg:.2f}" + (" ◆P1" if keep else ""))
                       for t, a, pg, keep in zip(b.t, b.accel_peak, b.post_gyro_mean, b["P1 で残る"])])
    name = "item5_113449_candidates.png"
    plots.plot_event_gallery(sig, ev, mk, ctx.fig / name, 6.0, 6.0, detections=None, params=BASELINE,
                             title="11:34 baseline 検出 9 件（0 = 検出時刻）", cols=5)
    ctx.p(f"![{name}](figures/{name})\n")


# ------------------------------------------------------------------------------------------------
# 6. tune / eval

def item6_split(ctx: Ctx) -> None:
    ctx.h("6. 調整用と評価用を分離した成績")
    ctx.p(f"調整（tune）: {short(TUNE)} のみ（label=complete）。P1 はこのセッションだけで決めた"
          "（F1 が下がらない範囲で最も厳しい静止閾値）。P2 は参考値で、評価用セッションの結果を見て選んではいない。\n")
    rows = []
    for k, p in VARIANTS.items():
        res = run_variant(ctx, k, p)
        for group, names in (("tune (complete)", [TUNE]),
                             ("eval likely", ctx.by_label("likely")),
                             ("eval uncertain", ctx.by_label("uncertain")),
                             ("eval none", ctx.by_label("none"))):
            rs = [res[n] for n in names]
            c = {kk: sum(r.counts[kk] for r in rs) for kk in rs[0].counts}
            hours = sum(r.duration_s for r in rs) / 3600
            row = {"検知器": k, "区分": group, "セッション": len(rs), "CAST": c["cast_markers"], "一致": c["matched"],
                   "未検出": c["missed"], "検出": c["detections"], "未照合": c["unmatched_detections"],
                   "precision": np.nan, "recall": np.nan, "recall_ref": np.nan, "検出/時": np.nan}
            if group.startswith("tune"):
                row["precision"] = c["matched"] / c["detections"] if c["detections"] else np.nan
                row["recall"] = c["matched"] / c["cast_markers"]
            elif group == "eval likely":
                row["recall_ref"] = c["matched"] / c["cast_markers"]
            elif group == "eval none":
                row["検出/時"] = c["detections"] / hours
            rows.append(row)
        r = res["session_20261025_113449"]
        rows.append({"検知器": k, "区分": "eval 11:34（件数既知 2）", "セッション": 1, "CAST": 2,
                     "一致": r.counts["matched"], "未検出": r.counts["missed"], "検出": r.counts["detections"],
                     "未照合": r.counts["unmatched_detections"], "precision": np.nan, "recall": np.nan,
                     "recall_ref": np.nan, "検出/時": np.nan})
    df = pd.DataFrame(rows)
    df.to_csv(ctx.out / "item6_split.csv", index=False)
    ctx.table(df)
    ctx.p("- precision / recall は complete（tune）のみ。tune の数値は調整に使ったデータでの成績で、汎化性能ではない\n"
          "- likely は recall_ref（参考値）と未照合の件数のみ。uncertain / none は件数のみ\n"
          "- 11:34 は CAST マーカーの位置がずれている（項目 1・5）ため、一致/未検出より「検出数 − 2」の方が意味を持つ\n")

    # 閾値スイープ（tune と eval を並べて、tune だけ見て決める危うさを示す）
    th = np.arange(18, 33, 1.0)
    curves = []
    for post in (0.0, 1.5):
        for a in th:
            p = replace(BASELINE, accel_peak_min=float(a), post_gyro_mean_max=post)
            res = run_variant(ctx, "sweep", p)
            t = res[TUNE].counts
            lk = [res[n].counts for n in ctx.by_label("likely")]
            nn = [res[n] for n in ctx.by_label("none")]
            curves.append(dict(post=post, a=a, tune_recall=t["matched"] / t["cast_markers"], tune_fp=t["detections"] - t["matched"],
                               likely_recall=sum(c["matched"] for c in lk) / sum(c["cast_markers"] for c in lk),
                               likely_unmatched=sum(c["unmatched_detections"] for c in lk),
                               none_per_hour=sum(r.counts["detections"] for r in nn) / (sum(r.duration_s for r in nn) / 3600),
                               d113449=res["session_20261025_113449"].counts["detections"]))
    cv = pd.DataFrame(curves)
    cv.to_csv(ctx.out / "item6_threshold_sweep.csv", index=False)
    fig, axes = plt.subplots(1, 3, figsize=(16, 4.2), gridspec_kw=dict(wspace=0.28))
    for post, ls_, lab in ((0.0, "-", "静止フィルタなし"), (1.5, "--", "静止フィルタ 1.5")):
        g = cv[cv.post == post]
        axes[0].plot(g.a, g.tune_recall, color=C_CAST, ls=ls_, lw=2, label=f"tune recall（{lab}）")
        axes[0].plot(g.a, g.likely_recall, color=C_THIRD, ls=ls_, lw=2, label=f"likely recall_ref（{lab}）")
        axes[1].plot(g.a, g.tune_fp, color=C_CAST, ls=ls_, lw=2, label=f"tune 誤検出（{lab}）")
        axes[1].plot(g.a, g.likely_unmatched, color=C_THIRD, ls=ls_, lw=2, label=f"likely 誤検出候補（{lab}）")
        axes[1].plot(g.a, g.d113449 - 2, color=C_OTHER, ls=ls_, lw=2, label=f"11:34 検出数−2（{lab}）")
        axes[2].plot(g.a, g.none_per_hour, color=C_CAST, ls=ls_, lw=2, label=lab)
    axes[0].set_title("再現率", loc="left")
    axes[1].set_title("誤検出（確定 / 候補 / 下限）の件数", loc="left")
    axes[2].set_title("ラベルなしセッションの検出数 / 時（正解不明）", loc="left")
    for ax in axes:
        ax.set_xlabel("accel_peak_min [m/s²]")
        ax.axvline(25, color=plots.INK_MUTED, lw=0.8, ls=":")
        ax.legend(fontsize=7)
    ctx.save(fig, "item6_threshold_sweep.png")


# ------------------------------------------------------------------------------------------------
# 7. min_interval

def item7_interval(ctx: Ctx) -> None:
    ctx.h("7. 検知間隔（min_interval_s = 8 秒）の影響")
    # (a) 1 回のキャスト内で閾値を超える副ピークの時間的広がり
    spread = []
    nointerval = replace(BASELINE, min_interval_s=0.0)
    for n in ctx.by_label("complete", "likely"):
        ls = ctx.sessions[n]
        d = detect(ls.signals, nointerval)
        dp = detect(ls.signals, replace(P1, min_interval_s=0.0))
        r = evaluate_session(ctx.config.session(n, True), ls.session.duration_s, ls.session.markers,
                             detect(ls.signals, BASELINE), ctx.config.matching)
        for t in r.markers.loc[r.markers.status == "matched", "det_t"]:
            near = d[(d.t >= t - 10) & (d.t <= t + 10) & (d.t != t)]
            nearp = dp[(dp.t >= t - 10) & (dp.t <= t + 10) & (dp.t != t)]
            spread.append(dict(session=short(n), cast_t=round(t, 1),
                               extra_peaks=len(near), max_dist=float(np.abs(near.t - t).max()) if len(near) else 0.0,
                               extra_peaks_P1=len(nearp),
                               max_dist_P1=float(np.abs(nearp.t - t).max()) if len(nearp) else 0.0))
    sp = pd.DataFrame(spread)
    sp.to_csv(ctx.out / "item7_within_cast.csv", index=False)
    ctx.p(f"(a) 一致したキャスト {len(sp)} 回のうち、±10 秒以内に |a|≥25 の副ピークがあったのは "
          f"{int((sp.extra_peaks > 0).sum())} 回（最大距離 {sp.max_dist.max():.1f} 秒）。"
          f"静止フィルタ（P1）を通すと {int((sp.extra_peaks_P1 > 0).sum())} 回（最大距離 {sp.max_dist_P1.max():.1f} 秒）。\n")
    ctx.table(sp[sp.extra_peaks > 0])

    # (b) 実際のキャスト間隔
    gaps = []
    for n in ctx.by_label("complete", "likely"):
        c = markers_of(ctx.sessions[n], "CAST")
        gaps.extend(np.diff(c))
    gaps_none = []
    for n in ctx.by_label("none", "uncertain"):
        d = detect(ctx.sessions[n].signals, replace(P1, min_interval_s=0.0))
        gaps_none.extend(np.diff(d.t))
    ctx.p(f"(b) complete / likely の CAST マーカー間隔: 最小 {min(gaps):.1f} 秒、5% 点 {np.percentile(gaps, 5):.1f} 秒。"
          f"uncertain には 9.0 秒（09:13 の 1578 s → 1587 s）・1.0 秒（10:28）の間隔があるが、誤タップとの区別はつかない。\n")

    # (c) min_interval スイープ
    rows = []
    for mi in (0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 12.0):
        for k, p in (("baseline", BASELINE), ("P1", P1)):
            res = run_variant(ctx, k, replace(p, min_interval_s=mi))
            t = res[TUNE].counts
            lk = [res[n].counts for n in ctx.by_label("likely")]
            rows.append({"min_interval_s": mi, "検知器": k, "tune 一致": t["matched"], "tune 誤検出": t["detections"] - t["matched"],
                         "likely 一致": sum(c["matched"] for c in lk),
                         "likely 誤検出候補": sum(c["unmatched_detections"] for c in lk),
                         "none 検出": sum(res[n].counts["detections"] for n in ctx.by_label("none"))})
    df = pd.DataFrame(rows)
    df.to_csv(ctx.out / "item7_min_interval_sweep.csv", index=False)
    ctx.p("(c) min_interval_s を変えたときの件数:\n")
    ctx.table(df, floatfmt=".0f")

    # (d) 合成データ: 実キャストの波形を Δ 秒後に複製して、2 回として数えられるか
    ls = ctx.sessions[TUNE]
    sig = ls.signals
    r = evaluate_session(ctx.config.session(TUNE, True), ls.session.duration_s, ls.session.markers,
                         detect(sig, BASELINE), ctx.config.matching)
    cast_peaks = r.markers.loc[r.markers.status == "matched", "det_t"].to_numpy()
    fs = sig.fs
    pre, post = 3.0, 4.0  # 複製する区間（ピーク基準）
    sim = []
    for delta in (3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 10.0):
        for mi in (3.0, 5.0, 8.0):
            for k, p in (("baseline", BASELINE), ("P1", P1)):
                ok = 0
                for t in cast_peaks:
                    i0 = int(round((t - sig.t[0]) * fs))
                    seg = slice(i0 - int(pre * fs), i0 + int(post * fs))
                    dst0 = i0 + int(delta * fs) - int(pre * fs)
                    if dst0 + (seg.stop - seg.start) >= len(sig.t):
                        continue
                    acc, gyr = sig.accel.copy(), sig.gyro.copy()
                    acc[dst0 : dst0 + seg.stop - seg.start] = sig.accel[seg]
                    gyr[dst0 : dst0 + seg.stop - seg.start] = sig.gyro[seg]
                    w = slice(i0 - int(10 * fs), i0 + int((delta + 10) * fs))
                    d = detect(Signals(sig.t[w], fs, acc[w], gyr[w]), replace(p, min_interval_s=mi))
                    hits = d[(d.t >= t - 1) & (d.t <= t + delta + 1)]
                    ok += int(len(hits) >= 2)
                sim.append({"Δ[s]": delta, "min_interval_s": mi, "検知器": k, "2 回とも検出": ok, "試行": len(cast_peaks)})
    sd = pd.DataFrame(sim)
    sd.to_csv(ctx.out / "item7_splice_sim.csv", index=False)
    piv = sd.pivot_table(index=["検知器", "min_interval_s"], columns="Δ[s]", values="2 回とも検出").reset_index()
    ctx.p(f"(d) 合成テスト: 15:09 の一致キャスト {len(cast_peaks)} 回それぞれについて、ピーク −3〜+4 秒の波形を Δ 秒後に複製し、"
          "2 回として検出できた数（列 = Δ 秒）。複製元の直後に上書きするため、Δ が小さいと 1 回目の「ピーク後静止」区間も"
          "2 回目の振りで上書きされる点に注意（現実の連続キャストに近い）。\n")
    piv.columns = [str(c) for c in piv.columns]
    ctx.table(piv, floatfmt=".0f")

    fig, ax = plt.subplots(figsize=(8, 3.6))
    ax.hist(gaps, bins=np.arange(0, 130, 5), color=C_CAST, edgecolor=plots.SURFACE, label="CAST マーカー間隔（complete / likely）")
    ax.axvline(8, color=plots.INK_2, ls="--", lw=1)
    ax.text(8.5, ax.get_ylim()[1] * 0.9, "min_interval 8 s", fontsize=8, color=plots.INK_2)
    ax.set_xlabel("間隔 [s]")
    ax.set_ylabel("件数")
    ax.set_title("キャスト間隔の分布", loc="left")
    ctx.save(fig, "item7_cast_gaps.png")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=Path, default=ANALYSIS.parent / "captures" / "sessions")
    ap.add_argument("--out", type=Path, default=ANALYSIS / "reports" / "2026-10-25_detector_review")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ctx = Ctx(args.sessions, args.out)
    ctx.p("# 検知アルゴリズム レビュー（自動生成部分）\n")
    ctx.p("検知器: " + "; ".join(f"**{k}** = {p.to_dict()}" for k, p in VARIANTS.items()) + "\n")
    for f in (item1_offsets, item2_missed, item3_reel, item4_other, item5_113449, item6_split, item7_interval):
        f(ctx)
    (args.out / "results.md").write_text("\n".join(ctx.md) + "\n", encoding="utf-8")
    print(f"\n→ {args.out / 'results.md'}")


if __name__ == "__main__":
    main()
