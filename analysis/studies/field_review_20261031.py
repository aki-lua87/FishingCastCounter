"""実釣データ（2026-10-31）での P1 検知器の評価。

    cd analysis
    .venv\\Scripts\\python.exe studies\\field_review_20261031.py [--out reports/2026-10-31_field_review]

対象: 根岸港 13:59 と鶴見川 19:39〜21:20 の 8 セッション。
磯子海づり施設の 2 セッション（15:33 / 16:11、アンダーキャスト指定）は動作条件が違うため読み込まない。

検知器・パラメータは変更しない（ウォッチ上の AUTO_CAST と PC 再検知が一致することを確認して、AUTO_CAST をそのまま使う）。

ラベルの扱い:
- CAST マーカーは正解として扱う。ただし「事後入力」（キャスト後に CAST → 直後に REEL）と
  「CAST 押し忘れ」（REEL だけ）がある
- REEL は押し忘れが多い。REEL が無いことを「巻いていない」とはみなさない
- FALSE はユーザーが「キャストしていないのに増えた」と申告したもの（ref_id = 対象 AUTO_CAST）。誤検知（申告）として扱う
- UNDO は直前の手動イベントの取り消し。castlab の読み込みで反映済み（元イベントは残る）
- マーカーの無い区間の検出は誤検知と確定しない（「誤検知候補」）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import linear_sum_assignment

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from castlab import plots  # noqa: E402
from castlab.config import load_config  # noqa: E402
from castlab.detector import DetectorParams, detect  # noqa: E402
from castlab.evaluate import cast_window_after  # noqa: E402
from castlab.pipeline import LoadedSession, load  # noqa: E402
from castlab.signals import Signals  # noqa: E402

plt = plots.plt

ANALYSIS = Path(__file__).resolve().parent.parent
SESSIONS = {
    "session_20261031_135912": "根岸港",
    "session_20261031_193927": "鶴見川",
    "session_20261031_195220": "鶴見川",
    "session_20261031_200612": "鶴見川",
    "session_20261031_202426": "鶴見川",
    "session_20261031_210209": "鶴見川",
    "session_20261031_211905": "鶴見川（移動）",
    "session_20261031_212035": "鶴見川",
}
EXCLUDED = ["session_20261031_153345", "session_20261031_161120"]  # 磯子海づり施設（アンダーキャスト）。読み込まない
LONG = "session_20261031_212035"

P1 = DetectorParams.load(ANALYSIS / "params" / "default.json")
# 特徴量を見るための緩い候補抽出（静止フィルタなし、近接 3 秒で間引き）。前回レビューと同じ
POOL = DetectorParams(accel_peak_min=15.0, gyro_peak_min=0.0, min_interval_s=3.0, post_gyro_mean_max=0.0)

# 事後入力: CAST の後この秒数以内に REEL がある（キャスト後に CAST → すぐ REEL と押した）
POSTHOC_REEL_S = 3.0
POSTHOC_BEFORE_S = 20.0
# REEL だけのキャスト: AUTO_CAST の後この範囲に REEL があり、CAST が無い
REEL_ONLY_RANGE = (2.0, 15.0)
# 短周期: 次の CAST までがこれ未満（失敗・やり直しの可能性）
SHORT_CYCLE_S = 35.0

C_CAST, C_OTHER, C_THIRD, C_BAD = "#2a78d6", "#eb6834", "#1baf7a", "#d03b3b"


class Ctx:
    def __init__(self, sessions_dir: Path, out: Path):
        self.out = out
        self.fig = out / "figures"
        self.fig.mkdir(parents=True, exist_ok=True)
        self.config = load_config(ANALYSIS / "sessions.toml")
        self.loaded: dict[str, LoadedSession] = {n: load(sessions_dir / f"{n}.csv") for n in SESSIONS}
        self.md: list[str] = []

    def h(self, text: str) -> None:
        self.md.append(f"\n## {text}\n")

    def p(self, text: str = "") -> None:
        self.md.append(text)

    def table(self, df: pd.DataFrame, floatfmt: str = ".2f") -> None:
        if df.empty:
            self.p("（なし）\n")
            return
        cols = list(df.columns)
        lines = ["| " + " | ".join(map(str, cols)) + " |", "|" + "---|" * len(cols)]
        for _, r in df.astype(object).iterrows():  # 行ごとに float へ揃えられて整数が小数表示になるのを防ぐ
            cells = []
            for c in cols:
                v = r[c]
                cells.append(format(v, floatfmt) if isinstance(v, (float, np.floating)) and not pd.isna(v) else
                             ("" if (not isinstance(v, str) and pd.isna(v)) else str(v)))
            lines.append("| " + " | ".join(cells) + " |")
        self.p("\n".join(lines) + "\n")

    def save(self, fig, name: str) -> None:
        fig.savefig(self.fig / name, dpi=90, bbox_inches="tight")
        plt.close(fig)
        self.p(f"![{name}](figures/{name})\n")

    def csv(self, df: pd.DataFrame, name: str) -> None:
        df.to_csv(self.out / name, index=False, encoding="utf-8")


def short(name: str) -> str:
    s = name.rsplit("_", 1)[-1]
    return f"{s[:2]}:{s[2:4]}"


def gwin(sig: Signals, arr: np.ndarray, t: float, a: float, b: float) -> np.ndarray:
    i0 = max(0, int(round((t + a - sig.t[0]) * sig.fs)))
    i1 = max(0, int(round((t + b - sig.t[0]) * sig.fs)))
    return np.nan_to_num(arr[i0:i1])


def pre_swing(sig: Signals, t: float) -> float:
    """振りかぶり: ピーク 4〜1 秒前の |g| 最大。"""
    w = gwin(sig, sig.gyro_mag, t, -4.0, -1.0)
    return float(w.max()) if len(w) else np.nan


# --- 照合 -------------------------------------------------------------------------------------------------


def match(markers: np.ndarray, dets: np.ndarray, before: np.ndarray, after: np.ndarray, expected: float):
    """マーカーごとの窓 [m − before, m + after] で 1 対 1 照合（castlab.evaluate と同じコスト）。"""
    if len(markers) == 0 or len(dets) == 0:
        return []
    dt = dets[None, :] - markers[:, None]
    ok = (dt >= -before[:, None]) & (dt <= after[:, None])
    cost = np.where(ok, np.abs(dt - expected), 1e9)
    r, c = linear_sum_assignment(cost)
    return [(int(i), int(j)) for i, j in zip(r, c) if ok[i, j]]


def session_tables(ctx: Ctx, name: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """CAST マーカーと AUTO_CAST の照合・分類。

    1 段目: sessions.toml の照合設定（−2〜+12 秒、REEL 上限 30 秒）
    2 段目: 事後入力の CAST（直後に REEL）は窓を [−20 秒, REEL] に広げる
    """
    ls = ctx.loaded[name]
    s, sig = ls.session, ls.signals
    m = ctx.config.matching
    ev = s.events
    auto = ev[ev["type"] == "AUTO_CAST"].reset_index(drop=True)
    mk = s.markers
    cast_t = mk.loc[mk["type"] == "CAST", "t"].to_numpy()
    reel_t = mk.loc[mk["type"] == "REEL", "t"].to_numpy()
    other_t = mk.loc[mk["type"].isin(["OTHER", "WALK"]), "t"].to_numpy()
    false_ref = set(ev.loc[(ev["type"] == "FALSE") & ev["active"], "ref_id"].dropna().astype(int))
    a_t = auto["t"].to_numpy()

    after = cast_window_after(mk, cast_t, m)
    before = np.full(len(cast_t), m.window_before_s)
    pairs = match(cast_t, a_t, before, after, m.expected_offset_s)

    next_reel = np.array([next((r - t for r in reel_t if r > t), np.nan) for t in cast_t])
    posthoc = next_reel <= POSTHOC_REEL_S
    used_c = {i for i, _ in pairs}
    used_a = {j for _, j in pairs}
    rest_c = [i for i in range(len(cast_t)) if i not in used_c and posthoc[i]]
    rest_a = [j for j in range(len(a_t)) if j not in used_a]
    pairs2 = []
    if rest_c and rest_a:
        sub = match(cast_t[rest_c], a_t[rest_a], np.full(len(rest_c), POSTHOC_BEFORE_S),
                    next_reel[rest_c], m.expected_offset_s)
        pairs2 = [(rest_c[i], rest_a[j]) for i, j in sub]

    # 未照合 CAST の周辺ピーク（緩い候補）
    pool = detect(sig, POOL)
    pool["pre_swing"] = [pre_swing(sig, t) for t in pool["t"]]

    rows = []
    a_of = {i: j for i, j in pairs} | {i: j for i, j in pairs2}
    for i, t in enumerate(cast_t):
        nxt_cast = cast_t[i + 1] - t if i + 1 < len(cast_t) else np.nan
        has_reel_before_next = not np.isnan(next_reel[i]) and (np.isnan(nxt_cast) or next_reel[i] < nxt_cast)
        row = dict(session=name, cast_t=t, next_reel_s=next_reel[i] if has_reel_before_next else np.nan,
                   next_cast_s=nxt_cast)
        if i in a_of:
            j = a_of[i]
            d = auto.iloc[j]["detail"]
            row |= dict(status="matched_posthoc" if (i, j) in pairs2 else "matched", det_t=a_t[j],
                        offset_s=a_t[j] - t, accel_peak=float(d["accel_peak"]), gyro_peak=float(d["gyro_peak"]),
                        post_gyro_mean=float(d["post_gyro_mean"]), pre_swing=pre_swing(sig, a_t[j]),
                        auto_event_id=int(auto.iloc[j]["event_id"]))
        else:
            hi = t + (after[i] if after[i] < m.window_after_s else 15.0)
            c = pool[(pool["t"] >= t - m.window_before_s) & (pool["t"] <= hi)]
            still = c[(c["post_gyro_mean"] <= P1.post_gyro_mean_max) & (c["gyro_peak"] >= P1.gyro_peak_min)]
            best = (still if len(still) else c).sort_values("accel_peak").tail(1)
            row["status"] = "missed"
            if len(best):
                b = best.iloc[0]
                row |= dict(det_t=b["t"], offset_s=b["t"] - t, accel_peak=b["accel_peak"], gyro_peak=b["gyro_peak"],
                            post_gyro_mean=b["post_gyro_mean"], pre_swing=b["pre_swing"])
                fails = []
                if b["accel_peak"] < P1.accel_peak_min:
                    fails.append("|a|")
                if b["gyro_peak"] < P1.gyro_peak_min:
                    fails.append("|g|")
                if b["post_gyro_mean"] > P1.post_gyro_mean_max:
                    fails.append("静止")
                row["fails"] = "+".join(fails)
        # 分類
        short_cycle = not np.isnan(nxt_cast) and nxt_cast < SHORT_CYCLE_S
        if row["status"] == "missed":
            if not has_reel_before_next and short_cycle:
                cls = "中止・やり直し候補（REEL なしで次の CAST）"
            elif row.get("fails") == "|a|":
                cls = "弱いキャスト（|a| 不足のみ）" + ("・短周期" if short_cycle else "")
            elif "accel_peak" in row:
                cls = f"条件不一致（{row.get('fails')}）" + ("・短周期" if short_cycle else "")
            else:
                cls = "候補ピークなし（|a|<15）"
        else:
            cls = "事後入力で一致" if row["status"] == "matched_posthoc" else "一致"
        row["class"] = cls
        rows.append(row)
    casts = pd.DataFrame(rows)

    drows = []
    matched_a = {j for _, j in pairs} | {j for _, j in pairs2}
    for j, r in auto.iterrows():
        d = r["detail"]
        t = r["t"]
        row = dict(session=name, event_id=int(r["event_id"]), det_t=t, accel_peak=float(d["accel_peak"]),
                   gyro_peak=float(d["gyro_peak"]), post_gyro_mean=float(d["post_gyro_mean"]),
                   pre_swing=pre_swing(sig, t))
        near = mk[(mk["t"] >= t - 30) & (mk["t"] <= t + 30)]
        row["nearby"] = " ".join(f"{x.type}{x.t - t:+.1f}" for x in near.itertuples())
        if j in matched_a:
            cls = "一致"
        elif int(r["event_id"]) in false_ref:
            cls = "誤検知（FALSE 申告）"
        else:
            reel_after = reel_t[(reel_t - t >= REEL_ONLY_RANGE[0]) & (reel_t - t <= REEL_ONLY_RANGE[1])]
            cast_near = cast_t[np.abs(cast_t - t) <= 30]
            oth = other_t[(other_t - t >= 0) & (other_t - t <= REEL_ONLY_RANGE[1])]
            if len(reel_after) and not len(cast_near) and len(oth):
                cls = "曖昧（CAST の代わりに OTHER？）"
            elif len(reel_after) and not len(cast_near):
                cls = "CAST 押し忘れ推定（REEL のみ）"
            elif not len(near):
                cls = "誤検知候補（前後 30 秒にマーカーなし）"
            else:
                cls = "誤検知候補（未分類）"
        row["class"] = cls
        drows.append(row)
    dets = pd.DataFrame(drows, columns=["session", "event_id", "det_t", "accel_peak", "gyro_peak", "post_gyro_mean",
                                        "pre_swing", "nearby", "class"])
    return casts, dets


# --- 各節 -------------------------------------------------------------------------------------------------


def sec_summary(ctx: Ctx) -> None:
    ctx.h("1. セッション別サマリー")
    rows = []
    for name, place in SESSIONS.items():
        s = ctx.loaded[name].session
        ev = s.events
        act = ev[ev["active"]]
        cnt = act["type"].value_counts()
        acc = s.accel["t"].to_numpy()
        gaps = int((np.diff(acc) > 0.2).sum())
        auto = ev[ev["type"] == "AUTO_CAST"]
        pc = detect(ctx.loaded[name].signals, P1)
        parity = "一致" if len(pc) == len(auto) and np.allclose(pc["t"].to_numpy(), auto["t"].to_numpy(), atol=1e-6) else "不一致"
        rows.append({
            "セッション": short(name), "場所": place, "記録 [分]": s.duration_s / 60,
            "ACCEL [Hz]": len(acc) / s.duration_s, "0.2s超の欠損": gaps,
            "CAST": int(cnt.get("CAST", 0)), "REEL": int(cnt.get("REEL", 0)),
            "OTHER/WALK": int(cnt.get("OTHER", 0) + cnt.get("WALK", 0)),
            "AUTO_CAST": len(auto), "FALSE": int(cnt.get("FALSE", 0)), "MISS": int(cnt.get("MISS", 0)),
            "UNDO": int((ev["type"] == "UNDO").sum()), "補正後": s.meta.get("corrected_cast_count", ""),
            "PC 再検知": parity,
        })
    df = pd.DataFrame(rows)
    tot = df.select_dtypes("number").sum()
    tot["ACCEL [Hz]"] = np.nan
    df.loc[len(df)] = {"セッション": "計", "場所": "", **tot.to_dict(), "補正後": "", "PC 再検知": ""}
    ctx.table(df, ".1f")
    ctx.csv(df, "summary.csv")


def sec_matching(ctx: Ctx, casts: pd.DataFrame, dets: pd.DataFrame) -> None:
    ctx.h("2. 照合結果")
    m = ctx.config.matching
    ctx.p(f"1 段目: `sessions.toml` の照合設定（CAST −{m.window_before_s:g}〜+{m.window_after_s:g} 秒、"
          f"REEL 上限 {m.reel_bound_max_s:g} 秒）。2 段目: CAST の {POSTHOC_REEL_S:g} 秒以内に REEL がある CAST"
          f"（事後入力）だけ窓を [−{POSTHOC_BEFORE_S:g} 秒, REEL] に広げる。\n")
    ctx.p("**CAST マーカーの分類**\n")
    ctx.table(casts.groupby(["class"]).size().rename("件数").reset_index())
    ctx.p("**AUTO_CAST の分類**\n")
    ctx.table(dets.groupby(["class"]).size().rename("件数").reset_index())

    ok = casts[casts["status"].str.startswith("matched")]
    ctx.p("**時間差（AUTO_CAST ピーク − CAST 押下）**\n")
    q = ok.groupby(ok["session"].map(short))["offset_s"].describe()[["count", "min", "25%", "50%", "75%", "max"]]
    q.loc["全体"] = ok["offset_s"].describe()[["count", "min", "25%", "50%", "75%", "max"]]
    ctx.table(q.reset_index().rename(columns={"session": "セッション"}))
    reel = ok.dropna(subset=["next_reel_s"])
    pr = (reel["next_reel_s"] - reel["offset_s"])
    ctx.p(f"- ピーク → 直後の REEL: 中央値 {pr.median():.1f} 秒（{pr.min():.1f}〜{pr.max():.1f}、n={len(pr)}）\n")

    # 窓の感度
    sweep = []
    for before in (2.0, 5.0, 10.0, 15.0, 20.0):
        for after_s in (8.0, 10.0, 12.0, 15.0, 20.0):
            for reel_bound in (False, True):
                n_match = 0
                for name in SESSIONS:
                    s = ctx.loaded[name].session
                    ct = s.markers.loc[s.markers["type"] == "CAST", "t"].to_numpy()
                    at = s.events.loc[s.events["type"] == "AUTO_CAST", "t"].to_numpy()
                    cfg = type(m)(**{**m.__dict__, "window_after_s": after_s,
                                     "reel_bound_max_s": m.reel_bound_max_s if reel_bound else 0.0})
                    aft = cast_window_after(s.markers, ct, cfg)
                    n_match += len(match(ct, at, np.full(len(ct), before), aft, m.expected_offset_s))
                sweep.append(dict(before_s=before, after_s=after_s, reel_bound=reel_bound, matched=n_match))
    sw = pd.DataFrame(sweep)
    ctx.csv(sw, "window_sweep.csv")
    piv = sw.pivot_table(index=["reel_bound", "before_s"], columns="after_s", values="matched").reset_index()
    ctx.p("**照合窓の感度**（一致数。CAST は全 {} 件）\n".format(len(casts)))
    ctx.table(piv, ".0f")

    fig, ax = plt.subplots(figsize=(9, 3.2))
    ax.hist(ok["offset_s"], bins=np.arange(-12, 14, 0.5), color=C_CAST)
    for x in (-m.window_before_s, m.window_after_s):
        ax.axvline(x, color=plots.INK_MUTED, ls="--", lw=0.8)
    ax.set_xlabel("AUTO_CAST ピーク − CAST 押下 [s]（破線: 照合窓 −2 / +12）")
    ax.set_ylabel("件数")
    ax.set_title("一致したキャストの時間差（負の値は事後入力）", loc="left")
    ctx.save(fig, "offsets.png")


def sec_candidates(ctx: Ctx, casts: pd.DataFrame, dets: pd.DataFrame) -> None:
    ctx.h("3. 未検知・誤検知の候補一覧")
    ctx.p("**未照合の CAST**（候補ピーク: CAST −2 秒〜REEL（無ければ +15 秒）の緩い候補のうち、"
          "静止・|g| 条件を満たす中で |a| 最大。満たすものが無ければ |a| 最大）\n")
    miss = casts[casts["status"] == "missed"].copy()
    miss.insert(0, "S", miss["session"].map(short))
    ctx.table(miss[["S", "cast_t", "next_reel_s", "next_cast_s", "offset_s", "accel_peak", "gyro_peak",
                    "post_gyro_mean", "pre_swing", "fails", "class"]], ".2f")
    ctx.p("**一致しなかった AUTO_CAST**\n")
    un = dets[dets["class"] != "一致"].copy()
    ph = casts[casts["status"] == "matched_posthoc"]
    un.insert(0, "S", un["session"].map(short))
    ctx.table(un[["S", "event_id", "det_t", "accel_peak", "gyro_peak", "post_gyro_mean", "pre_swing", "nearby", "class"]])
    if len(ph):
        ctx.p("**事後入力で一致した CAST**\n")
        ph = ph.assign(S=ph["session"].map(short))
        ctx.table(ph[["S", "cast_t", "det_t", "offset_s", "next_reel_s", "accel_peak", "post_gyro_mean"]])
    ctx.csv(casts, "cast_markers.csv")
    ctx.csv(dets, "auto_casts.csv")


def sec_waveforms(ctx: Ctx, casts: pd.DataFrame, dets: pd.DataFrame) -> None:
    ctx.h("4. 波形")
    miss = casts[casts["status"] == "missed"]
    rows = [(r["session"], r["cast_t"], f"{short(r['session'])} CAST {r['cast_t']:.0f}s\n{r['class']}")
            for _, r in miss.iterrows()]
    _gallery_multi(ctx, rows, -5, 18, "missed_casts.png", "未照合の CAST（0 = CAST 押下）")

    un = dets[dets["class"] != "一致"]
    rows = [(r["session"], r["det_t"], f"{short(r['session'])} AUTO#{r['event_id']} {r['det_t']:.0f}s\n{r['class']}")
            for _, r in un.iterrows()]
    _gallery_multi(ctx, rows, -12, 12, "unmatched_autos.png", "一致しなかった AUTO_CAST（0 = ピーク）")

    # 典型的な一致例と、20:24 の弱いキャスト
    ok = casts[casts["status"] == "matched"]
    typical = ok[ok["session"] == "session_20261031_193927"].head(3)
    weak = casts[casts["session"] == "session_20261031_202426"]
    rows = [(r.session, r.det_t if not np.isnan(r.det_t) else r.cast_t,
             f"{short(r.session)} {r.cast_t:.0f}s |a|={r.accel_peak:.1f}") for r in typical.itertuples()]
    rows += [(r.session, r.det_t if not np.isnan(r.det_t) else r.cast_t,
              f"{short(r.session)} {r.cast_t:.0f}s |a|={r.accel_peak:.1f}" + (" ✓" if r.status != "missed" else ""))
             for r in weak.itertuples()]
    _gallery_multi(ctx, rows, -6, 8, "typical_vs_2024.png",
                   "19:39 の典型的なキャスト（左 3 枚）と 20:24 の全 CAST（0 = 候補ピーク）")

    # 静止フィルタで落ちた強い動き・歩行
    w = [("session_20261031_212035", 1600.26, "21:20 1600s（|a|=44, |g|=19.7, 静止 1.96）"),
         ("session_20261031_212035", 3246.44, "21:20 3246s（|a|=38, 静止 2.34）"),
         ("session_20261031_211905", 21.5, "21:19 WALK 区間（|a|≈40, 静止 3.9）")]
    _gallery_multi(ctx, w, -10, 10, "rejected_strong.png", "|a| は大きいが静止フィルタで除外された動き")

    # 特徴量の散布図
    feat = []
    for name in SESSIONS:
        sig = ctx.loaded[name].signals
        pool = detect(sig, POOL)
        pool = pool[pool["accel_peak"] >= 18]
        for p in pool.itertuples():
            feat.append(dict(session=name, t=p.t, accel_peak=p.accel_peak, post=p.post_gyro_mean, gyro=p.gyro_peak))
    feat = pd.DataFrame(feat)
    cast_pk = casts.dropna(subset=["det_t"])
    def kind(r):
        c = cast_pk[(cast_pk["session"] == r.session) & (np.abs(cast_pk["det_t"] - r.t) < 0.05)]
        if len(c):
            return "CAST に対応するピーク"
        d = dets[(dets["session"] == r.session) & (np.abs(dets["det_t"] - r.t) < 0.05) & (dets["class"] != "一致")]
        if len(d):
            return "一致しなかった AUTO_CAST"
        return "その他のピーク"
    feat["kind"] = [kind(r) for r in feat.itertuples()]
    fig, ax = plt.subplots(figsize=(9, 5))
    for k, col, mk in (("その他のピーク", plots.INK_MUTED, "."), ("CAST に対応するピーク", C_CAST, "o"),
                       ("一致しなかった AUTO_CAST", C_OTHER, "D")):
        f = feat[feat["kind"] == k]
        ax.scatter(f["accel_peak"], f["post"].clip(upper=6), s=14 if mk == "." else 22, color=col, marker=mk,
                   label=f"{k}（{len(f)}）", alpha=0.8)
    ax.axvline(P1.accel_peak_min, color=plots.INK_MUTED, ls="--", lw=0.8)
    ax.axhline(P1.post_gyro_mean_max, color=plots.INK_MUTED, ls="--", lw=0.8)
    ax.set_xlabel("|a| ピーク [m/s²]（破線 25）")
    ax.set_ylabel("ピーク後 0.5〜3 s の |g| 平均 [rad/s]（6 で打ち切り。破線 1.5）")
    ax.set_title("候補ピークの特徴量（|a|≥18、8 セッション）。左下＝弱いが静止しているキャスト", loc="left")
    ax.legend(loc="upper right")
    ctx.save(fig, "feature_scatter.png")
    ctx.csv(feat, "candidate_pool.csv")

    # セッション別の CAST ピーク |a|
    fig, ax = plt.subplots(figsize=(9, 3.6))
    names = [n for n in SESSIONS if (casts["session"] == n).any()]
    for k, n in enumerate(names):
        c = casts[(casts["session"] == n)].dropna(subset=["accel_peak"])
        x = np.full(len(c), k) + np.linspace(-0.15, 0.15, len(c)) if len(c) > 1 else np.full(len(c), k)
        col = np.where(c["status"] == "missed", C_BAD, C_CAST)
        ax.scatter(x, c["accel_peak"], c=col, s=18)
    ax.axhline(P1.accel_peak_min, color=plots.INK_MUTED, ls="--", lw=0.8)
    ax.set_xticks(range(len(names)), [short(n) for n in names])
    ax.set_ylabel("|a| ピーク [m/s²]")
    ax.set_title("CAST ごとのピーク |a|（青 = 一致、赤 = 未照合の候補ピーク）", loc="left")
    ctx.save(fig, "cast_peaks_by_session.png")


def _gallery_multi(ctx: Ctx, rows: list[tuple[str, float, str]], pre: float, post: float, name: str, title: str) -> None:
    """複数セッションにまたがる |a| / |g| の小さな図。縦軸は全パネルで共通。"""
    if not rows:
        return
    cols = min(4, len(rows))
    nrow = int(np.ceil(len(rows) / cols))
    fig = plt.figure(figsize=(3.6 * cols, 3.3 * nrow + 0.5))
    outer = fig.add_gridspec(nrow, cols, hspace=0.55, wspace=0.22)
    r1 = r2 = None
    for k, (sname, t, label) in enumerate(rows):
        ls = ctx.loaded[sname]
        sig, mk = ls.signals, ls.session.markers
        dets = ls.session.events[ls.session.events["type"] == "AUTO_CAST"]
        r, c = divmod(k, cols)
        inner = outer[r, c].subgridspec(2, 1, hspace=0.06)
        ax1 = fig.add_subplot(inner[0], sharey=r1)
        ax2 = fig.add_subplot(inner[1], sharey=r2, sharex=ax1)
        r1, r2 = r1 or ax1, r2 or ax2
        s = sig.slice(t + pre, t + post)
        ax1.plot(s.t - t, s.accel_mag, color=plots.MAG_COLOR, lw=0.8)
        ax2.plot(s.t - t, s.gyro_mag, color=plots.MAG_COLOR, lw=0.8)
        ax1.axhline(P1.accel_peak_min, color=plots.INK_MUTED, ls="--", lw=0.7)
        ax2.axhline(P1.post_gyro_mean_max, color=plots.INK_MUTED, ls=":", lw=0.7)
        local = mk[(mk["t"] >= t + pre) & (mk["t"] <= t + post)]
        for ax in (ax1, ax2):
            for x in local.itertuples():
                ax.axvline(x.t - t, color=plots.INK, **plots.MARKER_STYLE.get(x.type, {}))
            ax.set_xlim(pre, post)
        for x in local.itertuples():
            ax1.text(x.t - t, 0.97, x.type, transform=ax1.get_xaxis_transform(), fontsize=7, va="top", ha="left")
        d = dets[(dets["t"] >= t + pre) & (dets["t"] <= t + post)]
        for x in d.itertuples():
            ax1.plot(x.t - t, float(x.detail["accel_peak"]), marker="v", color="#0ca30c", ms=7)
        ax1.set_title(label, fontsize=8, loc="left")
        ax1.tick_params(labelbottom=False)
        if c == 0:
            ax1.set_ylabel("|a|")
            ax2.set_ylabel("|g|")
    fig.suptitle(title + "　▼ = AUTO_CAST、破線 |a|=25、点線 |g|=1.5", x=0.01,
                 y=1.0 + 0.4 / fig.get_figheight(), va="bottom", ha="left", fontsize=11)
    fig.savefig(ctx.fig / name, dpi=80, bbox_inches="tight")
    plt.close(fig)
    ctx.p(f"![{name}](figures/{name})\n")


def sec_long(ctx: Ctx, casts: pd.DataFrame, dets: pd.DataFrame) -> None:
    ctx.h("5. 長時間セッション（21:20、66 分）の前半・中盤・後半")
    s = ctx.loaded[LONG].session
    edges = np.linspace(0, s.duration_s, 4)
    rows = []
    for k, part in enumerate(["前半", "中盤", "後半"]):
        a, b = edges[k], edges[k + 1]
        c = casts[(casts["session"] == LONG) & (casts["cast_t"] >= a) & (casts["cast_t"] < b)]
        d = dets[(dets["session"] == LONG) & (dets["det_t"] >= a) & (dets["det_t"] < b)]
        ok = c[c["status"] != "missed"]
        rows.append({"区間": part, "範囲 [分]": f"{a / 60:.0f}–{b / 60:.0f}", "CAST": len(c), "一致": len(ok),
                     "recall": len(ok) / len(c) if len(c) else np.nan, "AUTO_CAST": len(d),
                     "未照合 AUTO": int((d["class"] != "一致").sum()),
                     "一致 |a| 中央値": ok["accel_peak"].median(), "一致 |a| 最小": ok["accel_peak"].min(),
                     "時間差 中央値": ok["offset_s"].median(), "静止 中央値": ok["post_gyro_mean"].median(),
                     "CAST 間隔 中央値": c["next_cast_s"].median()})
    ctx.table(pd.DataFrame(rows))
    ctx.csv(pd.DataFrame(rows), "long_session_thirds.csv")

    sig = ctx.loaded[LONG].signals
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(16, 5), sharex=True)
    ax1.plot(sig.t / 60, sig.accel_mag, color=plots.MAG_COLOR, lw=0.4)
    ax2.plot(sig.t / 60, sig.gyro_mag, color=plots.MAG_COLOR, lw=0.4)
    c = casts[casts["session"] == LONG]
    for r in c.itertuples():
        ax1.axvline(r.cast_t / 60, color=C_BAD if r.status == "missed" else plots.INK_MUTED, lw=0.8,
                    ls="-" if r.status == "missed" else ":")
    d = dets[dets["session"] == LONG]
    ax1.scatter(d["det_t"] / 60, d["accel_peak"], c=np.where(d["class"] == "一致", "#0ca30c", C_OTHER), s=16, zorder=3)
    ax1.axhline(P1.accel_peak_min, color=plots.INK_MUTED, ls="--", lw=0.8)
    for x in edges[1:-1]:
        for ax in (ax1, ax2):
            ax.axvline(x / 60, color=plots.INK, lw=1.2)
    ax1.set_ylabel("|a|")
    ax2.set_ylabel("|g|")
    ax2.set_xlabel("分")
    ax1.set_title("21:20 全体（点線 = CAST、赤線 = 未照合 CAST、緑点 = 一致した AUTO、橙点 = 一致しない AUTO、黒線 = 3 等分）",
                  loc="left")
    ctx.save(fig, "long_overview.png")


def sec_metrics(ctx: Ctx, casts: pd.DataFrame, dets: pd.DataFrame) -> None:
    ctx.h("6. Precision / Recall / F1（暫定）")
    ctx.p("- **TP** = CAST と一致（事後入力を含む）。**FN** = 未照合の CAST\n"
          "- Precision の下限: 一致しない AUTO_CAST をすべて誤検知とみなす\n"
          "- Precision の上限: FALSE 申告だけを誤検知とみなす（REEL のみ・曖昧・マーカーなしは正しい検知の可能性を残す）\n"
          "- Recall（中止除く）: 「中止・やり直し候補」の CAST を分母から除く\n")
    rows = []
    groups = [(short(n), [n]) for n in SESSIONS] + [("計", list(SESSIONS)),
                                                     ("計（20:24 を除く）", [n for n in SESSIONS if not n.endswith("202426")])]
    for label, names in groups:
        c = casts[casts["session"].isin(names)]
        d = dets[dets["session"].isin(names)]
        tp = int((c["status"] != "missed").sum())
        fn = int((c["status"] == "missed").sum())
        abort = int(c["class"].str.startswith("中止").sum())
        n_auto = len(d)
        fp_hi = int((d["class"] != "一致").sum())
        fp_lo = int((d["class"] == "誤検知（FALSE 申告）").sum())
        rec = tp / (tp + fn) if tp + fn else np.nan
        rec2 = tp / (tp + fn - abort) if tp + fn - abort else np.nan
        p_lo = tp / n_auto if n_auto else np.nan
        # 上限: 一致以外のうち FALSE 以外は正しい検知とみなす（ただし分母の正解側にも足す）
        tp_hi = n_auto - fp_lo
        p_hi = tp_hi / n_auto if n_auto else np.nan
        f1_lo = 2 * p_lo * rec / (p_lo + rec) if n_auto and tp + fn else np.nan
        rec_hi = tp_hi / (tp_hi + fn) if tp_hi + fn else np.nan
        f1_hi = 2 * p_hi * rec_hi / (p_hi + rec_hi) if n_auto and tp_hi + fn else np.nan
        rows.append({"セッション": label, "CAST": tp + fn, "TP": tp, "FN": fn, "中止候補": abort, "AUTO": n_auto,
                     "未照合 AUTO": fp_hi, "うち FALSE": fp_lo, "Recall": rec, "Recall（中止除く）": rec2,
                     "Precision 下限": p_lo, "Precision 上限": p_hi, "F1 下限": f1_lo, "F1 上限": f1_hi})
    df = pd.DataFrame(rows)
    ctx.table(df, ".3f")
    ctx.csv(df, "metrics.csv")


def _score(ctx: Ctx, params: DetectorParams) -> dict:
    """PC で再検知し、session_tables と同じ 2 段の照合で数える（FALSE 申告の対象は時刻で判定）。"""
    m = ctx.config.matching
    tp = fn = n_det = n_false = n_walk = 0
    for name in SESSIONS:
        ls = ctx.loaded[name]
        s = ls.session
        det = detect(ls.signals, params)["t"].to_numpy()
        n_det += len(det)
        if name.endswith("211905"):
            n_walk += len(det)
        mk = s.markers
        ct = mk.loc[mk["type"] == "CAST", "t"].to_numpy()
        rt = mk.loc[mk["type"] == "REEL", "t"].to_numpy()
        aft = cast_window_after(mk, ct, m)
        pairs = match(ct, det, np.full(len(ct), m.window_before_s), aft, m.expected_offset_s)
        nr = np.array([next((r - t for r in rt if r > t), np.nan) for t in ct])
        rc = [i for i in range(len(ct)) if i not in {p[0] for p in pairs} and nr[i] <= POSTHOC_REEL_S]
        ra = [j for j in range(len(det)) if j not in {p[1] for p in pairs}]
        n2 = len(match(ct[rc], det[ra], np.full(len(rc), POSTHOC_BEFORE_S), nr[rc], m.expected_offset_s)) if rc and ra else 0
        tp += len(pairs) + n2
        fn += len(ct) - len(pairs) - n2
        ev = s.events
        refs = ev.loc[(ev["type"] == "FALSE") & ev["active"], "ref_id"].dropna().astype(int)
        ft = ev.loc[ev["event_id"].isin(refs), "t"].to_numpy()
        n_false += int(sum(np.any(np.abs(det - t) < 0.05) for t in ft))
    return dict(TP=tp, FN=fn, 検出=n_det, 未照合=n_det - tp, うちFALSE対象=n_false, 移動中の検出=n_walk,
                Recall=tp / (tp + fn), 一致率=tp / n_det if n_det else np.nan)


def sec_whatif(ctx: Ctx, casts: pd.DataFrame) -> None:
    ctx.h("7. 参考: パラメータを変えた場合（what-if。採用はしない）")
    ctx.p("この 8 セッションを見たうえでの試算なので、ここで選んだ値をこのデータで評価してはいけない（tune / eval の分離）。\n")
    ok = casts[casts["status"] != "missed"]
    ctx.p("**一致したキャストの特徴量の分布**\n")
    ctx.table(ok[["accel_peak", "gyro_peak", "post_gyro_mean", "pre_swing", "offset_s"]].describe()
              .T.reset_index().rename(columns={"index": "特徴量"}))
    rows = []
    grid = [(a, 4.0, post) for a in (25.0, 24.0, 23.0, 22.0, 21.0, 20.0) for post in (1.5, 1.0)]
    grid += [(25.0, 6.0, 1.5), (22.0, 5.5, 1.5), (21.0, 5.5, 1.5), (21.0, 6.0, 1.5)]
    for a, g, post in grid:
        r = _score(ctx, P1.with_overrides([f"accel_peak_min={a}", f"gyro_peak_min={g}", f"post_gyro_mean_max={post}"]))
        rows.append({"accel_peak_min": a, "gyro_peak_min": g, "post_gyro_mean_max": post, **r})
    df = pd.DataFrame(rows)
    ctx.table(df, ".3f")
    ctx.csv(df, "whatif_threshold.csv")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions-dir", default=str(ANALYSIS.parent / "captures" / "sessions"))
    ap.add_argument("--out", default=str(ANALYSIS / "reports" / "2026-10-31_field_review"))
    args = ap.parse_args()
    ctx = Ctx(Path(args.sessions_dir), Path(args.out))
    ctx.p("# 実釣データ（2026-10-31）での P1 評価 — 自動生成の数値\n")
    ctx.p(f"`studies/field_review_20261031.py` が生成。検知器は `params/default.json`（P1）。"
          f"除外: {', '.join(EXCLUDED)}（アンダーキャスト）\n")
    casts, dets = [], []
    for name in SESSIONS:
        c, d = session_tables(ctx, name)
        casts.append(c)
        dets.append(d)
    casts = pd.concat(casts, ignore_index=True)
    dets = pd.concat(dets, ignore_index=True)
    sec_summary(ctx)
    sec_matching(ctx, casts, dets)
    sec_candidates(ctx, casts, dets)
    sec_waveforms(ctx, casts, dets)
    sec_long(ctx, casts, dets)
    sec_metrics(ctx, casts, dets)
    sec_whatif(ctx, casts)
    (ctx.out / "results.md").write_text("\n".join(ctx.md), encoding="utf-8")
    print(f"→ {ctx.out / 'results.md'}")


if __name__ == "__main__":
    main()
