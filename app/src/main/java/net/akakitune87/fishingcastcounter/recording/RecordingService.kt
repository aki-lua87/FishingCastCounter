package net.akakitune87.fishingcastcounter.recording

import android.app.Notification
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.hardware.SensorManager
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.util.Log
import androidx.core.app.NotificationChannelCompat
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import net.akakitune87.fishingcastcounter.R
import net.akakitune87.fishingcastcounter.detection.DetectorParams
import net.akakitune87.fishingcastcounter.events.SessionEvent
import net.akakitune87.fishingcastcounter.feedback.Haptics
import net.akakitune87.fishingcastcounter.presentation.MainActivity
import net.akakitune87.fishingcastcounter.sensor.MotionSensorSource
import java.io.File
import java.time.LocalDateTime
import java.time.format.DateTimeFormatter

/**
 * 計測中にアプリがバックグラウンドになったり画面がオフになっても記録を続けるための Foreground Service。
 *
 * - foregroundServiceType="health"（Android 14 以降で必須の型指定）
 * - 画面オフ中に CPU がサスペンドすると非ウェイクアップセンサーの配信が止まるため PARTIAL_WAKE_LOCK を保持する
 */
class RecordingService : Service() {

    private var session: RecordingSession? = null
    private val haptics by lazy { Haptics(this) }
    private var wakeLock: PowerManager.WakeLock? = null

    private val sessionListener = object : RecordingSession.Listener {
        override fun onError(session: RecordingSession, message: String) {
            RecordingRepository.reportError(message)
            ContextCompat.getMainExecutor(this@RecordingService).execute { stopRecording() }
        }

        override fun onWarning(session: RecordingSession, message: String) {
            RecordingRepository.onWarning(session, message)
        }

        override fun onEventsChanged(session: RecordingSession, snapshot: EventSnapshot) {
            RecordingRepository.onEventsChanged(session, snapshot)
        }

        override fun onAutoCast(session: RecordingSession, event: SessionEvent) {
            // 画面オフ・バックグラウンドでも気づけるよう、サービス側で振動させる
            haptics.autoCast()
        }

        override fun onClosed(session: RecordingSession) {
            RecordingRepository.onSessionClosed(session)
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_START -> startRecording()
            else -> stopRecording()
        }
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        stopRecording()
        super.onDestroy()
    }

    private fun startRecording() {
        // startForegroundService() で起動された場合は、何があっても先に startForeground() を呼ぶ必要がある
        try {
            startInForeground()
        } catch (e: Exception) {
            Log.e(TAG, "startForeground failed", e)
            RecordingRepository.reportError(getString(R.string.error_start_failed, e.message ?: e.javaClass.simpleName))
            stopRecording()
            return
        }
        if (session != null) return

        val sensorSource = MotionSensorSource(getSystemService(SensorManager::class.java))
        val missing = sensorSource.missingSensors()
        if (missing.isNotEmpty()) {
            RecordingRepository.reportError(getString(R.string.error_sensor_missing, missing.joinToString()))
            stopRecording()
            return
        }

        val baseName = "session_" + LocalDateTime.now().format(FILE_NAME_FORMAT)
        val dir = sessionsDir(this)
        val newSession = RecordingSession(
            sensorSource = sensorSource,
            csvFile = File(dir, "$baseName.csv"),
            eventsFile = File(dir, "$baseName.events.csv"),
            metaFile = File(dir, "$baseName.meta.txt"),
            detectorParams = DetectorParams(),
            listener = sessionListener,
        )
        session = newSession
        acquireWakeLock()
        RecordingRepository.onSessionStarted(newSession)
        newSession.start()
    }

    private fun stopRecording() {
        session?.stop()
        session = null
        releaseWakeLock()
        ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    private fun startInForeground() {
        val type = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            ServiceInfo.FOREGROUND_SERVICE_TYPE_HEALTH
        } else {
            0
        }
        ServiceCompat.startForeground(this, NOTIFICATION_ID, buildNotification(), type)
    }

    private fun buildNotification(): Notification {
        val manager = NotificationManagerCompat.from(this)
        manager.createNotificationChannel(
            NotificationChannelCompat.Builder(CHANNEL_ID, NotificationManagerCompat.IMPORTANCE_LOW)
                .setName(getString(R.string.notification_channel_name))
                .build(),
        )
        val openApp = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_recording)
            .setContentTitle(getString(R.string.notification_title))
            .setContentText(getString(R.string.notification_text))
            .setContentIntent(openApp)
            .setCategory(NotificationCompat.CATEGORY_WORKOUT)
            .setOngoing(true)
            .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)
            .build()
    }

    private fun acquireWakeLock() {
        if (wakeLock?.isHeld == true) return
        wakeLock = getSystemService(PowerManager::class.java)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, WAKE_LOCK_TAG)
            .apply {
                setReferenceCounted(false)
                acquire(WAKE_LOCK_TIMEOUT_MS)
            }
    }

    private fun releaseWakeLock() {
        wakeLock?.let { if (it.isHeld) it.release() }
        wakeLock = null
    }

    companion object {
        private const val TAG = "RecordingService"
        private const val ACTION_START = "net.akakitune87.fishingcastcounter.action.START_RECORDING"
        private const val ACTION_STOP = "net.akakitune87.fishingcastcounter.action.STOP_RECORDING"
        private const val CHANNEL_ID = "recording"
        private const val NOTIFICATION_ID = 1
        private const val WAKE_LOCK_TAG = "FishingCastCounter:Recording"

        /** 停止し忘れた場合の安全弁。釣行 1 回分を想定して 10 時間。 */
        private const val WAKE_LOCK_TIMEOUT_MS = 10 * 60 * 60 * 1000L
        private val FILE_NAME_FORMAT = DateTimeFormatter.ofPattern("yyyyMMdd_HHmmss")

        /** CSV の保存先（アプリ内部ストレージ: files/sessions）。 */
        fun sessionsDir(context: Context): File = File(context.filesDir, "sessions")

        fun start(context: Context) {
            val intent = Intent(context, RecordingService::class.java).setAction(ACTION_START)
            ContextCompat.startForegroundService(context, intent)
        }

        fun stop(context: Context) {
            val intent = Intent(context, RecordingService::class.java).setAction(ACTION_STOP)
            context.startService(intent)
        }
    }
}
