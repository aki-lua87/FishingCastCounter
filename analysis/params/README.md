# 検知パラメータ

| ファイル | 内容 |
|---|---|
| `default.json` | 現行（P1）。Wear OS の `DetectorParams` の既定値と同じ。2026-10-25 レビューで採用 |
| `baseline_v1.json` | Phase 2 当初の検知器（静止フィルタなし・間隔 8 秒）。比較用 |
| `p2_candidate.json` | P2 候補（2026-10-31 レビューの案 C: |a| ≥ 21 + 回転方向 ≥ 0.8 + 構え姿勢 ≥ 0.85）。Wear OS の `DetectorParams.P2_CANDIDATE` と同じ。ウォッチ上で P1 と並行評価中（正式なカウントは P1）。左腕装着が前提 |
| `grid.toml` / `grid_post_still.toml` | `tune` の探索グリッド |

`default.json` / `p2_candidate.json` を変えたら、`app/.../detection/CastDetector.kt` の `DetectorParams.P1` / `P2_CANDIDATE` も合わせ、
`tools/export_parity_fixtures.py`（`--full` も）で一致テスト用データを作り直す。
