#include <Arduino.h>
#include "TFT_eSPI.h"
#include "layout.h"
#include "secrets.h"
#include "ArduinoJson.h"
#include "HTTPClient.h"
#include "magic_circle.h"

#ifdef EPAPER_ENABLE // Only compile this code if the EPAPER_ENABLE is defined in User_Setup.h
EPaper epaper;
#endif

JsonDocument doc;

// ------------Config ------------
#define deviceNumber 1

#define SleepMode 0
#define NormalMode 1
#define WaiterMode 2

#define NormalModeDelay 30000
#define WaiterModeDelay 1000
#define SleepModeDelay 300000


const char* ssid     = WIFI_SSID;
const char* password = WIFI_PASSWORD;

String serverURL = SERVER_URL;
char maxRows = 8;
// -------------------------------
// -----------globals -----------

uint8_t row = 0;
uint8_t defaultFontSize = 3;
uint8_t timePassed = 9;
char mode = NormalMode;
String jsonResponse;
String lasjsonResponse;


static char* TableString = "Table:";
//uint8_t TableNumber = 4;
static char* TotalString = "Total:";
String TotalAmount = "$0.00";
// char* TimeString = "NEXT DISH IN:";

RTC_DATA_ATTR bool mainDrawn = false;
RTC_DATA_ATTR uint8_t TableNumber = 0;
RTC_DATA_ATTR uint8_t OrderID = 0;
bool itemsDrawn = false; 

void drawPartialString(const char* text, Rect_t rect, uint8_t fontsize = defaultFontSize);
void drawItem(int row, const char* name, const char* price, const char* detail, const float time, bool checked = false);
void drawCheckBox(Rect_t rect, bool checked);
void startupScreen();
void drawTotal(const char* totalAmount);
void drawProgressBar(float progress);
bool fetchOrders(String &jsonResponse, uint8_t tableNumber);
void normalMode();
void waiterMode();
void sleepMode();
void fetchCurrentTable();
void changeTableNumber(uint8_t newTableNumber);
void drawWaiterModeScreen();
bool fetchPendingOrders(String &jsonResponse, uint8_t tableNumber);
bool checkPending();
void goToSleep(uint32_t ms);
void drawSplashScreen();



// char* ItemName = "ITEM NAME TEST";
// char* ItemPrice = "$99.99";
// char* ItemDetail = "-Onions, -Tomatoes, -Lettuce";
// char* ItemTime = "15 min";
// bool ItemDelivered = false;

float NexDishProgress = 0.5;

void setup()
{
    //delay(3000);
    Serial.begin(115200);
    Serial.println("Starting up...");

    // Connect to Wi-Fi
    WiFi.begin(ssid, password);
    Serial.print("Connecting to Wi-Fi");
    while (WiFi.status() != WL_CONNECTED) {
        delay(500);
        Serial.print(".");
    }
    Serial.println();
    Serial.println("Connected to Wi-Fi");
    Serial.print("IP Address: ");
    Serial.println(WiFi.localIP());

    fetchCurrentTable();
    //Serial.printf("Current table number: %s\n", TableNumber.c_str());
    epaper.begin();
    epaper.fillScreen(TFT_WHITE);
    // Rect_t testRect = getItemRect(0);
    //Rect_t testRect_t = getItemTimeRect(testRect);
    
    

    if (OrderID == 0) {
        Serial.println("No order ID found, going to sleep");
        sleepMode();
    }
    if (!mainDrawn) {
        epaper.update();
        startupScreen();
        epaper.update(); // update the display
        mainDrawn = true;
    }

    if (checkPending()) {
        mode = WaiterMode;
    } else {
        mode = NormalMode;
    }

    if (mode == WaiterMode) {
        while (checkPending()) {
            waiterMode();
        }
        //mainDrawn = false;
        itemsDrawn = false;
        normalMode();
    } else if (mode == NormalMode) {
        normalMode();
    } 

    goToSleep(NormalModeDelay);
}

