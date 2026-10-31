package net.akakitune87.fishingcastcounter.detection

import kotlin.math.ceil
import kotlin.math.max
import kotlin.math.min
import kotlin.math.roundToLong
import kotlin.math.sqrt

/**
 * キャスト検知のパラメータ。名前と意味は Python 版（analysis/castlab/detector.py の DetectorParams）と同じ。
 * 既定値は analysis/params/default.json（P1）。P2 候補は [P2_CANDIDATE]（analysis/params/p2_candidate.json）。
 *
 * 方向条件（gyroDirCosMin / postDirCosMin）は軸の向きに依存する特徴量で、テンプレートは **左腕装着** で記録した
 * キャストから作ったもの。右腕装着では未検証（軸の符号が変わるので、そのままでは使えない）。
 */
data class DetectorParams(
    val accelPeakMin: Double = 25.0,
    val gyroPeakMin: Double = 4.0,
    val gyroWindowBeforeS: Double = 1.5,
    val gyroWindowAfterS: Double = 0.5,
    val minIntervalS: Double = 5.0,
    val smoothS: Double = 0.0,
    val postGyroMeanMax: Double = 1.5,
    val postStillStartS: Double = 0.5,
    val postStillEndS: Double = 3.0,
    /** リサンプリング周波数（Python 版 resample の fs）。 */
    val fs: Double = 50.0,
    /** これより長い記録抜けは補間しない（Python 版 resample の max_gap_s）。 */
    val maxGapS: Double = 0.2,
    /** 回転方向（ピーク付近で |g| 最大の時点の角速度ベクトル）とテンプレートのコサインの下限。-1 で無効（P1） */
    val gyroDirCosMin: Double = -1.0,
    /** ピーク後の構え姿勢（静止窓の加速度平均 = 重力の向き）とテンプレートのコサインの下限。-1 で無効（P1） */
    val postDirCosMin: Double = -1.0,
    val gyroDirX: Double = -0.8858,
    val gyroDirY: Double = 0.0390,
    val gyroDirZ: Double = 0.4623,
    val postDirX: Double = 0.3135,
    val postDirY: Double = -0.9483,
    val postDirZ: Double = 0.0487,
    val dirWindowBeforeS: Double = 0.3,
    val dirWindowAfterS: Double = 0.1,
) {
    init {
        // Python 版の平滑化は中心化移動平均（未来のデータが必要）で、既定では使っていないため未対応
        require(smoothS == 0.0) { "smoothS は未対応です（0 のみ）" }
    }

    /** ピークから AUTO_CAST が確定するまでの最大遅延（秒）。UI の案内やメタデータ用。 */
    val confirmationDelayS: Double get() = (maxNeighborOffset + postEnd + 1) / fs

    internal val before: Int get() = pyRound(gyroWindowBeforeS * fs)
    internal val after: Int get() = pyRound(gyroWindowAfterS * fs)
    internal val postStart: Int get() = pyRound(postStillStartS * fs)
    internal val postEnd: Int get() = pyRound(postStillEndS * fs)
    internal val minGap: Double get() = minIntervalS * fs
    internal val dirBefore: Int get() = pyRound(dirWindowBeforeS * fs)
    internal val dirAfter: Int get() = pyRound(dirWindowAfterS * fs)

    /** 方向条件を使うか（P2 候補）。使わなければ P1 と同じ処理・同じ出力。 */
    val usesDirection: Boolean get() = gyroDirCosMin > -1.0 || postDirCosMin > -1.0

    /** 近接抑制で比較相手になりうる最大のインデックス差（minGap 未満の最大の整数）。 */
    internal val maxNeighborOffset: Int get() = ceil(minGap).toInt() - 1

    fun describe(): List<String> = listOf(
        "accel_peak_min=$accelPeakMin",
        "gyro_peak_min=$gyroPeakMin",
        "gyro_window_before_s=$gyroWindowBeforeS",
        "gyro_window_after_s=$gyroWindowAfterS",
        "min_interval_s=$minIntervalS",
        "smooth_s=$smoothS",
        "post_gyro_mean_max=$postGyroMeanMax",
        "post_still_start_s=$postStillStartS",
        "post_still_end_s=$postStillEndS",
    ) + if (usesDirection) {
        listOf(
            "gyro_dir_cos_min=$gyroDirCosMin",
            "post_dir_cos_min=$postDirCosMin",
            "gyro_dir_x=$gyroDirX",
            "gyro_dir_y=$gyroDirY",
            "gyro_dir_z=$gyroDirZ",
            "post_dir_x=$postDirX",
            "post_dir_y=$postDirY",
            "post_dir_z=$postDirZ",
            "dir_window_before_s=$dirWindowBeforeS",
            "dir_window_after_s=$dirWindowAfterS",
        )
    } else {
        emptyList()
    }

    companion object {
        /** 現行の正式な検知器（analysis/params/default.json）。 */
        val P1 = DetectorParams()

        /**
         * P2 候補（2026-10-31 フィールドレビューの案 C。analysis/params/p2_candidate.json）。
         * P1 と並行して動かして評価するためのもので、正式なカウントには使わない。左腕装着が前提
         */
        val P2_CANDIDATE = DetectorParams(accelPeakMin = 21.0, gyroDirCosMin = 0.8, postDirCosMin = 0.85)
    }
}

