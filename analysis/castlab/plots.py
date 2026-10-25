"""波形・マーカー・検出結果のグラフ化（matplotlib, PNG 出力）。

配色:
- XYZ 軸はカテゴリ色 3 色（青・橙・緑系）。凡例あり
- 合成値 |a|, |g| は 1 系列ずつ別パネル（2 軸グラフにしない）
- マーカー種別は線種 + 文字ラベルで区別（色だけに頼らない）
- 検出の判定はステータス色 + 記号 + 文字で区別
"""

from __future__ import annotations

import logging
import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from .config import MatchingConfig  # noqa: E402
from .detector import DetectorParams  # noqa: E402
from .evaluate import cast_window_after  # noqa: E402
from .signals import Signals  # noqa: E402

AXIS_COLORS = {"x": "#2a78d6", "y": "#eb6834", "z": "#1baf7a"}
MAG_COLOR = "#2a78d6"
INK = "#0b0b0b"
INK_2 = "#52514e"
INK_MUTED = "#8a8985"
GRID = "#e4e3df"
SURFACE = "#fcfcfb"

MARKER_STYLE = {
    "CAST": dict(ls="-", lw=1.4),
    "REEL": dict(ls="--", lw=1.0),
    "WALK": dict(ls=":", lw=1.2),
    "OTHER": dict(ls="-.", lw=1.0),
}

# 検出ステータス: 色, 記号, 表示ラベル
STATUS_STYLE = {
    "matched": ("#0ca30c", "v", "✓ 一致"),
    "false_positive": ("#d03b3b", "X", "✗ 誤検出"),
    "unconfirmed": ("#fab219", "D", "? 誤検出候補"),
    "unlabeled": (INK_MUTED, "o", "· 検出（ラベルなし）"),
    "excluded": (INK_MUTED, "s", "除外"),
}
STATUS_EDGE = {"unconfirmed": "#8a5a00"}

_FONT_CANDIDATES = ["Yu Gothic", "Meiryo", "MS Gothic", "Hiragino Sans", "Noto Sans CJK JP", "IPAexGothic",
                    "Segoe UI Symbol", "DejaVu Sans"]
_installed = {f.name for f in font_manager.fontManager.ttflist}
logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)

plt.rcParams.update(
    {
        "font.family": [f for f in _FONT_CANDIDATES if f in _installed] or ["DejaVu Sans"],
        "axes.facecolor": SURFACE,
        "figure.facecolor": SURFACE,
        "axes.edgecolor": GRID,
        "axes.labelcolor": INK_2,
        "axes.grid": True,
        "grid.color": GRID,
        "grid.linewidth": 0.6,
        "xtick.color": INK_2,
        "ytick.color": INK_2,
        "axes.titlesize": 10,
        "axes.titlecolor": INK,
        "axes.labelsize": 9,
        "xtick.labelsize": 8,
        "ytick.labelsize": 8,
        "legend.fontsize": 8,
        "legend.frameon": False,
        "axes.unicode_minus": False,
    }
)


def _draw_markers(ax, markers: pd.DataFrame, t0: float, t1: float, label: bool, matching: MatchingConfig | None):
    after = {}
    if matching is not None:
        cast_t = markers.loc[markers["type"] == "CAST", "t"].to_numpy()
        after = dict(zip(cast_t, cast_window_after(markers, cast_t, matching)))
    for _, row in markers.iterrows():
        t = row["t"]
        if matching is not None and row["type"] == "CAST":
            a, b = t - matching.window_before_s, t + after[t]
            if b >= t0 and a <= t1:
                ax.axvspan(max(a, t0), min(b, t1), color="#cde2fb", alpha=0.35, lw=0, zorder=0)
        if not (t0 <= t <= t1):
            continue
        ax.axvline(t, color=INK_2, zorder=1, **MARKER_STYLE.get(row["type"], MARKER_STYLE["OTHER"]))
        if label:
            ax.text(t, 1.0, f" {row['type']}", transform=ax.get_xaxis_transform(), va="bottom", ha="left",
                    fontsize=8, color=INK, clip_on=False)