void goToSleep(uint32_t ms) {
    esp_sleep_enable_timer_wakeup((uint64_t)ms * 1000ULL);
    esp_deep_sleep_start();
}

void drawdebugmark(){
    epaper.fillCircle(10, 10, 50, TFT_BLACK);
    epaper.updataPartial(5, 5, 10, 10);
    delay(1000);
    epaper.fillCircle(10, 10, 50, TFT_WHITE);
    epaper.updataPartial(5, 5, 10, 10);
}

void loop()
{
    // it's too quiet in here
}

void waiterMode() {
    drawWaiterModeScreen();
    delay(WaiterModeDelay);
}
void sleepMode() {
    drawSplashScreen();
    mainDrawn = false;
    goToSleep(SleepModeDelay);
}
void normalMode() {
    if (fetchOrders(jsonResponse, TableNumber)) {
        DeserializationError error = deserializeJson(doc, jsonResponse);
        if (!error) {
            JsonArray items = doc.as<JsonArray>();
            float temp_total = 0.0;
            for (JsonObject item : items) {
                const char* itemName = item["itemName"];
                const char* itemDetails = item["details"];
                const float itemPrice = item["price"];
                const float itemTime = item["time"];
                bool itemDelivered = item["delivered"];

                temp_total += itemPrice;

                if (row < maxRows) {
                    drawItem(row, (char*)itemName, String(itemPrice, 2).c_str(), (char*)itemDetails, itemTime, itemDelivered);
                }
                row = (row + 1);
            }
            row = 0;
            TotalAmount = "$" + String(temp_total, 2);
            drawTotal(TotalAmount.c_str());
    }}
}

void drawSplashScreen() {
    epaper.fillScreen(TFT_WHITE);
    epaper.drawRect(0, 0, EPD_WIDTH, EPD_HEIGHT, TFT_BLACK);

    int16_t logoX = (EPD_WIDTH - MAGIC_CIRCLE_WIDTH) / 2;
    int16_t logoY = 50;
    epaper.drawXBitmap(logoX, logoY, magic_circle_bits, MAGIC_CIRCLE_WIDTH, MAGIC_CIRCLE_HEIGHT, TFT_BLACK);

    epaper.setTextColor(TFT_BLACK);
    epaper.setTextSize(4);
    epaper.setCursor(20, logoY + MAGIC_CIRCLE_HEIGHT + 30);
    epaper.print("Automatic Waiter");
    epaper.setTextSize(3);
    epaper.setCursor(20, logoY + MAGIC_CIRCLE_HEIGHT + 75);
    epaper.print("Device #: ");
    epaper.print(deviceNumber);
    epaper.update();
}

void drawWaiterModeScreen() {
    if (!itemsDrawn) {
        
        Rect_t titleRect = getItemRect(1);
        Rect_t tableRect = getItemRect(0);
        epaper.fillRect(tableRect.x, tableRect.y, tableRect.width, EPD_HEIGHT-tableRect.y, TFT_WHITE);
        epaper.fillRect(titleRect.x, titleRect.y-5, titleRect.width, titleRect.height+10, TFT_BLACK);
        epaper.update();
        drawPartialString("Is this correct?", titleRect, 5);
        itemsDrawn = true;
    }

    if(fetchPendingOrders(jsonResponse, TableNumber)) {
        row = 2;
        DeserializationError error = deserializeJson(doc, jsonResponse);
        if (!error) {
            JsonArray items = doc.as<JsonArray>();
            float temp_total = 0.0;
            for (JsonObject item : items) {
                const char* itemName = item["itemName"];
                const char* itemDetails = item["details"];
                const float itemPrice = item["price"];
                const float itemTime = item["time"];
                bool itemDelivered = item["delivered"];

                temp_total += itemPrice;

                if (row < maxRows) {
                    drawItem(row, (char*)itemName, String(itemPrice, 2).c_str(), (char*)itemDetails, itemTime, true);
                }
                row = (row + 1);
            }
    }}
}

