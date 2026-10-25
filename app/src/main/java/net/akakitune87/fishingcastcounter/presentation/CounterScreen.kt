package net.akakitune87.fishingcastcounter.presentation

import android.os.SystemClock
import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.tween
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.key
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier

import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.wear.compose.foundation.lazy.TransformingLazyColumn
import androidx.wear.compose.foundation.lazy.TransformingLazyColumnItemScope
import androidx.wear.compose.foundation.lazy.rememberTransformingLazyColumnState
import androidx.wear.compose.material3.AppScaffold
import androidx.wear.compose.material3.Button
import androidx.wear.compose.material3.ButtonDefaults
import androidx.wear.compose.material3.ListHeader
import androidx.wear.compose.material3.MaterialTheme
import androidx.wear.compose.material3.ScreenScaffold
import androidx.wear.compose.material3.SurfaceTransformation
import androidx.wear.compose.material3.Text
import androidx.wear.compose.material3.lazy.TransformationSpec
import androidx.wear.compose.material3.lazy.rememberTransformationSpec
import androidx.wear.compose.material3.lazy.transformedHeight
import androidx.wear.compose.ui.tooling.preview.WearPreviewDevices
import kotlinx.coroutines.delay
import net.akakitune87.fishingcastcounter.R
import net.akakitune87.fishingcastcounter.events.ActionResult
import net.akakitune87.fishingcastcounter.events.CastCounts
import net.akakitune87.fishingcastcounter.events.EventType
import net.akakitune87.fishingcastcounter.events.RejectReason
import net.akakitune87.fishingcastcounter.events.SessionEvent
import net.akakitune87.fishingcastcounter.feedback.Haptics
import net.akakitune87.fishingcastcounter.logging.MarkerType
import net.akakitune87.fishingcastcounter.presentation.theme.FishingCastCounterTheme
import net.akakitune87.fishingcastcounter.recording.EventSnapshot
import net.akakitune87.fishingcastcounter.recording.RecorderState
import net.akakitune87.fishingcastcounter.recording.SessionCounts
import net.akakitune87.fishingcastcounter.recording.toEventType

private const val REFRESH_INTERVAL_MS = 500L
private const val FLASH_MS = 600L
private const val FADE_MS = 400
private const val HINT_MS = 2_000L

/** 画面からの操作。FALSE / UNDO は画面に表示していた対象の ID を渡す。 */
class CounterActions(
    val onStart: () -> Unit,
    val onStop: () -> Unit,
    val onMark: (MarkerType) -> Unit,
    val onMiss: () -> Unit,
    val onFalse: (targetId: Long) -> Unit,
    val onUndo: (targetId: Long) -> Unit,
)

