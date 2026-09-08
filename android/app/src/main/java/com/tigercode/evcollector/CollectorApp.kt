package com.tigercode.evcollector

import android.app.Application
import com.tigercode.evcollector.data.AppDatabase
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch

class CollectorApp : Application() {
    private val applicationScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    val database: AppDatabase by lazy {
        AppDatabase.create(this)
    }

    override fun onCreate() {
        super.onCreate()
        // Open and migrate the outbox database before the user starts a run.
        // This performs no upload and does not start collection.
        applicationScope.launch {
            database.openHelper.writableDatabase
        }
    }
}
