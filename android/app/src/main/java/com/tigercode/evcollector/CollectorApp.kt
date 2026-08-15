package com.tigercode.evcollector

import android.app.Application
import com.tigercode.evcollector.data.AppDatabase

class CollectorApp : Application() {
    val database: AppDatabase by lazy {
        AppDatabase.create(this)
    }
}