def _draw_detections(ax, detections: pd.DataFrame | None, y_col: str, t0: float, t1: float):
    if detections is None or detections.empty:
        return
    d = detections[(detections["t"] >= t0) & (detections["t"] <= t1)]
    status_col = d["status"] if "status" in d else pd.Series("unlabeled", index=d.index)
    for status, group in d.groupby(status_col):
        color, mk, _ = STATUS_STYLE.get(status, STATUS_STYLE["unlabeled"])
        ax.scatter(group["t"], group[y_col], marker=mk, s=70, color=color, zorder=5,
                   edgecolors=STATUS_EDGE.get(status, SURFACE), linewidths=1.2)


def _status_legend(ax, detections: pd.DataFrame | None, missed: pd.DataFrame | None):
    handles = []
    present = set(detections["status"]) if detections is not None and "status" in detections else set()
    for status in STATUS_STYLE:
        if status in present:
            color, mk, lab = STATUS_STYLE[status]
            handles.append(ax.scatter([], [], marker=mk, s=60, color=color, edgecolors=STATUS_EDGE.get(status, SURFACE),
                                      label=lab))
    if missed is not None and len(missed):
        handles.append(ax.scatter([], [], marker="$✗$", s=80, color="#d03b3b", label="✗ 未検出 CAST"))
    if handles:
        ax.legend(handles=handles, loc="upper left", ncol=len(handles))


def plot_window(
    sig: Signals,
    markers: pd.DataFrame,
    path: Path,
    t0: float,
    t1: float,
    detections: pd.DataFrame | None = None,
    missed_markers: pd.DataFrame | None = None,
    params: DetectorParams | None = None,
    matching: MatchingConfig | None = None,
    title: str = "",
    width: float = 16.0,
) -> Path:
    """4 段（加速度 XYZ / ジャイロ XYZ / |a| / |g|）の時系列グラフを保存する。"""
    s = sig.slice(t0, t1)
    fig, axes = plt.subplots(4, 1, figsize=(width, 9.5), sharex=True, gridspec_kw=dict(hspace=0.25))
    ax_a, ax_g, ax_am, ax_gm = axes

    for i, c in enumerate("xyz"):
        ax_a.plot(s.t, s.accel[:, i], color=AXIS_COLORS[c], lw=0.9, label=f"accel {c}")
        ax_g.plot(s.t, s.gyro[:, i], color=AXIS_COLORS[c], lw=0.9, label=f"gyro {c}")
    ax_a.set_ylabel("加速度 [m/s²]")
    ax_g.set_ylabel("角速度 [rad/s]")
    ax_a.legend(loc="upper right", ncol=3)
    ax_g.legend(loc="upper right", ncol=3)

    det = detections

    ax_am.plot(s.t, s.accel_mag, color=MAG_COLOR, lw=0.9)
    ax_am.set_ylabel("|a| [m/s²]")
    ax_gm.plot(s.t, s.gyro_mag, color=MAG_COLOR, lw=0.9)
    ax_gm.set_ylabel("|g| [rad/s]")
    if params is not None:
        ax_am.axhline(params.accel_peak_min, color=INK_MUTED, ls="--", lw=0.8)
        ax_am.text(1.0, params.accel_peak_min, f" 閾値 {params.accel_peak_min:g}", transform=ax_am.get_yaxis_transform(),
                   va="center", ha="left", fontsize=8, color=INK_2, clip_on=False)
        ax_gm.axhline(params.gyro_peak_min, color=INK_MUTED, ls="--", lw=0.8)
        ax_gm.text(1.0, params.gyro_peak_min, f" 閾値 {params.gyro_peak_min:g}", transform=ax_gm.get_yaxis_transform(),
                   va="center", ha="left", fontsize=8, color=INK_2, clip_on=False)

    for k, ax in enumerate(axes):
        _draw_markers(ax, markers, t0, t1, label=(k == 0), matching=matching)
    _draw_detections(ax_am, det, "accel_peak", t0, t1)
    if missed_markers is not None and len(missed_markers):
        mm = missed_markers[(missed_markers["marker_t"] >= t0) & (missed_markers["marker_t"] <= t1)]
        for t in mm["marker_t"]:
            ax_am.text(t, 0.92, "✗ 未検出", transform=ax_am.get_xaxis_transform(), color="#d03b3b",
                       fontsize=9, ha="left", va="top", fontweight="bold")
    _status_legend(ax_am, det, missed_markers)

    ax_gm.set_xlabel("セッション開始からの時間 [s]")
    ax_gm.set_xlim(t0, t1)
    fig.suptitle(title, x=0.01, ha="left", fontsize=12, color=INK)
    fig.text(0.99, 0.985, "縦線: CAST ─ / REEL ╌ / WALK ┈ / OTHER ─·   薄青帯: CAST 照合窓", ha="right", va="top",
             fontsize=8, color=INK_2)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=90, bbox_inches="tight")
    plt.close(fig)
    return path


