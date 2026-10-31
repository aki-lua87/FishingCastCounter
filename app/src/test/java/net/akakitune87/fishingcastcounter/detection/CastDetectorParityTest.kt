package net.akakitune87.fishingcastcounter.detection

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test
import java.io.BufferedReader
import java.io.File
import java.io.InputStream
import java.util.zip.GZIPInputStream

/**
 * Python 版（analysis/castlab）と同じデータを流し、検知時刻・件数・特徴量が完全に一致することを確かめる。
 *
 * 期待値は analysis/tools/export_parity_fixtures.py で作る:
 * - src/test/resources/parity: 実データの切り出し（キャスト・見逃し・誤検出・歩行）。常に実行
 * - captures/parity: 全セッション分（git 管理外）。手元にあれば実行
 */
class CastDetectorParityTest {

    @Test
    fun segmentsMatchPython() {
        val names = listOf("casts_150936", "mixed_113449", "impacts_113449", "walk_085344", "weak_202426", "hook_200612")
        for (paramsName in listOf("p1", "baseline_v1", "p2_candidate")) {
            val params = parseParams(resource("params_$paramsName.json").readText())
            for (name in names) {
                val t0 = parseMeta(resource("$name.meta.txt").readText())
                val actual = run(params, t0, GZIPInputStream(resource("$name.csv.gz")).bufferedReader())
                val expected = parseExpected(resource("$name.$paramsName.expected.csv").readText())
                assertDetections("$name/$paramsName", expected, actual)
            }
        }
    }

    /** アプリで使うプリセットが Python 側のパラメータファイル（default.json / p2_candidate.json）と同じであること。 */
    @Test
    fun presetsMatchPythonParams() {
        assertEquals(parseParams(resource("params_p1.json").readText()), DetectorParams.P1)
        assertEquals(parseParams(resource("params_p2_candidate.json").readText()), DetectorParams.P2_CANDIDATE)
        assertTrue(!DetectorParams.P1.usesDirection && DetectorParams.P2_CANDIDATE.usesDirection)
    }

    @Test
    fun fullSessionsMatchPython() {
        val parityDir = File("../captures/parity")
        val sessionsDir = File("../captures/sessions")
        assumeTrue("captures/parity が無いので省略", parityDir.isDirectory && sessionsDir.isDirectory)
        val expectedFiles = parityDir.listFiles { f -> f.name.endsWith(".expected.csv") }.orEmpty().sortedBy { it.name }
        assumeTrue(expectedFiles.isNotEmpty())
        var total = 0
        for (file in expectedFiles) {
            val (session, paramsName) = file.name.removeSuffix(".expected.csv").split(".")
            val params = parseParams(File(parityDir, "params_$paramsName.json").readText())
            val t0 = parseMeta(File(sessionsDir, "$session.meta.txt").readText())
            val actual = File(sessionsDir, "$session.csv").bufferedReader().use { run(params, t0, it) }
            val expected = parseExpected(file.readText())
            assertDetections("$session/$paramsName", expected, actual)
            total += expected.size
        }
        // 比較対象が空でないこと（全部 0 件同士で一致、を防ぐ）
        assertTrue("期待値の検出が 0 件", total > 0)
    }

    private fun run(params: DetectorParams, t0: Long, reader: BufferedReader): List<CastDetection> {
        val detector = CastDetector(params, t0)
        val out = ArrayList<CastDetection>()
        reader.useLines { lines ->
            lines.drop(1).forEach { line ->
                val c = line.split(",")
                val kind = when (c[1]) {
                    "ACCEL" -> MotionKind.ACCEL
                    "GYRO" -> MotionKind.GYRO
                    else -> return@forEach
                }
                // ウォッチ上と同じく float32 の値を Double にして渡す
                out += detector.onSample(
                    kind,
                    c[0].toLong(),
                    c[2].toFloat().toDouble(),
                    c[3].toFloat().toDouble(),
                    c[4].toFloat().toDouble(),
                )
            }
        }
        out += detector.finish()
        return out
    }

