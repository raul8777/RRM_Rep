package com.cryptoagent.app

import android.os.Bundle
import android.widget.Button
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.net.HttpURLConnection
import java.net.URL

class MainActivity : AppCompatActivity() {
    
    private val API_BASE = "http://YOUR_LOCAL_IP:8000"
    private lateinit var statusTextView: TextView
    private lateinit var pauseButton: Button
    private lateinit var stopButton: Button
    private lateinit var startButton: Button
    
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        
        statusTextView = findViewById(R.id.statusTextView)
        pauseButton = findViewById(R.id.pauseButton)
        stopButton = findViewById(R.id.stopButton)
        startButton = findViewById(R.id.startButton)
        
        // Load initial status
        loadSystemStatus()
        
        // Set up button listeners
        pauseButton.setOnClickListener {
            sendControlCommand("pause")
        }
        
        stopButton.setOnClickListener {
            sendControlCommand("stop")
        }
        
        startButton.setOnClickListener {
            sendControlCommand("start")
        }
    }
    
    private fun loadSystemStatus() {
        CoroutineScope(Dispatchers.IO).launch {
            try {
                val response = makeHttpRequest("$API_BASE/control/status")
                withContext(Dispatchers.Main) {
                    statusTextView.text = "Status: ${response["state"]}"
                }
            } catch (e: Exception) {
                withContext(Dispatchers.Main) {
                    statusTextView.text = "Error loading status"
                }
            }
        }
    }
    
    private fun sendControlCommand(command: String) {
        CoroutineScope(Dispatchers.IO).launch {
            try {
                val endpoint = when (command) {
                    "pause" -> "$API_BASE/control/pause"
                    "stop" -> "$API_BASE/control/stop"
                    "start" -> "$API_BASE/control/start"
                    else -> return@launch
                }
                
                makeHttpRequest(endpoint, method = "POST", body = """{"reason": "Android app"}""")
                
                withContext(Dispatchers.Main) {
                    Toast.makeText(this@MainActivity, "$command command sent", Toast.LENGTH_SHORT).show()
                    loadSystemStatus()
                }
            } catch (e: Exception) {
                withContext(Dispatchers.Main) {
                    Toast.makeText(this@MainActivity, "Error: ${e.message}", Toast.LENGTH_SHORT).show()
                }
            }
        }
    }
    
    private suspend fun makeHttpRequest(
        urlString: String,
        method: String = "GET",
        body: String? = null
    ): Map<String, Any> {
        return withContext(Dispatchers.IO) {
            val url = URL(urlString)
            val connection = url.openConnection() as HttpURLConnection
            
            connection.requestMethod = method
            connection.setRequestProperty("Content-Type", "application/json")
            
            if (body != null) {
                connection.doOutput = true
                connection.outputStream.write(body.toByteArray())
            }
            
            val responseCode = connection.responseCode
            if (responseCode == HttpURLConnection.HTTP_OK) {
                // Parse JSON response (simplified)
                mapOf("state" to "RUNNING") // Placeholder
            } else {
                throw Exception("HTTP error: $responseCode")
            }
        }
    }
}
