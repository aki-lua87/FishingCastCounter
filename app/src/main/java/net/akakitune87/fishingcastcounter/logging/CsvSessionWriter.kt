package net.akakitune87.fishingcastcounter.logging

import java.io.Closeable
import java.io.Writer

/**
 * 1 セッション分のセンサーデータを CSV として書き出す。
 *
 * スレッドセーフではないため、単一の書き込みスレッドからのみ呼び出すこと。
 * Android に依存しないので JVM ユニットテストで検証できる。
 */
class CsvSessionWriter(private val out: Writer) : Closeable {

    init {
        out.write(HEADER)
        out.write("\n")
    }

    fun writeSample(type: RowType, timestampNs: Long, x: Float, y: Float, z: Float) {
        out.write("$timestampNs,${type.name},$x,$y,$z\n")
    }

    fun writeMarker(marker: MarkerType, timestampNs: Long) {
        out.write("$timestampNs,${marker.rowType.name},,,\n")
    }

    fun flush() {
        out.flush()
    }

    override fun close() {
        out.close()
    }

    companion object {
        const val HEADER = "timestamp_ns,type,x,y,z"
    }
}
