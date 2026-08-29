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
#include <Wire.h>
#include <Adafruit_PN532.h>
#include <Adafruit_GFX.h>
#include <Adafruit_LEDBackpack.h>

#include "AudioTools.h"
#include "board_pins.h"
#include "secrets.h"
#include "eye_emotes.h"

using namespace websockets;

// ------------------------------- config -------------------------------------


#define LED_MATRIX_LEFT 0x70
#define LED_MATRIX_RIGHT 0x71

static const char *WIFI_SSID_ = WIFI_SSID;
static const char *WIFI_PASS_ = WIFI_PASSWORD;
String serverURL = SERVER_URL;
static const uint16_t PORT = PORT_NUM;
static uint8_t deviceNumber = DEVICE_NUMBER;

// Resolved from GET /device/{DEVICE_NUMBER}".
static int TABLE_NUMBER = 0;

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
static uint16_t nfcMisses = 0;
static const uint16_t NFC_MISS_RESET_THRESHOLD = 10;

// Set from an ISR so a button edge is never missed or timestamped late just
// because loop() happens to be stuck inside a slow/stalled I2C call (NFC
// polling, LED matrix writes) when the physical press/release occurs.
volatile bool buttonIsrPending = false;
volatile bool buttonIsrState = false;
volatile uint32_t buttonIsrTime = 0;

// Diagnostics: is the button itself flaky (raw ISR-measured hold is short),
// or is real audio being dropped somewhere between a genuinely long hold and
// what the server receives (pump call/byte counts vs. that same hold time)?
static uint32_t pressIsrTime = 0;
static uint32_t pumpCallCount = 0;
static uint32_t pumpBytesSent = 0;

Adafruit_PN532 nfc(-1, -1, &Wire);
Adafruit_8x8matrix eyeLeft = Adafruit_8x8matrix();
Adafruit_8x8matrix eyeRight = Adafruit_8x8matrix();

uint8_t uid[7];
uint8_t uidLength;

const uint8_t table1_uid[] = {0x04,0xAF, 0xD3, 0xAA, 0x8B, 0x26 ,0x81};
const uint8_t table2_uid[] = {0x04, 0x33, 0xCA, 0xAA, 0x8B, 0x26, 0x81};
const uint8_t table3_uid[] = {0x04, 0x42, 0x11, 0xAB, 0x8B, 0x26, 0x81};

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
void initNFC();
void initMatrix(Adafruit_8x8matrix &m, uint8_t addr);
void showEyeEmote(Adafruit_8x8matrix &m, const uint8_t *emote);
void nfcTagRecon();
void changeTableNumber(uint8_t newTableNumber);
void IRAM_ATTR handleButtonInterrupt();

void scanI2CBus() {
  Serial.println("Scanning I2C bus...");
  uint8_t found = 0;
  for (uint8_t addr = 1; addr < 127; addr++) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() == 0) {
      Serial.printf("  device found at 0x%02X\n", addr);
      found++;
    }
  }
  if (found == 0) {
    Serial.println("  no I2C devices found - check wiring/power");
  }
}

void showEyeEmote(Adafruit_8x8matrix &m, const uint8_t *emote){
    m.clear();
    m.drawBitmap(0,0,emote,8,8,LED_ON);
    m.writeDisplay();
}

void setEyes(const uint8_t *emote){
    showEyeEmote(eyeLeft,emote);
    showEyeEmote(eyeRight,emote);
}


void blinkAnimation(){
    setEyes(idle_eye);
    delay(100);
    setEyes(closed_eye);
    delay(100);
    setEyes(idle_eye);
}

void happyAnimation(){
    for(int i=0; i<3;i++){
    setEyes(happy1);
    delay(100);
    setEyes(happy2);
    delay(100);
}
}

void sideEyeAnimation(){
    setEyes(leftidle_eye);
    delay(100);
    setEyes(rightidle_eye);
    delay(100);
    setEyes(idle_eye);
}

