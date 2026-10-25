package net.akakitune87.fishingcastcounter.feedback

import android.content.Context
import android.os.Build
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager

/**
 * 操作・自動検知時の振動。
 *
 * Compose の LocalHapticFeedback（View.performHapticFeedback）は USAGE_TOUCH 扱いのため、
 * 時計の「タッチ時の振動」がオフだとシステムに無視される（実機の dumpsys vibrator_manager で
 * ignored_for_settings を確認済み）。釣り中は画面を見ずに記録できたか確認したいので、
 * Vibrator を直接使い、タッチ振動の設定に左右されないようにする。
 *
 * 振動はセンサー（特に加速度）に小さなノイズを乗せる。AUTO_CAST の振動時刻は
 * events.csv の recorded_ns に残るので、解析時に区別できる。
 */
class Haptics(context: Context) {

    private val vibrator: Vibrator? = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
        context.getSystemService(VibratorManager::class.java)?.defaultVibrator
    } else {
        @Suppress("DEPRECATION")
        context.getSystemService(Vibrator::class.java)
    }

    private val markerEffect = VibrationEffect.createPredefined(VibrationEffect.EFFECT_HEAVY_CLICK)

    /** 拒否（対象なし）: 短く 2 回 */
    private val rejectEffect = VibrationEffect.createWaveform(longArrayOf(0, 40, 80, 40), -1)

    /** 自動検知: マーカー操作と区別できるよう、長めに 1 回 */
    private val autoCastEffect = VibrationEffect.createOneShot(250, VibrationEffect.DEFAULT_AMPLITUDE)

    fun marker() = vibrate(markerEffect)

    fun reject() = vibrate(rejectEffect)

    fun autoCast() = vibrate(autoCastEffect)

    private fun vibrate(effect: VibrationEffect) {
        val v = vibrator ?: return
        if (v.hasVibrator()) v.vibrate(effect)
    }
}
