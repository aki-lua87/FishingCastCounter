# 検知パラメータ

| ファイル | 内容 |
|---|---|
| `default.json` | 現行（P1）。Wear OS の `DetectorParams` の既定値と同じ。2026-10-25 レビューで採用 |
| `baseline_v1.json` | Phase 2 当初の検知器（静止フィルタなし・間隔 8 秒）。比較用 |
| `grid.toml` / `grid_post_still.toml` | `tune` の探索グリッド |

`default.json` を変えたら、`app/.../detection/CastDetector.kt` の `DetectorParams` の既定値も合わせ、
`tools/export_parity_fixtures.py` で一致テスト用データを作り直す。