def plot_session_pages(sig: Signals, markers: pd.DataFrame, out_dir: Path, page_s: float, prefix: str = "page",
                       **kwargs) -> list[Path]:
    """セッション全体を page_s 秒ごとのページに分けて保存する。"""
    paths = []
    start, end = float(sig.t[0]), float(sig.t[-1])
    n = max(1, math.ceil((end - start) / page_s))
    title = kwargs.pop("title", "")
    for k in range(n):
        t0 = start + k * page_s
        t1 = min(end, t0 + page_s)
        paths.append(plot_window(sig, markers, out_dir / f"{prefix}_{k + 1:03d}.png", t0, t1,
                                 title=f"{title}  [{t0:.0f}–{t1:.0f} s]  ({k + 1}/{n})", **kwargs))
    return paths


def plot_overview(sig: Signals, markers: pd.DataFrame, path: Path, detections: pd.DataFrame | None = None,
                  params: DetectorParams | None = None, title: str = "") -> Path:
    """全体像（|a| と |g| のみ、マーカーと検出つき）を 1 枚で。"""
    fig, (ax_am, ax_gm) = plt.subplots(2, 1, figsize=(18, 5.5), sharex=True, gridspec_kw=dict(hspace=0.2))
    ax_am.plot(sig.t, sig.accel_mag, color=MAG_COLOR, lw=0.5)
    ax_gm.plot(sig.t, sig.gyro_mag, color=MAG_COLOR, lw=0.5)
    ax_am.set_ylabel("|a| [m/s²]")
    ax_gm.set_ylabel("|g| [rad/s]")
    t0, t1 = float(sig.t[0]), float(sig.t[-1])
    for k, ax in enumerate((ax_am, ax_gm)):
        _draw_markers(ax, markers, t0, t1, label=False, matching=None)
    if detections is not None and not detections.empty:
        _draw_detections(ax_am, detections, "accel_peak", t0, t1)
        _status_legend(ax_am, detections, None)
    if params is not None:
        ax_am.axhline(params.accel_peak_min, color=INK_MUTED, ls="--", lw=0.8)
        ax_gm.axhline(params.gyro_peak_min, color=INK_MUTED, ls="--", lw=0.8)
    ax_gm.set_xlim(t0, t1)
    ax_gm.set_xlabel("セッション開始からの時間 [s]")
    fig.suptitle(title, x=0.01, ha="left", fontsize=12, color=INK)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=80, bbox_inches="tight")
    plt.close(fig)
    return path


