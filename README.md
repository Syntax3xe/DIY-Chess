# Smart Chess Board

NFC piece detection (64 × RC522), per-square WS2812B LEDs, and Stockfish move hints.

```
firmware/chessboard_firmware/chessboard_firmware.ino   ESP32 sketch (Arduino IDE)
host/chessboard.py                                     Python host app (python-chess + Stockfish)
host/test_chessboard.py                                Hardware-free tests for the host logic
host/requirements.txt
```

## Firmware
1. Arduino IDE → add ESP32 board support ("esp32" by Espressif).
2. Library Manager: **MFRC522** (GithubCommunity) and **Adafruit NeoPixel**.
3. Open `firmware/chessboard_firmware/chessboard_firmware.ino`, select *ESP32 Dev Module*, upload.

For single-rank bring-up, set `NUM_SQUARES` to `8` at the top of the sketch.
To slow the SPI clock, edit `MFRC522_SPICLOCK` in the library's `MFRC522.h`. A `#define` in the sketch does not reach the library's compiled code.

## Host
```bash
cd host
pip install -r requirements.txt
brew install stockfish            # or apt install stockfish / download for Windows
python chessboard.py --port /dev/cu.usbserial-XXXX --engine stockfish
python -m unittest test_chessboard   # optional, no hardware needed
```

Console commands: `hints`, `new`, `undo`, `tag <sq> <piece>`, `tagall`, `quit`.
