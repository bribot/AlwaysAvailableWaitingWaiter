#include <Arduino.h>
// 5.83-inch ePaper - Monochrome 648x480
#define EPD_WIDTH 648
#define EPD_HEIGHT 480

class Rect_t {
public:
    int32_t x;
    int32_t y;
    int32_t width;
    int32_t height;
};

class Line_t {
public:
    int32_t x1;
    int32_t y1;
    int32_t x2;
    int32_t y2;
};

// ------------Layout ------------
int8_t yoffset_default = 10;
int8_t xoffset_default = 10;

// Table #
const Rect_t TableRect = {  
    .x = 0, 
    .y = 0, 
    .width = EPD_WIDTH/2, 
    .height = EPD_HEIGHT/9 
};

const Rect_t TotalRect = {  
    .x = EPD_WIDTH/2, 
    .y = 0, 
    .width = EPD_WIDTH/2, 
    .height = EPD_HEIGHT/9 
};

const Line_t TableLine = {
    .x1 = 0,
    .y1 = EPD_HEIGHT/9,
    .x2 = EPD_WIDTH,
    .y2 = EPD_HEIGHT/9
};

const Rect_t ItemRectBase = {
    .x = 30,
    .y = EPD_HEIGHT/9 + yoffset_default,
    .width = EPD_WIDTH,
    .height = EPD_HEIGHT/9
};

const Rect_t ItemCheckBoxRectBase = {
    .x = 0,
    .y = EPD_HEIGHT/9 + yoffset_default,
    .width = 20,
    .height = 20
};

// Every rect below is derived from a given row's ItemRect, computed fresh
// each call instead of being baked in once — so moving the item rect
// (e.g. by row index) automatically moves everything that depends on it.
inline Rect_t getItemRect(int row) {
    Rect_t r = ItemRectBase;
    r.y += row * r.height;
    return r;
}

inline Rect_t getItemDetailRect(const Rect_t& item) {
    return Rect_t{ item.x, item.y + item.height/2, item.width, item.height/2 };
}

inline Rect_t getItemPriceRect(const Rect_t& item) {
    return Rect_t{ item.x + item.width*3/4, item.y, item.width/3, item.height };
}

inline Rect_t getItemTimeRect(const Rect_t& item) {
    return Rect_t{ item.x + item.width*2/4, item.y, item.width/3, item.height };
}

inline Rect_t getItemCheckBoxRect(int row, int32_t itemHeight) {
    Rect_t r = ItemCheckBoxRectBase;
    r.y += row * itemHeight;
    return r;
}

const Rect_t TimeRect = {  
    .x = EPD_WIDTH/3 - 2 * xoffset_default, 
    .y = EPD_HEIGHT*8/9, 
    .width = EPD_WIDTH/3,
    .height = EPD_HEIGHT/9 
};

const Rect_t ProgressBarRect = {  
    .x = EPD_WIDTH*2/3, 
    .y = EPD_HEIGHT*8/9, 
    .width = EPD_WIDTH/3,
    .height =20
};
