#include <Arduino.h>
#include "TFT_eSPI.h"
#include "layout.h"
#include "secrets.h"
#include "ArduinoJson.h"
#include "HTTPClient.h"

#ifdef EPAPER_ENABLE // Only compile this code if the EPAPER_ENABLE is defined in User_Setup.h
EPaper epaper;
#endif

JsonDocument doc;

// ------------Config ------------
#define deviceNumber 001

#define SleepMode 0
#define NormalMode 1
#define WaiterMode 2

#define NormalModeDelay 3000
#define WaiterModeDelay 1000


const char* ssid     = WIFI_SSID;
const char* password = WIFI_PASSWORD;

const char* serverURL = "http://homeassistant.local:8123/local/test/";
char maxRows = 8;
// -------------------------------
// -----------globals -----------

uint8_t row = 0;
uint8_t defaultFontSize = 3;
uint8_t timePassed = 9;

void drawPartialString(const char* text, Rect_t rect, uint8_t fontsize = defaultFontSize);
void drawItem(int row, const char* name, const char* price, const char* detail, const float time, bool checked = false);
void drawCheckBox(Rect_t rect, bool checked);
void startupScreen();
void drawTotal(const char* totalAmount);
void drawProgressBar(float progress);
bool fetchOrders(String &jsonResponse, const char* tableNumber);

static char* TableString = "Table:";
char* TableNumber = "VIP";
static char* TotalString = "Total:";
String TotalAmount = "$0.00";
char* TimeString = "NEXT DISH IN:";

// char* ItemName = "ITEM NAME TEST";
// char* ItemPrice = "$99.99";
// char* ItemDetail = "-Onions, -Tomatoes, -Lettuce";
// char* ItemTime = "15 min";
// bool ItemDelivered = false;

float NexDishProgress = 0.5;

void setup()
{
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

    epaper.begin();
    epaper.fillScreen(TFT_WHITE);
    // Rect_t testRect = getItemRect(0);
    //Rect_t testRect_t = getItemTimeRect(testRect);
    
    

    epaper.update();
    startupScreen();
    
    
    // for (int i=0; i<6; i++) {
    //     // Rect_t itemRect = getItemRect(i);
    //     // Rect_t itemPriceRect = getItemPriceRect(itemRect);
    //     // Rect_t itemDetailRect = getItemDetailRect(itemRect);
    //     // Rect_t itemTimeRect = getItemTimeRect(itemRect);
    //     // Rect_t itemCheckBoxRect = getItemCheckBoxRect(i, itemRect.height);

    //     // drawPartialString(ItemName, itemRect);
    //     // drawPartialString(ItemDetail, itemDetailRect,1);
    //     // drawPartialString(ItemPrice, itemPriceRect);
    //     // drawPartialString(ItemTime, itemTimeRect);
    //     // epaper.drawRect(itemCheckBoxRect.x, itemCheckBoxRect.y, itemCheckBoxRect.width, itemCheckBoxRect.height, TFT_BLACK);
    // }

    //drawPartialString(TimeString, TimeRect,3);
    //drawProgressBar(NexDishProgress);

    /*for (int i = 0; i < epaper.height() / 80; i++)
    {
        epaper.setTextSize(i + 1);
        epaper.drawString("Hewwo", 10, 80 + 60 * i);
        epaper.updataPartial(10, 80 + 60 * i,( i + 1 ) * 12 * 6, ( i + 1 ) * 8);
    }*/
    epaper.update(); // update the display

}
String jsonResponse;
String lasjsonResponse;

void drawdebugmark(){
    epaper.fillCircle(10, 10, 50, TFT_BLACK);
    epaper.updataPartial(5, 5, 10, 10);
    delay(1000);
    epaper.fillCircle(10, 10, 50, TFT_WHITE);
    epaper.updataPartial(5, 5, 10, 10);
} 

void loop()
{
    drawdebugmark();
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
    delay(NormalModeDelay);
    //drawItem(row, "ITEM NAME TEST", "$99.99", "-Onions, -Tomatoes, -Lettuce", "15 min", row % 2 == 0);
    //row = (row + 1) % 6;

}



bool fetchOrders(String &jsonResponse, const char* tableNumber) {
    HTTPClient http;
    http.begin(serverURL + String(tableNumber) + ".json"); 
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
    sprintf(buffer, "%s%s", TableString, TableNumber);
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
    float dishProgress = timePassed / time;

    drawPartialString(name, itemRect,3);
    drawPartialString(detail, itemDetailRect,2);
    drawPartialString(price, itemPriceRect);
    // drawPartialString(time, itemTimeRect);
    drawCheckBox(itemCheckBoxRect, checked);
    if (!checked) {
        drawProgressBar(itemTimeRect, dishProgress);
    } else {
        epaper.fillRect(itemTimeRect.x, itemTimeRect.y, itemTimeRect.width, itemTimeRect.height, TFT_WHITE);
        epaper.updataPartial(itemTimeRect.x, itemTimeRect.y, itemTimeRect.width, itemTimeRect.height);
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