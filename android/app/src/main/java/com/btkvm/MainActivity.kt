package com.btkvm

import android.Manifest
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.os.IBinder
import android.widget.ArrayAdapter
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import com.btkvm.databinding.ActivityMainBinding

class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private var service: ClipboardService? = null
    private var pairedDevices: List<BluetoothDevice> = emptyList()
    private val adapter = BluetoothAdapter.getDefaultAdapter()

    private val serviceConn = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName, binder: IBinder) {
            service = (binder as ClipboardService.LocalBinder).getService()
            service?.statusCallback = { msg ->
                runOnUiThread { binding.tvStatus.text = msg }
            }
            updateUi()
        }
        override fun onServiceDisconnected(name: ComponentName) {
            service = null
            updateUi()
        }
    }

    private val permissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { results ->
        if (results.values.all { it }) loadPairedDevices()
        else Toast.makeText(this, "블루투스 권한이 필요합니다.", Toast.LENGTH_LONG).show()
    }

    // ------------------------------------------------------------------ //

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        binding.btnConnect.setOnClickListener { onConnectClick() }
        binding.btnSend.setOnClickListener { service?.sendClipboard() }

        requestPermissions()
    }

    override fun onStart() {
        super.onStart()
        bindService(
            Intent(this, ClipboardService::class.java),
            serviceConn, Context.BIND_AUTO_CREATE,
        )
    }

    override fun onStop() {
        super.onStop()
        unbindService(serviceConn)
        service = null
    }

    // ------------------------------------------------------------------ //

    private fun requestPermissions() {
        val needed = mutableListOf<String>()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            needed += Manifest.permission.BLUETOOTH_CONNECT
            needed += Manifest.permission.BLUETOOTH_SCAN
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            needed += Manifest.permission.POST_NOTIFICATIONS
        }
        val missing = needed.filter {
            ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED
        }
        if (missing.isEmpty()) loadPairedDevices()
        else permissionLauncher.launch(missing.toTypedArray())
    }

    private fun loadPairedDevices() {
        pairedDevices = try {
            adapter?.bondedDevices?.toList() ?: emptyList()
        } catch (_: SecurityException) { emptyList() }

        val names = pairedDevices.map { it.name ?: it.address }
        binding.spinnerDevices.adapter =
            ArrayAdapter(this, android.R.layout.simple_spinner_item, names).also {
                it.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item)
            }

        if (pairedDevices.isEmpty()) {
            binding.tvStatus.text = "페어링된 BT 기기 없음"
        }
    }

    private fun onConnectClick() {
        val svc = service ?: return
        if (svc.isConnected) {
            svc.disconnect()
            binding.btnConnect.text = "연결"
            return
        }
        val idx = binding.spinnerDevices.selectedItemPosition
        if (idx < 0 || idx >= pairedDevices.size) {
            Toast.makeText(this, "기기를 선택하세요.", Toast.LENGTH_SHORT).show()
            return
        }
        val device = pairedDevices[idx]
        val intent = Intent(this, ClipboardService::class.java).apply {
            putExtra(ClipboardService.EXTRA_DEVICE, device.address)
        }
        ContextCompat.startForegroundService(this, intent)
        binding.tvStatus.text = "연결 중…"
        binding.btnConnect.text = "연결 끊기"
    }

    private fun updateUi() {
        val connected = service?.isConnected == true
        binding.btnConnect.text = if (connected) "연결 끊기" else "연결"
        binding.btnSend.isEnabled = connected
    }
}
