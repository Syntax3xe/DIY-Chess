/*
  Smart Chess Board - ESP32 firmware
  - 64x RC522 readers selected through 74HC138 decoders (library SS pin gates the decoders)
  - 64x WS2812B LEDs (serpentine, a1 = LED 0)
  - Piece identity is stored in the NTAG tag (page 4): 'C','H',piece,piece^0x5A

  Libraries: MFRC522 (GithubCommunity / miguelbalboa), Adafruit NeoPixel
  Board:     ESP32 Dev Module

  SPI clock note: the MFRC522 library sets its SPI clock (MFRC522_SPICLOCK, default 4 MHz)
  inside its own .cpp file, so a #define in this sketch has no effect. If the long shared bus
  is unreliable, edit MFRC522_SPICLOCK in the library's MFRC522.h (e.g. to 1000000u).

  Serial protocol (115200 baud, newline-terminated):
    ESP32 -> PC : "B <64 chars>"   board state ('.', PNBRQK / pnbrqk, '?')
    PC -> ESP32 : "H <64 chars>"   LED overlay (F T f t L R .)
    PC -> ESP32 : "T e1 K"         program tag on e1 as white king
    ESP32 -> PC : "OK TAG e1 K" / "ERR ..."
    PC -> ESP32 : "S"              re-send board state
*/

#include <SPI.h>
#include <MFRC522.h>
#include <Adafruit_NeoPixel.h>

// ---------------- Pins ----------------
constexpr uint8_t PIN_SCK  = 18;
constexpr uint8_t PIN_MISO = 19;
constexpr uint8_t PIN_MOSI = 23;
constexpr uint8_t PIN_SS   = 5;    // -> E1 of decoders U1..U8
constexpr uint8_t PIN_RST  = 22;   // -> RST of all RC522
constexpr uint8_t PIN_LED  = 4;    // -> level shifter -> LED chain
constexpr uint8_t PIN_FILE[3] = {25, 26, 27};  // A0..A2 of U1..U8
constexpr uint8_t PIN_RANK[3] = {32, 33, 13};  // A0..A2 of U0

constexpr uint8_t  NUM_SQUARES   = 64;    // set to 8 during single-rank bring-up
constexpr uint8_t  TAG_PAGE      = 4;     // first user page on NTAG213/215/216
constexpr uint16_t TIMEOUT_FAST  = 200;   // 200 ticks * 25 us = 5 ms
constexpr uint16_t TIMEOUT_SLOW  = 1000;  // 25 ms (used while writing tags)
constexpr uint8_t  LED_BRIGHTNESS = 80;

MFRC522 rfid(PIN_SS, 255);                // 255 = no reset pin (we reset all readers ourselves)
Adafruit_NeoPixel strip(64, PIN_LED, NEO_GRB + NEO_KHZ800);

// ---------------- State ----------------
char board[65];       // '.', FEN piece letter, or '?'
char sentBoard[65];
char overlay[65];

struct Cache {
  uint8_t uid[10];
  uint8_t len;
  char    piece;
};
Cache cache[64];

String rxLine;
uint32_t lastRender = 0;

// ---------------- Helpers ----------------
inline uint8_t ledIndex(uint8_t sq) {
  uint8_t rank = sq >> 3, file = sq & 7;
  return rank * 8 + ((rank & 1) ? (7 - file) : file);
}

void sqName(uint8_t sq, char *out) {
  out[0] = 'a' + (sq & 7);
  out[1] = '1' + (sq >> 3);
  out[2] = 0;
}

void selectReader(uint8_t sq) {
  uint8_t file = sq & 7, rank = sq >> 3;
  for (int i = 0; i < 3; i++) {
    digitalWrite(PIN_FILE[i], (file >> i) & 1);
    digitalWrite(PIN_RANK[i], (rank >> i) & 1);
  }
  delayMicroseconds(5);   // let the decoders settle
}

