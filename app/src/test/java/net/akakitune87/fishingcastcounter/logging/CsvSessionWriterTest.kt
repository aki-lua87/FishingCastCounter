package net.akakitune87.fishingcastcounter.logging

import org.junit.Assert.assertEquals
import org.junit.Test
import java.io.StringWriter

class CsvSessionWriterTest {

    @Test
    fun writesHeaderSamplesAndMarkersInArrivalOrder() {
        val out = StringWriter()
        CsvSessionWriter(out).use { writer ->
            writer.writeSample(RowType.ACCEL, 1000L, 0.5f, -9.8f, 1.25f)
            writer.writeSample(RowType.GYRO, 1500L, 0.0f, 1.0f, -2.0f)
            writer.writeMarker(MarkerType.CAST, 1700L)
            writer.writeMarker(MarkerType.OTHER, 1800L)
        }

        val expected = """
            timestamp_ns,type,x,y,z
            1000,ACCEL,0.5,-9.8,1.25
            1500,GYRO,0.0,1.0,-2.0
            1700,MARK_CAST,,,
            1800,MARK_OTHER,,,
        """.trimIndent() + "\n"
        assertEquals(expected, out.toString())
    }

    @Test
    fun everyRowHasFiveColumns() {
        val out = StringWriter()
        CsvSessionWriter(out).use { writer ->
            writer.writeSample(RowType.GYRO, 1L, 1f, 2f, 3f)
            MarkerType.entries.forEach { writer.writeMarker(it, 2L) }
        }

        out.toString().trimEnd().lines().forEach { line ->
            assertEquals(line, 5, line.split(",").size)
        }
    }
}
