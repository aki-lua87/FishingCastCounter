package net.akakitune87.fishingcastcounter.logging

/** CSV の type 列に出力する値。 */
enum class RowType {
    ACCEL,
    GYRO,
    MARK_CAST,
    MARK_REEL,
    MARK_WALK,
    MARK_OTHER,
}

/** ユーザーが手動で記録するマーカーの種類。 */
enum class MarkerType(val rowType: RowType) {
    CAST(RowType.MARK_CAST),
    REEL(RowType.MARK_REEL),
    WALK(RowType.MARK_WALK),
    OTHER(RowType.MARK_OTHER),
}
