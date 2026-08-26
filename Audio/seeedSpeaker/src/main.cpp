/*
 * Voice ordering mic - XIAO ESP32S3 + ReSpeaker Lite
 *
 * Push-to-talk: hold the button on D3, speak, release. The clip goes to the
 * restaurant server over a WebSocket, which hands it to Gemini and stages the
 * parsed items into that table's pending order for confirmation.
 *
 * The table number isn't hardcoded - on boot (and on every reconnect) this
 * calls GET /device/{DEVICE_NUMBER}, the same lookup the e-paper display
 * uses, so reassigning a table's hardware doesn't require reflashing.
 *
 * Wire protocol (see Server/README.md, "Voice ordering"):
 *   -> {"type":"start"}     button pressed
 *   -> <binary frames>      raw 16-bit mono PCM @ 16 kHz, little-endian
 *   -> {"type":"stop"}      button released
 *   <- ready | listening | processing | result | error | cancelled | pong
 *
 * The ReSpeaker Lite is the I2S master and gives us 32-bit stereo frames. We
 * take the left channel and shift down to 16-bit before sending: a quarter of
 * the bytes over Wi-Fi, and exactly what the server expects.
 */
#include <Arduino.h>

#include <ArduinoJson.h>
#include <ArduinoWebsockets.h>
#include <HTTPClient.h>
#include <WiFi.h>

#include "AudioTools.h"
#include "board_pins.h"
#include "secrets.h"

using namespace websockets;

// ------------------------------- config -------------------------------------
static const char *WIFI_SSID_ = WIFI_SSID;
static const char *WIFI_PASS_ = WIFI_PASSWORD;
static const char *HOST = HOST_ADDR;
static const uint16_t PORT = PORT_NUM;
static const char *DEVICE_NAME = DEVICE_ID;

// Resolved from GET /device/{DEVICE_NUMBER} - -1 means "not yet known".
static int TABLE_NUMBER = -1;

static const int BUTTON_PIN = D3;      // push-to-talk, active LOW
static const uint32_t SAMPLE_RATE = 16000;
static const uint16_t DEBOUNCE_MS = 40;
static const uint32_t MAX_UTTERANCE_MS = 30000;  // server truncates past this
static const uint32_t RECONNECT_MS = 3000;

// I2S frames arrive 32-bit stereo; we send 16-bit mono.
static const size_t FRAMES_PER_CHUNK = 256;
static const size_t I2S_CHUNK_BYTES = FRAMES_PER_CHUNK * 2 * sizeof(int32_t);

// ------------------------------- globals ------------------------------------
I2SStream i2sIn;
AudioInfo micInfo(SAMPLE_RATE, 2, 32);
WebsocketsClient ws;

static int32_t i2sBuffer[FRAMES_PER_CHUNK * 2];
static int16_t pcmBuffer[FRAMES_PER_CHUNK];

static bool wsConnected = false;
static bool capturing = false;
static bool lastButtonDown = false;
static uint32_t lastButtonChange = 0;
static uint32_t captureStartedAt = 0;
static uint32_t lastReconnectAttempt = 0;

// ------------------------------ prototypes ----------------------------------
void connectWiFi();
bool fetchAssignedTable(int &tableNumberOut);
bool connectSocket();
void onWsMessage(WebsocketsMessage message);
void onWsEvent(WebsocketsEvent event, String data);
void startCapture();
void stopCapture();
void cancelCapture();
void pumpAudio();
void setStatus(uint8_t r, uint8_t g, uint8_t b);

// -------------------------------- setup -------------------------------------
void setup() {
    Serial.begin(115200);
    pinMode(BUTTON_PIN, INPUT_PULLUP);

    delay(3000);

    setStatus(40, 0, 0);  // red until we're online
    connectWiFi();

    while (!fetchAssignedTable(TABLE_NUMBER)) {
        Serial.println("[device] lookup failed, retrying...");
        delay(2000);
    }
    Serial.printf("[device] mic %d assigned to table %d\n", DEVICE_NUMBER, TABLE_NUMBER);

    // The ReSpeaker Lite drives BCLK/WS, so the ESP32 is the I2S slave.
    auto cfg = i2sIn.defaultConfig(RX_MODE);
    cfg.copyFrom(micInfo);
    cfg.i2s_format = I2S_STD_FORMAT;
    cfg.is_master = false;
    cfg.use_apll = false;
    cfg.pin_bck = 8;
    cfg.pin_ws = 7;
    cfg.pin_data = 43;
    cfg.pin_data_rx = 44;
    i2sIn.begin(cfg);

    ws.onMessage(onWsMessage);
    ws.onEvent(onWsEvent);
    connectSocket();
}

