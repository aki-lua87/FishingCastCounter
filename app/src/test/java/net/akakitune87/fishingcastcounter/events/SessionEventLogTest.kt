package net.akakitune87.fishingcastcounter.events

import net.akakitune87.fishingcastcounter.detection.CastDetection
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.StringWriter

class SessionEventLogTest {

    private val s = 1_000_000_000L

    private fun auto(log: SessionEventLog, tSec: Long): SessionEvent =
        log.recordAutoCast(CastDetection(tSec * 50, tSec * s, 30.0, 8.0, 0.2), recordedNs = (tSec + 8) * s)

    @Test
    fun idsAreUniqueAndSequentialAcrossTypes() {
        val log = SessionEventLog()
        auto(log, 10)
        log.recordMarker(EventType.CAST, 20 * s)
        log.recordMiss(30 * s, detectorPending = false)
        auto(log, 40)
        assertEquals(listOf(1L, 2L, 3L, 4L), log.all.map { it.id })
    }

    @Test
    fun candidateCastDoesNotAffectCountsOrTargets() {
        val log = SessionEventLog()
        val p1 = auto(log, 10)
        val p2 = log.recordCandidateCast(
            CastDetection(12 * 50L, 12 * s, 22.0, 7.0, 0.3, gyroDirCos = 0.95, postDirCos = 0.97),
            recordedNs = 20 * s,
        )
        assertEquals(EventType.AUTO_CAST_P2, p2.type)
        assertEquals("P2_CANDIDATE", p2.detail["detector"])
        assertEquals("0.9500", p2.detail["gyro_dir_cos"])
        assertEquals("0.9700", p2.detail["post_dir_cos"])
        assertEquals(1, log.candidateCount)
        // 正式なカウント・FALSE / UNDO の対象・直近の自動検知は P1 だけで決まる
        assertEquals(CastCounts(auto = 1), log.counts)
        assertEquals(p1.id, log.lastAutoCast?.id)
        assertEquals(p1.id, log.falseTarget()?.id)
        assertNull(log.undoTarget())
        assertEquals(EventType.FALSE, log.recordFalse(21 * s, targetId = p1.id).event?.type)
        assertNull(log.recordFalse(22 * s, targetId = p2.id).event)
        // P1 の AUTO_CAST の detail は従来どおり（方向の項目は付かない）
        assertEquals(listOf("grid", "accel_peak", "gyro_peak", "post_gyro_mean", "delay_ms"), p1.detail.keys.toList())
    }

    /** P2 候補が間に挟まって ID が飛び飛びになっても、FALSE / MISS / UNDO は P1 の AUTO_CAST と手動イベントだけを見る。 */
    @Test
    fun falseMissUndoIgnoreInterleavedCandidateCasts() {
        val log = SessionEventLog()
        fun p2(tSec: Long) = log.recordCandidateCast(
            CastDetection(tSec * 50, tSec * s, 22.0, 7.0, 0.3, 0.9, 0.95),
            recordedNs = (tSec + 8) * s,
        )
        p2(5) // #1
        val a1 = auto(log, 10) // #2
        p2(10) // #3（同じピークを P2 も検知）
        val cast = log.recordMarker(EventType.CAST, 15 * s).event!! // #4
        p2(20) // #5
        val a2 = auto(log, 30) // #6
        p2(31) // #7
        assertEquals(listOf(2L, 6L), log.all.filter { it.type == EventType.AUTO_CAST }.map { it.id })

        // FALSE の対象は FALSE の付いていない最新の P1。P2 の方が新しくても P2 にはならない
        assertEquals(a2.id, log.falseTarget()?.id)
        assertEquals(a2.id, log.recordFalse(40 * s, a2.id).event?.refId)
        assertEquals(a1.id, log.falseTarget()?.id)
        assertEquals(a1.id, log.recordFalse(41 * s, a1.id).event?.refId)
        assertNull(log.falseTarget())

        // MISS の「直前の自動検知」は P1
        p2(45)
        val miss = log.recordMiss(50 * s, detectorPending = false).event!!
        assertEquals(a2.id.toString(), miss.detail["last_auto_id"])

        // UNDO は手動イベントだけをさかのぼる（P2 は飛ばす）
        assertEquals(miss.id, log.undoTarget()?.id)
        log.recordUndo(51 * s, miss.id)
        val falses = log.all.filter { it.type == EventType.FALSE }
        log.recordUndo(52 * s, falses[1].id)
        log.recordUndo(53 * s, falses[0].id)
        assertEquals(cast.id, log.undoTarget()?.id)

        assertEquals(CastCounts(auto = 2, miss = 0, falseCount = 0, undo = 3), log.counts)
        assertEquals(5, log.candidateCount)
    }

