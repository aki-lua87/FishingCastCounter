package net.akakitune87.fishingcastcounter.sensor

import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.os.Handler
import net.akakitune87.fishingcastcounter.logging.RowType

/** 1 件のセンサーイベント。timestampNs は SensorEvent.timestamp（elapsedRealtimeNanos 基準）。 */
class MotionSample(
    val type: RowType,
    val timestampNs: Long,
    val x: Float,
    val y: Float,
    val z: Float,
)

/**
 * 加速度センサーとジャイロセンサーの購読を管理する。
 * イベントは start() に渡した Handler のスレッドで通知される。
 */
class MotionSensorSource(private val sensorManager: SensorManager) {

    private val accelerometer: Sensor? = sensorManager.getDefaultSensor(Sensor.TYPE_ACCELEROMETER)
    private val gyroscope: Sensor? = sensorManager.getDefaultSensor(Sensor.TYPE_GYROSCOPE)
    private var listener: SensorEventListener? = null

    /** 利用できないセンサーの名前。空なら記録可能。 */
    fun missingSensors(): List<String> = buildList {
        if (accelerometer == null) add("加速度")
        if (gyroscope == null) add("ジャイロ")
    }

    /** セッションのメタ情報に残すセンサーの説明。 */
    fun describeSensors(): List<String> = listOfNotNull(accelerometer, gyroscope).map {
        "${it.stringType} name=${it.name} vendor=${it.vendor} minDelayUs=${it.minDelay} maxDelayUs=${it.maxDelay}"
    }

    /**
     * センサーの購読を開始する。いずれかのセンサーが無い、または登録に失敗した場合は false を返す。
     */
    fun start(handler: Handler, samplingPeriodUs: Int, onSample: (MotionSample) -> Unit): Boolean {
        val accel = accelerometer ?: return false
        val gyro = gyroscope ?: return false
        stop()

        val newListener = object : SensorEventListener {
            override fun onSensorChanged(event: SensorEvent) {
                val type = when (event.sensor.type) {
                    Sensor.TYPE_ACCELEROMETER -> RowType.ACCEL
                    Sensor.TYPE_GYROSCOPE -> RowType.GYRO
                    else -> return
                }
                val v = event.values
                onSample(MotionSample(type, event.timestamp, v[0], v[1], v[2]))
            }

            override fun onAccuracyChanged(sensor: Sensor, accuracy: Int) = Unit
        }
        listener = newListener

        val ok = sensorManager.registerListener(newListener, accel, samplingPeriodUs, handler) &&
            sensorManager.registerListener(newListener, gyro, samplingPeriodUs, handler)
        if (!ok) stop()
        return ok
    }

    fun stop() {
        listener?.let { sensorManager.unregisterListener(it) }
        listener = null
    }

    companion object {
        /** 約 50Hz（20ms）を要求する。実際の間隔は端末依存なので timestamp で確認する。 */
        const val SAMPLING_PERIOD_US = 20_000
    }
}
