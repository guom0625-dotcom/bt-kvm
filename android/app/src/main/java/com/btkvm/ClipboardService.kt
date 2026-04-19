package com.btkvm

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothSocket
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.os.Binder
import android.os.Build
import android.os.IBinder
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.lifecycle.LifecycleService
import java.io.BufferedReader
import java.io.InputStreamReader
import java.io.PrintWriter
import java.util.UUID
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.concurrent.thread

class ClipboardService : LifecycleService() {

    companion object {
        const val TAG = "ClipboardService"
        const val CHANNEL_ID = "btkvm_clipboard"
        const val NOTIF_ID = 1
        const val RFCOMM_CHANNEL = 4
        // Standard SPP UUID — Linux registers RFCOMM on channel 4
        val SPP_UUID: UUID = UUID.fromString("00001101-0000-1000-8000-00805F9B34FB")

        const val ACTION_SEND = "com.btkvm.ACTION_SEND_CLIPBOARD"
        const val EXTRA_DEVICE = "device_address"
    }

    inner class LocalBinder : Binder() {
        fun getService() = this@ClipboardService
    }

    private val binder = LocalBinder()
    private var socket: BluetoothSocket? = null
    private var writer: PrintWriter? = null
    private var reader: BufferedReader? = null
    private val running = AtomicBoolean(false)
    private var lastClip: String? = null

    var statusCallback: ((String) -> Unit)? = null

    // ------------------------------------------------------------------ //

    override fun onBind(intent: Intent): IBinder {
        super.onBind(intent)
        return binder
    }

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        super.onStartCommand(intent, flags, startId)

        if (intent?.action == ACTION_SEND) {
            sendClipboard()
            return START_STICKY
        }