void changeTableAnimation(uint8_t table){
    switch (table)
    {
    case 1:
        setEyes(tag1);
        break;
    case 2:
        setEyes(tag2);
        break;
    case 3:
        setEyes(tag3);
        break;
    
    default:
        break;
    }
    delay(1000);
    happyAnimation();
    sideEyeAnimation();
}


// -------------------------------- setup -------------------------------------
void setup() {
    Serial.begin(115200);
    pinMode(BUTTON_PIN, INPUT_PULLUP);
    attachInterrupt(digitalPinToInterrupt(BUTTON_PIN), handleButtonInterrupt, CHANGE);

    Wire.begin();

    delay(3000); // delay so i can connect to the serial, remove!
    scanI2CBus();
    initNFC();
    initMatrix(eyeLeft, LED_MATRIX_LEFT);
    initMatrix(eyeRight, LED_MATRIX_RIGHT);

    setEyes(question_eye);
    delay(2000);
    setEyes(happy_face);
    delay(2000);
    setEyes(sad_eye);
    delay(2000);
    setEyes(error);
    delay(2000);

    happyAnimation();
    blinkAnimation();

    setEyes(idle_eye);

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

    // ---- debounced push-to-talk (edge captured by ISR, not polling) --------
    if (buttonIsrPending) {
        noInterrupts();
        bool newState = buttonIsrState;
        uint32_t changeTime = buttonIsrTime;
        buttonIsrPending = false;
        interrupts();

        if (newState != lastButtonDown && changeTime - lastButtonChange > DEBOUNCE_MS) {
            lastButtonChange = changeTime;
            lastButtonDown = newState;
            if (newState) {
                pressIsrTime = changeTime;
                startCapture();
                setEyes(attention_eye);
            } else {
                Serial.printf("[diag] ISR-measured hold: %lu ms (press t=%lu, release t=%lu)\n",
                              changeTime - pressIsrTime, pressIsrTime, changeTime);
                stopCapture();
                Serial.printf("[diag] pumpAudio calls=%lu, bytes sent=%lu (~%.2fs of audio)\n",
                              pumpCallCount, pumpBytesSent,
                              pumpBytesSent / (float)(SAMPLE_RATE * sizeof(int16_t)));
            }
        }
    }

    if (capturing) {
        //Serial.printf("Capturing...");
        pumpAudio();
        // Don't let a stuck button stream forever.
        if (millis() - captureStartedAt > MAX_UTTERANCE_MS) {
            Serial.println("[mic] max utterance reached");
            stopCapture();
        }
    } else {
        // Keep draining I2S so the DMA buffers don't overflow with stale audio.
        i2sIn.readBytes((uint8_t *)i2sBuffer, I2S_CHUNK_BYTES);

        bool tagPresent = 0;//nfc.readPassiveTargetID(PN532_MIFARE_ISO14443A, uid, &uidLength, 100);

        if (tagPresent){
            nfcMisses = 0;
            nfcTagRecon();
        } else if (++nfcMisses > NFC_MISS_RESET_THRESHOLD) {
            // Known arduino-esp32 I2C-NG driver bug: an I2C NACK (which the PN532
            // sends routinely while polled without an IRQ pin) can wedge the bus
            // into ESP_ERR_INVALID_STATE permanently. Reset the driver to recover.
            // https://github.com/espressif/arduino-esp32/issues/11374
            nfcMisses = 0;
            Wire.end();
            Wire.begin();
        }
    }
}

void initNFC(){
    nfc.begin();
    uint32_t version = nfc.getFirmwareVersion();
    if (!version) {
        Serial.println("PN532 not found!!");
        while (true) {
            delay(1000);
        }
    }
    nfc.SAMConfig();
}

void initMatrix(Adafruit_8x8matrix &m, uint8_t addr){
    if (!m.begin(addr, &Wire)){
        Serial.printf("Matrix %02X not found",addr);
        return;
    } 
    m.setBrightness(8);
    m.clear();
    m.writeDisplay();
}


