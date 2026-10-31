package net.akakitune87.fishingcastcounter.events

import net.akakitune87.fishingcastcounter.detection.CastDetection
import java.util.Locale

/** session_*.events.csv の type 列。 */
enum class EventType(val isManual: Boolean) {
    /** 自動検知（ウォッチ上の P1 検知器）。正式なカウント。正解ラベルではない */
    AUTO_CAST(false),

    /**
     * P2 候補の検知器の検知（P1 と並行評価するためのもの）。カウンター・振動・FALSE / MISS の対象には使わない。
     * 正解ラベルではない
     */
    AUTO_CAST_P2(false),
    CAST(true),
    REEL(true),
    WALK(true),
    OTHER(true),

    /** 実際にキャストしたのに自動カウンターが増えなかった */
    MISS(true),

    /** キャストしていないのに自動カウンターが増えた（ref_id = 対象の AUTO_CAST） */
    FALSE(true),

    /** 手動イベントの取り消し（ref_id = 取り消したイベント）。元のイベントは消さない */
    UNDO(false),
}

/**
 * 1 件のイベント。
 *
 * @param timestampNs イベントの時刻（elapsedRealtimeNanos 基準）。AUTO_CAST はピーク時刻、
 *   MISS は推定キャスト時刻（押した時刻 − 想定遅れ）、その他は押した時刻
 * @param recordedNs 記録した時刻。AUTO_CAST は確定（＝振動）時刻、手動イベントは押した時刻
 * @param refId FALSE → 対象 AUTO_CAST、UNDO → 取り消したイベント
 * @param detail 解析用の付加情報（key=value。CSV では ; 区切り）
 */
data class SessionEvent(
    val id: Long,
    val type: EventType,
    val timestampNs: Long,
    val recordedNs: Long,
    val refId: Long? = null,
    val detail: Map<String, String> = emptyMap(),
)

/** 自動検知数と手動補正。自動検知の生データ（AUTO_CAST）は補正で変えない。 */
data class CastCounts(
    val auto: Int = 0,
    val miss: Int = 0,
    val falseCount: Int = 0,
    val undo: Int = 0,
) {
    /** 補正後投数 = 自動検知数 + 有効な MISS − 有効な FALSE */
    val corrected: Int get() = auto + miss - falseCount
}

/** 手動操作の結果（UI のフィードバック用）。 */
data class ActionResult(
    val seq: Long,
    val type: EventType,
    /** 記録したイベント。拒否された場合は null */
    val event: SessionEvent?,
    val rejectReason: RejectReason? = null,
)

enum class RejectReason {
    /** FALSE の対象にできる AUTO_CAST が無い（または既に FALSE 済み） */
    NO_FALSE_TARGET,

    /** 取り消せる手動イベントが無い（または画面表示後に状態が変わった） */
    NO_UNDO_TARGET,
}

/**
 * セッション中のイベントと補正状態。Android 非依存。単一スレッドから呼ぶこと。
 *
 * 規則:
 * - イベント ID はセッション内で 1 から連番（種類をまたいで一意）
 * - FALSE は AUTO_CAST 1 件につき有効なものを 1 件まで
 * - UNDO は「まだ取り消されていない最新の手動イベント」だけを対象にする（連続 UNDO で 1 件ずつさかのぼる）
 * - 取り消しても元イベントは残し、UNDO イベントを追加する
 */