void setTimeoutTicks(uint16_t ticks) {
  rfid.PCD_WriteRegister(MFRC522::TReloadRegH, ticks >> 8);
  rfid.PCD_WriteRegister(MFRC522::TReloadRegL, ticks & 0xFF);
}

bool validPiece(char p) {
  return p != 0 && strchr("PNBRQKpnbrqk", p) != nullptr;
}

// ---------------- LEDs ----------------
void renderLeds() {
  float pulse = 0.6f + 0.4f * sinf(millis() / 180.0f);
  for (uint8_t sq = 0; sq < 64; sq++) {
    uint8_t r = 0, g = 0, b = 0;
    char p = board[sq];
    if (p == '?')       { r = 50; }
    else if (p != '.')  {
      if (p >= 'A' && p <= 'Z') { r = 30; g = 40; b = 60; }   // white piece: cool white
      else                      { r = 60; g = 20; b = 0;  }   // black piece: warm orange
    }
    switch (overlay[sq]) {
      case 'F': r = 0;   g = 200; b = 0;  break;
      case 'T': r = 0;   g = (uint8_t)(255 * pulse); b = (uint8_t)(120 * pulse); break;
      case 'f': r = 110; g = 90;  b = 0;  break;
      case 't': r = 200; g = 160; b = 0;  break;
      case 'L': r = 70;  g = 0;   b = 90; break;
      case 'R': r = (uint8_t)(255 * pulse); g = 0; b = 0; break;
      default: break;
    }
    strip.setPixelColor(ledIndex(sq), strip.Color(r, g, b));
  }
  strip.show();
  lastRender = millis();
}

// ---------------- Tag access ----------------
char readPieceFromTag() {
  byte buf[18];
  byte size = sizeof(buf);
  if (rfid.MIFARE_Read(TAG_PAGE, buf, &size) != MFRC522::STATUS_OK) return '?';
  if (buf[0] != 'C' || buf[1] != 'H') return '?';
  char p = (char)buf[2];
  if ((byte)(p ^ 0x5A) != buf[3] || !validPiece(p)) return '?';
  return p;
}

// Returns 0 if no tag, '?' if tag with unknown content, or the piece letter.
char scanSquare(uint8_t sq) {
  selectReader(sq);
  rfid.PCD_AntennaOn();
  delay(2);                                   // let the field settle

  byte atqa[2];
  byte atqaSize = sizeof(atqa);
  MFRC522::StatusCode st = rfid.PICC_WakeupA(atqa, &atqaSize);
  char result = 0;

  if ((st == MFRC522::STATUS_OK || st == MFRC522::STATUS_COLLISION) && rfid.PICC_ReadCardSerial()) {
    Cache &c = cache[sq];
    bool same = (c.len == rfid.uid.size) && memcmp(c.uid, rfid.uid.uidByte, c.len) == 0;
    if (!same || c.piece == '?') {
      c.len = rfid.uid.size;
      memcpy(c.uid, rfid.uid.uidByte, c.len);
      c.piece = readPieceFromTag();
    }
    result = c.piece;
    rfid.PICC_HaltA();
  }
  rfid.PCD_AntennaOff();
  return result;
}

bool programTag(uint8_t sq, char piece) {
  selectReader(sq);
  rfid.PCD_AntennaOn();
  delay(2);
  setTimeoutTicks(TIMEOUT_SLOW);              // EEPROM writes need longer than a read

  byte atqa[2];
  byte atqaSize = sizeof(atqa);
  MFRC522::StatusCode st = rfid.PICC_WakeupA(atqa, &atqaSize);
  bool ok = false;
  if ((st == MFRC522::STATUS_OK || st == MFRC522::STATUS_COLLISION) && rfid.PICC_ReadCardSerial()) {
    byte page[4] = {'C', 'H', (byte)piece, (byte)(piece ^ 0x5A)};
    ok = (rfid.MIFARE_Ultralight_Write(TAG_PAGE, page, 4) == MFRC522::STATUS_OK);
    rfid.PICC_HaltA();
  }
  setTimeoutTicks(TIMEOUT_FAST);
  rfid.PCD_AntennaOff();
  cache[sq].len = 0;                          // force a re-read next scan
  return ok;
}