void nfcTagRecon(){
    // Serial.print("Tag UID:");
    // for (uint8_t i = 0; i < uidLength; i++) {
    //   Serial.printf(" %02X", uid[i]);
    // }
    // Serial.println();
    // if (uidLength == sizeof(table1_uid) && memcmp(uid, table1_uid, uidLength) == 0){
    //   showEyeEmote(eyeLeft, tag_found_bmp);
    //   showEyeEmote(eyeRight, tag_found_bmp);
    // }
    uint8_t newtable = 0;
    if (uidLength == sizeof(table1_uid)){
        
            if (uid[1] == table1_uid[1]){
                newtable = 1;
            } else if (uid[1] == table2_uid[1]){
                newtable = 2;
            } else if (uid[1] == table3_uid[1])
            {
                newtable = 3;
            }
            if(newtable != 0 && newtable != TABLE_NUMBER){
                changeTableNumber(newtable);
                changeTableAnimation(newtable);
        }
        
    }
}

void changeTableNumber(uint8_t newTableNumber) {
    Serial.printf("Table changed to %d",newTableNumber);
    HTTPClient http;
    String url = serverURL + ":" + String(PORT) + "/device/" + String(deviceNumber);
    http.begin(url);
    http.addHeader("Content-Type", "application/json");
    http.PUT("{\"table_number\":" + String(newTableNumber) + "}");
    http.end();
    TABLE_NUMBER = newTableNumber;
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
    String url = serverURL + ":" + String(PORT) + "/device/" + String(DEVICE_NUMBER);
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
    ws.close();
    int fetched;
    if (fetchAssignedTable(fetched)) {
        if (fetched != TABLE_NUMBER) {
            Serial.printf("[device] table changed %d -> %d\n", TABLE_NUMBER, fetched);
        }
        TABLE_NUMBER = fetched;
    } else {
        Serial.println("[device] lookup failed, using last known table");
    }

    // serverURL includes the "http://" scheme (needed for HTTPClient calls
    // elsewhere); strip it here since we're building a ws:// URL instead.
    String host = serverURL;
    host.replace("http://", "");

    char url[160];
#ifdef API_KEY_STR
    snprintf(url, sizeof(url), "ws://%s:%u/table/%d/voice?device=%s&api_key=%s", host.c_str(),
             PORT, TABLE_NUMBER, DEVICE_NAME, API_KEY_STR);
#else
    snprintf(url, sizeof(url), "ws://%s:%u/table/%d/voice?device=%u", host.c_str(), PORT,
             TABLE_NUMBER, deviceNumber);
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
        setEyes(wait_eye);
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
        happyAnimation();
        setEyes(idle_eye);

    } else if (!strcmp(type, "error")) {
        Serial.printf("[ws] error: %s\n", doc["message"] | "");
        setStatus(60, 0, 0);
        delay(400);
        setStatus(0, 30, 0);
        setEyes(error);
        delay(1000);
        sideEyeAnimation();

    } else if (!strcmp(type, "cancelled")) {
        setStatus(0, 30, 0);
        setEyes(sad_eye);
        delay(1000);
        sideEyeAnimation();
    }
}

// -------------------------------- capture -----------------------------------
// Minimal and non-blocking, as an ISR must be: just latch the new state and
// the exact time it happened. All the real work (debounce, startCapture,
// stopCapture, any I2C/Serial/WS calls) stays in loop(), which is not
// interrupt-safe to call from here.
void IRAM_ATTR handleButtonInterrupt() {
    buttonIsrState = (digitalRead(BUTTON_PIN) == LOW);
    buttonIsrTime = millis();
    buttonIsrPending = true;
}

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
    pumpCallCount = 0;
    pumpBytesSent = 0;
    ws.send("{\"type\":\"start\"}");
}

void stopCapture() {
    if (!capturing) return;
    capturing = false;
    Serial.println("[mic] stop");
    ws.send("{\"type\":\"stop\"}");
    setEyes(wait_eye);
}

void cancelCapture() {
    if (!capturing) return;
    capturing = false;
    ws.send("{\"type\":\"cancel\"}");
}

void pumpAudio() {
    pumpCallCount++;
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
        return;
    }
    pumpBytesSent += frames * sizeof(int16_t);
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