@Composable
fun CounterScreen(
    state: RecorderState,
    countsProvider: () -> SessionCounts,
    actions: CounterActions,
) {
    // 経過時間・イベント数は高頻度で変わるので StateFlow には載せず、表示側で定期的に読み取る
    var counts by remember { mutableStateOf(countsProvider()) }
    var nowNanos by remember { mutableLongStateOf(SystemClock.elapsedRealtimeNanos()) }
    LaunchedEffect(state.isRecording) {
        counts = countsProvider()
        while (state.isRecording) {
            nowNanos = SystemClock.elapsedRealtimeNanos()
            counts = countsProvider()
            delay(REFRESH_INTERVAL_MS)
        }
    }
    val haptics = rememberHaptics()
    val ev = state.events

    // 操作結果のフィードバック: 該当ボタンを光らせ、拒否されたら短い 2 回振動。
    // 画面に戻ってきたときの再表示では反応しないよう、最後に見た seq と比較する
    var seenSeq by remember { mutableLongStateOf(ev.lastAction?.seq ?: 0L) }
    var flashing by remember { mutableStateOf<EventType?>(null) }
    LaunchedEffect(ev.lastAction?.seq) {
        val action = ev.lastAction ?: return@LaunchedEffect
        if (action.seq <= seenSeq) return@LaunchedEffect
        seenSeq = action.seq
        if (action.event == null) haptics.reject()
        flashing = action.type
        delay(FLASH_MS)
        flashing = null
    }
    // 自動検知でカウンターを光らせる（振動はサービス側）
    var seenAutoId by remember { mutableLongStateOf(ev.lastAutoCast?.id ?: 0L) }
    var autoFlash by remember { mutableStateOf(false) }
    LaunchedEffect(ev.lastAutoCast?.id) {
        val id = ev.lastAutoCast?.id ?: return@LaunchedEffect
        if (id <= seenAutoId) return@LaunchedEffect
        seenAutoId = id
        autoFlash = true
        delay(FLASH_MS)
        autoFlash = false
    }
    // 長押しボタンを短くタップしたときの案内
    var hint by remember { mutableStateOf<String?>(null) }
    val longPressHint = stringResource(R.string.long_press_hint)
    LaunchedEffect(hint) {
        if (hint != null) {
            delay(HINT_MS)
            hint = null
        }
    }

    FishingCastCounterTheme {
        AppScaffold {
            // 待機中 ⇔ 記録中で項目構成が変わるので、スクロール位置を引き継がず先頭から表示する
            val listState = key(state.isRecording) { rememberTransformingLazyColumnState() }
            val spec = rememberTransformationSpec()
            ScreenScaffold(scrollState = listState) { contentPadding ->
                TransformingLazyColumn(contentPadding = contentPadding, state = listState) {
                    if (!state.isRecording) {
                        item {
                            ListHeader(
                                modifier = Modifier.fillMaxWidth().transformedHeight(this, spec),
                                transformation = SurfaceTransformation(spec),
                            ) { Text(stringResource(R.string.status_idle)) }
                        }
                        item {
                            Button(
                                onClick = actions.onStart,
                                modifier = Modifier.fillMaxWidth().transformedHeight(this, spec),
                                transformation = SurfaceTransformation(spec),
                            ) { Text(stringResource(R.string.start_recording)) }
                        }
                        if (ev.counts != CastCounts()) {
                            item {
                                InfoText(stringResource(R.string.last_session_summary, ev.counts.auto, ev.counts.corrected))
                            }
                        }
                    } else {
                        // 1 画面目にカウンターと MISS / FALSE が収まるよう、見出しは小さな 1 行にする
                        item {
                            AutoCounter(
                                ev,
                                elapsed = formatElapsed(nowNanos - state.startElapsedRealtimeNanos),
                                highlighted = autoFlash,
                            )
                        }
                        item {
                            Row(horizontalArrangement = Arrangement.spacedBy(4.dp), modifier = Modifier.fillMaxWidth()) {
                                ActionButton(
                                    title = stringResource(R.string.miss_button),
                                    subtitle = stringResource(R.string.miss_sub),
                                    flashing = flashing == EventType.MISS,
                                    onClick = {
                                        haptics.marker()
                                        actions.onMiss()
                                    },
                                    modifier = Modifier.weight(1f).height(56.dp),
                                )
                                val target = ev.falseTarget
                                ActionButton(
                                    title = stringResource(R.string.false_button),
                                    subtitle = target?.let { stringResource(R.string.false_sub, it.id) }
                                        ?: stringResource(R.string.false_sub_none),
                                    flashing = flashing == EventType.FALSE,
                                    enabled = target != null,
                                    onClick = {
                                        haptics.marker()
                                        target?.let { actions.onFalse(it.id) }
                                    },
                                    modifier = Modifier.weight(1f).height(56.dp),
                                )
                            }
                        }
                        item { FeedbackText(ev, nowNanos, hint) }
                        MarkerType.entries.chunked(2).forEach { rowMarkers ->
                            item {
                                Row(horizontalArrangement = Arrangement.spacedBy(4.dp), modifier = Modifier.fillMaxWidth()) {
                                    rowMarkers.forEach { marker ->
                                        ActionButton(
                                            title = marker.name,
                                            subtitle = state.markerCount(marker).toString(),
                                            flashing = flashing == marker.toEventType(),
                                            onClick = {
                                                haptics.marker()
                                                actions.onMark(marker)
                                            },
                                            modifier = Modifier.weight(1f),
                                        )
                                    }
                                }
                            }
                        }
                        item {
                            val target = ev.undoTarget
                            ActionButton(
                                title = stringResource(R.string.undo_button),
                                subtitle = target?.let { stringResource(R.string.undo_sub, it.type.name, it.id) }
                                    ?: stringResource(R.string.undo_sub_none),
                                flashing = flashing == EventType.UNDO,
                                enabled = target != null,
                                onClick = {
                                    haptics.reject()
                                    hint = longPressHint
                                },
                                onLongClick = {
                                    haptics.marker()
                                    target?.let { actions.onUndo(it.id) }
                                },
                                modifier = Modifier.fillMaxWidth().transformedHeight(this, spec),
                                transformation = SurfaceTransformation(spec),
                            )
                        }
                        item {
                            StopButton(
                                spec,
                                onShortTap = {
                                    haptics.reject()
                                    hint = longPressHint
                                },
                                onStop = actions.onStop,
                            )
                        }
                        // 長押しボタンは画面下部にあり、上の案内表示が見えないことがあるので近くにも出す
                        hint?.let { item { InfoText(it, isError = true) } }
                    }
                    state.errorMessage?.let { item { InfoText(it, isError = true) } }
                    state.warningMessage?.let { item { InfoText(it, isError = true) } }
                    if (state.isRecording) {
                        item { InfoText(stringResource(R.string.event_count, counts.sensorEvents)) }
                    }
                    (state.currentFileName ?: state.lastSavedFileName)?.let {
                        // 丸型画面で折り返さないよう、日時部分だけ表示する
                        val shortName = it.removePrefix("session_").removeSuffix(".csv")
                        item { InfoText(stringResource(R.string.last_saved, shortName)) }
                    }
                }
            }
        }
    }
}

