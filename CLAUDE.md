# FishingCastCounter

Pixel Watch 3 向けの、釣りのキャスト（投げ）回数を自動カウントする Wear OS アプリ。
スマートウォッチの加速度センサー・ジャイロセンサーでキャスト動作を検知する。

## 想定する使用条件

- 右手でキャストし、左手でリールを巻く
- 時計は基本的に **左腕** に装着（＝キャスト腕とは逆の腕。リール操作側の動きが主に記録される点に注意）
- 将来的には左右どちらの腕に装着しても使えるようにする（装着腕の設定、または軸反転で吸収する想定）

## 開発方針

1. **いきなり自動判定を作らない。** まず加速度・ジャイロの生データを記録するロガーを実装する。
2. 実際の釣り場でキャストデータを収集し、PC 側で分析（波形の可視化・特徴量の検討）する。
3. 分析結果をもとに検知アルゴリズムを設計し、記録済みデータで再現テストできる形で実装する。
4. 検知ロジックは Android 依存のない純粋な Kotlin クラスとして分離し、JVM ユニットテストで記録データを流して検証できるようにする。

ロガーに関する方針：
- 記録データは CSV など PC で扱いやすい形式にする（タイムスタンプはセンサーイベントの `timestamp`（ns）を使う）
- 「ここでキャストした」というラベル（手動マーカー）を付けられるようにすると分析しやすい
- 画面オフ中も記録を継続するには Foreground Service が必要になる見込み
- 高サンプリングレート（200Hz 超）を使う場合は `HIGH_SAMPLING_RATE_SENSORS` 権限が必要
- バッテリー消費に注意（釣行は数時間続く）

## 技術スタック

- Kotlin / Jetpack Compose for Wear OS（`androidx.wear.compose:compose-material3`）
- AGP 9.4.1（Kotlin は AGP 組み込みサポート。`kotlin-android` プラグインは不要）、Kotlin 2.2.10
- Gradle 9.6.0、Gradle Daemon JVM は Java 25（`gradle/gradle-daemon-jvm.properties`）
- compileSdk / targetSdk 37、minSdk 30
- スタンドアロンアプリ（`com.google.android.wearable.standalone = true`）
- 依存バージョンは `gradle/libs.versions.toml` で管理する
- パッケージ: `net.akakitune87.fishingcastcounter`

## ビルド

`java` が PATH に無い環境では、Android Studio 同梱の JBR を `JAVA_HOME` に指定してビルドする
（各自の実際のパスは Git 管理外の `CLAUDE.local.md` に書く）。

```powershell
$env:JAVA_HOME = "<Android Studio のインストール先>\jbr"
.\gradlew.bat assembleDebug
```

- APK 出力先: `app/build/outputs/apk/debug/app-debug.apk`
- Android SDK の場所は `local.properties`（Git 管理外）の `sdk.dir`
- 実機（Pixel Watch 3）・Wear OS エミュレーターで動作確認する

## 構成（Phase 1: センサーロガー）

| パッケージ | 役割 |
|---|---|
| `sensor` | `MotionSensorSource`: 加速度・ジャイロの購読（約 50Hz を要求） |
| `logging` | `CsvSessionWriter`, `RowType`, `MarkerType`: CSV 書き出し（Android 非依存、JVM テストあり） |
| `recording` | `RecordingService`（Foreground Service, type=health, PARTIAL_WAKE_LOCK）, `RecordingSession`（HandlerThread 上で受信・書き込み）, `RecordingRepository`（UI と状態共有） |
| `presentation` | `MainActivity`, `CounterScreen`（自動カウント表示、MISS / FALSE、マーカー、UNDO・停止は長押し） |
| `detection` | `CastDetector`（P1 / P2 候補の自動キャスト検知のストリーミング実装、Android 非依存）, `DetectorParams`（`P1` / `P2_CANDIDATE`） |
| `events` | `SessionEventLog`（AUTO_CAST / AUTO_CAST_P2 / MISS / FALSE / UNDO の状態管理・ID 付与）, `EventCsvWriter`（Android 非依存） |
| `feedback` | `Haptics`（マーカー・拒否・自動検知の振動） |