void fetchCurrentTable() {
    HTTPClient http;
    String url = serverURL + "/device/" + String(deviceNumber);
    http.begin(url);
    int httpCode = http.GET();
    uint8_t newOrderID = 0;
    uint8_t newTableNumber = 0;

    if (httpCode > 0) {
        if (httpCode == HTTP_CODE_OK) {
            String response = http.getString();
            http.end();

            DeserializationError error = deserializeJson(doc, response);
            if (!error) {
                newTableNumber = doc["table_number"].as<uint8_t>();
                if (TableNumber != newTableNumber) {
                    Serial.printf("Table number changed from %d to %d\n", TableNumber, newTableNumber);
                    TableNumber = newTableNumber;
                    mainDrawn = false;
                }
                
                newOrderID = doc["order_id"].as<uint8_t>();
                if (newOrderID != OrderID) {
                    Serial.printf("Order ID changed from %d to %d\n", OrderID, newOrderID);
                    OrderID = newOrderID;
                    mainDrawn = false;
                }
            } else {
                Serial.println("Failed to parse JSON");
            }
        } else {
            Serial.printf("HTTP GET failed, error: %s\n", http.errorToString(httpCode).c_str());
        }
    } else {
        Serial.printf("HTTP GET failed, error: %s\n", http.errorToString(httpCode).c_str());
    }
}

void changeTableNumber(uint8_t newTableNumber) {
    HTTPClient http;
    String url = serverURL + "/device/" + String(deviceNumber);
    http.begin(url);
    http.addHeader("Content-Type", "application/json");
    http.PUT("{\"table_number\":" + String(newTableNumber) + "}");
    http.end();
}

bool fetchOrders(String &jsonResponse, uint8_t tableNumber) {
    HTTPClient http;
    String url = serverURL + "/table/" + String(tableNumber);
    http.begin(url); 
    int httpCode = http.GET();

    if (httpCode > 0) {
        if (httpCode == HTTP_CODE_OK) {
            jsonResponse = http.getString();
            http.end();
            if (jsonResponse != lasjsonResponse) {
                lasjsonResponse = jsonResponse;
                return true;
            }
            return false;
        }
    } else {
        Serial.printf("HTTP GET failed, error: %s\n", http.errorToString(httpCode).c_str());
    }

    http.end();
    return false; 
}

bool checkPending() {
    HTTPClient http;
    String url = serverURL + "/table/" + String(TableNumber) + "/checkPending";
    http.begin(url); 
    int httpCode = http.GET();

    if (httpCode > 0) {
        if (httpCode == HTTP_CODE_OK) {
            String response = http.getString();
            http.end();
            if (response == "true") {
                return true;
            } else {
                return false;
            }
        }
    } else {
        Serial.printf("HTTP GET failed, error: %s\n", http.errorToString(httpCode).c_str());
    }

    http.end();
    return false; 
}

bool fetchPendingOrders(String &jsonResponse, uint8_t tableNumber) {
    HTTPClient http;
    String url = serverURL + "/table/" + String(tableNumber) + "/pending";
    http.begin(url); 
    int httpCode = http.GET();

    if (httpCode > 0) {
        if (httpCode == HTTP_CODE_OK) {
            jsonResponse = http.getString();
            http.end();
            if (jsonResponse != lasjsonResponse) {
                lasjsonResponse = jsonResponse;
                return true;
            }
            return false;
        }
    } else {
        Serial.printf("HTTP GET failed, error: %s\n", http.errorToString(httpCode).c_str());
    }

    http.end();
    return false; 
}