    /** P2 候補の detail は EventCsvWriter で書ける（禁止文字なし）。P1 の行の形式は変わらない。 */
    @Test
    fun candidateCastIsWrittenAsItsOwnRow() {
        val log = SessionEventLog()
        auto(log, 10)
        log.recordCandidateCast(CastDetection(500, 10 * s, 22.0, 7.0, 0.3, 0.9, 0.95), recordedNs = 18 * s)
        val out = StringWriter()
        EventCsvWriter(out).use { w -> log.all.forEach(w::write) }
        val lines = out.toString().lines()
        assertEquals("event_id,type,timestamp_ns,recorded_ns,ref_id,detail", lines[0])
        assertEquals("1,AUTO_CAST,10000000000,18000000000,,grid=500;accel_peak=30.0000;gyro_peak=8.0000;" +
            "post_gyro_mean=0.2000;delay_ms=8000", lines[1])
        assertEquals("2,AUTO_CAST_P2,10000000000,18000000000,,grid=500;accel_peak=22.0000;gyro_peak=7.0000;" +
            "post_gyro_mean=0.3000;delay_ms=8000;gyro_dir_cos=0.9000;post_dir_cos=0.9500;detector=P2_CANDIDATE", lines[2])
    }

    @Test
    fun falseIsLinkedToTheAutoCastTheUserSaw() {
        val log = SessionEventLog()
        val first = auto(log, 10)
        assertEquals(first.id, log.falseTarget()?.id)
        // 画面に #1 を表示した後で #2 が確定しても、FALSE は表示していた #1 に付く
        val second = auto(log, 30)
        val r = log.recordFalse(35 * s, targetId = first.id)
        assertEquals(first.id, r.event?.refId)
        assertEquals(EventType.FALSE, r.event?.type)
        // 次の対象は FALSE の付いていない最新（#2）
        assertEquals(second.id, log.falseTarget()?.id)
        assertEquals(CastCounts(auto = 2, falseCount = 1), log.counts)
        assertEquals(1, log.counts.corrected)
    }

    @Test
    fun secondFalseForSameAutoCastIsRejected() {
        val log = SessionEventLog()
        val a = auto(log, 10)
        log.recordFalse(12 * s, a.id)
        val r = log.recordFalse(13 * s, a.id)
        assertNull(r.event)
        assertEquals(RejectReason.NO_FALSE_TARGET, r.rejectReason)
        assertNull(log.falseTarget())
        assertEquals(1, log.counts.falseCount)
    }

    @Test
    fun falseRequiresAnAutoCast() {
        val log = SessionEventLog()
        val m = log.recordMarker(EventType.CAST, 5 * s).event!!
        assertEquals(RejectReason.NO_FALSE_TARGET, log.recordFalse(6 * s, targetId = 99).rejectReason)
        assertEquals(RejectReason.NO_FALSE_TARGET, log.recordFalse(6 * s, targetId = m.id).rejectReason)
        assertEquals(0, log.counts.falseCount)
    }

    @Test
    fun missRecordsEstimatedCastTimeAndPressTime() {
        val log = SessionEventLog(missAssumedLagNs = 10 * s)
        auto(log, 100)
        val e = log.recordMiss(200 * s, detectorPending = true).event!!
        assertEquals(190 * s, e.timestampNs)
        assertEquals(200 * s, e.recordedNs)
        assertEquals("1", e.detail["detector_pending"])
        assertEquals("1", e.detail["last_auto_id"])
        assertEquals(CastCounts(auto = 1, miss = 1), log.counts)
        assertEquals(2, log.counts.corrected)
    }

