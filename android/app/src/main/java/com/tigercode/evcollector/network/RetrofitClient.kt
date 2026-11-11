package com.tigercode.evcollector.network

import android.content.Context
import com.tigercode.evcollector.AppPreferences
import okhttp3.OkHttpClient
import retrofit2.Retrofit
import retrofit2.converter.gson.GsonConverterFactory
import java.util.concurrent.TimeUnit

object RetrofitClient {
    fun serverApi(baseUrl: String, context: Context, controlPlane: Boolean = false): ServerApi {
        val normalized = if (baseUrl.endsWith("/")) baseUrl else "$baseUrl/"
        val client = OkHttpClient.Builder()
            .connectTimeout(if (controlPlane) 3 else 10, TimeUnit.SECONDS)
            .readTimeout(if (controlPlane) 3 else 120, TimeUnit.SECONDS)
            .writeTimeout(if (controlPlane) 3 else 30, TimeUnit.SECONDS)
            .callTimeout(if (controlPlane) 5 else 120, TimeUnit.SECONDS)
            .addInterceptor { chain ->
                val token = AppPreferences.deviceToken(context)
                val request = if (token.isNotEmpty()) {
                    chain.request().newBuilder()
                        .header("Authorization", "Bearer $token")
                        .build()
                } else {
                    chain.request()
                }
                chain.proceed(request)
            }
            .build()
        return Retrofit.Builder()
            .baseUrl(normalized)
            .client(client)
            .addConverterFactory(GsonConverterFactory.create())
            .build()
            .create(ServerApi::class.java)
    }
}