// -------------------------------- loop --------------------------------------
void loop() {
    if (WiFi.status() != WL_CONNECTED) {
        wsConnected = false;
        capturing = false;
        setStatus(40, 0, 0);
        connectWiFi();
        return;
    }

    if (!wsConnected) {
        capturing = false;
        if (millis() - lastReconnectAttempt > RECONNECT_MS) {
            lastReconnectAttempt = millis();
            connectSocket();
        }
        delay(10);
        return;
    }

    ws.poll();

    // ---- debounced push-to-talk ----
    bool buttonDown = digitalRead(BUTTON_PIN) == LOW;
    //Serial.printf("[mic] button %s\n", buttonDown ? "down" : "up");
    if (buttonDown != lastButtonDown && millis() - lastButtonChange > DEBOUNCE_MS) {
        lastButtonChange = millis();
        lastButtonDown = buttonDown;
        if (buttonDown) {
            startCapture();
        } else {
            stopCapture();
        }
    }

    if (capturing) {
        pumpAudio();
        // Don't let a stuck button stream forever.
        if (millis() - captureStartedAt > MAX_UTTERANCE_MS) {
            Serial.println("[mic] max utterance reached");
            stopCapture();
        }
    } else {
        // Keep draining I2S so the DMA buffers don't overflow with stale audio.
        i2sIn.readBytes((uint8_t *)i2sBuffer, I2S_CHUNK_BYTES);
    }
}

// ------------------------------ networking ----------------------------------
void connectWiFi() {
    if (WiFi.status() == WL_CONNECTED) return;

    Serial.printf("[wifi] connecting to %s", WIFI_SSID_);
    WiFi.mode(WIFI_STA);
    WiFi.begin(WIFI_SSID_, WIFI_PASS_);
    while (WiFi.status() != WL_CONNECTED) {
        delay(500);
        Serial.print(".");
    }
    Serial.printf("\n[wifi] connected, ip=%s\n", WiFi.localIP().toString().c_str());
}

bool fetchAssignedTable(int &tableNumberOut) {
    HTTPClient http;
    String url = String("http://") + HOST + ":" + String(PORT) + "/device/" + String(DEVICE_NUMBER);
    http.begin(url);
    int httpCode = http.GET();

    bool ok = false;
    if (httpCode == HTTP_CODE_OK) {
        String response = http.getString();
        JsonDocument doc;
        if (!deserializeJson(doc, response)) {
            tableNumberOut = doc["table_number"].as<int>();
            ok = true;
        } else {
            Serial.println("[device] failed to parse JSON");
        }
    } else {
        Serial.printf("[device] GET failed, error: %s\n", http.errorToString(httpCode).c_str());
    }
    http.end();
    return ok;
}

bool connectSocket() {
    // Re-check on every (re)connect attempt, not just at boot, so a table
    // reassignment (PUT /device/{n} elsewhere) is picked up without a reflash.
    int fetched;
    if (fetchAssignedTable(fetched)) {
        if (fetched != TABLE_NUMBER) {
            Serial.printf("[device] table changed %d -> %d\n", TABLE_NUMBER, fetched);
        }
        TABLE_NUMBER = fetched;
    } else {
        Serial.println("[device] lookup failed, using last known table");
    }

    char url[160];
#ifdef API_KEY_STR
    snprintf(url, sizeof(url), "ws://%s:%u/table/%d/voice?device=%s&api_key=%s", HOST,
             PORT, TABLE_NUMBER, DEVICE_NAME, API_KEY_STR);
#else
    snprintf(url, sizeof(url), "ws://%s:%u/table/%d/voice?device=%s", HOST, PORT,
             TABLE_NUMBER, DEVICE_NAME);
#endif

    Serial.printf("[ws] connecting to %s\n", url);
    wsConnected = ws.connect(url);
    if (wsConnected) {
        Serial.println("[ws] connected");
        setStatus(0, 30, 0);  // green: idle and ready
    } else {
        Serial.println("[ws] connect failed");
        setStatus(40, 0, 0);
    }
    return wsConnected;
}

void onWsEvent(WebsocketsEvent event, String data) {
    if (event == WebsocketsEvent::ConnectionClosed) {
        Serial.println("[ws] closed");
        wsConnected = false;
        capturing = false;
        setStatus(40, 0, 0);
    }
}

