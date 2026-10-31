package net.akakitune87.fishingcastcounter.recording

import net.akakitune87.fishingcastcounter.detection.CastDetection
import net.akakitune87.fishingcastcounter.detection.CastDetector
import net.akakitune87.fishingcastcounter.detection.DetectorParams
import net.akakitune87.fishingcastcounter.detection.MotionKind
import net.akakitune87.fishingcastcounter.detection.StreamingCastDetector
import net.akakitune87.fishingcastcounter.events.EventType
import net.akakitune87.fishingcastcounter.events.SessionEventLog
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.IOException
import java.util.zip.GZIPInputStream

/**
 * P2 候補の失敗（検知器の例外・記録の失敗）が P1 に影響しないこと。
 *
 * RecordingSession と同じ順序（P1 の検知・記録 → P2 候補）でイベントログに記録し、P2 候補なしの場合と P1 の結果を比べる。
 * RecordingSession 自体（Android の Handler・ファイル）はここでは動かしていない。
 */
class CandidateDetectorRunnerTest {

    private class Row(val ts: Long, val kind: MotionKind, val x: Double, val y: Double, val z: Double)

    private fun load(name: String): Pair<Long, List<Row>> {
        val meta = javaClass.getResourceAsStream("/parity/$name.meta.txt")!!.bufferedReader().readText()
        val t0 = meta.lines().first { it.startsWith("start_elapsed_realtime_ns=") }.substringAfter("=").toLong()
        val rows = GZIPInputStream(javaClass.getResourceAsStream("/parity/$name.csv.gz")!!).bufferedReader()
            .readLines().drop(1).mapNotNull { line ->
                val c = line.split(",")
                val kind = when (c[1]) {
                    "ACCEL" -> MotionKind.ACCEL
                    "GYRO" -> MotionKind.GYRO
                    else -> return@mapNotNull null
                }
                Row(c[0].toLong(), kind, c[2].toFloat().toDouble(), c[3].toFloat().toDouble(), c[4].toFloat().toDouble())
            }
        return t0 to rows
    }

    /** RecordingSession.onSample / stop と同じ順序で P1 と P2 候補を動かし、イベントログを返す。 */
    private fun session(
        name: String,
        candidateFactory: ((Long) -> StreamingCastDetector)?,
        record: (SessionEventLog, CastDetection, Boolean) -> Unit = { log, d, f -> log.recordCandidateCast(d, d.timestampNs, f) },
    ): Pair<SessionEventLog, CandidateDetectorRunner?> {
        val (t0, rows) = load(name)
        val log = SessionEventLog()
        val p1 = CastDetector(DetectorParams.P1, t0)
        val runner = candidateFactory?.let { f -> CandidateDetectorRunner(f(t0), { d, fin -> record(log, d, fin) }) }
        for (r in rows) {
            p1.onSample(r.kind, r.ts, r.x, r.y, r.z).forEach { log.recordAutoCast(it, it.timestampNs + 8_000_000_000L) }
            runner?.onSample(r.kind, r.ts, r.x, r.y, r.z)
        }
        p1.finish().forEach { log.recordAutoCast(it, it.timestampNs, finalFlush = true) }
        runner?.finish()
        return log to runner
    }

    private fun p1Events(log: SessionEventLog) =
        log.all.filter { it.type == EventType.AUTO_CAST }.map { it.timestampNs to it.detail }

    @Test
    fun candidateRunsAlongsideWithoutChangingP1() {
        for (name in listOf("hook_200612", "weak_202426", "casts_150936")) {
            val (alone, _) = session(name, null)
            val (both, runner) = session(name, { CastDetector(DetectorParams.P2_CANDIDATE, it) })
            assertEquals(name, p1Events(alone), p1Events(both))
            assertEquals(name, alone.counts, both.counts)
            assertTrue(runner!!.active)
            assertNull(runner.error)
        }
        // hook_200612: P1 は 2 件検知（FALSE 申告された作業動作）、P2 候補は 0 件
        val (log, _) = session("hook_200612", { CastDetector(DetectorParams.P2_CANDIDATE, it) })
        assertEquals(2, log.counts.auto)
        assertEquals(0, log.candidateCount)
    }

    @Test
    fun detectorExceptionStopsOnlyTheCandidate() {
        var calls = 0
        val throwing = object : StreamingCastDetector {
            override fun onSample(kind: MotionKind, timestampNs: Long, x: Double, y: Double, z: Double): List<CastDetection> {
                if (++calls == 1000) throw IllegalStateException("boom")
                return emptyList()
            }

            override fun finish(): List<CastDetection> = error("finish はもう呼ばれないはず")
        }
        val (alone, _) = session("casts_150936", null)
        val (both, runner) = session("casts_150936", { throwing })
        assertEquals(1000, calls) // 失敗後は呼ばれない
        assertFalse(runner!!.active)
        assertEquals("IllegalStateException: boom", runner.error)
        assertEquals(p1Events(alone), p1Events(both))
        assertEquals(alone.counts, both.counts)
    }

    @Test
    fun recordFailureStopsOnlyTheCandidate() {
        for (failure in listOf(IOException("disk full"), IllegalArgumentException("bad detail"))) {
            var attempts = 0
            val (alone, _) = session("casts_150936", null)
            val (both, runner) = session(
                "casts_150936",
                { CastDetector(DetectorParams.P2_CANDIDATE, it) },
                record = { _, _, _ ->
                    attempts++
                    throw failure
                },
            )
            assertEquals(1, attempts) // 最初の失敗で止まり、以後は記録しようとしない
            assertFalse(runner!!.active)
            assertEquals("${failure.javaClass.simpleName}: ${failure.message}", runner.error)
            assertEquals(p1Events(alone), p1Events(both))
            assertEquals(alone.counts, both.counts)
        }
    }
}