def plot_event_gallery(sig: Signals, events: pd.DataFrame, markers: pd.DataFrame, path: Path, pre: float, post: float,
                       detections: pd.DataFrame | None = None, params: DetectorParams | None = None,
                       title: str = "", cols: int = 5) -> Path | None:
    """イベント（列: t, label）ごとに [t-pre, t+post] の |a| / |g| を小さな図で並べる。"""
    if events.empty:
        return None
    n = len(events)
    cols = min(cols, n)
    rows = math.ceil(n / cols)
    fig = plt.figure(figsize=(3.4 * cols, 3.3 * rows + 0.6))
    outer = fig.add_gridspec(rows, cols, hspace=0.42, wspace=0.22)
    ax_ref1 = ax_ref2 = None
    for k, (_, ev) in enumerate(events.iterrows()):
        r, c = divmod(k, cols)
        inner = outer[r, c].subgridspec(2, 1, hspace=0.06)
        # 同じ図の中では縦軸を揃え、波形の大きさを比較できるようにする
        ax1 = fig.add_subplot(inner[0], sharey=ax_ref1)
        ax2 = fig.add_subplot(inner[1], sharey=ax_ref2, sharex=ax1)
        ax_ref1, ax_ref2 = ax_ref1 or ax1, ax_ref2 or ax2
        t = ev["t"]
        s = sig.slice(t - pre, t + post)
        rel = s.t - t
        ax1.plot(rel, s.accel_mag, color=MAG_COLOR, lw=0.9)
        ax2.plot(rel, s.gyro_mag, color=MAG_COLOR, lw=0.9)
        for ax in (ax1, ax2):
            local = markers[(markers["t"] >= t - pre) & (markers["t"] <= t + post)]
            _draw_markers(ax, local.assign(t=local["t"] - t), -pre, post, label=False, matching=None)
            ax.set_xlim(-pre, post)
        if detections is not None and not detections.empty:
            d = detections[(detections["t"] >= t - pre) & (detections["t"] <= t + post)]
            _draw_detections(ax1, d.assign(t=d["t"] - t), "accel_peak", -pre, post)
        if params is not None:
            ax1.axhline(params.accel_peak_min, color=INK_MUTED, ls="--", lw=0.7)
            ax2.axhline(params.gyro_peak_min, color=INK_MUTED, ls="--", lw=0.7)
        ax1.set_title(ev["label"], fontsize=9, loc="left")
        ax1.tick_params(labelbottom=False)
        if c == 0:
            ax1.set_ylabel("|a| [m/s²]")
            ax2.set_ylabel("|g| [rad/s]")
    # 各パネルのタイトル（2 行まで）と重ならないよう、図の上端より 0.5 インチ上に置く
    fig.suptitle(title, x=0.01, y=1.0 + 0.5 / fig.get_figheight(), va="bottom", ha="left", fontsize=12, color=INK)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=80, bbox_inches="tight")
    plt.close(fig)
    return path


def plot_epochs(epochs: dict[str, list[tuple[np.ndarray, np.ndarray, np.ndarray]]], path: Path, title: str = "",
                xlabel: str = "マーカーからの時間 [s]") -> Path:
    """マーカー種別ごとに切り出した波形の重ね描き（列 = 種別、行 = |a| / |g|）。

    epochs[type] = [(rel_t, accel_mag, gyro_mag), ...]
    """
    types = [k for k, v in epochs.items() if v]
    fig, axes = plt.subplots(2, len(types), figsize=(4.2 * len(types), 6), squeeze=False, sharey="row", sharex=True,
                             gridspec_kw=dict(hspace=0.12, wspace=0.08))
    for c, typ in enumerate(types):
        ax1, ax2 = axes[0, c], axes[1, c]
        items = epochs[typ]
        for rel, am, gm in items:
            ax1.plot(rel, am, color=INK_MUTED, lw=0.6, alpha=0.45)
            ax2.plot(rel, gm, color=INK_MUTED, lw=0.6, alpha=0.45)
        grid = items[0][0]
        if all(len(it[0]) == len(grid) for it in items):
            ax1.plot(grid, np.nanmedian([it[1] for it in items], axis=0), color=MAG_COLOR, lw=2, label="中央値")
            ax2.plot(grid, np.nanmedian([it[2] for it in items], axis=0), color=MAG_COLOR, lw=2, label="中央値")
        ax1.set_title(f"{typ}（n={len(items)}）", loc="left")
        ax1.axvline(0, color=INK_2, lw=1)
        ax2.axvline(0, color=INK_2, lw=1)
        ax2.set_xlabel(xlabel)
        if c == 0:
            ax1.set_ylabel("|a| [m/s²]")
            ax2.set_ylabel("|g| [rad/s]")
            ax1.legend(loc="upper left", handles=[
                plt.Line2D([], [], color=INK_MUTED, lw=1, label="個々の波形"),
                plt.Line2D([], [], color=MAG_COLOR, lw=2, label="中央値"),
            ])
    fig.suptitle(title, x=0.01, ha="left", fontsize=12, color=INK)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=90, bbox_inches="tight")
    plt.close(fig)
    return path


