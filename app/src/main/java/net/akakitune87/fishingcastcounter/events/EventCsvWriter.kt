package net.akakitune87.fishingcastcounter.events

import java.io.Closeable
import java.io.Writer

/**
 * イベントを session_*.events.csv に書き出す。センサー CSV（CsvSessionWriter）とは別ファイル。
 *
 * 列: event_id,type,timestamp_ns,recorded_ns,ref_id,detail
 * - ref_id は無ければ空
 * - detail は key=value を ; で連結（値にカンマ・セミコロンを含めないこと）
 *
 * スレッドセーフではないため、単一の書き込みスレッドからのみ呼び出すこと。
 */
class EventCsvWriter(private val out: Writer) : Closeable {

    init {
        out.write(HEADER)
        out.write("\n")
    }

    fun write(e: SessionEvent) {
        val detail = e.detail.entries.joinToString(";") { (k, v) ->
            require(k.none { it in FORBIDDEN } && v.none { it in FORBIDDEN }) { "detail に使えない文字: $k=$v" }
            "$k=$v"
        }
        out.write("${e.id},${e.type.name},${e.timestampNs},${e.recordedNs},${e.refId ?: ""},$detail\n")
    }

    fun flush() {
        out.flush()
    }

    override fun close() {
        out.close()
    }

    companion object {
        const val HEADER = "event_id,type,timestamp_ns,recorded_ns,ref_id,detail"
        private val FORBIDDEN = charArrayOf(',', ';', '\n', '\r', '=')
    }
}
