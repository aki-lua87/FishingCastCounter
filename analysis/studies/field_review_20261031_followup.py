"""実釣データ（2026-10-31）の再評価 — 20:24 の釣り方の申告と、閾値以外の改善方法。

    cd analysis
    .venv\\Scripts\\python.exe studies\\field_review_20261031_followup.py [--out reports/2026-10-31_field_review]

`field_review_20261031.py` を先に実行しておくこと（cast_markers.csv を使う）。出力は followup_results.md と figures/fu_*.png。

項目:
  1. 20:24（フリーリグ、意図的に弱いキャスト）の加速度・角速度は他より小さいか
  2. 未検知キャストに共通する特徴
  3. |a| 閾値 25 → 21 の寄与（10/31 で選んだ値の成績と、未知データでの予測を分ける）
  4. 閾値以外の特徴: ピーク時の回転方向（ジャイロの向き）、ピーク後の姿勢（重力の向き）
  5. 弱いキャストを拾いつつ、歩行・巻き取りの誤検知を増やさない組み合わせ

データの使い分け:
- 回転方向・姿勢のテンプレートと閾値は 10/25 15:09（tune, complete）だけで決める。10/31 は検証に使う
  （ただし「回転方向がそろっている」ことに気づいたのは 10/31 の波形を見たとき。発想は独立していない）
- |a| 閾値 21 は 10/31 を見て選んだ値。10/31 での成績は楽観的になる
- 未知データでの予測の目安として、10/25 の tune 以外のセッションと、10/31 のセッション単位の交差検証を使う
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import mannwhitneyu

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import field_review_20261031 as base  # noqa: E402
from castlab.config import load_config  # noqa: E402
from castlab.detector import DetectorParams, detect  # noqa: E402
from castlab.evaluate import cast_window_after, evaluate_session  # noqa: E402
from castlab.io import find_sessions  # noqa: E402
from castlab.pipeline import load, run  # noqa: E402
from castlab.signals import Signals  # noqa: E402

plt = base.plt
plots = base.plots
ANALYSIS = base.ANALYSIS
P1 = base.P1
SESSIONS = base.SESSIONS
short = base.short
FREE_RIG = "session_20261031_202426"
TEMPLATE_SESSION = "session_20261025_150936"
WALK_SESSIONS = {"session_20261031_211905", "session_20261025_085344"}

# 回転方向・姿勢の閾値（15:09 の一致キャスト 28 件の分布から決める。下の template() 参照）
GCOS_MIN = 0.8
PCOS_MIN = 0.85

C_FREE, C_OK, C_MISS, C_RET, C_WALK = "#eb6834", "#2a78d6", "#d03b3b", "#8a8985", "#1baf7a"


def seg(sig: Signals, arr: np.ndarray, t: float, a: float, b: float) -> np.ndarray:
    i = int(round((t - sig.t[0]) * sig.fs))
    return np.nan_to_num(arr[max(0, i + int(round(a * sig.fs))): max(0, i + int(round(b * sig.fs)) + 1)])


def peak_gyro_vec(sig: Signals, t: float) -> np.ndarray:
    """|a| ピーク −0.3〜+0.1 秒で |g| が最大の時点の角速度ベクトル（振りの回転軸と向き）。"""
    g = seg(sig, sig.gyro, t, -0.3, 0.1)
    return g[np.argmax(np.linalg.norm(g, axis=1))] if len(g) else np.zeros(3)


def post_gravity(sig: Signals, t: float) -> np.ndarray:
    """ピーク後 0.5〜3 秒の加速度平均（静止中の重力の向き = ロッドを構えた姿勢）。"""
    a = seg(sig, sig.accel, t, 0.5, 3.0)
    return a.mean(axis=0) if len(a) else np.zeros(3)


def cos(v: np.ndarray, u: np.ndarray) -> float:
    return float(v @ u / (np.linalg.norm(v) * np.linalg.norm(u) + 1e-9))


def swing_angle(sig: Signals, t: float) -> float:
    """振りの回転量: ピーク −1.0〜+0.3 秒の |g| の積分 [rad]。"""
    g = seg(sig, sig.gyro_mag, t, -1.0, 0.3)
    return float(g.sum() / sig.fs)


def still_length(sig: Signals, t: float, level: float = 1.5, smooth_s: float = 0.2) -> float:
    """ピーク +0.5 秒以降、|g|（0.2 秒移動平均）が level を超えるまでの秒数（上限 20 秒）。"""
    g = seg(sig, sig.gyro_mag, t, 0.5, 20.0)
    n = max(1, int(round(smooth_s * sig.fs)))
    gs = np.convolve(g, np.ones(n) / n, mode="same")
    over = np.flatnonzero(gs > level)
    return 0.5 + (over[0] / sig.fs if len(over) else 19.5)


class Template:
    def __init__(self, sessions_dir: Path, config):
        ls = load(sessions_dir / f"{TEMPLATE_SESSION}.csv")
        res = run(ls, P1, config)
        t = res.markers.loc[res.markers["status"] == "matched", "det_t"].to_numpy()
        g = np.array([peak_gyro_vec(ls.signals, x) for x in t])
        p = np.array([post_gravity(ls.signals, x) for x in t])
        self.gyro = g.mean(axis=0) / np.linalg.norm(g.mean(axis=0))
        self.post = p.mean(axis=0) / np.linalg.norm(p.mean(axis=0))
        self.self_gcos = np.array([cos(v, self.gyro) for v in g])
        self.self_pcos = np.array([cos(v, self.post) for v in p])
        self.n = len(t)


def features(sig: Signals, t: float, tpl: Template) -> dict:
    return dict(gcos=cos(peak_gyro_vec(sig, t), tpl.gyro), pcos=cos(post_gravity(sig, t), tpl.post),
                swing=swing_angle(sig, t), still_s=still_length(sig, t), pre_swing=base.pre_swing(sig, t))


# --- 変形した検知器（what-if。castlab / アプリの検知器は変えない） -------------------------------------


def suppress(cands: pd.DataFrame, min_interval_s: float) -> pd.DataFrame:
    """castlab.detector と同じ近接抑制（前後 min_interval_s 以内に |a| が大きい候補があれば捨てる。同値は先が勝つ）。"""
    t = cands["t"].to_numpy()
    a = cands["accel_peak"].to_numpy()
    keep = []
    for k in range(len(t)):
        beaten = False
        for j in range(k - 1, -1, -1):
            if t[k] - t[j] >= min_interval_s - 1e-9:
                break
            if a[j] >= a[k]:
                beaten = True
                break
        if not beaten:
            for j in range(k + 1, len(t)):
                if t[j] - t[k] >= min_interval_s - 1e-9:
                    break
                if a[j] > a[k]:
                    beaten = True
                    break
        keep.append(not beaten)
    return cands[np.array(keep, dtype=bool)].reset_index(drop=True) if len(cands) else cands


def variant_detect(sig: Signals, params: DetectorParams, tpl: Template, gcos_min: float | None,
                   pcos_min: float | None) -> pd.DataFrame:
    """P1 の候補（近接抑制前）に回転方向・姿勢の条件を足してから近接抑制する。"""
    cands = detect(sig, replace(params, min_interval_s=0.0))
    if len(cands) and (gcos_min is not None or pcos_min is not None):
        ok = np.ones(len(cands), dtype=bool)
        for i, t in enumerate(cands["t"]):
            if gcos_min is not None and cos(peak_gyro_vec(sig, t), tpl.gyro) < gcos_min:
                ok[i] = False
            elif pcos_min is not None and cos(post_gravity(sig, t), tpl.post) < pcos_min:
                ok[i] = False
        cands = cands[ok].reset_index(drop=True)
    return suppress(cands, params.min_interval_s)


VARIANTS = {
    "P1（現行）": (P1, None, None),
    "A: |a|≥21, |g|≥5.5": (P1.with_overrides(["accel_peak_min=21", "gyro_peak_min=5.5"]), None, None),
    "B: |a|≥21 + 回転方向": (P1.with_overrides(["accel_peak_min=21"]), GCOS_MIN, None),
    "C: |a|≥21 + 回転方向 + 姿勢": (P1.with_overrides(["accel_peak_min=21"]), GCOS_MIN, PCOS_MIN),
    "D: |a|≥18 + 回転方向 + 姿勢": (P1.with_overrides(["accel_peak_min=18"]), GCOS_MIN, PCOS_MIN),
    "E: |a|≥25 + 回転方向 + 姿勢": (P1, GCOS_MIN, PCOS_MIN),
    # 案 C を castlab の検知器に組み込んだもの（テンプレートは小数 4 桁に丸めた固定値）。C と同じ結果になるはず（回帰確認）
    "P2 候補（params/p2_candidate.json）": (DetectorParams.load(ANALYSIS / "params" / "p2_candidate.json"), None, None),
}


def count_1031(ls, name: str, det_t: np.ndarray, config) -> dict:
    """field_review_20261031 と同じ 2 段照合（事後入力を含む）で数える。"""
    m = config.matching
    s = ls.session
    mk = s.markers
    ct = mk.loc[mk["type"] == "CAST", "t"].to_numpy()
    rt = mk.loc[mk["type"] == "REEL", "t"].to_numpy()
    aft = cast_window_after(mk, ct, m)
    pairs = base.match(ct, det_t, np.full(len(ct), m.window_before_s), aft, m.expected_offset_s)
    nr = np.array([next((r - t for r in rt if r > t), np.nan) for t in ct])
    rc = [i for i in range(len(ct)) if i not in {p[0] for p in pairs} and nr[i] <= base.POSTHOC_REEL_S]
    ra = [j for j in range(len(det_t)) if j not in {p[1] for p in pairs}]
    pairs2 = base.match(ct[rc], det_t[ra], np.full(len(rc), base.POSTHOC_BEFORE_S), nr[rc],
                        m.expected_offset_s) if rc and ra else []
    tp = len(pairs) + len(pairs2)
    matched_c = {i for i, _ in pairs} | {rc[i] for i, _ in pairs2}
    ev = s.events
    refs = ev.loc[(ev["type"] == "FALSE") & ev["active"], "ref_id"].dropna().astype(int)
    ft = ev.loc[ev["event_id"].isin(refs), "t"].to_numpy()
    return dict(cast=len(ct), tp=tp, fn=len(ct) - tp, det=len(det_t), unmatched=len(det_t) - tp,
                false_hit=int(sum(np.any(np.abs(det_t - t) < 0.05) for t in ft)),
                matched_cast_t=ct[sorted(matched_c)])


class Ctx(base.Ctx):
    def __init__(self, sessions_dir: Path, out: Path):
        super().__init__(sessions_dir, out)
        self.sessions_dir = sessions_dir
        self.tpl = Template(sessions_dir, self.config)
        # 10/25 のうち ignore 以外。tune（15:09）はテンプレート作成に使ったので別扱い
        paths = [p for p in find_sessions([sessions_dir]) if p.stem.startswith("session_20261025")
                 and not self.config.is_ignored(p.stem)]
        self.holdout = {p.stem: load(p) for p in paths}


# --- 各節 -------------------------------------------------------------------------------------------------


def cast_table(ctx: Ctx) -> pd.DataFrame:
    """10/31 の CAST ごとの特徴量（cast_markers.csv の候補ピークに、回転方向などを足す）。"""
    c = pd.read_csv(ctx.out / "cast_markers.csv")
    c = c.dropna(subset=["det_t"]).copy()
    rows = []
    for r in c.itertuples():
        sig = ctx.loaded[r.session].signals
        rows.append(features(sig, r.det_t, ctx.tpl))
    f = pd.DataFrame(rows)
    c = pd.concat([c.reset_index(drop=True).drop(columns=[k for k in f.columns if k in c.columns]), f], axis=1)
    c["group"] = np.where(c["session"] == FREE_RIG, "20:24（フリーリグ）",
                          np.where(c["status"] == "missed", "他・未検知", "他・一致"))
    return c


def sec_free_rig(ctx: Ctx, c: pd.DataFrame) -> None:
    ctx.h("F1. 20:24（フリーリグ・意図的に弱いキャスト）は他より小さいか")
    feats = [("accel_peak", "|a| ピーク [m/s²]"), ("gyro_peak", "|g| ピーク [rad/s]"),
             ("swing", "振りの回転量 [rad]"), ("pre_swing", "振りかぶり [rad/s]"),
             ("gcos", "回転方向の一致度"), ("post_gyro_mean", "ピーク後静止 [rad/s]"),
             ("still_s", "静止の長さ [s]"), ("offset_s", "CAST→ピーク [s]")]
    free = c[c["group"] == "20:24（フリーリグ）"]
    other = c[c["session"] != FREE_RIG]
    rows = []
    for k, label in feats:
        p = mannwhitneyu(free[k], other[k]).pvalue
        rows.append({"特徴量": label, "20:24 中央値": free[k].median(), "20:24 範囲": f"{free[k].min():.2f}〜{free[k].max():.2f}",
                     "他 中央値": other[k].median(), "他 範囲": f"{other[k].min():.2f}〜{other[k].max():.2f}",
                     "他・未検知 中央値": c.loc[c["group"] == "他・未検知", k].median(),
                     "p 値（20:24 vs 他）": p})
    ctx.p(f"20:24 は n={len(free)}、他は n={len(other)}（未検知 {int((c['group'] == '他・未検知').sum())} を含む）。"
          "p 値は Mann–Whitney U（20:24 は 6 件しかないので目安）\n")
    df = pd.DataFrame(rows)
    ctx.table(df, ".3g")
    ctx.csv(c, "fu_cast_features.csv")

    # REEL 後の巻き取り中の |g|（釣り方全体の動きの大きさ）
    rows = []
    for name in SESSIONS:
        if name.endswith("211905"):
            continue
        ls = ctx.loaded[name]
        mk = ls.session.markers
        rt = mk.loc[mk["type"] == "REEL", "t"].to_numpy()
        ct = mk.loc[mk["type"] == "CAST", "t"].to_numpy()
        vals = []
        for r in rt:
            nxt = ct[ct > r]
            end = min(r + 30.0, (nxt.min() - 2.0) if len(nxt) else r + 30.0)
            if end - r > 5:
                vals.append(np.nanmean(seg(ls.signals, ls.signals.gyro_mag, r, 1.0, end - r)))
        rows.append({"セッション": short(name), "REEL 後 30 秒の |g| 平均（中央値）": np.median(vals) if vals else np.nan,
                     "区間数": len(vals)})
    ctx.p("**巻き取り中（REEL +1〜+30 秒、次の CAST の 2 秒前まで）の動きの大きさ**\n")
    ctx.table(pd.DataFrame(rows))

    fig, axes = plt.subplots(1, 4, figsize=(15, 3.6))
    order = ["他・一致", "他・未検知", "20:24（フリーリグ）"]
    colors = [C_OK, C_MISS, C_FREE]
    for ax, (k, label) in zip(axes, [feats[0], feats[1], feats[2], feats[4]]):
        for i, (g, col) in enumerate(zip(order, colors)):
            v = c.loc[c["group"] == g, k].to_numpy()
            x = i + np.linspace(-0.18, 0.18, len(v)) if len(v) > 1 else np.full(len(v), i)
            ax.scatter(x, v, s=14, color=col, alpha=0.8)
            if len(v):
                ax.hlines(np.median(v), i - 0.3, i + 0.3, color=plots.INK, lw=1.2)
        ax.set_xticks(range(3), ["他・一致", "他・未検知", "20:24"])
        ax.set_title(label, loc="left")
    axes[0].axhline(P1.accel_peak_min, color=plots.INK_MUTED, ls="--", lw=0.8)
    axes[1].axhline(P1.gyro_peak_min, color=plots.INK_MUTED, ls="--", lw=0.8)
    axes[3].axhline(GCOS_MIN, color=plots.INK_MUTED, ls="--", lw=0.8)
    fig.suptitle("CAST ごとの特徴量（横線 = 中央値、破線 = 現行または提案の閾値）", x=0.01, y=1.06, ha="left")
    ctx.save(fig, "fu_free_rig_features.png")

    # 20:24 と典型キャストの 3 軸ジャイロ
    typical = c[(c["session"] == "session_20261031_193927")].head(2)
    rows = [(r.session, r.det_t, f"19:39 {r.cast_t:.0f}s |a|={r.accel_peak:.1f} cos={r.gcos:.2f}") for r in typical.itertuples()]
    rows += [(r.session, r.det_t, f"20:24 {r.cast_t:.0f}s |a|={r.accel_peak:.1f} cos={r.gcos:.2f}") for r in free.itertuples()]
    gallery_axes(ctx, rows, -2.0, 4.0, "fu_gyro_axes.png",
                 "3 軸ジャイロ（0 = 候補ピーク）。典型は x 負・z 正の鋭い山、20:24 は形・向きが違うものがある")


def gallery_axes(ctx: Ctx, rows, pre: float, post: float, name: str, title: str) -> None:
    cols = min(4, len(rows))
    nrow = int(np.ceil(len(rows) / cols))
    fig, axes = plt.subplots(nrow, cols, figsize=(3.8 * cols, 2.6 * nrow), sharey=True, squeeze=False)
    for k, (sname, t, label) in enumerate(rows):
        ax = axes[k // cols][k % cols]
        sig = ctx.loaded[sname].signals
        s = sig.slice(t + pre, t + post)
        for i, a in enumerate("xyz"):
            ax.plot(s.t - t, s.gyro[:, i], color=plots.AXIS_COLORS[a], lw=0.9, label=f"gyro {a}")
        ax.set_title(label, fontsize=8, loc="left")
        ax.set_xlim(pre, post)
    for k in range(len(rows), nrow * cols):
        axes[k // cols][k % cols].axis("off")
    axes[0][0].legend(loc="lower left", fontsize=7)
    fig.suptitle(title, x=0.01, ha="left", fontsize=11)
    fig.tight_layout()
    fig.savefig(ctx.fig / name, dpi=80, bbox_inches="tight")
    plt.close(fig)
    ctx.p(f"![{name}](figures/{name})\n")


def sec_missed(ctx: Ctx, c: pd.DataFrame) -> None:
    ctx.h("F2. 未検知キャストに共通する特徴")
    rows = []
    for g in ["他・一致", "他・未検知", "20:24（フリーリグ）"]:
        x = c[c["group"] == g]
        rows.append({"グループ": g, "n": len(x), "|a| 中央値": x["accel_peak"].median(),
                     "|g| 中央値": x["gyro_peak"].median(), "回転量 中央値": x["swing"].median(),
                     "回転方向 ≥0.8": f"{int((x['gcos'] >= GCOS_MIN).sum())}/{len(x)}",
                     "姿勢 ≥0.85": f"{int((x['pcos'] >= PCOS_MIN).sum())}/{len(x)}",
                     "静止 ≤1.5": f"{int((x['post_gyro_mean'] <= 1.5).sum())}/{len(x)}",
                     "振りかぶり 中央値": x["pre_swing"].median(),
                     "短周期（<35 s）": f"{int((x['next_cast_s'] < base.SHORT_CYCLE_S).sum())}/{len(x)}"})
    ctx.table(pd.DataFrame(rows))
    miss = c[c["status"] == "missed"].copy()
    miss.insert(0, "S", miss["session"].map(short))
    ctx.table(miss[["S", "cast_t", "accel_peak", "gyro_peak", "swing", "gcos", "pcos", "post_gyro_mean", "pre_swing",
                    "next_cast_s", "class"]], ".2f")


def per_session_counts(ctx: Ctx, variant) -> dict[str, dict]:
    params, gmin, pmin = variant
    out = {}
    for name in SESSIONS:
        ls = ctx.loaded[name]
        d = variant_detect(ls.signals, params, ctx.tpl, gmin, pmin)
        out[name] = count_1031(ls, name, d["t"].to_numpy(), ctx.config)
    return out


def holdout_counts(ctx: Ctx, variant) -> dict:
    """10/25（ignore 以外）での成績。ラベル信頼度別に数える。"""
    params, gmin, pmin = variant
    agg: dict[str, dict] = {}
    for name, ls in ctx.holdout.items():
        s = ls.session
        scfg = ctx.config.session(name, has_cast_markers=bool((s.markers["type"] == "CAST").any()))
        d = variant_detect(ls.signals, params, ctx.tpl, gmin, pmin)
        res = evaluate_session(scfg, s.duration_s, s.markers, d, ctx.config.matching)
        k = "tune（15:09）" if name == TEMPLATE_SESSION else scfg.label
        a = agg.setdefault(k, dict(cast=0, tp=0, unmatched=0, det=0, hours=0.0, walk=0, s113449=0))
        cnt = res.counts
        a["cast"] += cnt["cast_markers"]
        a["tp"] += cnt["matched"]
        a["unmatched"] += cnt["unmatched_detections"]
        a["det"] += cnt["detections"]
        a["hours"] += s.duration_s / 3600
        if name in WALK_SESSIONS:
            a["walk"] += cnt["detections"]
        if name.endswith("113449"):
            a["s113449"] += cnt["detections"]
    return agg


def sec_threshold(ctx: Ctx) -> dict:
    ctx.h("F3. |a| 閾値 25 → 21 の寄与")
    p21 = (P1.with_overrides(["accel_peak_min=21"]), None, None)
    a = per_session_counts(ctx, VARIANTS["P1（現行）"])
    b = per_session_counts(ctx, p21)
    rows = []
    for label, names in [("20:24（フリーリグ）", [FREE_RIG]), ("他の 6 セッション + 移動", [n for n in SESSIONS if n != FREE_RIG])]:
        r = {"対象": label, "CAST": sum(a[n]["cast"] for n in names)}
        r["TP 25"] = sum(a[n]["tp"] for n in names)
        r["TP 21"] = sum(b[n]["tp"] for n in names)
        r["対応しない検出 25"] = sum(a[n]["unmatched"] for n in names)
        r["対応しない検出 21"] = sum(b[n]["unmatched"] for n in names)
        rows.append(r)
    ctx.p("**10/31 で選んだ値を 10/31 に当てた成績（楽観的）**。|g|≥4・静止 ≤1.5 は現行のまま\n")
    ctx.table(pd.DataFrame(rows))

    # セッション単位の交差検証: 1 セッションを外し、残りで閾値を選んで、外したセッションに当てる
    grid = [25.0, 24.0, 23.0, 22.0, 21.0, 20.0, 19.0, 18.0]
    per = {th: per_session_counts(ctx, (P1.with_overrides([f"accel_peak_min={th}"]), None, None)) for th in grid}
    names = [n for n in SESSIONS if not n.endswith("211905")]
    rows = []
    tot = dict(cast=0, tp=0, unmatched=0)
    for held in names:
        def objective(th):
            tp = sum(per[th][n]["tp"] for n in SESSIONS if n != held)
            un = sum(per[th][n]["unmatched"] for n in SESSIONS if n != held)
            return tp - un  # 見逃し 1 件と、対応しない検出 1 件を同じ重さとみなす
        best = max(grid, key=lambda th: (objective(th), th))  # 同点なら高い閾値（保守的）
        r = per[best][held]
        tot["cast"] += r["cast"]
        tot["tp"] += r["tp"]
        tot["unmatched"] += r["unmatched"]
        rows.append({"外したセッション": short(held), "残りで選んだ閾値": best, "CAST": r["cast"], "TP": r["tp"],
                     "TP（25 のまま）": per[25.0][held]["tp"], "対応しない検出": r["unmatched"],
                     "同（25 のまま）": per[25.0][held]["unmatched"]})
    rows.append({"外したセッション": "計", "残りで選んだ閾値": np.nan, "CAST": tot["cast"], "TP": tot["tp"],
                 "TP（25 のまま）": sum(per[25.0][n]["tp"] for n in names), "対応しない検出": tot["unmatched"],
                 "同（25 のまま）": sum(per[25.0][n]["unmatched"] for n in names)})
    ctx.p("**セッション単位の交差検証**（残りのセッションで「TP − 対応しない検出」が最大の閾値を選び、外したセッションに当てる）\n")
    ctx.table(pd.DataFrame(rows), ".0f")
    return per


def sec_features(ctx: Ctx) -> None:
    ctx.h("F4. 回転方向・姿勢の特徴量")
    tpl = ctx.tpl
    ctx.p(f"テンプレート: {TEMPLATE_SESSION}（tune, complete）の P1 一致キャスト {tpl.n} 件の平均。"
          f"回転方向 = ({', '.join(f'{v:+.2f}' for v in tpl.gyro)})、ピーク後の重力方向 = "
          f"({', '.join(f'{v:+.2f}' for v in tpl.post)})\n")
    q = lambda v: ", ".join(f"{x:.2f}" for x in np.sort(v)[:4])  # noqa: E731
    ctx.p(f"- 15:09 自身の一致度: 回転方向 最小 4 件 = {q(tpl.self_gcos)}、姿勢 最小 4 件 = {q(tpl.self_pcos)}\n"
          f"- これを見て閾値を **回転方向 ≥ {GCOS_MIN}、姿勢 ≥ {PCOS_MIN}** とした（10/31 は見ていない）\n")
    # 10/31 の候補ピークをラベル付け
    c = pd.read_csv(ctx.out / "cast_markers.csv").dropna(subset=["det_t"])
    rows = []
    for name in SESSIONS:
        ls = ctx.loaded[name]
        sig, mk = ls.signals, ls.session.markers
        ct = mk.loc[mk["type"] == "CAST", "t"].to_numpy()
        rt = mk.loc[mk["type"] == "REEL", "t"].to_numpy()
        pos = c[c["session"] == name]
        auto = ls.session.events.loc[ls.session.events["type"] == "AUTO_CAST", "t"].to_numpy()
        for p in detect(sig, base.POOL).itertuples():
            hit = pos[np.abs(pos["det_t"] - p.t) < 0.05]
            if len(hit):
                lab = "キャスト（未検知）" if hit["status"].iloc[0] == "missed" else "キャスト（一致）"
            elif name in WALK_SESSIONS:
                lab = "歩行"
            else:
                prev_r = rt[rt < p.t - 2.0]
                prev_c = ct[ct < p.t]
                nxt = ct[ct > p.t]
                in_retrieve = (len(prev_r) and (not len(prev_c) or prev_c.max() < prev_r.max())
                               and (not len(nxt) or nxt.min() > p.t + 2.0))
                lab = "REEL 後（巻き取り中）" if in_retrieve else "その他"
            rows.append(dict(session=name, t=p.t, label=lab, accel_peak=p.accel_peak, gyro_peak=p.gyro_peak,
                             post=p.post_gyro_mean, auto=bool(np.any(np.abs(auto - p.t) < 0.05)),
                             **features(sig, p.t, tpl)))
    d = pd.DataFrame(rows)
    ctx.csv(d, "fu_candidate_features.csv")
    ctx.p("**10/31 の候補ピーク（|a|≥15、静止フィルタなし）の特徴量**。「REEL 後」は REEL の 2 秒後から次の CAST の 2 秒前まで。"
          "CAST の押し忘れが混ざりうる（例: 21:20 3429 s）ので、非キャストとしては「原則」\n")
    rows = []
    for lab in ["キャスト（一致）", "キャスト（未検知）", "REEL 後（巻き取り中）", "歩行", "その他"]:
        x = d[d["label"] == lab]
        rows.append({"ラベル": lab, "n": len(x), "回転方向 中央値": x["gcos"].median(),
                     "回転方向 ≥0.8": f"{int((x['gcos'] >= GCOS_MIN).sum())}/{len(x)}",
                     "姿勢 中央値": x["pcos"].median(), "姿勢 ≥0.85": f"{int((x['pcos'] >= PCOS_MIN).sum())}/{len(x)}",
                     "静止 ≤1.5": f"{int((x['post'] <= 1.5).sum())}/{len(x)}",
                     "|a|≥21 & 静止 ≤1.5": int(((x["accel_peak"] >= 21) & (x["post"] <= 1.5)).sum()),
                     "上記 + 回転方向 + 姿勢": int(((x["accel_peak"] >= 21) & (x["post"] <= 1.5) & (x["gcos"] >= GCOS_MIN)
                                              & (x["pcos"] >= PCOS_MIN)).sum())})
    ctx.table(pd.DataFrame(rows))

    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    styles = [("REEL 後（巻き取り中）", C_RET, "."), ("歩行", C_WALK, "s"), ("その他", "#c8c7c2", "."),
              ("キャスト（一致）", C_OK, "o"), ("キャスト（未検知）", C_MISS, "D")]
    for lab, col, mk in styles:
        x = d[d["label"] == lab]
        axes[0].scatter(x["accel_peak"], x["gcos"], color=col, marker=mk, s=16 if mk == "." else 24, alpha=0.8,
                        label=f"{lab}（{len(x)}）")
        xs = x[x["post"] <= 1.5]
        axes[1].scatter(xs["gcos"], xs["pcos"], color=col, marker=mk, s=16 if mk == "." else 24, alpha=0.8)
    free = d[(d["session"] == FREE_RIG) & d["label"].str.startswith("キャスト")]
    for ax, (xc, yc) in zip(axes, [("accel_peak", "gcos"), ("gcos", "pcos")]):
        f = free if xc == "accel_peak" else free[free["post"] <= 1.5]
        ax.scatter(f[xc], f[yc], facecolors="none", edgecolors=C_FREE, s=110, lw=1.4, label="20:24 のキャスト（○）")
    axes[0].axvline(25, color=plots.INK_MUTED, ls="--", lw=0.8)
    axes[0].axvline(21, color=plots.INK_MUTED, ls=":", lw=0.8)
    axes[0].axhline(GCOS_MIN, color=plots.INK_MUTED, ls="--", lw=0.8)
    axes[0].set_xlabel("|a| ピーク [m/s²]（破線 25、点線 21）")
    axes[0].set_ylabel("回転方向の一致度（cos、破線 0.8）")
    axes[0].set_title("全候補: |a| と回転方向", loc="left")
    axes[0].legend(loc="lower right", fontsize=7)
    axes[1].axvline(GCOS_MIN, color=plots.INK_MUTED, ls="--", lw=0.8)
    axes[1].axhline(PCOS_MIN, color=plots.INK_MUTED, ls="--", lw=0.8)
    axes[1].set_xlabel("回転方向の一致度")
    axes[1].set_ylabel("ピーク後の姿勢の一致度（cos、破線 0.85）")
    axes[1].set_ylim(-0.8, 1.02)
    axes[1].set_title("静止 ≤ 1.5 の候補だけ: 回転方向と姿勢（右上がキャスト）", loc="left")
    ctx.save(fig, "fu_direction_scatter.png")


def sec_variants(ctx: Ctx) -> None:
    ctx.h("F5. 組み合わせの比較（10/31 = 調整を見たデータ、10/25 = 未知データの目安）")
    rows = []
    for vname, v in VARIANTS.items():
        per = per_session_counts(ctx, v)
        cast = sum(x["cast"] for x in per.values())
        tp = sum(x["tp"] for x in per.values())
        un = sum(x["unmatched"] for x in per.values())
        fr = per[FREE_RIG]
        ho = holdout_counts(ctx, v)
        tune = ho.get("tune（15:09）", {})
        lk = ho.get("likely", {})
        un_c = ho.get("uncertain", {})
        no = ho.get("none", {})
        rows.append({
            "検知器": vname,
            "10/31 TP": f"{tp}/{cast}", "10/31 対応しない": un,
            "うち FALSE 対象": sum(x["false_hit"] for x in per.values()),
            "20:24 TP": f"{fr['tp']}/{fr['cast']}", "21:19 移動": per["session_20261031_211905"]["det"],
            "10/25 15:09 TP / 誤検出": f"{tune.get('tp', 0)}/{tune.get('cast', 0)} / {tune.get('unmatched', 0)}",
            "10/25 likely TP / 候補": f"{lk.get('tp', 0)}/{lk.get('cast', 0)} / {lk.get('unmatched', 0)}",
            "10/25 11:34（実 2 回）": un_c.get("s113449", 0),
            "10/25 uncertain 対応しない": un_c.get("unmatched", 0),
            "10/25 none 件/時": no.get("det", 0) / no["hours"] if no.get("hours") else np.nan,
            "10/25 08:53 歩行": no.get("walk", 0),
        })
    df = pd.DataFrame(rows)
    ctx.table(df, ".1f")
    ctx.csv(df, "fu_variants.csv")


HOOK_SESSION = "session_20261031_200612"


def sec_hook(ctx: Ctx, c: pd.DataFrame) -> None:
    """20:06 の最初の CAST より前（フックがバッグに刺さり、外そうとしていたという申告。仮説）。"""
    ctx.h("F6. 20:06 の FALSE 申告 2 件（フック外しの動作という申告。仮説）")
    ls = ctx.loaded[HOOK_SESSION]
    sig, ev = ls.signals, ls.session.events
    first_cast = float(ls.session.markers.loc[ls.session.markers["type"] == "CAST", "t"].min())
    auto = ev[(ev["type"] == "AUTO_CAST") & (ev["t"] < first_cast)]
    false = ev[ev["type"] == "FALSE"]
    ctx.p(f"最初の CAST: {first_cast:.1f} s。AUTO_CAST {', '.join(f'#{r.event_id} {r.t:.1f} s' for r in auto.itertuples())}"
          f"（間隔 {np.diff(auto['t']).round(1).tolist()} s）。FALSE 押下 {', '.join(f'{r.t:.1f} s' for r in false.itertuples())}\n")

    rows = []
    for r in auto.itertuples():
        f = features(sig, r.t, ctx.tpl)
        gv = peak_gyro_vec(sig, r.t)
        rows.append({"対象": f"AUTO #{r.event_id} {r.t:.1f} s", "|a|": float(r.detail["accel_peak"]),
                     "|g| ピーク": float(r.detail["gyro_peak"]), "ピーク後静止": float(r.detail["post_gyro_mean"]),
                     "振りかぶり": f["pre_swing"], "振りの回転量": f["swing"], "回転方向": f["gcos"], "姿勢": f["pcos"],
                     "静止の長さ": f["still_s"], "角速度ベクトル": "(" + ", ".join(f"{v:+.1f}" for v in gv) + ")"})
    ok = c[c["status"] != "missed"]
    for label, x in [("一致キャスト 中央値", ok), ("一致キャスト 最小", None), ("未検知キャスト 中央値", c[c["status"] == "missed"])]:
        if x is None:
            rows.append({"対象": label, "|a|": ok["accel_peak"].min(), "|g| ピーク": ok["gyro_peak"].min(),
                         "ピーク後静止": ok["post_gyro_mean"].min(), "振りかぶり": ok["pre_swing"].min(),
                         "振りの回転量": ok["swing"].min(), "回転方向": ok["gcos"].min(), "姿勢": ok["pcos"].min(),
                         "静止の長さ": ok["still_s"].min(), "角速度ベクトル": ""})
        else:
            rows.append({"対象": label, "|a|": x["accel_peak"].median(), "|g| ピーク": x["gyro_peak"].median(),
                         "ピーク後静止": x["post_gyro_mean"].median(), "振りかぶり": x["pre_swing"].median(),
                         "振りの回転量": x["swing"].median(), "回転方向": x["gcos"].median(), "姿勢": x["pcos"].median(),
                         "静止の長さ": x["still_s"].median(), "角速度ベクトル": ""})
    ctx.table(pd.DataFrame(rows))
    sw = c["swing"]
    ctx.p(f"- 振りの回転量: 一致キャスト {ok['swing'].min():.2f}〜{ok['swing'].max():.2f} rad、"
          f"未検知キャスト {c.loc[c['status'] == 'missed', 'swing'].min():.2f}〜{c.loc[c['status'] == 'missed', 'swing'].max():.2f} rad"
          f"（全 CAST の 5% 点 {sw.quantile(0.05):.2f}）\n")

    # 釣り開始前と釣り中の動きの大きさ
    gm = np.nan_to_num(sig.gyro_mag)
    act = []
    for a in np.arange(0, 420, 30):
        w = gm[int(a * sig.fs): int((a + 30) * sig.fs)]
        act.append({"区間 [s]": f"{a:.0f}–{a + 30:.0f}", "|g| 平均": w.mean(), "|g| 最大": w.max(),
                    "|a| 最大": np.nan_to_num(sig.accel_mag)[int(a * sig.fs): int((a + 30) * sig.fs)].max()})
    ctx.p("**30 秒ごとの動きの大きさ（0〜420 s）**\n")
    ctx.table(pd.DataFrame(act))

    # 最初の CAST より前の候補ピークと、検知器ごとの検出
    pool = detect(sig, base.POOL)
    pool = pool[pool["t"] < first_cast - 2]
    prow = []
    for p in pool.itertuples():
        f = features(sig, p.t, ctx.tpl)
        prow.append({"t": p.t, "|a|": p.accel_peak, "|g|": p.gyro_peak, "静止": p.post_gyro_mean,
                     "回転量": f["swing"], "回転方向": f["gcos"], "姿勢": f["pcos"]})
    ctx.p(f"**最初の CAST より前の候補ピーク（|a|≥15、静止フィルタなし）**\n")
    ctx.table(pd.DataFrame(prow))
    vrows = []
    for th in (25.0, 23.0, 21.0, 20.0, 18.0):
        d = detect(sig, P1.with_overrides([f"accel_peak_min={th}"]))
        vrows.append({"検知器": f"|a|≥{th:g}（|g|≥4、静止 ≤1.5）", "最初の CAST より前の検出": ", ".join(f"{x:.1f}" for x in d.loc[d["t"] < first_cast - 2, "t"]) or "なし"})
    for vname, v in VARIANTS.items():
        d = variant_detect(sig, v[0], ctx.tpl, v[1], v[2])
        vrows.append({"検知器": vname, "最初の CAST より前の検出": ", ".join(f"{x:.1f}" for x in d.loc[d["t"] < first_cast - 2, "t"]) or "なし"})
    ctx.table(pd.DataFrame(vrows))

    # 図: 全体（0〜420 s）
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(16, 4.8), sharex=True)
    s = sig.slice(0, 420)
    ax1.plot(s.t, s.accel_mag, color=plots.MAG_COLOR, lw=0.5)
    ax2.plot(s.t, s.gyro_mag, color=plots.MAG_COLOR, lw=0.5)
    ax1.axhline(P1.accel_peak_min, color=plots.INK_MUTED, ls="--", lw=0.8)
    mk = ls.session.markers
    for x in mk[mk["t"] < 420].itertuples():
        for ax in (ax1, ax2):
            ax.axvline(x.t, color=plots.INK, **plots.MARKER_STYLE.get(x.type, {}))
        ax1.text(x.t, 0.97, x.type, transform=ax1.get_xaxis_transform(), fontsize=7, va="top")
    for r in false.itertuples():
        for ax in (ax1, ax2):
            ax.axvline(r.t, color=C_MISS, ls=":", lw=1.0)
        ax1.text(r.t, 0.85, "FALSE", transform=ax1.get_xaxis_transform(), fontsize=7, va="top", color=C_MISS)
    for r in auto.itertuples():
        ax1.plot(r.t, float(r.detail["accel_peak"]), marker="v", color=C_MISS, ms=8)
    ax1.set_ylabel("|a|")
    ax2.set_ylabel("|g|")
    ax2.set_xlabel("セッション開始からの時間 [s]")
    ax1.set_title(f"20:06 の 0〜420 s（最初の CAST は {first_cast:.0f} s、▼ = FALSE 申告された AUTO_CAST）", loc="left")
    ctx.save(fig, "fu_hook_overview.png")

    typical = c[(c["session"] == "session_20261031_193927")].head(2)
    rows = [(HOOK_SESSION, r.t, f"20:06 AUTO#{r.event_id} {r.t:.0f}s（FALSE）") for r in auto.itertuples()]
    rows += [(r.session, r.det_t, f"19:39 典型キャスト {r.cast_t:.0f}s") for r in typical.itertuples()]
    gallery_axes(ctx, rows, -4.0, 6.0, "fu_hook_gyro.png", "3 軸ジャイロ: FALSE 申告の 2 件（左）と典型的なキャスト（右）。0 = |a| ピーク")
    base._gallery_multi(ctx, rows, -10, 10, "fu_hook_mag.png", "|a| / |g|: FALSE 申告の 2 件（左）と典型的なキャスト（右）")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions-dir", default=str(ANALYSIS.parent / "captures" / "sessions"))
    ap.add_argument("--out", default=str(ANALYSIS / "reports" / "2026-10-31_field_review"))
    args = ap.parse_args()
    ctx = Ctx(Path(args.sessions_dir), Path(args.out))
    ctx.p("# 実釣データ（2026-10-31）再評価 — 自動生成の数値\n")
    ctx.p("`studies/field_review_20261031_followup.py` が生成。検知器・パラメータは変更していない（以下は what-if）\n")
    c = cast_table(ctx)
    sec_free_rig(ctx, c)
    sec_missed(ctx, c)
    sec_threshold(ctx)
    sec_features(ctx)
    sec_variants(ctx)
    sec_hook(ctx, c)
    (ctx.out / "followup_results.md").write_text("\n".join(ctx.md), encoding="utf-8")
    print(f"-> {ctx.out / 'followup_results.md'}")


if __name__ == "__main__":
    main()
