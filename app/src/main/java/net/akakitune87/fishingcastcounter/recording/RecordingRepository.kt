package net.akakitune87.fishingcastcounter.recording

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import net.akakitune87.fishingcastcounter.logging.MarkerType

data class RecorderState(
    val isRecording: Boolean = false,
    val startElapsedRealtimeNanos: Long = 0L,
    val currentFileName: String? = null,
    val lastSavedFileName: String? = null,
    val errorMessage: String? = null,
    val warningMessage: String? = null,
    /** 自動検知・手動イベントの状態（セッション開始でリセット、停止後は最後の値を保持）。 */
    val events: EventSnapshot = EventSnapshot(),
) {
    fun markerCount(marker: MarkerType): Int = events.markerCounts[marker] ?: 0
}

/** RecordingService と UI の間で記録状態を共有する。 */
object RecordingRepository {

    private val _state = MutableStateFlow(RecorderState())
    val state: StateFlow<RecorderState> = _state.asStateFlow()

    /** 直近のセッション。停止後もイベント数を表示するため保持し続ける。 */
    @Volatile
    private var session: RecordingSession? = null

    fun counts(): SessionCounts = session?.counts() ?: SessionCounts.ZERO

    fun addMarker(marker: MarkerType) {
        if (_state.value.isRecording) session?.mark(marker)
    }

    fun reportMiss() {
        if (_state.value.isRecording) session?.miss()
    }

    fun reportFalse(targetId: Long) {
        if (_state.value.isRecording) session?.markFalse(targetId)
    }

    fun undo(targetId: Long) {
        if (_state.value.isRecording) session?.undo(targetId)
    }

    fun reportError(message: String) {
        _state.update { it.copy(errorMessage = message) }
    }

    internal fun onSessionStarted(newSession: RecordingSession) {
        session = newSession
        _state.value = RecorderState(
            isRecording = true,
            startElapsedRealtimeNanos = newSession.startElapsedRealtimeNanos,
            currentFileName = newSession.csvFile.name,
            lastSavedFileName = _state.value.lastSavedFileName,
        )
    }

    internal fun onWarning(source: RecordingSession, message: String) {
        if (source !== session) return
        _state.update { it.copy(warningMessage = message) }
    }

    internal fun onEventsChanged(source: RecordingSession, snapshot: EventSnapshot) {
        if (source !== session) return
        _state.update { it.copy(events = snapshot) }
    }

    internal fun onSessionClosed(source: RecordingSession) {
        if (source !== session) return
        _state.update {
            it.copy(
                isRecording = false,
                currentFileName = null,
                lastSavedFileName = source.csvFile.name,
            )
        }
    }
}