- CSV: `files/sessions/session_yyyyMMdd_HHmmss.csv`（列: `timestamp_ns,type,x,y,z`）。同名の `.meta.txt` にセンサー情報・件数・時間基準チェック結果を保存
- センサーの `timestamp` とマーカーの `SystemClock.elapsedRealtimeNanos()` は同じ時間基準（起動時チェックの結果を meta に記録）
- PC への取り出し: `.\scripts\pull-sessions.ps1`（`captures/sessions` に保存。debug ビルドのみ。毎回ウォッチ上の全ファイルを上書きコピー）
- ウォッチ上の不要セッション削除: `.\scripts\delete-sessions.ps1 <session名...>`（確認あり・`-WhatIf` 可。PC 側に同サイズのコピーが無いものは削除しない）

## 構成（Phase 2: PC 側解析 `analysis/`）

- Python パッケージ `castlab`（`python -m castlab plot|epochs|detect|evaluate|tune`）。詳細は `analysis/README.md`
- `analysis/sessions.toml`: セッションごとのラベル信頼度（complete / likely / uncertain / none）と役割（tune / eval）
- 未照合の検出を誤検出と確定するのは `complete` のみ。マーカーなし区間を非キャスト扱いしない。tune と eval はセッション単位で分離
- 出力は `analysis/output/`（git 管理外）

## 構成（Phase 3: 自動キャスト検知 PoC）

- 記録開始と同時に `CastDetector` が動き、確定した検知を AUTO_CAST として `session_*.events.csv` に記録・振動する
  - 確定はピークの約 8 秒後（`min_interval_s` + ピーク後静止窓 3 秒）。`timestamp_ns` = ピーク時刻、`recorded_ns` = 確定（振動）時刻
  - 検知器は CSV 書き込みの後に呼び、例外が出たら検知だけ止めて記録は続ける
- `events.csv`（列: `event_id,type,timestamp_ns,recorded_ns,ref_id,detail`）
  - FALSE の `ref_id` = 対象 AUTO_CAST、UNDO の `ref_id` = 取り消したイベント。元イベントは消さない（追記のみ）
  - MISS の `timestamp_ns` = 押した時刻 − 10 秒（推定キャスト時刻）、`recorded_ns` = 押した時刻
  - CAST / REEL / WALK / OTHER は従来どおりセンサー CSV にも `MARK_*` 行を書く（互換のため）。UNDO は events.csv にだけ反映される
- 補正後投数 = AUTO_CAST + 有効な MISS − 有効な FALSE。AUTO_CAST 自体は補正で変えない。セッション終了時に meta へ保存
- **AUTO_CAST は正解ラベルではない**。解析（`castlab`）の正解は手動マーカーのみ。events.csv があれば UNDO を反映したマーカーを使う
- **P2 候補の並行評価**: P1 と同じセンサーデータで P2 候補（`DetectorParams.P2_CANDIDATE` = `analysis/params/p2_candidate.json`。
  2026-10-31 フィールドレビューの案 C: |a| ≥ 21 + 回転方向 + 構え姿勢）も動かし、`AUTO_CAST_P2` として events.csv に記録する
  - 正式なカウント・振動・FALSE / MISS / UNDO の対象は P1（AUTO_CAST）だけ。AUTO_CAST_P2 は UI に出さない
  - P2 候補の例外は P1・記録を止めない。meta は `candidate_detector_*` / `candidate_auto_cast_count`（P1 の `detector_*` とは別の接頭辞）
  - 方向条件のテンプレートは **左腕装着** で作ったもの。右腕装着は未検証
- **Python 版 `analysis/castlab/detector.py` と Kotlin 版 `CastDetector` は完全一致させる**（P1・P2 候補とも）
  - どちらかを変えたら `analysis/tools/export_parity_fixtures.py`（`--full` も）で期待値を作り直し、`CastDetectorParityTest` を通す
  - 入力は float32（CSV の値は float32 に丸めて使う）。リサンプリング・合計は numpy と同じ式・順序
  - 既定パラメータは `analysis/params/default.json` と `DetectorParams` の既定値（`DetectorParams.P1`）、
    `analysis/params/p2_candidate.json` と `DetectorParams.P2_CANDIDATE` を一致させる（`presetsMatchPythonParams` で確認）

## コーディング規約

- Kotlin official code style（`gradle.properties` の `kotlin.code.style=official`）
- UI は Wear Compose Material3 のコンポーネントを使う（スマホ用 Material3 ではない）
- センサー処理・記録処理・検知ロジック・UI はレイヤーを分ける
- 機能追加は段階的に。各段階で実機ビルド・動作確認を行う
