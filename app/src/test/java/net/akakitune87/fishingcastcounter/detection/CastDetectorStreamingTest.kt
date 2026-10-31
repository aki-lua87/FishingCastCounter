package net.akakitune87.fishingcastcounter.detection

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.zip.GZIPInputStream

/** ストリーミング特有の性質（到着順・確定遅延・判定中フラグ）。一致性そのものは CastDetectorParityTest。 */
class CastDetectorStreamingTest {

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

    private fun detect(t0: Long, rows: List<Row>, params: DetectorParams = DetectorParams()): List<CastDetection> {
        val d = CastDetector(params, t0)
        val out = rows.flatMap { d.onSample(it.kind, it.ts, it.x, it.y, it.z) }.toMutableList()
        out += d.finish()
        return out
    }

    @Test
    fun resultDoesNotDependOnBatchingBetweenSensors() {
        for ((name, params) in listOf("mixed_113449" to DetectorParams.P1, "weak_202426" to DetectorParams.P2_CANDIDATE)) {
            val (t0, rows) = load(name)
            val original = detect(t0, rows, params)
            assertTrue(name, original.isNotEmpty())
            // 完全に時刻順に交互に届く場合
            assertEquals(name, original, detect(t0, rows.sortedBy { it.ts }, params))
            // 5 秒ごとに ACCEL をまとめて、その後 GYRO をまとめて届ける場合（画面オフ時のバッチ配信を想定）
            val batched = rows.groupBy { (it.ts - t0) / 5_000_000_000L }.toSortedMap().values.flatMap { batch ->
                batch.filter { it.kind == MotionKind.ACCEL } + batch.filter { it.kind == MotionKind.GYRO }
            }
            assertEquals(name, original, detect(t0, batched, params))
        }
    }

    @Test
    fun detectionsAreConfirmedWithinTheDocumentedDelay() {
        for (params in listOf(DetectorParams.P1, DetectorParams.P2_CANDIDATE)) {
            for (name in listOf("casts_150936", "mixed_113449", "impacts_113449", "weak_202426")) {
                val (t0, rows) = load(name)
                val d = CastDetector(params, t0)
                for (r in rows.sortedBy { it.ts }) {
                    for (det in d.onSample(r.kind, r.ts, r.x, r.y, r.z)) {
                        val delayS = (r.ts - det.timestampNs) / 1e9
                        assertTrue("$name: 遅延 $delayS s", delayS > 0 && delayS <= params.confirmationDelayS + 0.05)
                    }
                }
            }
            assertEquals(8.0, params.confirmationDelayS, 0.001)
        }
    }

    /** 同じ入力で P1 と P2 候補を並行に動かしても、それぞれ単独で動かした結果と同じ（状態を共有しない）。 */
    @Test
    fun runningP1AndCandidateSideBySideMatchesRunningEachAlone() {
        for (name in listOf("hook_200612", "weak_202426", "casts_150936")) {
            val (t0, rows) = load(name)
            val p1 = CastDetector(DetectorParams.P1, t0)
            val p2 = CastDetector(DetectorParams.P2_CANDIDATE, t0)
            val out1 = ArrayList<CastDetection>()
            val out2 = ArrayList<CastDetection>()
            for (r in rows) {
                out1 += p1.onSample(r.kind, r.ts, r.x, r.y, r.z)
                out2 += p2.onSample(r.kind, r.ts, r.x, r.y, r.z)
            }
            out1 += p1.finish()
            out2 += p2.finish()
            assertEquals(name, detect(t0, rows, DetectorParams.P1), out1)
            assertEquals(name, detect(t0, rows, DetectorParams.P2_CANDIDATE), out2)
            assertTrue(out1.all { it.gyroDirCos.isNaN() } && out2.none { it.gyroDirCos.isNaN() })
        }
    }

    @Test
    fun pendingFlagIsSetWhileCandidateIsUndecided() {
        val (t0, rows) = load("casts_150936")
        val d = CastDetector(DetectorParams(), t0)
        var sawPending = false
        var confirmed = 0
        for (r in rows.sortedBy { it.ts }) {
            val out = d.onSample(r.kind, r.ts, r.x, r.y, r.z)
            if (d.hasPending) sawPending = true
            confirmed += out.size
        }
        assertTrue(sawPending)
        assertTrue(confirmed > 0)
        d.finish()
        assertFalse(d.hasPending)
    }

    @Test(expected = IllegalArgumentException::class)
    fun smoothingIsRejected() {
        DetectorParams(smoothS = 0.2)
    }
}
