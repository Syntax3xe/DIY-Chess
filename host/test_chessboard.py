"""Hardware-free tests for chessboard.py. Run with: python -m unittest test_chessboard"""
import unittest

import chess

from chessboard import App, LineReader, UNKNOWN, parse_board_line


class FakeSerial:
    def __init__(self, incoming=b""):
        self.incoming = bytearray(incoming)
        self.sent = []

    @property
    def in_waiting(self):
        return len(self.incoming)

    def read(self, n):
        n = min(n, 3)  # deliver in small pieces to exercise line buffering
        out, self.incoming = bytes(self.incoming[:n]), self.incoming[n:]
        return out

    def write(self, data):
        self.sent.append(data.decode().strip())


class FakeEngine:
    def analyse(self, board, limit, multipv=1):
        moves = list(board.legal_moves)[:multipv]
        return [{"pv": [m], "score": chess.engine.PovScore(chess.engine.Cp(0), chess.WHITE)}
                for m in moves]


def line_for(board):
    chars = ["."] * 64
    for sq, p in board.piece_map().items():
        chars[sq] = p.symbol()
    return "B " + "".join(chars)


class AppTest(unittest.TestCase):
    def setUp(self):
        import io, contextlib
        self._quiet = contextlib.redirect_stdout(io.StringIO())
        self._quiet.__enter__()
        self.ser = FakeSerial()
        self.app = App(self.ser, FakeEngine(), 0.1, 3)

    def tearDown(self):
        self._quiet.__exit__(None, None, None)

    def feed(self, board):
        self.app.on_line(line_for(board))

    def play(self, *sans):
        """Play moves on a 'physical' board and feed each resulting position."""
        phys = self.app.board.copy()
        for san in sans:
            phys.push_san(san)
            self.feed(phys)
        return phys

    def test_start_then_moves(self):
        self.feed(chess.Board())
        self.assertFalse(self.app.restoring)
        self.play("e4", "e5", "Nf3")
        self.assertEqual(len(self.app.board.move_stack), 3)
        self.assertTrue(self.ser.sent[-1].startswith("H "))

    def test_castling_en_passant_promotion(self):
        self.feed(chess.Board())
        self.play("e4", "Nf6", "Nf3", "Nc6", "Be2", "a6", "O-O")
        self.assertEqual(self.app.board.king(chess.WHITE), chess.G1)
        self.play("a5", "e5", "d5", "exd6")         # en passant
        self.assertEqual(self.app.board.peek().uci(), "e5d6")
        self.assertIsNone(self.app.board.piece_at(chess.D5))
        self.play("Ra6", "dxc7", "Ra8", "cxd8=N")   # capture-underpromotion to knight
        self.assertEqual(self.app.board.piece_at(chess.D8), chess.Piece.from_symbol("N"))

    def test_intermediate_states_ignored(self):
        self.feed(chess.Board())
        b = chess.Board()
        b.remove_piece_at(chess.E2)                 # pawn lifted
        self.app.on_line(line_for(b))
        self.assertEqual(len(self.app.board.move_stack), 0)

    def test_undo_does_not_replay(self):
        self.feed(chess.Board())
        phys = self.play("e4")
        self.app.undo()
        self.feed(phys)                             # board still shows e4
        self.assertEqual(len(self.app.board.move_stack), 0)
        self.assertTrue(self.app.restoring)
        self.assertIn("R", self.ser.sent[-1])
        self.feed(chess.Board())                    # pawn put back
        self.assertFalse(self.app.restoring)

    def test_unknown_tag_shows_red(self):
        self.app.on_line("B " + "?" + "." * 63)
        self.assertEqual(self.app.sensed[0], UNKNOWN)
        self.assertEqual(self.app.overlay()[0], "R")

    def test_reset_to_start_starts_new_game(self):
        self.feed(chess.Board())
        self.play("e4")
        self.feed(chess.Board())
        self.assertEqual(len(self.app.board.move_stack), 0)

    def test_line_reader_reassembles(self):
        r = LineReader(FakeSerial(b"READY\nB " + b"." * 64 + b"\n"))
        self.assertEqual(r.readline(), "READY")
        self.assertEqual(len(r.readline()), 66)
        self.assertIsNone(r.readline())

    def test_parse(self):
        s = parse_board_line(line_for(chess.Board()))
        self.assertEqual(s, chess.Board().piece_map())


if __name__ == "__main__":
    unittest.main()