/** Python の round()（偶数丸め）。int(round(x)) と同じ結果にする。 */
private fun pyRound(x: Double): Int = Math.rint(x).toInt()

/** コサイン類似度。Python 版 detector._cos と同じ式・順序（どちらかが零ベクトルなら 0）。 */
internal fun cosine(vx: Double, vy: Double, vz: Double, ux: Double, uy: Double, uz: Double): Double {
    val nv = sqrt(vx * vx + vy * vy + vz * vz)
    val nu = sqrt(ux * ux + uy * uy + uz * uz)
    if (nv == 0.0 || nu == 0.0) return 0.0
    return (vx * ux + vy * uy + vz * uz) / (nv * nu)
}

enum class MotionKind { ACCEL, GYRO }

/**
 * 確定したキャスト検知。
 *
 * @param gridIndex セッション開始（t0）からのグリッド番号。時刻 = t0 + gridIndex / fs
 * @param timestampNs ピーク時刻（センサーと同じ elapsedRealtimeNanos 基準）
 * @param gyroDirCos 回転方向のコサイン。方向条件を使う検知器（P2 候補）のときだけ値が入り、P1 では NaN
 * @param postDirCos 構え姿勢のコサイン。同上
 */
data class CastDetection(
    val gridIndex: Long,
    val timestampNs: Long,
    val accelPeak: Double,
    val gyroPeak: Double,
    val postGyroMean: Double,
    val gyroDirCos: Double = Double.NaN,
    val postDirCos: Double = Double.NaN,
)

/** ストリーミングのキャスト検知器（[CastDetector]。P2 候補の隔離のテストでは差し替える）。 */
interface StreamingCastDetector {
    fun onSample(kind: MotionKind, timestampNs: Long, x: Double, y: Double, z: Double): List<CastDetection>

    fun finish(): List<CastDetection>
}

/**
 * P1 キャスト検知器のストリーミング実装。Python 版（analysis/castlab/detector.py + signals.resample）と
 * 同じ入力に対して同じ検知時刻・特徴量を返す（app/src/test の CastDetectorParityTest で確認）。
 *
 * 処理:
 * 1. ACCEL / GYRO をそれぞれ t0 基準の 50Hz グリッドに線形補間（numpy.interp と同じ式。max_gap 超の抜けは NaN）
 * 2. 両方そろったグリッド点ごとに |a|, |g| を計算（NaN は 0）
 * 3. |a| の局所最大 → ジャイロ条件 → ピーク後静止条件 →（P2 候補のみ）回転方向・構え姿勢 → 前後 minInterval 内の比較で確定
 *
 * 確定にはピーク後 [maxNeighborOffset + postEnd] サンプル分のデータが必要なので、
 * 検知は [DetectorParams.confirmationDelayS]（既定で約 8 秒）遅れて返る。
 *
 * スレッドセーフではない。単一スレッドから呼ぶこと。
 */