class SessionEventLog(
    /** MISS を押すまでの想定遅れ（検知遅延 + 気づくまで）。推定キャスト時刻 = 押した時刻 − これ */
    private val missAssumedLagNs: Long = DEFAULT_MISS_ASSUMED_LAG_NS,
    /** 解析で実際のキャストを探す範囲（押した時刻からさかのぼる秒数）。detail に残すだけ */
    private val missLookbackS: Int = DEFAULT_MISS_LOOKBACK_S,
) {
    private val events = ArrayList<SessionEvent>()
    private val byId = HashMap<Long, SessionEvent>()
    private val undone = HashSet<Long>()

    /** AUTO_CAST id → それを対象にした有効な FALSE の id */
    private val falseByAuto = HashMap<Long, Long>()
    private var nextId = 1L
    private var actionSeq = 0L

    var counts = CastCounts()
        private set

    /** AUTO_CAST_P2 の件数（評価用。補正後投数には含めない） */
    var candidateCount = 0
        private set

    val all: List<SessionEvent> get() = events

    val lastAutoCast: SessionEvent? get() = events.lastOrNull { it.type == EventType.AUTO_CAST }

    fun isActive(id: Long): Boolean = id !in undone

    /** FALSE の対象候補: FALSE が付いていない最新の AUTO_CAST。 */
    fun falseTarget(): SessionEvent? =
        events.lastOrNull { it.type == EventType.AUTO_CAST && it.id !in falseByAuto }

    /** UNDO の対象候補: 取り消されていない最新の手動イベント。 */
    fun undoTarget(): SessionEvent? = events.lastOrNull { it.type.isManual && it.id !in undone }

    /** 種類別の有効な手動マーカー数（CAST / REEL / WALK / OTHER）。 */
    fun activeCount(type: EventType): Int = events.count { it.type == type && it.id !in undone }

    fun recordAutoCast(d: CastDetection, recordedNs: Long, finalFlush: Boolean = false): SessionEvent {
        val e = add(EventType.AUTO_CAST, d.timestampNs, recordedNs, null, detectionDetail(d, recordedNs, finalFlush))
        counts = counts.copy(auto = counts.auto + 1)
        return e
    }

    /** P2 候補の検知。カウンター・FALSE / UNDO の対象・lastAutoCast には影響しない。 */
    fun recordCandidateCast(d: CastDetection, recordedNs: Long, finalFlush: Boolean = false): SessionEvent {
        val detail = detectionDetail(d, recordedNs, finalFlush)
        detail["gyro_dir_cos"] = fmt(d.gyroDirCos)
        detail["post_dir_cos"] = fmt(d.postDirCos)
        detail["detector"] = "P2_CANDIDATE"
        candidateCount++
        return add(EventType.AUTO_CAST_P2, d.timestampNs, recordedNs, null, detail)
    }

    private fun detectionDetail(d: CastDetection, recordedNs: Long, finalFlush: Boolean): LinkedHashMap<String, String> {
        val detail = linkedMapOf(
            "grid" to d.gridIndex.toString(),
            "accel_peak" to fmt(d.accelPeak),
            "gyro_peak" to fmt(d.gyroPeak),
            "post_gyro_mean" to fmt(d.postGyroMean),
            "delay_ms" to ((recordedNs - d.timestampNs) / 1_000_000).toString(),
        )
        if (finalFlush) detail["final_flush"] = "1"
        return detail
    }

    fun recordMarker(type: EventType, pressNs: Long): ActionResult {
        require(type in MARKER_TYPES) { "マーカーではありません: $type" }
        return result(type, add(type, pressNs, pressNs, null, emptyMap()))
    }

    /** @param detectorPending 押した時点で検知器に判定待ちの候補があったか（後で AUTO_CAST になると二重計上の可能性） */
    fun recordMiss(pressNs: Long, detectorPending: Boolean): ActionResult {
        val last = lastAutoCast
        val detail = linkedMapOf(
            "assumed_lag_s" to (missAssumedLagNs / 1_000_000_000L).toString(),
            "lookback_s" to missLookbackS.toString(),
            "detector_pending" to if (detectorPending) "1" else "0",
        )
        if (last != null) {
            detail["last_auto_id"] = last.id.toString()
            detail["since_last_auto_s"] = fmt((pressNs - last.timestampNs) / 1e9)
        }
        val e = add(EventType.MISS, pressNs - missAssumedLagNs, pressNs, null, detail)
        counts = counts.copy(miss = counts.miss + 1)
        return result(EventType.MISS, e)
    }

    /**
     * @param targetId 画面に表示していた FALSE 対象の AUTO_CAST id。表示後に新しい AUTO_CAST が来ても、
     *   ユーザーが見ていたものに関連付ける
     */
    fun recordFalse(pressNs: Long, targetId: Long): ActionResult {
        val target = byId[targetId]
        if (target == null || target.type != EventType.AUTO_CAST || targetId in falseByAuto) {
            return reject(EventType.FALSE, RejectReason.NO_FALSE_TARGET)
        }
        val detail = mapOf("since_target_s" to fmt((pressNs - target.timestampNs) / 1e9))
        val e = add(EventType.FALSE, pressNs, pressNs, targetId, detail)
        falseByAuto[targetId] = e.id
        counts = counts.copy(falseCount = counts.falseCount + 1)
        return result(EventType.FALSE, e)
    }

    /** @param targetId 画面に表示していた取り消し対象。現在の対象と違えば（状態が変わっていれば）拒否 */
    fun recordUndo(pressNs: Long, targetId: Long): ActionResult {
        val target = undoTarget()
        if (target == null || target.id != targetId) return reject(EventType.UNDO, RejectReason.NO_UNDO_TARGET)
        val e = add(EventType.UNDO, pressNs, pressNs, target.id, mapOf("target_type" to target.type.name))
        undone += target.id
        counts = when (target.type) {
            EventType.MISS -> counts.copy(miss = counts.miss - 1, undo = counts.undo + 1)
            EventType.FALSE -> {
                falseByAuto.remove(target.refId)
                counts.copy(falseCount = counts.falseCount - 1, undo = counts.undo + 1)
            }
            else -> counts.copy(undo = counts.undo + 1)
        }
        return result(EventType.UNDO, e)
    }

    private fun add(type: EventType, ts: Long, recorded: Long, ref: Long?, detail: Map<String, String>): SessionEvent {
        val e = SessionEvent(nextId++, type, ts, recorded, ref, detail)
        events += e
        byId[e.id] = e
        return e
    }

    private fun result(type: EventType, e: SessionEvent) = ActionResult(++actionSeq, type, e)

    private fun reject(type: EventType, reason: RejectReason) = ActionResult(++actionSeq, type, null, reason)

    private fun fmt(v: Double): String = String.format(Locale.ROOT, "%.4f", v)

    companion object {
        val MARKER_TYPES = setOf(EventType.CAST, EventType.REEL, EventType.WALK, EventType.OTHER)
        const val DEFAULT_MISS_ASSUMED_LAG_NS = 10_000_000_000L
        const val DEFAULT_MISS_LOOKBACK_S = 30
    }
}
