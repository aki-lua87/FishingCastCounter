package net.akakitune87.fishingcastcounter.recording

import android.os.Build
import android.os.Handler
import android.os.HandlerThread
import android.os.SystemClock
import android.util.Log
import net.akakitune87.fishingcastcounter.detection.CastDetection
import net.akakitune87.fishingcastcounter.detection.CastDetector
import net.akakitune87.fishingcastcounter.detection.DetectorParams
import net.akakitune87.fishingcastcounter.detection.MotionKind
import net.akakitune87.fishingcastcounter.events.ActionResult
import net.akakitune87.fishingcastcounter.events.CastCounts
import net.akakitune87.fishingcastcounter.events.EventCsvWriter
import net.akakitune87.fishingcastcounter.events.EventType
import net.akakitune87.fishingcastcounter.events.SessionEvent
import net.akakitune87.fishingcastcounter.events.SessionEventLog
import net.akakitune87.fishingcastcounter.logging.CsvSessionWriter
import net.akakitune87.fishingcastcounter.logging.MarkerType
import net.akakitune87.fishingcastcounter.logging.RowType
import net.akakitune87.fishingcastcounter.sensor.MotionSample
import net.akakitune87.fishingcastcounter.sensor.MotionSensorSource
import java.io.File
import java.io.IOException
import java.time.ZonedDateTime
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong

data class SessionCounts(val accel: Long, val gyro: Long, val markers: Long) {
    val sensorEvents: Long get() = accel + gyro

    companion object {
        val ZERO = SessionCounts(0, 0, 0)
    }
}

/** UI に渡すイベント・カウンターの状態（書き込みスレッドで作るスナップショット）。 */
data class EventSnapshot(
    val counts: CastCounts = CastCounts(),
    val markerCounts: Map<MarkerType, Int> = emptyMap(),
    val lastAutoCast: SessionEvent? = null,
    val falseTarget: SessionEvent? = null,
    val undoTarget: SessionEvent? = null,
    val lastAction: ActionResult? = null,
    /** 検知器に判定待ちの候補がある（数秒後に AUTO_CAST になるかもしれない） */
    val detectorPending: Boolean = false,
    /** 自動検知が動いているか（例外で止まったら false。センサー記録は続ける） */
    val detectorActive: Boolean = true,
)

fun MarkerType.toEventType(): EventType = when (this) {
    MarkerType.CAST -> EventType.CAST
    MarkerType.REEL -> EventType.REEL
    MarkerType.WALK -> EventType.WALK
    MarkerType.OTHER -> EventType.OTHER
}

/**
 * 1 回の計測セッション。
 *
 * センサーイベントの受信・CSV 書き込み・自動検知・イベント記録は専用の HandlerThread 上で行い、
 * UI スレッドではファイル I/O をしない。[writer] [eventWriter] [detector] [eventLog] はこのスレッドからのみ触る。
 *
 * 自動検知はセンサー行を CSV に書いた後に行い、検知器で例外が起きても検知だけを止めて記録は続ける。
 */
