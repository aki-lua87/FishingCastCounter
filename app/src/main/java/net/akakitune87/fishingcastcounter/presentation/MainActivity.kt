package net.akakitune87.fishingcastcounter.presentation

import android.Manifest
import android.content.pm.PackageManager
import android.hardware.SensorManager
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.core.content.ContextCompat
import net.akakitune87.fishingcastcounter.R
import net.akakitune87.fishingcastcounter.recording.RecordingRepository
import net.akakitune87.fishingcastcounter.recording.RecordingService
import net.akakitune87.fishingcastcounter.sensor.MotionSensorSource

class MainActivity : ComponentActivity() {

    // 通知権限の結果に関わらず記録は開始する（拒否された場合は通知が表示されないだけ）
    private val notificationPermissionLauncher =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) {
            RecordingService.start(this)
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            val state by RecordingRepository.state.collectAsState()
            CounterScreen(
                state = state,
                countsProvider = RecordingRepository::counts,
                actions = CounterActions(
                    onStart = ::startRecording,
                    onStop = { RecordingService.stop(this) },
                    onMark = RecordingRepository::addMarker,
                    onMiss = RecordingRepository::reportMiss,
                    onFalse = RecordingRepository::reportFalse,
                    onUndo = RecordingRepository::undo,
                ),
            )
        }
    }

    private fun startRecording() {
        val missing = MotionSensorSource(getSystemService(SensorManager::class.java)).missingSensors()
        if (missing.isNotEmpty()) {
            RecordingRepository.reportError(getString(R.string.error_sensor_missing, missing.joinToString()))
            return
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            notificationPermissionLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
        } else {
            RecordingService.start(this)
        }
    }
}
