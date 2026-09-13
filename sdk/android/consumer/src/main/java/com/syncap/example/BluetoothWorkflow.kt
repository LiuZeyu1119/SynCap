package com.syncap.example

import com.syncap.sdk.bluetooth.ProvisioningClient
import org.json.JSONObject

/** The host owns permissions, user selection, pairing, and the phone's network. */
class BluetoothWorkflow(private val bluetooth: ProvisioningClient) {
    suspend fun scanNetworks(addressSelectedByUser: String): List<JSONObject> {
        val response = JSONObject(bluetooth.scanWifi(addressSelectedByUser).rawJson)
        check(response.optString("state") == "completed") { response.optString("error", "Wi-Fi scan failed") }
        val networks = response.getJSONArray("networks")
        return (0 until networks.length()).map { networks.getJSONObject(it) }
    }

    suspend fun connect(addressSelectedByUser: String, networkSelectedByUser: JSONObject, password: String): String {
        val response = JSONObject(bluetooth.configureWifi(addressSelectedByUser,
            networkSelectedByUser.getString("ssid"), password,
            networkSelectedByUser.getString("security")).rawJson)
        check(response.optString("state") == "connected") { response.optString("error", "Wi-Fi connection failed") }
        return response.getString("ipAddress")
    }
}