    private fun assertDetections(label: String, expected: List<CastDetection>, actual: List<CastDetection>) {
        assertEquals("$label: 検出時刻", expected.map { it.gridIndex }, actual.map { it.gridIndex })
        assertEquals("$label: timestamp", expected.map { it.timestampNs }, actual.map { it.timestampNs })
        assertEquals("$label: |a| ピーク", expected.map { it.accelPeak }, actual.map { it.accelPeak })
        assertEquals("$label: |g| ピーク", expected.map { it.gyroPeak }, actual.map { it.gyroPeak })
        assertEquals("$label: ピーク後 |g| 平均", expected.map { it.postGyroMean }, actual.map { it.postGyroMean })
        // P1 では両方 NaN（List の比較は Double.equals なので NaN 同士は等しい）
        assertEquals("$label: 回転方向", expected.map { it.gyroDirCos }, actual.map { it.gyroDirCos })
        assertEquals("$label: 構え姿勢", expected.map { it.postDirCos }, actual.map { it.postDirCos })
    }

    private fun resource(name: String): InputStream =
        javaClass.getResourceAsStream("/parity/$name") ?: error("テストデータがありません: parity/$name")

    private fun InputStream.readText(): String = bufferedReader().use { it.readText() }

    private fun parseMeta(text: String): Long =
        text.lines().first { it.startsWith("start_elapsed_realtime_ns=") }.substringAfter("=").toLong()

    /** 方向条件を使うパラメータ（P2 候補）の期待値には gyro_dir_cos, post_dir_cos 列が付く。 */
    private fun parseExpected(text: String): List<CastDetection> = text.lines().drop(1).filter { it.isNotBlank() }.map {
        val c = it.split(",")
        CastDetection(
            c[0].toLong(), c[1].toLong(), c[2].toDouble(), c[3].toDouble(), c[4].toDouble(),
            gyroDirCos = c.getOrNull(5)?.toDouble() ?: Double.NaN,
            postDirCos = c.getOrNull(6)?.toDouble() ?: Double.NaN,
        )
    }

    companion object {
        /** Python 版 DetectorParams.save() の JSON（フラットな数値のみ）を読む。方向条件の項目は無ければ既定値。 */
        fun parseParams(json: String): DetectorParams {
            val v = Regex("\"(\\w+)\"\\s*:\\s*([-0-9.eE]+)").findAll(json)
                .associate { it.groupValues[1] to it.groupValues[2].toDouble() }
            val d = DetectorParams()
            return DetectorParams(
                gyroDirCosMin = v["gyro_dir_cos_min"] ?: d.gyroDirCosMin,
                postDirCosMin = v["post_dir_cos_min"] ?: d.postDirCosMin,
                gyroDirX = v["gyro_dir_x"] ?: d.gyroDirX,
                gyroDirY = v["gyro_dir_y"] ?: d.gyroDirY,
                gyroDirZ = v["gyro_dir_z"] ?: d.gyroDirZ,
                postDirX = v["post_dir_x"] ?: d.postDirX,
                postDirY = v["post_dir_y"] ?: d.postDirY,
                postDirZ = v["post_dir_z"] ?: d.postDirZ,
                dirWindowBeforeS = v["dir_window_before_s"] ?: d.dirWindowBeforeS,
                dirWindowAfterS = v["dir_window_after_s"] ?: d.dirWindowAfterS,
                accelPeakMin = v.getValue("accel_peak_min"),
                gyroPeakMin = v.getValue("gyro_peak_min"),
                gyroWindowBeforeS = v.getValue("gyro_window_before_s"),
                gyroWindowAfterS = v.getValue("gyro_window_after_s"),
                minIntervalS = v.getValue("min_interval_s"),
                smoothS = v.getValue("smooth_s"),
                postGyroMeanMax = v.getValue("post_gyro_mean_max"),
                postStillStartS = v.getValue("post_still_start_s"),
                postStillEndS = v.getValue("post_still_end_s"),
            )
        }
    }
}
