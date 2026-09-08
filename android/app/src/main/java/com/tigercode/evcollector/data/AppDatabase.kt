package com.tigercode.evcollector.data

import android.content.Context
import androidx.room.Database
import androidx.room.Room
import androidx.room.RoomDatabase
import androidx.room.migration.Migration
import androidx.sqlite.db.SupportSQLiteDatabase

@Database(
    entities = [ScanTaskEntity::class, StationResultEntity::class],
    version = 2,
    exportSchema = false,
)
abstract class AppDatabase : RoomDatabase() {
    abstract fun scanTaskDao(): ScanTaskDao
    abstract fun stationResultDao(): StationResultDao

    companion object {
        private val MIGRATION_1_2 = object : Migration(1, 2) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL(
                    "ALTER TABLE station_results " +
                        "ADD COLUMN syncEligible INTEGER NOT NULL DEFAULT 0"
                )
                // Preserve old local test rows without uploading them. Any
                // unfinished result tied to a remote task remains recoverable.
                db.execSQL(
                    """UPDATE station_results
                       SET syncEligible = 1
                       WHERE uploaded = 0
                         AND resultJson LIKE '%"remoteTaskId"%'
                         AND resultJson NOT LIKE '%"remoteTaskId":""%'
                         AND resultJson NOT LIKE '%"remoteTaskId": ""%'"""
                )
            }
        }

        fun create(context: Context): AppDatabase =
            Room.databaseBuilder(context, AppDatabase::class.java, "ev-collector.db")
                .addMigrations(MIGRATION_1_2)
                .build()
    }
}