        val address = intent?.getStringExtra(EXTRA_DEVICE) ?: return START_NOT_STICKY
        startForeground(NOTIF_ID, buildNotification("연결 중…"))
        connect(address)
        return START_STICKY
    }

    override fun onDestroy() {
        disconnect()
        super.onDestroy()
    }

    // ------------------------------------------------------------------ //
    // Connection

    fun connect(address: String) {
        if (running.get()) return
        thread(name = "bt-connect") {
            try {
                val adapter = BluetoothAdapter.getDefaultAdapter()
                val device: BluetoothDevice = adapter.getRemoteDevice(address)

                // Use reflection to connect to a specific RFCOMM channel
                // (avoids SDP lookup issues when channel is fixed)
                val sock = device.javaClass
                    .getMethod("createRfcommSocket", Int::class.java)
                    .invoke(device, RFCOMM_CHANNEL) as BluetoothSocket

                adapter.cancelDiscovery()
                sock.connect()
                socket = sock
                writer = PrintWriter(sock.outputStream, true)
                reader = BufferedReader(InputStreamReader(sock.inputStream))
                running.set(true)

                updateNotification("연결됨 ✓  — 클립보드 동기화 중")
                updateStatus("연결됨")
                Log.i(TAG, "Connected to $address")

                startClipboardMonitor()
                receiveLoop()
            } catch (e: Exception) {
                Log.e(TAG, "connect failed: $e")
                updateStatus("연결 실패: ${e.message}")
                updateNotification("연결 실패")
                disconnect()
            }
        }
    }

    fun disconnect() {
        running.set(false)
        try { socket?.close() } catch (_: Exception) {}
        socket = null; writer = null; reader = null
        updateStatus("연결 끊김")
        updateNotification("연결 안 됨")
    }

    val isConnected get() = running.get()

    // ------------------------------------------------------------------ //
    // Receive loop (Linux → Android)

    private fun receiveLoop() {
        try {
            while (running.get()) {
                val line = reader?.readLine() ?: break
                handleLine(line.trim())
            }
        } catch (e: Exception) {
            Log.d(TAG, "receiveLoop end: $e")
        } finally {
            disconnect()
        }
    }

    private fun handleLine(line: String) {
        when {
            line.startsWith("CLIP:") -> {
                val text = try {
                    String(android.util.Base64.decode(line.substring(5),
                                                      android.util.Base64.DEFAULT))
                } catch (e: Exception) {
                    Log.e(TAG, "decode: $e"); return
                }
                Log.i(TAG, "← Linux clipboard (${text.length} chars)")
                lastClip = text
                val cm = getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
                cm.setPrimaryClip(ClipData.newPlainText("bt-kvm", text))
                updateStatus("← Linux: ${text.take(40).replace('\n', ' ')}…")
            }
            line == "PING" -> send("PONG")
        }
    }

    // ------------------------------------------------------------------ //
    // Send clipboard (Android → Linux)

    /** Called from notification button or UI — reads and sends current clipboard. */
    fun sendClipboard() {
        val cm = getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
        val text = cm.primaryClip?.getItemAt(0)?.text?.toString() ?: return
        if (text == lastClip) return
        lastClip = text
        val encoded = android.util.Base64.encodeToString(
            text.toByteArray(Charsets.UTF_8), android.util.Base64.NO_WRAP)
        Log.i(TAG, "→ Linux clipboard (${text.length} chars)")
        send("CLIP:$encoded")
        updateStatus("→ Linux: ${text.take(40).replace('\n', ' ')}…")
    }

    // ------------------------------------------------------------------ //
    // Clipboard change monitor (Android 10+: auto-send when app is visible)

    private fun startClipboardMonitor() {
        val cm = getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
        cm.addPrimaryClipChangedListener {
            // Android 10+: getPrimaryClip() works when the app is in foreground.
            // If called from background it returns null — the user can always
            // press the notification button as a fallback.
            if (!running.get()) return@addPrimaryClipChangedListener
            val text = try {
                cm.primaryClip?.getItemAt(0)?.text?.toString()
            } catch (_: SecurityException) { null } ?: return@addPrimaryClipChangedListener

            if (text.isNotEmpty() && text != lastClip) {
                lastClip = text
                val encoded = android.util.Base64.encodeToString(
                    text.toByteArray(Charsets.UTF_8), android.util.Base64.NO_WRAP)
                Log.i(TAG, "→ Linux clipboard (auto, ${text.length} chars)")
                send("CLIP:$encoded")
                updateStatus("→ Linux: ${text.take(40).replace('\n', ' ')}…")
            }
        }
    }

    // ------------------------------------------------------------------ //

    private fun send(msg: String) {
        thread(name = "bt-send") {
            try { writer?.println(msg) } catch (e: Exception) {
                Log.d(TAG, "send: $e"); disconnect()
            }
        }
    }

    private fun updateStatus(msg: String) {
        statusCallback?.invoke(msg)
    }

    // ------------------------------------------------------------------ //
    // Notification

    private fun createNotificationChannel() {
        val ch = NotificationChannel(CHANNEL_ID, "bt-kvm 클립보드",
            NotificationManager.IMPORTANCE_LOW)
        getSystemService(NotificationManager::class.java).createNotificationChannel(ch)
    }

    private fun buildNotification(status: String): Notification {
        val openIntent = PendingIntent.getActivity(
            this, 0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE,
        )
        val sendIntent = PendingIntent.getService(
            this, 1,
            Intent(this, ClipboardService::class.java).apply {
                action = ACTION_SEND
            },
            PendingIntent.FLAG_IMMUTABLE,
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("bt-kvm")
            .setContentText(status)
            .setSmallIcon(android.R.drawable.ic_menu_share)
            .setContentIntent(openIntent)
            .addAction(android.R.drawable.ic_menu_send, "클립보드 전송", sendIntent)
            .setOngoing(true)
            .build()
    }

    private fun updateNotification(status: String) {
        val nm = getSystemService(NotificationManager::class.java)
        nm.notify(NOTIF_ID, buildNotification(status))
    }
}