    @Test
    fun undoAddsEventAndKeepsOriginal() {
        val log = SessionEventLog()
        val miss = log.recordMiss(10 * s, false).event!!
        val undo = log.recordUndo(11 * s, miss.id).event!!
        assertEquals(EventType.UNDO, undo.type)
        assertEquals(miss.id, undo.refId)
        assertEquals("MISS", undo.detail["target_type"])
        assertEquals(2, log.all.size) // 元の MISS は消えない
        assertFalse(log.isActive(miss.id))
        assertEquals(CastCounts(miss = 0, undo = 1), log.counts)
    }

    @Test
    fun undoWalksBackOneManualEventAtATimeAndSkipsAutoCast() {
        val log = SessionEventLog()
        val cast = log.recordMarker(EventType.CAST, 1 * s).event!!
        val reel = log.recordMarker(EventType.REEL, 2 * s).event!!
        auto(log, 3) // 自動検知は取り消し対象にならない
        assertEquals(reel.id, log.undoTarget()?.id)
        log.recordUndo(4 * s, reel.id)
        assertEquals(cast.id, log.undoTarget()?.id)
        assertEquals(1, log.activeCount(EventType.CAST))
        assertEquals(0, log.activeCount(EventType.REEL))
        log.recordUndo(5 * s, cast.id)
        assertNull(log.undoTarget())
        assertEquals(1, log.counts.auto)
    }

    @Test
    fun undoWithStaleTargetIsRejected() {
        val log = SessionEventLog()
        val first = log.recordMarker(EventType.WALK, 1 * s).event!!
        log.recordMarker(EventType.OTHER, 2 * s)
        // 画面に WALK を表示している間に OTHER が記録された → 表示と違うので取り消さない
        val r = log.recordUndo(3 * s, first.id)
        assertEquals(RejectReason.NO_UNDO_TARGET, r.rejectReason)
        assertTrue(log.isActive(first.id))
    }

    @Test
    fun undoingFalseRestoresCountAndAllowsNewFalse() {
        val log = SessionEventLog()
        val a = auto(log, 10)
        val f = log.recordFalse(20 * s, a.id).event!!
        assertNull(log.falseTarget())
        log.recordUndo(21 * s, f.id)
        assertEquals(0, log.counts.falseCount)
        assertEquals(a.id, log.falseTarget()?.id)
        val again = log.recordFalse(22 * s, a.id)
        assertNotNull(again.event)
        assertEquals(1, log.counts.falseCount)
        assertEquals(CastCounts(auto = 1, falseCount = 1, undo = 1), log.counts)
    }

    @Test
    fun correctedCountNeverChangesAutoCount() {
        val log = SessionEventLog()
        repeat(5) { auto(log, it * 60L) }
        val target = log.falseTarget()!!.id
        log.recordFalse(400 * s, target)
        log.recordMiss(410 * s, false)
        log.recordMiss(420 * s, false)
        assertEquals(5, log.counts.auto)
        assertEquals(5 + 2 - 1, log.counts.corrected)
        assertEquals(5, log.all.count { it.type == EventType.AUTO_CAST })
    }

    @Test
    fun csvWriterOutputsIdsAndReferences() {
        val log = SessionEventLog()
        val a = auto(log, 10)
        val f = log.recordFalse(20 * s, a.id).event!!
        log.recordUndo(21 * s, f.id)
        val out = StringWriter()
        EventCsvWriter(out).use { w -> log.all.forEach(w::write) }
        val lines = out.toString().trimEnd().lines()
        assertEquals(EventCsvWriter.HEADER, lines[0])
        assertTrue(lines[1], lines[1].startsWith("1,AUTO_CAST,10000000000,18000000000,,grid=500;accel_peak=30.0000;"))
        assertEquals("2,FALSE,20000000000,20000000000,1,since_target_s=10.0000", lines[2])
        assertEquals("3,UNDO,21000000000,21000000000,2,target_type=FALSE", lines[3])
        lines.forEach { assertEquals(it, 6, it.split(",").size) }
    }
}