void drawProgressBar(Rect_t rect, float progress) {
    // epaper.drawRect(ProgressBarRect.x, ProgressBarRect.y, ProgressBarRect.width, ProgressBarRect.height, TFT_WHITE);
    // epaper.fillRect(ProgressBarRect.x, ProgressBarRect.y, ProgressBarRect.width * progress, ProgressBarRect.height, TFT_BLACK);
    // epaper.updataPartial(ProgressBarRect.x, ProgressBarRect.y, ProgressBarRect.width, ProgressBarRect.height);

    if (progress > 1) {
        progress = 1;
    } else if (progress < 0) {
        progress = 0;
    }
    char progressText[16] = "       ";
    for (int i = 0; i < progress * 5; i++) {
        progressText[i] = '>';
    }
    epaper.setTextSize(3);
    epaper.drawString(progressText, rect.x+1, rect.y+1);
    epaper.drawRect(rect.x, rect.y, rect.width, rect.height, TFT_BLACK);
    epaper.updataPartial(rect.x, rect.y, rect.width, rect.height+1); // +1 because the lib is dumb and the bottom is outside the rect
}

void startupScreen(){
    char buffer[64];
    sprintf(buffer, "%s%d", TableString, TableNumber);
    drawPartialString(buffer, TableRect, 5);
    drawTotal(TotalAmount.c_str());
    epaper.drawFastVLine(TotalRect.x - xoffset_default, TotalRect.y, TotalRect.height, TFT_BLACK);
    epaper.drawLine(TableLine.x1, TableLine.y1, TableLine.x2, TableLine.y2, TFT_BLACK);
}

void drawTotal(const char* totalAmount) {
    char buffer[64];
    sprintf(buffer, "%s%s", TotalString, totalAmount);
    drawPartialString(buffer, TotalRect, 4);
    epaper.drawFastVLine(TotalRect.x - xoffset_default, TotalRect.y, TotalRect.height, TFT_BLACK);
}

void drawPartialString(const char* text, Rect_t rect, uint8_t fontsize) {
    epaper.setTextSize(fontsize);
    epaper.drawString(text, rect.x, rect.y);
    epaper.updataPartial(rect.x, rect.y, rect.width, rect.height);
}

void drawItem(int row, const char* name, const char* price, const char* detail, const float time, bool checked) {
    Rect_t itemRect = getItemRect(row);
    Rect_t itemPriceRect = getItemPriceRect(itemRect);
    Rect_t itemDetailRect = getItemDetailRect(itemRect);
    Rect_t itemTimeRect = getItemTimeRect(itemRect);
    Rect_t itemCheckBoxRect = getItemCheckBoxRect(row, itemRect.height);
    float dishProgress = 1;
    if (time >0) {
        dishProgress = timePassed / time;
    } 
    

    drawPartialString(name, itemRect,3);
    drawPartialString(detail, itemDetailRect,2);
    drawPartialString(price, itemPriceRect);
    // drawPartialString(time, itemTimeRect);
    drawCheckBox(itemCheckBoxRect, checked);
    if (!checked) {
        drawProgressBar(itemTimeRect, dishProgress);
    } else {
        epaper.fillRect(itemTimeRect.x, itemTimeRect.y, itemTimeRect.width, itemTimeRect.height+1, TFT_WHITE);
        epaper.updataPartial(itemTimeRect.x, itemTimeRect.y, itemTimeRect.width, itemTimeRect.height+1);
    }
}

void drawCheckBox(Rect_t rect, bool checked) {
    epaper.drawRect(rect.x, rect.y, rect.width, rect.height, TFT_BLACK);
    if (checked) {
        epaper.fillRect(rect.x + 2, rect.y + 2, rect.width - 4, rect.height - 4, TFT_BLACK);
    } else {
        epaper.fillRect(rect.x + 2, rect.y + 2, rect.width - 4, rect.height - 4, TFT_WHITE);
    }
    epaper.updataPartial(rect.x, rect.y, rect.width, rect.height + 1); // +1 because the lib is dumb and the bottom is outside the rect
}