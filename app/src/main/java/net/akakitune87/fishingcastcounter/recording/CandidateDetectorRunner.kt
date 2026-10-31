package net.akakitune87.fishingcastcounter.recording

import net.akakitune87.fishingcastcounter.detection.CastDetection
import net.akakitune87.fishingcastcounter.detection.MotionKind
import net.akakitune87.fishingcastcounter.detection.StreamingCastDetector

/**
 * P2 候補の検知器を P1 から隔離して動かす。Android 非依存（RecordingSession の書き込みスレッドから呼ぶ）。
 *
 * - 検知器の例外・検知の記録（[record]）の失敗は、どちらもここで捕まえて P2 候補だけを止める
 *   （P1・センサー記録には伝えない。利用者にも知らせない）
 * - 記録の失敗には IOException も含む。events.csv 自体が壊れていれば、P1 側の書き込みで従来どおり検出される
 * - P1 の処理（検知・記録・振動）の後に呼ぶこと
 *
 * スレッドセーフではない。
 */
internal class CandidateDetectorRunner(
    detector: StreamingCastDetector,
    /** 確定した検知を記録する（finalFlush = 停止時の確定）。例外を投げてよい */
    private val record: (detection: CastDetection, finalFlush: Boolean) -> Unit,
    private val onFailure: (Throwable) -> Unit = {},
) {
    private var detector: StreamingCastDetector? = detector

    /** 止まった理由（例外の種類とメッセージ）。動いていれば null */
    var error: String? = null
        private set

    val active: Boolean get() = detector != null

    fun onSample(kind: MotionKind, timestampNs: Long, x: Double, y: Double, z: Double) {
        guard { d -> d.onSample(kind, timestampNs, x, y, z).forEach { record(it, false) } }
    }

    fun finish() {
        guard { d -> d.finish().forEach { record(it, true) } }
    }

    private inline fun guard(block: (StreamingCastDetector) -> Unit) {
        val d = detector ?: return
        try {
            block(d)
        } catch (e: Exception) {
            detector = null
            error = "${e.javaClass.simpleName}: ${e.message}"
            onFailure(e)
        }
    }
}
