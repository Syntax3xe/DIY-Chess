#!/usr/bin/env python3
"""
Smart chess board host.
 - Reads the sensed board from the ESP32 over USB serial.
 - Detects which legal move was played by matching the sensed piece placement.
 - Asks Stockfish for the top moves and sends LED highlights back.

Usage:
    python chessboard.py --port /dev/ttyUSB0 --engine stockfish
"""
import argparse
import queue
import sys
import threading
import time

import chess
import chess.engine

ERROR_DELAY = 6.0   # seconds of a stable, non-matching board before squares turn red
UNKNOWN = "?"       # sensed value for a tag that is present but unreadable/unprogrammed


def fmt_score(score):
    s = score.white()
    if s.is_mate():
        return f"M{s.mate():+d}"
    return f"{s.score() / 100:+.2f}"


def parse_board_line(line):
    """'B ........' -> {square: Piece or UNKNOWN}. Empty squares are omitted."""
    payload = line[2:66]
    sensed = {}
    for sq, ch in enumerate(payload):
        if ch == ".":
            continue
        try:
            sensed[sq] = chess.Piece.from_symbol(ch)
        except ValueError:
            sensed[sq] = UNKNOWN
    return sensed


class LineReader:
    """Buffers serial input so lines are never split by a read timeout."""

    def __init__(self, ser):
        self.ser = ser
        self.buf = bytearray()

    def readline(self):
        """Return one complete line (stripped), or None if none is ready yet."""
        while True:
            nl = self.buf.find(b"\n")
            if nl >= 0:
                raw, self.buf = self.buf[:nl], self.buf[nl + 1:]
                return raw.decode(errors="ignore").strip()
            chunk = self.ser.read(self.ser.in_waiting or 1)
            if not chunk:
                return None
            self.buf += chunk