void updateSquare(uint8_t sq) {
  char r = scanSquare(sq);
  // If a piece was here a moment ago, retry before declaring the square empty
  if (!r && board[sq] != '.') {
    r = scanSquare(sq);
    if (!r) r = scanSquare(sq);
  }
  board[sq] = r ? r : '.';
}

// ---------------- Serial ----------------
void handleLine(const String &s) {
  if (s.startsWith("H ") && s.length() >= 66) {
    for (int i = 0; i < 64; i++) overlay[i] = s[2 + i];
    renderLeds();
  } else if (s == "S") {
    memset(sentBoard, 0, sizeof(sentBoard));          // force a re-send
  } else if (s.startsWith("T ") && s.length() >= 6) {
    int file = s[2] - 'a', rank = s[3] - '1';
    char piece = s[5];
    if (file < 0 || file > 7 || rank < 0 || rank > 7 || !validPiece(piece)) {
      Serial.println("ERR bad args");
      return;
    }
    uint8_t sq = rank * 8 + file;
    char name[3];
    sqName(sq, name);
    if (programTag(sq, piece)) Serial.printf("OK TAG %s %c\n", name, piece);
    else                       Serial.printf("ERR TAG %s\n", name);
  } else if (s.length()) {
    Serial.println("ERR unknown command");
  }
}

void pollSerial() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') { handleLine(rxLine); rxLine = ""; }
    else if (c != '\r' && rxLine.length() < 120) rxLine += c;
  }
}

// ---------------- Setup / loop ----------------
void setup() {
  Serial.begin(115200);
  rxLine.reserve(128);

  for (int i = 0; i < 3; i++) {
    pinMode(PIN_FILE[i], OUTPUT);
    pinMode(PIN_RANK[i], OUTPUT);
  }
  pinMode(PIN_SS, OUTPUT);
  digitalWrite(PIN_SS, HIGH);

  pinMode(PIN_RST, OUTPUT);                  // hard-reset every reader once
  digitalWrite(PIN_RST, LOW);  delay(10);
  digitalWrite(PIN_RST, HIGH); delay(60);

  SPI.begin(PIN_SCK, PIN_MISO, PIN_MOSI, -1);

  strip.begin();
  strip.setBrightness(LED_BRIGHTNESS);
  strip.clear();
  strip.show();

  memset(board, '.', 64);   board[64] = 0;
  memset(overlay, '.', 64); overlay[64] = 0;
  memset(sentBoard, 0, sizeof(sentBoard));
  memset(cache, 0, sizeof(cache));

  // Initialise each reader, with a progress light and a dead-reader check
  for (uint8_t sq = 0; sq < NUM_SQUARES; sq++) {
    selectReader(sq);
    rfid.PCD_Init();
    rfid.PCD_SetAntennaGain(MFRC522::RxGain_max);
    setTimeoutTicks(TIMEOUT_FAST);
    rfid.PCD_AntennaOff();

    byte v = rfid.PCD_ReadRegister(MFRC522::VersionReg);
    bool dead = (v == 0x00 || v == 0xFF);
    if (dead) {
      char name[3]; sqName(sq, name);
      Serial.printf("WARN reader %s not responding (version 0x%02X)\n", name, v);
    }
    strip.setPixelColor(ledIndex(sq), dead ? strip.Color(120, 0, 0) : strip.Color(0, 0, 80));
    strip.show();
  }
  delay(500);                                // leave the boot map visible briefly
  Serial.println("READY");
}

void loop() {
  for (uint8_t sq = 0; sq < NUM_SQUARES; sq++) {
    updateSquare(sq);
    pollSerial();
    if (millis() - lastRender > 50) renderLeds();   // keeps pulses animating
  }
  renderLeds();

  if (strcmp(board, sentBoard) != 0) {
    Serial.print("B ");
    Serial.println(board);
    strcpy(sentBoard, board);
  }
}