@Composable
private fun AutoCounter(ev: EventSnapshot, elapsed: String, highlighted: Boolean) {
    val color by animateColorAsState(
        targetValue = if (highlighted) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.onSurface,
        animationSpec = tween(durationMillis = if (highlighted) 0 else FADE_MS),
        label = "autoCounter",
    )
    Column(horizontalAlignment = Alignment.CenterHorizontally, modifier = Modifier.fillMaxWidth()) {
        Text(
            text = stringResource(R.string.status_recording, elapsed),
            style = MaterialTheme.typography.labelMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Row(verticalAlignment = Alignment.Bottom) {
            Text(
                text = ev.counts.auto.toString(),
                style = MaterialTheme.typography.displayLarge,
                color = color,
            )
            Text(
                text = " " + stringResource(R.string.auto_label),
                style = MaterialTheme.typography.labelMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(bottom = 8.dp),
            )
        }
        Text(
            text = stringResource(R.string.corrected_summary, ev.counts.corrected, ev.counts.miss, ev.counts.falseCount),
            style = MaterialTheme.typography.bodySmall,
            textAlign = TextAlign.Center,
        )
        val status = when {
            !ev.detectorActive -> stringResource(R.string.detector_stopped)
            ev.detectorPending -> stringResource(R.string.detector_pending)
            else -> null
        }
        status?.let {
            Text(
                text = it,
                style = MaterialTheme.typography.labelSmall,
                color = if (ev.detectorActive) MaterialTheme.colorScheme.tertiary else MaterialTheme.colorScheme.error,
                textAlign = TextAlign.Center,
            )
        }
    }
}

@Composable
private fun FeedbackText(ev: EventSnapshot, nowNanos: Long, hint: String?) {
    val action = ev.lastAction
    val (text, isError) = when {
        hint != null -> hint to true
        action != null -> actionText(action) to (action.event == null)
        else -> {
            val auto = ev.lastAutoCast
            if (auto == null) {
                stringResource(R.string.no_auto_yet) to false
            } else {
                stringResource(R.string.last_auto, auto.id, secondsSince(auto, nowNanos)) to false
            }
        }
    }
    Column(horizontalAlignment = Alignment.CenterHorizontally, modifier = Modifier.fillMaxWidth()) {
        Text(
            text = text,
            style = MaterialTheme.typography.bodySmall,
            color = if (isError) MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.primary,
            textAlign = TextAlign.Center,
            maxLines = 2,
        )
        // 操作結果を表示している間も、直近の自動検知は小さく出しておく
        val auto = ev.lastAutoCast
        if ((hint != null || action != null) && auto != null) {
            Text(
                text = stringResource(R.string.last_auto, auto.id, secondsSince(auto, nowNanos)),
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                textAlign = TextAlign.Center,
                maxLines = 1,
            )
        }
    }
}

@Composable
private fun actionText(action: ActionResult): String {
    val e = action.event
    return when {
        e == null && action.rejectReason == RejectReason.NO_FALSE_TARGET -> stringResource(R.string.action_reject_false)
        e == null -> stringResource(R.string.action_reject_undo)
        e.type == EventType.FALSE -> stringResource(R.string.action_false_recorded, e.id, e.refId ?: 0L)
        e.type == EventType.UNDO -> stringResource(
            R.string.action_undo_recorded,
            e.detail["target_type"] ?: "",
            e.refId ?: 0L,
        )
        e.type == EventType.MISS && e.detail["detector_pending"] == "1" ->
            stringResource(R.string.action_miss_pending, e.id)
        else -> stringResource(R.string.action_recorded, e.type.name, e.id)
    }
}

private fun secondsSince(e: SessionEvent, nowNanos: Long): Int =
    ((nowNanos - e.timestampNs) / 1_000_000_000L).toInt().coerceAtLeast(0)

@Composable
private fun ActionButton(
    title: String,
    subtitle: String,
    flashing: Boolean,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    enabled: Boolean = true,
    onLongClick: (() -> Unit)? = null,
    transformation: SurfaceTransformation? = null,
) {
    val colors = MaterialTheme.colorScheme
    // 記録直後は塗りつぶし色に切り替え、そこから通常色へフェードして戻す
    val containerColor by animateColorAsState(
        targetValue = if (flashing) colors.primary else colors.surfaceContainer,
        animationSpec = tween(durationMillis = if (flashing) 0 else FADE_MS),
        label = "actionContainer",
    )
    val contentColor by animateColorAsState(
        targetValue = if (flashing) colors.onPrimary else colors.onSurface,
        animationSpec = tween(durationMillis = if (flashing) 0 else FADE_MS),
        label = "actionContent",
    )
    Button(
        onClick = onClick,
        onLongClick = onLongClick,
        enabled = enabled,
        colors = ButtonDefaults.buttonColors(containerColor = containerColor, contentColor = contentColor),
        contentPadding = PaddingValues(horizontal = 4.dp, vertical = 2.dp),
        transformation = transformation,
        modifier = modifier,
    ) {
        Column(horizontalAlignment = Alignment.CenterHorizontally, modifier = Modifier.fillMaxWidth()) {
            Text(title, style = MaterialTheme.typography.labelMedium, maxLines = 1)
            Text(subtitle, style = MaterialTheme.typography.labelSmall, maxLines = 1)
        }
    }
}

@Composable
private fun TransformingLazyColumnItemScope.StopButton(
    spec: TransformationSpec,
    onShortTap: () -> Unit,
    onStop: () -> Unit,
) {
    Button(
        onClick = onShortTap,
        onLongClick = onStop,
        colors = ButtonDefaults.buttonColors(
            containerColor = MaterialTheme.colorScheme.errorContainer,
            contentColor = MaterialTheme.colorScheme.onErrorContainer,
        ),
        modifier = Modifier.fillMaxWidth().transformedHeight(this, spec),
        transformation = SurfaceTransformation(spec),
    ) {
        Text(stringResource(R.string.stop_recording_long))
    }
}

@Composable
private fun InfoText(text: String, isError: Boolean = false) {
    Text(
        text = text,
        style = MaterialTheme.typography.bodySmall,
        color = if (isError) MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.onSurfaceVariant,
        textAlign = TextAlign.Center,
        modifier = Modifier.fillMaxWidth(),
    )
}

@Composable
private fun rememberHaptics(): Haptics {
    val context = LocalContext.current
    return remember(context) { Haptics(context) }
}

private fun formatElapsed(nanos: Long): String {
    val totalSeconds = (nanos / 1_000_000_000L).coerceAtLeast(0)
    val h = totalSeconds / 3600
    val m = totalSeconds % 3600 / 60
    val s = totalSeconds % 60
    return "%d:%02d:%02d".format(h, m, s)
}

@WearPreviewDevices
@Composable
fun CounterScreenPreview() {
    val now = SystemClock.elapsedRealtimeNanos()
    val auto = SessionEvent(12, EventType.AUTO_CAST, now - 15_000_000_000L, now - 7_000_000_000L)
    CounterScreen(
        state = RecorderState(
            isRecording = true,
            startElapsedRealtimeNanos = now - 3_600_000_000_000L,
            currentFileName = "session_20261101_120000.csv",
            events = EventSnapshot(
                counts = CastCounts(auto = 12, miss = 1, falseCount = 2),
                markerCounts = mapOf(MarkerType.CAST to 3, MarkerType.REEL to 3),
                lastAutoCast = auto,
                falseTarget = auto,
                undoTarget = SessionEvent(14, EventType.FALSE, now, now, refId = 11),
                detectorPending = true,
            ),
        ),
        countsProvider = { SessionCounts(accel = 1200, gyro = 1180, markers = 6) },
        actions = CounterActions({}, {}, {}, {}, {}, {}),
    )
}