class CastDetector(
    val params: DetectorParams,
    private val t0Ns: Long,
) : StreamingCastDetector {
    private val resampler = StreamingResampler(params.fs, params.maxGapS, t0Ns)

    // グリッド（先頭 = Python 版の配列インデックス 0）上の |a|, |g|。古い値はリングバッファで捨てる
    private val capacity = Integer.highestOneBit((max(params.before, params.dirBefore) + params.postEnd + 4) * 2)
    private val am = DoubleArray(capacity)
    private val gm = DoubleArray(capacity)

    // 方向条件用の 3 軸の値（0〜2 = 加速度 xyz、3〜5 = 角速度 xyz。NaN は 0）。P1 では確保しない
    private val axes: Array<DoubleArray>? = if (params.usesDirection) Array(6) { DoubleArray(capacity) } else null
    private var count = 0L // 受け取ったグリッド点の数（= 次のインデックス）
    private var firstGridIndex = 0L

    private class Candidate(
        val i: Long,
        val accel: Double,
        var gyro: Double = 0.0,
        var post: Double = 0.0,
        var gyroDirCos: Double = Double.NaN,
        var postDirCos: Double = Double.NaN,
    )

    /** |a| の局所最大で、ジャイロ・静止条件の判定待ちのもの。 */
    private val rawPeaks = ArrayDeque<Candidate>()

    /** 条件を満たした候補（時刻順）。近接抑制の判定と、後続候補の比較相手として保持する。 */
    private val candidates = ArrayList<Candidate>()
    private var decidedUpTo = 0 // candidates のうち確定判定済みの数

    /** 判定待ちの候補があるか（UI の「判定中」表示用）。 */
    val hasPending: Boolean get() = rawPeaks.isNotEmpty() || decidedUpTo < candidates.size

    override fun onSample(kind: MotionKind, timestampNs: Long, x: Double, y: Double, z: Double): List<CastDetection> {
        val out = ArrayList<CastDetection>(0)
        resampler.add(kind, timestampNs, x, y, z) { k, a, g -> onGrid(k, a, g, out) }
        return out
    }

    /** 入力の終わり。窓が足りない候補も Python 版と同じく切り詰めた窓で判定する。 */
    override fun finish(): List<CastDetection> {
        val out = ArrayList<CastDetection>(0)
        val n = count
        while (rawPeaks.isNotEmpty()) evaluate(rawPeaks.removeFirst(), n)
        decide(Long.MAX_VALUE, out)
        return out
    }

    private fun onGrid(k: Long, accel: DoubleArray, gyro: DoubleArray, out: MutableList<CastDetection>) {
        if (count == 0L) firstGridIndex = k
        val n = count
        am[slot(n)] = nanToZero(norm(accel))
        gm[slot(n)] = nanToZero(norm(gyro))
        axes?.let {
            for (c in 0 until 3) {
                it[c][slot(n)] = nanToZero(accel[c])
                it[3 + c][slot(n)] = nanToZero(gyro[c])
            }
        }
        count++

        // 1 つ前の点が局所最大か（Python: am[i] >= thr && am[i] > am[i-1] && am[i] >= am[i+1]）
        val i = n - 1
        if (i >= 1) {
            val c = am[slot(i)]
            if (c >= params.accelPeakMin && c > am[slot(i - 1)] && c >= am[slot(n)]) rawPeaks.addLast(Candidate(i, c))
        }
        // ピーク後の窓がそろった候補を判定
        while (rawPeaks.isNotEmpty() && n >= rawPeaks.first().i + params.postEnd) {
            evaluate(rawPeaks.removeFirst(), count)
        }
        // ここまでで、インデックス n - postEnd 以前の候補はすべて判定済み
        decide(n - params.postEnd - params.maxNeighborOffset, out)
    }

    /** ジャイロ条件と静止条件。size は現時点のグリッド点数（finish 時は窓を切り詰める）。 */
    private fun evaluate(c: Candidate, size: Long) {
        val from = max(0L, c.i - params.before)
        val to = min(size, c.i + params.after + 1)
        var gpk = Double.NEGATIVE_INFINITY
        for (j in from until to) gpk = max(gpk, gm[slot(j)])
        if (gpk < params.gyroPeakMin) return

        val pFrom = c.i + params.postStart
        val pTo = min(size, c.i + params.postEnd + 1)
        val post = if (pTo > pFrom) {
            val values = DoubleArray((pTo - pFrom).toInt()) { gm[slot(pFrom + it)] }
            NumpySum.pairwise(values, 0, values.size) / values.size
        } else {
            Double.POSITIVE_INFINITY
        }
        if (params.postGyroMeanMax > 0 && post > params.postGyroMeanMax) return
        c.gyro = gpk
        c.post = post
        if (axes != null && !directionMatches(axes, c, size, pFrom, pTo)) return
        candidates.add(c)
    }

    /** 回転方向と構え姿勢（Python 版 detect の 4a / 4b と同じ計算）。 */
    private fun directionMatches(ax: Array<DoubleArray>, c: Candidate, size: Long, pFrom: Long, pTo: Long): Boolean {
        // |g| が最大の時点（同値なら先の時点。numpy.argmax と同じ）
        val from = max(0L, c.i - params.dirBefore)
        val to = min(size, c.i + params.dirAfter + 1)
        var jm = from
        for (j in from + 1 until to) if (gm[slot(j)] > gm[slot(jm)]) jm = j
        val s = slot(jm)
        val gyroCos = cosine(ax[3][s], ax[4][s], ax[5][s], params.gyroDirX, params.gyroDirY, params.gyroDirZ)
        if (gyroCos < params.gyroDirCosMin) return false

        // 静止判定と同じ窓の加速度平均（軸ごとに numpy の pairwise 加算で合計）
        val len = (pTo - pFrom).toInt()
        val mean = DoubleArray(3)
        if (len > 0) {
            for (k in 0 until 3) {
                val values = DoubleArray(len) { ax[k][slot(pFrom + it)] }
                mean[k] = NumpySum.pairwise(values, 0, len) / len
            }
        }
        val postCos = cosine(mean[0], mean[1], mean[2], params.postDirX, params.postDirY, params.postDirZ)
        if (postCos < params.postDirCosMin) return false
        c.gyroDirCos = gyroCos
        c.postDirCos = postCos
        return true
    }

    /** インデックスが limit 以下の候補を確定する。 */
    private fun decide(limit: Long, out: MutableList<CastDetection>) {
        while (decidedUpTo < candidates.size && candidates[decidedUpTo].i <= limit) {
            val k = decidedUpTo
            val c = candidates[k]
            if (!isBeaten(k)) out.add(toDetection(c))
            decidedUpTo++
        }
        // 比較相手として必要な範囲より古い候補を捨てる。まだ判定していない候補・今後見つかる候補
        // （インデックスは count - 1 以上）のどれからも minGap 以上離れていれば不要
        var oldestNeeded = count - 1
        if (decidedUpTo < candidates.size) oldestNeeded = min(oldestNeeded, candidates[decidedUpTo].i)
        rawPeaks.firstOrNull()?.let { oldestNeeded = min(oldestNeeded, it.i) }
        var drop = 0
        while (drop < decidedUpTo && oldestNeeded - candidates[drop].i >= params.minGap) drop++
        if (drop > 0) {
            candidates.subList(0, drop).clear()
            decidedUpTo -= drop
        }
    }

    /** Python 版と同じ規則: 前後 minGap 未満により大きい候補（同値なら先の候補）があれば負け。 */
    private fun isBeaten(k: Int): Boolean {
        val c = candidates[k]
        for (j in k - 1 downTo 0) {
            val d = candidates[j]
            if (c.i - d.i >= params.minGap) break
            if (d.accel >= c.accel) return true
        }
        for (j in k + 1 until candidates.size) {
            val d = candidates[j]
            if (d.i - c.i >= params.minGap) break
            if (d.accel > c.accel) return true
        }
        return false
    }

    private fun toDetection(c: Candidate): CastDetection {
        val grid = firstGridIndex + c.i
        return CastDetection(
            gridIndex = grid,
            timestampNs = t0Ns + (grid * 1e9 / params.fs).roundToLong(),
            accelPeak = c.accel,
            gyroPeak = c.gyro,
            postGyroMean = c.post,
            gyroDirCos = c.gyroDirCos,
            postDirCos = c.postDirCos,
        )
    }

    private fun slot(i: Long): Int = (i and (capacity - 1).toLong()).toInt()

    private fun norm(v: DoubleArray): Double = sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])

    private fun nanToZero(x: Double): Double = if (x.isNaN()) 0.0 else x
}

