"""セッションごとのラベル信頼度・役割（調整用 / 評価用）と照合設定の読み込み。

設定ファイル（既定: analysis/sessions.toml）の例:

    [matching]
    window_before_s = 2.0
    window_after_s = 12.0
    expected_offset_s = 5.0

    [sessions.session_20261025_150936]
    label = "complete"
    role = "tune"
    note = "UNFIX S86M-5 30 キャスト"
    exclude = [[0.0, 10.0]]

    [sessions.session_20261018_154843]
    ignore = true
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

LABEL_LEVELS = ("complete", "likely", "uncertain", "none")
"""
- complete : 全キャストに CAST マーカーがある。未照合の検出は誤検出として確定してよい
- likely   : マーカー数は実キャスト数とほぼ一致すると思われる。再現率は参考値、未照合検出は「誤検出候補」
- uncertain: 誤タップ・マーカー漏れ・記憶違いの可能性がある。件数のみ表示し、率は算出しない
- none     : マーカーなし（または使わない）。検出の一覧のみ。正解として扱わない
"""

ROLES = ("tune", "eval")

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "sessions.toml"


@dataclass(frozen=True)
class MatchingConfig:
    window_before_s: float = 2.0
    """検出が CAST マーカーより最大何秒前まで照合対象にするか"""
    window_after_s: float = 12.0
    """検出が CAST マーカーより最大何秒後まで照合対象にするか"""
    expected_offset_s: float = 5.0
    """1 対 1 割り当てで優先する「マーカー→検出」の典型的な遅れ（秒）"""
    reel_bound_max_s: float = 0.0
    """>0 のとき、CAST の後（次の CAST より前）この秒数以内に REEL マーカーがあれば、
    照合窓の終わりを window_after_s ではなくその REEL の時刻にする（キャストは巻き始めより前に起きるため）。0 で無効"""


@dataclass(frozen=True)
class SessionConfig:
    name: str
    label: str
    role: str
    note: str = ""
    exclude: tuple[tuple[float, float], ...] = ()
    explicit: bool = False
    """設定ファイルに記載があるか（False は既定値からの推定）"""


@dataclass
class Config:
    matching: MatchingConfig = field(default_factory=MatchingConfig)
    sessions: dict[str, dict] = field(default_factory=dict)
    path: Path | None = None

    def is_ignored(self, name: str) -> bool:
        """ignore = true のセッションはどのコマンドでも読み込まない（動作確認用の記録など）。"""
        return bool(self.sessions.get(name, {}).get("ignore", False))

    def session(self, name: str, has_cast_markers: bool) -> SessionConfig:
        d = self.sessions.get(name)
        if d is None:
            return SessionConfig(
                name=name,
                label="uncertain" if has_cast_markers else "none",
                role="eval",
            )
        label = d.get("label", "uncertain" if has_cast_markers else "none")
        role = d.get("role", "eval")
        if label not in LABEL_LEVELS:
            raise ValueError(f"{name}: label は {LABEL_LEVELS} のいずれか: {label}")
        if role not in ROLES:
            raise ValueError(f"{name}: role は {ROLES} のいずれか: {role}")
        if label != "none" and not has_cast_markers:
            label = "none"
        exclude = tuple((float(a), float(b)) for a, b in d.get("exclude", []))
        return SessionConfig(name, label, role, d.get("note", ""), exclude, explicit=True)


def load_config(path: str | Path | None = None) -> Config:
    path = Path(path) if path else DEFAULT_CONFIG
    if not path.exists():
        return Config(path=None)
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    m = data.get("matching", {})
    return Config(
        matching=MatchingConfig(**{k: float(v) for k, v in m.items()}),
        sessions=data.get("sessions", {}),
        path=path,
    )