class App:
    def __init__(self, ser, engine, think_time, lines):
        self.ser = ser
        self.reader = LineReader(ser)
        self.engine = engine
        self.think_time = think_time
        self.lines = lines
        self.hints = True
        self.sensed = {}
        self.new_game(announce=False)

    # ---------- serial ----------
    def send(self, text):
        self.ser.write((text + "\n").encode())

    def send_and_wait(self, cmd, timeout=6.0):
        self.send(cmd)
        end = time.time() + timeout
        while time.time() < end:
            line = self.reader.readline()
            if line and line.startswith(("OK", "ERR")):
                return line
        return "ERR timeout"

    # ---------- game state ----------
    def new_game(self, announce=True):
        self.board = chess.Board()
        self.last_move = None
        self.suggestions = []
        self.restoring = True          # waiting for the physical board to match exactly
        self.show_errors = False
        self.mismatch_since = None
        if announce:
            print("New game. Set the pieces on their starting squares.")

    def undo(self):
        if not self.board.move_stack:
            print("Nothing to undo.")
            return
        self.board.pop()
        self.last_move = self.board.peek() if self.board.move_stack else None
        self.suggestions = []
        self.restoring = True
        print("Move undone. Put the pieces back on the red squares.")
        self.push_overlay()

    # ---------- analysis ----------
    def analyse(self):
        self.suggestions = []
        if self.board.is_game_over():
            print(f"\nGame over: {self.board.result()} ({self.board.outcome().termination.name})")
            return
        infos = self.engine.analyse(
            self.board, chess.engine.Limit(time=self.think_time), multipv=self.lines
        )
        for info in infos:
            if info.get("pv") and "score" in info:
                self.suggestions.append((info["pv"][0], info["score"]))

        side = "White" if self.board.turn else "Black"
        print(f"\n{self.board}\n\n{side} to move. Top moves:")
        for i, (mv, score) in enumerate(self.suggestions, 1):
            print(f"  {i}. {self.board.san(mv):<8} eval {fmt_score(score)}")

    # ---------- LEDs ----------
    def overlay(self):
        ov = ["."] * 64
        if self.restoring or self.show_errors:
            expected = self.board.piece_map()
            for sq in range(64):
                if expected.get(sq) != self.sensed.get(sq):
                    ov[sq] = "R"
            return "".join(ov)

        if self.last_move:
            ov[self.last_move.from_square] = "L"
            ov[self.last_move.to_square] = "L"
        if self.hints:
            for i in reversed(range(len(self.suggestions))):   # best move written last
                mv, _ = self.suggestions[i]
                if i == 0:
                    ov[mv.from_square], ov[mv.to_square] = "F", "T"
                else:
                    ov[mv.from_square], ov[mv.to_square] = "f", "t"
        if self.board.is_check():
            ov[self.board.king(self.board.turn)] = "R"
        return "".join(ov)

    def push_overlay(self):
        self.send("H " + self.overlay())

    # ---------- sensing ----------
    def match_move(self, sensed):
        if UNKNOWN in sensed.values():
            return None
        for mv in self.board.legal_moves:       # includes all four promotion choices
            self.board.push(mv)
            ok = self.board.piece_map() == sensed
            self.board.pop()
            if ok:
                return mv
        return None

    def on_board(self, sensed):
        self.sensed = sensed
        expected = self.board.piece_map()

        # Pieces reset to the starting position mid-game -> new game
        if self.board.move_stack and sensed == chess.Board().piece_map():
            self.new_game()
            expected = self.board.piece_map()

        if sensed == expected:
            self.mismatch_since = None
            self.show_errors = False
            if self.restoring:
                self.restoring = False
                print("Position matches. Game on!")
                self.analyse()
            self.push_overlay()
            return

        # While restoring (new game / undo) only an exact match is accepted. Otherwise
        # the pieces still standing after an undo would immediately replay that move.
        move = None if self.restoring else self.match_move(sensed)
        if move:
            san = self.board.san(move)
            self.board.push(move)
            self.last_move = move
            self.show_errors = False
            self.mismatch_since = None
            print(f"\nMove played: {san}")
            self.analyse()
            self.push_overlay()
            return

        # No match yet: the player is probably mid-move
        if any(isinstance(p, chess.Piece) and p.piece_type == chess.PAWN
               and chess.square_rank(sq) in (0, 7) for sq, p in sensed.items()):
            print("Pawn on the last rank: swap it for a Q, R, B or N piece to promote.")
        self.mismatch_since = time.monotonic()   # restart the timer on every change
        if self.show_errors or self.restoring:
            self.push_overlay()

    def tick(self):
        if (self.mismatch_since and not self.restoring and not self.show_errors
                and time.monotonic() - self.mismatch_since > ERROR_DELAY):
            self.show_errors = True
            print("Board does not match a legal move. Red squares show the differences.")
            self.push_overlay()

    # ---------- input ----------
    def on_line(self, line):
        if line.startswith("B ") and len(line) >= 66:
            self.on_board(parse_board_line(line))
        elif line:
            print(f"[board] {line}")

    def on_command(self, cmd):
        parts = cmd.split()
        if not parts:
            return
        name = parts[0].lower()
        if name == "hints":
            self.hints = not self.hints
            print("Hints", "on" if self.hints else "off")
            self.push_overlay()
        elif name == "new":
            self.new_game()
            self.push_overlay()
        elif name == "undo":
            self.undo()
        elif name == "tag" and len(parts) == 3:
            print(self.send_and_wait(f"T {parts[1].lower()} {parts[2]}"))
            self.send("S")                     # board lines were skipped while waiting
        elif name == "tagall":
            start = chess.Board()
            for sq in chess.SQUARES:
                piece = start.piece_at(sq)
                if piece:
                    sqn = chess.square_name(sq)
                    print(sqn, piece.symbol(), self.send_and_wait(f"T {sqn} {piece.symbol()}"))
            print("Done. Re-check for any ERR lines above and retry those with: tag <square> <piece>")
            self.send("S")
        elif name in ("quit", "exit"):
            raise SystemExit
        else:
            print("Commands: hints | new | undo | tag <sq> <piece> | tagall | quit")


def stdin_reader(q):
    for line in sys.stdin:
        q.put(line.strip())


def main():
    ap = argparse.ArgumentParser(description="Smart chess board host")
    ap.add_argument("--port", required=True, help="serial port, e.g. /dev/ttyUSB0 or COM5")
    ap.add_argument("--engine", default="stockfish", help="path to a UCI engine")
    ap.add_argument("--time", type=float, default=0.7, help="seconds of analysis per move")
    ap.add_argument("--lines", type=int, default=3, help="number of suggested moves to show")
    args = ap.parse_args()

    import serial  # imported here so the rest of the module can be tested without pyserial

    ser = serial.Serial(args.port, 115200, timeout=0.05)
    time.sleep(2)                                  # ESP32 resets when the port opens
    engine = chess.engine.SimpleEngine.popen_uci(args.engine)

    app = App(ser, engine, args.time, args.lines)
    cmds = queue.Queue()
    threading.Thread(target=stdin_reader, args=(cmds,), daemon=True).start()

    print("Waiting for the board... (type 'hints', 'new', 'undo', 'tagall', 'quit')")
    app.send("S")                                  # ask for the current state

    try:
        while True:
            line = app.reader.readline()
            if line:
                app.on_line(line)
            app.tick()
            while not cmds.empty():
                app.on_command(cmds.get())
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        engine.quit()
        ser.close()


if __name__ == "__main__":
    main()