/**
 * ACCEL / GYRO を共通の等間隔グリッドに線形補間する（Python 版 signals.resample と同じ規則）。
 *
 * - グリッド: t = k / fs（t0 からの秒）。開始は ceil(max(最初の ACCEL, 最初の GYRO) * fs)
 * - 補間: numpy.interp と同じ式
 * - 連続する 2 サンプルの間隔が maxGapS を超える区間内のグリッド点は NaN
 * - 両方のセンサーの値がそろったグリッド点から順に emit する
 *
 * 各センサーの timestamp は単調増加を前提とし、同じか過去の timestamp は捨てる（Python 版の重複除去に相当）。
 */
internal class StreamingResampler(private val fs: Double, private val maxGapS: Double, private val t0Ns: Long) {

    private class Sample(val t: Double, val v: DoubleArray)

    private inner class Channel {
        var first: Double = Double.NaN
        var lastNs: Long = Long.MIN_VALUE
        var prev: Sample? = null
        val pending = ArrayDeque<Sample>() // 開始グリッドが決まるまで / 補間待ちのサンプル
        var nextK = Long.MIN_VALUE
        val values = ArrayDeque<DoubleArray>() // nextK より前に計算済みで、相手待ちの値
        var valuesStartK = Long.MIN_VALUE
    }

