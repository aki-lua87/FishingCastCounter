# castlab — キャスト検知のオフライン解析・評価環境（Phase 2）

Pixel Watch 3 で記録したセンサー CSV（`captures/sessions/`）を PC で読み込み、

- 波形（加速度・ジャイロの XYZ と合成値）とマーカーの可視化
- マーカー前後の波形の切り出し・比較・特徴量の出力
- ルールベースのキャスト候補検知（試作）
- 検知結果と CAST マーカーの 1 対 1 照合、ラベル信頼度を考慮した集計
- パラメータ調整（tune セッション）と評価（eval セッション）の分離

を行う。Wear OS アプリには依存しない。

## セットアップ

Python 3.11 以上。`analysis/` に仮想環境を作る。

```powershell
cd analysis
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

以降のコマンドは `analysis/` で実行する（`python` は `.\.venv\Scripts\python.exe` に読み替える）。
セッションを省略すると `../captures/sessions/` の全 CSV が対象になる。出力は `analysis/output/`（git 管理外）。

## 使い方

### 1. 波形グラフ — `plot`

```powershell
python -m castlab plot ..\captures\sessions\session_20261025_150936.csv            # 全体図 + 120 秒ごとのページ
python -m castlab plot <csv> --start 360 --end 480                                  # 区間を 1 枚
python -m castlab plot <csv> --detect --set accel_peak_min=22                       # 検出結果も重ねる
```

出力: `output/plots/<session>/overview.png`, `pages/page_XXX.png`

ページは 4 段: 加速度 XYZ / ジャイロ XYZ / |a| / |g|。縦線がマーカー（CAST 実線・REEL 破線・WALK 点線・OTHER 一点鎖線）、
薄青帯が CAST マーカーの照合窓。`--detect` 時は |a| 段に検出（✓ 一致・✗ 誤検出・◆ 誤検出候補・● ラベルなし）と未検出 CAST を表示。

### 2. マーカー前後の波形比較 — `epochs`

```powershell
python -m castlab epochs --labels complete,likely                    # 信頼できるラベルのセッションだけ
python -m castlab epochs --labels complete,likely --align accel_peak --pre 6 --post 6
python -m castlab epochs <csv> --types CAST,REEL --pre 3 --post 12
```

出力: `output/epochs/`

- `all_epochs.png`: 種別（CAST / REEL / WALK / OTHER）ごとに |a|, |g| を重ね描き + 中央値
- `all_cast_axes.png`: CAST 前後の XYZ 軸別
- `<session>/...`: セッション別（`--combined-only` で省略）
- `features.csv`: マーカーごとの特徴量（探索窓内の |a| / |g| のピーク値・マーカーからの時間差・ピーク時の各軸値など）

`--align accel_peak` は探索窓（既定 マーカー −2〜+12 秒）内で |a| が最大の時刻に揃える。**形を比べるための整列で、その時刻を正解とみなすものではない。**

### 3. キャスト候補の時刻一覧 — `detect`

```powershell
python -m castlab detect                                   # 全セッション
python -m castlab detect <csv> --params my_params.json --set gyro_peak_min=5
```

出力: `output/detect/detections.csv`（`session, t, timestamp_ns, accel_peak, gyro_peak, nearest_marker`）。
`timestamp_ns` は元 CSV と同じ時間基準（`elapsedRealtimeNanos`）。

### 4. マーカーとの照合・評価 — `evaluate`

```powershell
python -m castlab evaluate                                 # 全セッション + HTML レポート
python -m castlab evaluate --no-pages                      # 時系列ページを省略（速い）
python -m castlab evaluate --set accel_peak_min=22         # パラメータを変えて再評価
```

コンソールにセッション別・集計表と、未検出 CAST / 誤検出（候補）の一覧を出す。
**目視確認は `output/evaluate/index.html` をブラウザで開く。** セッションごとのページに以下がある:

- 全体図（|a|, |g|, マーカー, 検出）
- CAST マーカーごとの拡大図（0 = マーカー時刻。一致した検出のオフセット / 未検出）
- マーカーと対応しない検出の拡大図（近傍マーカーの種類と時間差つき）
- 照合表・検出一覧、120 秒ごとの時系列ページ

CSV: `summary.csv`（セッション別）, `aggregate.csv`（役割 × 信頼度別）, `detections_all.csv`, 各セッションの `detections.csv` / `cast_markers.csv`

### 5. パラメータ調整と評価の分離 — `tune`

```powershell
python -m castlab tune                                               # sessions.toml の role="tune" で調整
python -m castlab tune --tune-sessions session_20261025_105022       # 調整用セッションを差し替え
python -m castlab tune --grid params\grid.toml --no-pages
```

1. `role = "tune"` のセッションのうち `label = "complete"` のものだけで、`params/grid.toml` の全組み合わせを評価し F1 で順位付けする（`output/tune/ranking.csv`）
2. 最良パラメータを `output/tune/best_params.json` に保存
3. それを tune 以外の全セッションに適用して `output/tune/report/` にレポートを出す

tune セッションは評価側に入らない（セッション単位で分離）。tune セッションの成績は「調整に使ったデータでの成績」と明記して別行に集計する。
`complete` の tune セッションが無い場合はエラーにする（不完全なラベルでは誤検出を確定できず、目的関数が作れないため）。

`best_params.json` はそのまま `--params` に渡せる。採用するなら `params/default.json` を更新する。

## ラベル信頼度と評価のルール（`sessions.toml`）

| label | 意味 | 未照合の検出 | 算出する指標 |
|---|---|---|---|
| `complete` | 全キャストに CAST マーカーあり | ✗ 誤検出（確定） | precision / recall / F1 |
| `likely` | マーカー数 ≈ 実キャスト数 | ? 誤検出候補 | recall_ref（参考値）のみ。precision は出さない |
| `uncertain` | 誤タップ・漏れ・記憶違いの可能性 | ? 誤検出候補 | 件数のみ（率は出さない） |
| `none` | マーカーを使わない | · ラベルなし | 件数・1 時間あたり検出数のみ |

- `sessions.toml` に書かれていないセッションは、CAST マーカーがあれば `uncertain`、無ければ `none`
- マーカーの無い区間を非キャストとはみなさない。未照合の検出を「誤検出」と確定するのは `complete` のみ
- 推測した検出結果を正解データに昇格させない（`--align` も形の比較用）
- 信頼度の異なるセッションは集計で合算しない
- `exclude = [[開始秒, 終了秒], ...]` で照合から除く区間を指定できる（例: 記録開始直後の操作）
- `ignore = true` のセッションはどのコマンドでも読み込まない（アプリの動作確認時の記録など）。`--include-ignored` で一時的に含められる。
  ウォッチ上からも消すなら `..\scripts\delete-sessions.ps1 <session名...>`（`pull-sessions.ps1` は毎回ウォッチ上の全ファイルを取り出すため）

現在の設定:

| セッション | label | role | 備考 |
|---|---|---|---|
| `session_20261025_150936` | complete | tune | UNFIX S86M-5 30 キャスト、毎回マーカー記録 |
| `session_20261025_095018` / `104438` / `111555` | likely | eval | CAST マーカー各 5 回 |
| `session_20261018_154843` / `20261025_034220` / `035604` / `040144` | （ignore） | — | アプリ作成直後の動作確認。解析対象外 |
| その他のマーカー付き | uncertain | eval | （既定） |
| マーカーなし | none | eval | （既定） |

### 照合（`[matching]`）

- 検出 d が CAST マーカー m の窓 `[m − window_before_s, m + window_after_s]`（既定 −2〜+12 秒）に入れば対応可能
  - マーカーはキャスト直前に押すため、実際の動作は数秒後（30 キャストのセッションでは |a| ピークが概ね +4〜+6 秒）
- 1 マーカー ↔ 1 検出の 1 対 1。ハンガリアン法で一致数を最大化し、その中で `|d − m − expected_offset_s|` の合計が最小の組み合わせを選ぶ
- 同じキャストへの 2 つ目の検出は一致にならず、未照合（誤検出 / 誤検出候補）として数える
- `reel_bound_max_s`（`sessions.toml` で 30、0 で無効）: CAST の後（次の CAST より前）その秒数以内に REEL マーカーがあれば、窓の終わりを REEL の時刻にする。CAST マーカーを早めに押したセッションでも照合できる。REEL は照合（評価）にだけ使い、検知器の入力には使わない

## 分析レポート

- [reports/2026-10-25_detector_review/](reports/2026-10-25_detector_review/README.md): 照合窓・見逃し・リトリーブ・歩行・11:34・tune/eval 分離・検知間隔のレビュー（`studies/detector_review.py` で再生成）
- Phase 3 の変更前後の比較: `studies/phase3_before_after.py`（結果は同フォルダの `phase3_before_after.csv`）

## 検知器（P1）— `castlab/detector.py`

Wear OS 版（`app/.../detection/CastDetector.kt`）と同じ結果になる（下の「Python 版と Kotlin 版の一致」）。

1. |a|（重力込みの加速度合成値）が `accel_peak_min` 以上の局所最大を候補にする
2. 候補の前 `gyro_window_before_s`〜後 `gyro_window_after_s` 秒の |g| 最大が `gyro_peak_min` 以上なら残す
3. ピーク後 `post_still_start_s`〜`post_still_end_s` 秒の |g| 平均が `post_gyro_mean_max` 以下なら残す（キャスト直後は腕が止まる）
4. 前後 `min_interval_s` 未満に |a| がより大きい候補（同値なら先の候補）があれば捨てる。
   判定に前後の候補しか使わないので、ウォッチ上でも一定の遅延（約 8 秒）で同じ結果を出せる
   （Phase 2 当初は全体を |a| 順に貪欲に選んでいた。変更の影響は 11:34 の 1 件のみ）

| パラメータ | 既定 | 意味 |
|---|---|---|
| `accel_peak_min` | 25.0 | |a| ピークの下限 [m/s²] |
| `gyro_peak_min` | 4.0 | ピーク周辺の |g| 最大の下限 [rad/s] |
| `gyro_window_before_s` / `gyro_window_after_s` | 1.5 / 0.5 | ジャイロ条件の窓 [s] |
| `min_interval_s` | 5.0 | 検出同士の最小間隔 [s] |
| `smooth_s` | 0.0 | |a| の移動平均幅 [s] |
| `post_gyro_mean_max` | 1.5 | ピーク後静止フィルタ: `post_still_start_s`〜`post_still_end_s`（既定 0.5〜3.0 s）の |g| 平均の上限 [rad/s]。0 で無効 |

パラメータは `--params <json>`（既定 `params/default.json` = P1）と `--set key=value`（複数可）で変える。
Phase 2 当初の検知器は `params/baseline_v1.json`（`--params paramsaseline_v1.json` で比較できる）。
前処理: ACCEL / GYRO を 50 Hz の共通グリッドに線形補間（0.2 秒超の記録抜けは補間しない）。

## 初期の観察メモ（2026-10-25 時点のデータ）

- 30 キャストのセッションでは、CAST マーカーの +4〜+6 秒付近に |a| 25〜43 m/s² の鋭いピークがある（左腕装着）
- `--align accel_peak` で揃えると、CAST はピークの前後数秒で |g| がほぼ 0 になる（静止 → 振り → 静止）。REEL はピーク後も |g| が 2 rad/s 前後で続く。次の特徴量候補
- 既定パラメータ（`accel_peak_min=25`）で: complete（tune）30 中 28 一致・誤検出 0 / likely（eval）15 中 14 一致・誤検出候補 0
- `tune` の最良（`accel_peak_min=20, gyro_peak_min=5, min_interval_s=12`）は tune で 30/30・誤検出 2 だが、likely（eval）では誤検出候補が 5 件に増える。調整データ 1 セッションへの過適合に注意
- 10/18 と 10/25 03:xx の 4 セッションは、CAST マーカーがあっても |a| が 25 を超えるピークが無かった。アプリの動作確認時の記録と判明したので `ignore` にした

## ウォッチ上のイベント（Phase 3）

ウォッチは `session_*.events.csv` に AUTO_CAST（自動検知）と手動イベント（CAST / REEL / WALK / OTHER / MISS / FALSE / UNDO）を ID 付きで記録する。
`pull-sessions.ps1` でセンサー CSV と一緒に取り出される。

- `load_session` は events.csv があれば読み込み、UNDO を反映する（`session.events` の `active` 列）
- 正解ラベル（`session.markers`）は手動マーカーのみ。**AUTO_CAST は正解として扱わない**
- `session.device_events("AUTO_CAST" | "MISS" | "FALSE" | ...)` で取得
- `evaluate` のセッションページに「ウォッチ上の自動検知と申告」節が出る
  - AUTO_CAST / MISS / FALSE / UNDO の件数と補正後投数
  - PC で同じパラメータ（meta の `detector_*`）を使って再検知した結果との突き合わせ（実装が一致していれば不一致 0）
  - FALSE 申告された検知の波形、MISS を押す前 30 秒の波形（検知されなかったキャストを探す範囲）

## Python 版と Kotlin 版の一致

検知器を変えたら、次で期待値を作り直して Kotlin の `CastDetectorParityTest` を通す。

```powershell
python tools\export_parity_fixtures.py          # app/src/test/resources/parity（切り出し 4 区間、git 管理）
python tools\export_parity_fixtures.py --full   # captures/parity（全セッション、git 管理外。手元にあればテストで使われる）
cd ..; .\gradlew.bat :app:testDebugUnitTest
```

- 入力はウォッチと同じ float32（`load_session` は x,y,z を float32 に丸める）
- 補間（numpy.interp）・合成値・平均（numpy の pairwise 加算）は Kotlin 側で同じ式・順序を再現しているので、検知時刻・特徴量はビット単位で一致する

## テスト

```powershell
python -m pytest
```

照合（1 対 1・窓境界・重複検出・信頼度別の集計・REEL 上限）、検知器、CSV / events.csv 読み込みのテストがある。

## ファイル構成

```
analysis/
  castlab/
    io.py         CSV / meta 読み込み（ACCEL・GYRO を時刻順に並べ替え、マーカー分離）
    signals.py    50 Hz グリッドへのリサンプリング、合成値
    epochs.py     マーカー前後の切り出し、特徴量
    detector.py   ルールベース検知器とパラメータ
    evaluate.py   1 対 1 照合、ラベル信頼度別の集計
    config.py     sessions.toml の読み込み
    plots.py      グラフ
    report.py     HTML レポート
    pipeline.py   読み込み → 検出 → 照合
    device.py     ウォッチ上のイベント（AUTO_CAST / MISS / FALSE）の集計・PC 再検知との突き合わせ
    __main__.py   CLI
  params/              検知パラメータ（default.json = P1、baseline_v1.json = Phase 2 当初）と探索グリッド
  sessions.toml        ラベル信頼度・役割・照合設定
  studies/             分析スクリプト（レポートの再生成）
  tools/               Kotlin 一致テスト用データの作成
  reports/             分析レポート
  tests/
```