void onWsMessage(WebsocketsMessage message) {
    JsonDocument doc;
    if (deserializeJson(doc, message.data())) {
        Serial.println("[ws] unparseable message");
        return;
    }

    const char *type = doc["type"] | "";

    if (!strcmp(type, "ready")) {
        Serial.printf("[ws] ready, table=%d voice_enabled=%d\n", (int)(doc["table"] | 0),
                      (int)(doc["enabled"] | false));
        if (!(doc["enabled"] | false)) {
            Serial.println("[ws] server has no GEMINI_API_KEY - orders will fail");
            setStatus(40, 20, 0);  // amber: connected but voice is disabled
        }

    } else if (!strcmp(type, "listening")) {
        setStatus(0, 0, 60);  // blue: recording

    } else if (!strcmp(type, "processing")) {
        setStatus(30, 20, 0);  // amber: waiting on Gemini
        Serial.printf("[ws] processing %.2fs of audio\n", (float)(doc["seconds"] | 0.0));

    } else if (!strcmp(type, "result")) {
        Serial.printf("[ws] heard: %s\n", doc["transcript"] | "");
        if (doc["confirmed"] | false) {
            Serial.println("      confirmed - pending order sent to the kitchen");
        } else if (doc["cancelled"] | false) {
            Serial.println("      cancelled - pending order cleared");
        } else {
            for (JsonObject item : doc["items"].as<JsonArray>()) {
                Serial.printf("      %dx %s%s\n", (int)(item["quantity"] | 1),
                              item["name"] | "?",
                              (item["matched"] | false) ? "" : "  <-- NOT ON MENU");
            }
            Serial.printf("[ws] %d matched, %d unmatched, staged to pending\n",
                          (int)(doc["matched_count"] | 0), (int)(doc["unmatched_count"] | 0));
        }
        setStatus(0, 30, 0);

    } else if (!strcmp(type, "error")) {
        Serial.printf("[ws] error: %s\n", doc["message"] | "");
        setStatus(60, 0, 0);
        delay(400);
        setStatus(0, 30, 0);

    } else if (!strcmp(type, "cancelled")) {
        setStatus(0, 30, 0);
    }
}

// -------------------------------- capture -----------------------------------
void startCapture() {
    if (!wsConnected) return;
    Serial.println("[mic] start");

    // Drop whatever the DMA buffered while we were idle, so the clip starts at
    // the button press instead of a second earlier.
    for (int i = 0; i < 4; i++) {
        i2sIn.readBytes((uint8_t *)i2sBuffer, I2S_CHUNK_BYTES);
    }

    capturing = true;
    captureStartedAt = millis();
    ws.send("{\"type\":\"start\"}");
}

void stopCapture() {
    if (!capturing) return;
    capturing = false;
    Serial.println("[mic] stop");
    ws.send("{\"type\":\"stop\"}");
}

void cancelCapture() {
    if (!capturing) return;
    capturing = false;
    ws.send("{\"type\":\"cancel\"}");
}

void pumpAudio() {
    size_t bytesRead = i2sIn.readBytes((uint8_t *)i2sBuffer, I2S_CHUNK_BYTES);
    if (bytesRead < sizeof(int32_t) * 2) return;

    // 32-bit stereo -> 16-bit mono. Left channel only: on the ReSpeaker Lite the
    // processed mic signal is there, and averaging both channels just halves it.
    size_t frames = bytesRead / (sizeof(int32_t) * 2);
    for (size_t i = 0; i < frames; i++) {
        pcmBuffer[i] = (int16_t)(i2sBuffer[i * 2] >> 16);
    }

    if (!ws.sendBinary((const char *)pcmBuffer, frames * sizeof(int16_t))) {
        Serial.println("[ws] send failed, dropping utterance");
        capturing = false;
        wsConnected = false;
    }
}

// -------------------------------- status LED --------------------------------
#if __has_include(<Adafruit_NeoPixel.h>)
#include <Adafruit_NeoPixel.h>
static Adafruit_NeoPixel pixels(NUMPIXELS, PIN, NEO_GRB + NEO_KHZ800);
static bool pixelsStarted = false;

void setStatus(uint8_t r, uint8_t g, uint8_t b) {
    if (!pixelsStarted) {
        pixels.begin();
        pixelsStarted = true;
    }
    pixels.setPixelColor(0, pixels.Color(r, g, b));
    pixels.show();
}
#else
void setStatus(uint8_t r, uint8_t g, uint8_t b) {
    (void)r; (void)g; (void)b;  // no LED library installed; status is serial-only
}
#endif
