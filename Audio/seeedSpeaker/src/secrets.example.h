#pragma once

// Copy to secrets.h and fill in. secrets.h is not tracked by git.

#define WIFI_SSID      "your-ssid"
#define WIFI_PASSWORD  "your-password"

// Where the restaurant API container is running.
#define HOST_ADDR   "192.168.1.50"
#define PORT_NUM    8000

// Which device slot this mic occupies - shares the 1..MAX_DEVICES pool with
// the e-paper display at the same table. GET /device/{DEVICE_NUMBER} on boot
// (and every reconnect) resolves this to a table number.
#define DEVICE_NUMBER 1

// A name for the server logs.
#define DEVICE_ID   "mic-1"

// Only needed if the server has API_KEY set.
// #define API_KEY_STR "change-me"