class RecordingSession(
    private val sensorSource: MotionSensorSource,
    val csvFile: File,
    private val eventsFile: File,
    private val metaFile: File,
    private val detectorParams: DetectorParams,
    private val listener: Listener,
) {
    interface Listener {
        fun onError(session: RecordingSession, message: String)

        /** 記録は続くが利用者に知らせたい問題（時間基準のずれ、自動検知の停止など）。 */
        fun onWarning(session: RecordingSession, message: String)

        /** イベント・カウンターの状態が変わったとき。書き込みスレッドから呼ばれる。 */
        fun onEventsChanged(session: RecordingSession, snapshot: EventSnapshot)

        /** AUTO_CAST を記録した直後。書き込みスレッドから呼ばれる（振動用）。 */
        fun onAutoCast(session: RecordingSession, event: SessionEvent)
        fun onClosed(session: RecordingSession)
    }

    val startElapsedRealtimeNanos: Long = SystemClock.elapsedRealtimeNanos()
    private val startWallClock: ZonedDateTime = ZonedDateTime.now()

    private val thread = HandlerThread("RecordingSession").apply { start() }
    private val handler = Handler(thread.looper)
    private val stopped = AtomicBoolean(false)

    private val accelCount = AtomicLong()
    private val gyroCount = AtomicLong()
    private val markerCount = AtomicLong()

    private var writer: CsvSessionWriter? = null
    private var eventWriter: EventCsvWriter? = null
    private val firstEventOffsetNs = mutableMapOf<RowType, Long>()

    private var detector: CastDetector? = CastDetector(detectorParams, startElapsedRealtimeNanos)
    private var detectorError: String? = null
    private val eventLog = SessionEventLog()
    private var lastAction: ActionResult? = null
    private var lastPending = false

    private val flushTask = object : Runnable {
        override fun run() {
            try {
                writer?.flush()
                eventWriter?.flush()
            } catch (e: IOException) {
                fail("CSV の書き込みに失敗しました", e)
                return
            }
            handler.postDelayed(this, FLUSH_INTERVAL_MS)
        }
    }

    fun counts(): SessionCounts = SessionCounts(accelCount.get(), gyroCount.get(), markerCount.get())

    fun start() {
        handler.post {
            try {
                csvFile.parentFile?.mkdirs()
                writer = CsvSessionWriter(csvFile.bufferedWriter(bufferSize = WRITE_BUFFER_SIZE))
                eventWriter = EventCsvWriter(eventsFile.bufferedWriter())
            } catch (e: IOException) {
                fail("CSV ファイルを作成できません: ${csvFile.name}", e)
                return@post
            }
            val registered = sensorSource.start(
                handler,
                MotionSensorSource.SAMPLING_PERIOD_US,
                ::onSample,
            )
            if (!registered) {
                fail("センサーの登録に失敗しました", null)
                return@post
            }
            handler.postDelayed(flushTask, FLUSH_INTERVAL_MS)
            publish()
        }
    }

    /** マーカーを記録する。時刻は押された瞬間の elapsedRealtimeNanos を使う。 */
    fun mark(marker: MarkerType) {
        val pressNs = SystemClock.elapsedRealtimeNanos()
        handler.post {
            val w = writer ?: return@post
            try {
                // 従来どおりセンサー CSV にも MARK_* 行を書く（既存の解析・スクリプトとの互換）
                w.writeMarker(marker, pressNs)
                markerCount.incrementAndGet()
            } catch (e: IOException) {
                fail("CSV の書き込みに失敗しました", e)
                return@post
            }
            act(eventLog.recordMarker(marker.toEventType(), pressNs))
        }
    }

    /** 未検知（実際にキャストしたのにカウンターが増えなかった）。 */
    fun miss() {
        val pressNs = SystemClock.elapsedRealtimeNanos()
        handler.post {
            if (writer == null) return@post
            act(eventLog.recordMiss(pressNs, detectorPending = detector?.hasPending == true))
        }
    }

    /** 誤検知。[targetId] は画面に表示していた AUTO_CAST の ID。 */
    fun markFalse(targetId: Long) {
        val pressNs = SystemClock.elapsedRealtimeNanos()
        handler.post {
            if (writer == null) return@post
            act(eventLog.recordFalse(pressNs, targetId))
        }
    }

    /** 直前の手動イベントを取り消す。[targetId] は画面に表示していた取り消し対象の ID。 */
    fun undo(targetId: Long) {
        val pressNs = SystemClock.elapsedRealtimeNanos()
        handler.post {
            if (writer == null) return@post
            act(eventLog.recordUndo(pressNs, targetId))
        }
    }

    /** 計測を終了する。キュー済みのイベントを書き終え、検知器の判定待ちを確定させてからファイルを閉じる。 */
    fun stop() {
        if (!stopped.compareAndSet(false, true)) return
        sensorSource.stop()
        handler.post {
            handler.removeCallbacks(flushTask)
            runDetector { it.finish() }?.let { recordDetections(it, finalFlush = true) }
            closeWriters()
            writeMetaFile()
            listener.onClosed(this)
        }
        thread.quitSafely()
    }

    private fun onSample(sample: MotionSample) {
        val w = writer ?: return
        if (sample.type !in firstEventOffsetNs) checkTimeBase(sample)
        try {
            w.writeSample(sample.type, sample.timestampNs, sample.x, sample.y, sample.z)
        } catch (e: IOException) {
            fail("CSV の書き込みに失敗しました", e)
            return
        }
        val kind = when (sample.type) {
            RowType.ACCEL -> {
                accelCount.incrementAndGet()
                MotionKind.ACCEL
            }
            RowType.GYRO -> {
                gyroCount.incrementAndGet()
                MotionKind.GYRO
            }
            else -> return
        }
        val detections = runDetector {
            it.onSample(kind, sample.timestampNs, sample.x.toDouble(), sample.y.toDouble(), sample.z.toDouble())
        } ?: return
        if (detections.isNotEmpty()) {
            recordDetections(detections, finalFlush = false)
        } else if (detector?.hasPending != lastPending) {
            publish()
        }
    }

    private fun recordDetections(detections: List<CastDetection>, finalFlush: Boolean) {
        val now = SystemClock.elapsedRealtimeNanos()
        for (d in detections) {
            val e = eventLog.recordAutoCast(d, recordedNs = now, finalFlush = finalFlush)
            if (!writeEvent(e)) return
            listener.onAutoCast(this, e)
        }
        publish()
    }

    /** 検知器を呼ぶ。例外が出たら検知だけを止めてセンサー記録は続ける。 */
    private fun <T> runDetector(block: (CastDetector) -> T): T? {
        val d = detector ?: return null
        return try {
            block(d)
        } catch (e: RuntimeException) {
            Log.e(TAG, "cast detector failed", e)
            detector = null
            detectorError = "${e.javaClass.simpleName}: ${e.message}"
            listener.onWarning(this, "自動検知でエラーが発生したため停止しました（記録は継続）")
            publish()
            null
        }
    }

    private fun act(result: ActionResult) {
        lastAction = result
        result.event?.let { if (!writeEvent(it)) return }
        publish()
    }

    private fun writeEvent(e: SessionEvent): Boolean {
        val w = eventWriter ?: return false
        return try {
            w.write(e)
            true
        } catch (ex: IOException) {
            fail("イベント CSV の書き込みに失敗しました", ex)
            false
        }
    }

    private fun publish() {
        lastPending = detector?.hasPending == true
        listener.onEventsChanged(
            this,
            EventSnapshot(
                counts = eventLog.counts,
                markerCounts = MarkerType.entries.associateWith { eventLog.activeCount(it.toEventType()) },
                lastAutoCast = eventLog.lastAutoCast,
                falseTarget = eventLog.falseTarget(),
                undoTarget = eventLog.undoTarget(),
                lastAction = lastAction,
                detectorPending = lastPending,
                detectorActive = detector != null,
            ),
        )
    }

    /**
     * SensorEvent.timestamp がマーカーと同じ elapsedRealtimeNanos 基準かを確認する。
     * 受信時刻との差は通常数 ms〜数十 ms（配信遅延）に収まる。大きくずれていれば時間基準が異なる。
     */
    private fun checkTimeBase(sample: MotionSample) {
        val offsetNs = SystemClock.elapsedRealtimeNanos() - sample.timestampNs
        firstEventOffsetNs[sample.type] = offsetNs
        Log.i(TAG, "time base check ${sample.type}: elapsedRealtimeNanos - event.timestamp = $offsetNs ns")
        if (offsetNs !in -TIME_BASE_TOLERANCE_NS..TIME_BASE_TOLERANCE_NS) {
            listener.onWarning(
                this,
                "${sample.type} の timestamp が elapsedRealtime と ${offsetNs / 1_000_000} ms ずれています",
            )
        }
    }

    private fun fail(message: String, cause: Throwable?) {
        Log.e(TAG, message, cause)
        sensorSource.stop()
        handler.removeCallbacks(flushTask)
        closeWriters()
        listener.onError(this, message)
    }

    private fun closeWriters() {
        writer?.let { w ->
            writer = null
            try {
                w.close()
            } catch (e: IOException) {
                Log.e(TAG, "failed to close ${csvFile.name}", e)
            }
        }
        eventWriter?.let { w ->
            eventWriter = null
            try {
                w.close()
            } catch (e: IOException) {
                Log.e(TAG, "failed to close ${eventsFile.name}", e)
            }
        }
    }

    private fun writeMetaFile() {
        val counts = counts()
        val c = eventLog.counts
        val lines = buildList {
            add("csv_file=${csvFile.name}")
            add("events_file=${eventsFile.name}")
            add("start_wall_clock=$startWallClock")
            add("start_elapsed_realtime_ns=$startElapsedRealtimeNanos")
            add("stop_elapsed_realtime_ns=${SystemClock.elapsedRealtimeNanos()}")
            add("requested_sampling_period_us=${MotionSensorSource.SAMPLING_PERIOD_US}")
            add("accel_events=${counts.accel}")
            add("gyro_events=${counts.gyro}")
            add("marker_events=${counts.markers}")
            firstEventOffsetNs.forEach { (type, offset) ->
                add("time_base_offset_ns_$type=$offset")
            }
            // 自動検知とユーザー補正（AUTO_CAST は検知器の出力であって正解ではない）
            add("auto_cast_count=${c.auto}")
            add("miss_count=${c.miss}")
            add("false_count=${c.falseCount}")
            add("undo_count=${c.undo}")
            add("corrected_cast_count=${c.corrected}")
            add("detector=P1")
            add("detector_active_at_stop=${detector != null}")
            detectorError?.let { add("detector_error=$it") }
            detectorParams.describe().forEach { add("detector_$it") }
            add("detector_confirmation_delay_s=${detectorParams.confirmationDelayS}")
            add("device=${Build.MANUFACTURER} ${Build.MODEL}")
            add("android_sdk=${Build.VERSION.SDK_INT}")
            sensorSource.describeSensors().forEach { add("sensor=$it") }
        }
        try {
            metaFile.writeText(lines.joinToString("\n", postfix = "\n"))
        } catch (e: IOException) {
            Log.e(TAG, "failed to write ${metaFile.name}", e)
        }
    }

    companion object {
        private const val TAG = "RecordingSession"
        private const val FLUSH_INTERVAL_MS = 1_000L
        private const val WRITE_BUFFER_SIZE = 64 * 1024
        private const val TIME_BASE_TOLERANCE_NS = 1_000_000_000L
    }
}
