#include <Arduino.h>
#include "TFT_eSPI.h"
#include "layout.h"
#include "secrets.h"

#ifdef EPAPER_ENABLE // Only compile this code if the EPAPER_ENABLE is defined in User_Setup.h
EPaper epaper;
#endif

// ------------Config ------------
const char* ssid     = WIFI_SSID;
const char* password = WIFI_PASSWORD;
// -------------------------------
// -----------globals -----------

#define SleepMode 0
#define NormalMode 60000
#define WaiterMode 5000

//------------------------------

uint8_t defaultFontSize = 2;

void drawPartialString(char* text, Rect_t rect, uint8_t fontsize = defaultFontSize);
void drawItem(int row, char* name, char* price, char* detail, char* time, bool checked = false);
void drawCheckBox(Rect_t rect, bool checked);

char* TableString = "Table:";
char* TableNumber = "VIP";
char* TotalString = "Total:";
char* TotalAmount = "$999.99";
char* TimeString = "NEXT DISH IN:";

char* ItemName = "ITEM NAME TEST";
char* ItemPrice = "$99.99";
char* ItemDetail = "-Onions, -Tomatoes, -Lettuce";
char* ItemTime = "15 min";

float NexDishProgress = 0.5;

void setup()
{
#ifdef EPAPER_ENABLE
    epaper.begin();
    epaper.fillScreen(TFT_WHITE);
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

    drawPartialString(TimeString, TimeRect,3);
    epaper.fillRect(ProgressBarRect.x, ProgressBarRect.y, ProgressBarRect.width * NexDishProgress, ProgressBarRect.height, TFT_BLACK);

    /*for (int i = 0; i < epaper.height() / 80; i++)
    {
        epaper.setTextSize(i + 1);
        epaper.drawString("Hewwo", 10, 80 + 60 * i);
        epaper.updataPartial(10, 80 + 60 * i,( i + 1 ) * 12 * 6, ( i + 1 ) * 8);
    }*/
    epaper.update(); // update the display

#endif
}
uint8_t row = 0;
void loop()
{
    delay(1000);
    drawItem(row, "ITEM NAME TEST", "$99.99", "-Onions, -Tomatoes, -Lettuce", "15 min", row % 2 == 0);
    row = (row + 1) % 6;

}

void startupScreen(){
    char buffer[64];
    sprintf(buffer, "%s%s", TableString, TableNumber);
    drawPartialString(buffer, TableRect, 5);
    sprintf(buffer, "%s%s", TotalString, TotalAmount);
    drawPartialString(buffer, TotalRect, 4);
    epaper.drawFastVLine(TotalRect.x - xoffset_default, TotalRect.y, TotalRect.height, TFT_BLACK);
    epaper.drawLine(TableLine.x1, TableLine.y1, TableLine.x2, TableLine.y2, TFT_BLACK);
}

void drawPartialString(char* text, Rect_t rect, uint8_t fontsize) {
    epaper.setTextSize(fontsize);
    epaper.drawString(text, rect.x, rect.y);
    epaper.updataPartial(rect.x, rect.y, rect.width, rect.height);
}

void drawItem(int row, char* name, char* price, char* detail, char* time, bool checked) {
    Rect_t itemRect = getItemRect(row);
    Rect_t itemPriceRect = getItemPriceRect(itemRect);
    Rect_t itemDetailRect = getItemDetailRect(itemRect);
    Rect_t itemTimeRect = getItemTimeRect(itemRect);
    Rect_t itemCheckBoxRect = getItemCheckBoxRect(row, itemRect.height);

    drawPartialString(name, itemRect);
    drawPartialString(detail, itemDetailRect,1);
    drawPartialString(price, itemPriceRect);
    drawPartialString(time, itemTimeRect);
    drawCheckBox(itemCheckBoxRect, checked);
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