def plot_axis_epochs(epochs: list[tuple[np.ndarray, np.ndarray, np.ndarray]], path: Path, title: str = "",
                     xlabel: str = "マーカーからの時間 [s]") -> Path:
    """1 種別の切り出し波形を XYZ 軸別に重ね描き（行 = accel/gyro, 列 = x/y/z）。

    epochs = [(rel_t, accel(N,3), gyro(N,3)), ...]
    """
    fig, axes = plt.subplots(2, 3, figsize=(15, 6), sharex=True, sharey="row", gridspec_kw=dict(hspace=0.12, wspace=0.08))
    for c, axis in enumerate("xyz"):
        for rel, acc, gyr in epochs:
            axes[0, c].plot(rel, acc[:, c], color=AXIS_COLORS[axis], lw=0.6, alpha=0.35)
            axes[1, c].plot(rel, gyr[:, c], color=AXIS_COLORS[axis], lw=0.6, alpha=0.35)
        axes[0, c].set_title(f"accel {axis}", loc="left")
        axes[1, c].set_title(f"gyro {axis}", loc="left")
        axes[1, c].set_xlabel(xlabel)
        for r in range(2):
            axes[r, c].axvline(0, color=INK_2, lw=1)
    axes[0, 0].set_ylabel("加速度 [m/s²]")
    axes[1, 0].set_ylabel("角速度 [rad/s]")
    fig.suptitle(title, x=0.01, ha="left", fontsize=12, color=INK)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=90, bbox_inches="tight")
    plt.close(fig)
    return path


def plot_device_overview(sig: Signals, markers: pd.DataFrame, auto: pd.DataFrame, misses: pd.DataFrame,
                         pc: pd.DataFrame, path: Path, title: str = "") -> Path:
    """ウォッチのイベント全体図: AUTO_CAST（FALSE 申告つきは ✗）、MISS の探索範囲、PC 再検知との差。"""
    fig, (ax_am, ax_gm) = plt.subplots(2, 1, figsize=(18, 5.5), sharex=True, gridspec_kw=dict(hspace=0.2))
    ax_am.plot(sig.t, sig.accel_mag, color=MAG_COLOR, lw=0.5)
    ax_gm.plot(sig.t, sig.gyro_mag, color=MAG_COLOR, lw=0.5)
    t0, t1 = float(sig.t[0]), float(sig.t[-1])
    for ax in (ax_am, ax_gm):
        _draw_markers(ax, markers, t0, t1, label=False, matching=None)
        for r in misses.itertuples():
            ax.axvspan(r.window_start_t, r.pressed_t, color="#fab219", alpha=0.18, lw=0)
            ax.axvline(r.estimated_t, color="#8a5a00", lw=1, ls="--")
    handles = []
    if len(auto):
        ok = auto[auto["false_event"].isna()]
        ng = auto[auto["false_event"].notna()]
        handles.append(ax_am.scatter(ok["t"], ok["accel_peak"], marker="v", s=60, color="#0ca30c", edgecolors=SURFACE,
                                     zorder=5, label="AUTO_CAST"))
        if len(ng):
            handles.append(ax_am.scatter(ng["t"], ng["accel_peak"], marker="X", s=80, color="#d03b3b",
                                         edgecolors=SURFACE, zorder=6, label="AUTO_CAST（FALSE 申告）"))
    if len(pc):
        diff = pc[pc["source"] != "both"]
        if len(diff):
            handles.append(ax_am.scatter(diff["t"], diff["accel_peak"] + 3, marker="o", s=50, facecolors="none",
                                         edgecolors=INK, zorder=7, label="PC 再検知と不一致"))
    if len(misses):
        handles.append(plt.Rectangle((0, 0), 1, 1, color="#fab219", alpha=0.3, label="MISS 探索範囲（押下前 30 s）"))
    if handles:
        ax_am.legend(handles=handles, loc="upper left", ncol=len(handles))
    ax_am.set_ylabel("|a| [m/s²]")
    ax_gm.set_ylabel("|g| [rad/s]")
    ax_gm.set_xlim(t0, t1)
    ax_gm.set_xlabel("セッション開始からの時間 [s]")
    fig.suptitle(title, x=0.01, ha="left", fontsize=12, color=INK)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=80, bbox_inches="tight")
    plt.close(fig)
    return path