    private val accel = Channel()
    private val gyro = Channel()
    private var startK = Long.MIN_VALUE

    fun add(kind: MotionKind, timestampNs: Long, x: Double, y: Double, z: Double, emit: (Long, DoubleArray, DoubleArray) -> Unit) {
        val ch = if (kind == MotionKind.ACCEL) accel else gyro
        if (timestampNs <= ch.lastNs) return
        ch.lastNs = timestampNs
        // Python: (timestamp_ns - t0_ns) / 1e9（int64 の差を float64 で割る）
        val t = (timestampNs - t0Ns).toDouble() / 1e9
        if (ch.first.isNaN()) ch.first = t
        ch.pending.addLast(Sample(t, doubleArrayOf(x, y, z)))

        if (startK == Long.MIN_VALUE) {
            if (accel.first.isNaN() || gyro.first.isNaN()) return
            startK = ceil(max(accel.first, gyro.first) * fs).toLong()
            for (c in listOf(accel, gyro)) {
                c.nextK = startK
                c.valuesStartK = startK
            }
            interpolate(accel)
            interpolate(gyro)
        } else {
            interpolate(ch)
        }
        while (accel.values.isNotEmpty() && gyro.values.isNotEmpty()) {
            val k = accel.valuesStartK
            emit(k, accel.values.removeFirst(), gyro.values.removeFirst())
            accel.valuesStartK++
            gyro.valuesStartK++
        }
    }

    /** 最新サンプルの時刻以下のグリッド点をすべて補間する。 */
    private fun interpolate(ch: Channel) {
        while (ch.pending.isNotEmpty()) {
            val next = ch.pending.first()
            while (true) {
                val x = ch.nextK.toDouble() / fs
                val prev = ch.prev
                if (x > next.t) break
                ch.values.addLast(
                    when {
                        x == next.t -> next.v.copyOf()
                        prev == null -> DoubleArray(3) { Double.NaN } // 開始グリッドより前のサンプルが無い（起こらない）
                        x == prev.t -> prev.v.copyOf()
                        next.t - prev.t > maxGapS && x > prev.t -> DoubleArray(3) { Double.NaN }
                        else -> DoubleArray(3) { NumpyInterp.between(x, prev.t, next.t, prev.v[it], next.v[it]) }
                    },
                )
                ch.nextK++
            }
            ch.prev = next
            ch.pending.removeFirst()
        }
    }
}

/** numpy の数値計算を同じ順序で再現する（Python 版と完全一致させるため）。 */
internal object NumpyInterp {
    /** numpy.interp の区間内補間（compiled_base.c の arr_interp と同じ式）。 */
    fun between(x: Double, x0: Double, x1: Double, y0: Double, y1: Double): Double {
        val slope = (y1 - y0) / (x1 - x0)
        var r = slope * (x - x0) + y0
        if (r.isNaN()) {
            r = slope * (x - x1) + y1
            if (r.isNaN() && y0 == y1) r = y0
        }
        return r
    }
}

internal object NumpySum {
    private const val BLOCK = 128

    /** numpy の pairwise_sum（float64 の add.reduce）と同じ加算順序。 */
    fun pairwise(a: DoubleArray, start: Int, n: Int): Double {
        if (n < 8) {
            var res = 0.0
            for (i in 0 until n) res += a[start + i]
            return res
        }
        if (n <= BLOCK) {
            val r = DoubleArray(8) { a[start + it] }
            var i = 8
            while (i < n - (n % 8)) {
                for (j in 0 until 8) r[j] += a[start + i + j]
                i += 8
            }
            var res = ((r[0] + r[1]) + (r[2] + r[3])) + ((r[4] + r[5]) + (r[6] + r[7]))
            while (i < n) {
                res += a[start + i]
                i++
            }
            return res
        }
        var n2 = n / 2
        n2 -= n2 % 8
        return pairwise(a, start, n2) + pairwise(a, start + n2, n - n2)
    }
}
