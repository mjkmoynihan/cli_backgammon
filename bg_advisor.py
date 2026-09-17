#!/usr/bin/env python3
"""
bg_advisor.py - interactive backgammon move advisor backed by GNU Backgammon.

Two ways to use it (you are asked at startup, or pass --mode):
  bot     play against gnubg (a pre-trained neural-net bot, "world class" at 2-ply).
          Dice are rolled automatically for both sides; the bot moves by itself and
          you can chose to play e.g. "me 13/9 6/5" or type "hint" for recommendations
          
  manual  track a real game: type the opponent's moves as they happen and your
          roll, and the script tells you what gnubg would play.

Requirements:
    sudo apt install gnubg          # Debian/Ubuntu/WSL
Run:
    python3 bg_advisor.py [--mode bot|manual] [--plies 2] [--gnubg /path/to/gnubg] [--verbose]

Typical session. All moves use the board's numbering as printed (your point of view):
you move 24 -> 1 and bear off from 1-6; the opponent moves 1 -> 24, enters from the bar
onto 1-6 and bears off from 19-24 (e.g. opp 12/17, opp bar/3, opp 22/off).

    > opp 1/5 12/17         opponent moved; now it's your turn
    > 31                    you rolled 3-1  -> recommendations printed
    > play                  apply the top recommendation (or: play 2, or: me 8/5 6/5)
    > opp 12/18*            opponent hit you (the * is optional, hits are detected)
    > 64                    ...
    > board                 show the board any time
    > help                  full command list

Hits are applied automatically when a checker lands on a lone enemy checker.
"""

import argparse
import base64
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile

try:
    import readline  # noqa: F401  (line editing / history for input())
except ImportError:
    pass

ME, OPP = "me", "opp"
BAR, OFF = 25, 0
START_ID = "4HPwATDgc/ABMA"  # gnubg position ID of the opening position


class MoveError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Board model
# --------------------------------------------------------------------------- #
class Board:
    """Checker counts for each side in that side's own numbering.

    me[p] / opp[p]: p in 1..24 are points, 25 is the bar, 0 is borne off.
    My point p is the opponent's point 25-p.
    """

    def __init__(self):
        self.me = [0] * 26
        self.opp = [0] * 26
        self.width = 80
        self.margin = 20
        for arr in (self.me, self.opp):
            arr[24], arr[13], arr[8], arr[6] = 2, 5, 3, 5

    def copy(self):
        b = Board.__new__(Board)
        b.me, b.opp = self.me[:], self.opp[:]
        return b

    def sides(self, who):
        return (self.me, self.opp) if who == ME else (self.opp, self.me)

    def apply(self, who, text):
        """Apply a move in `who`'s own notation, e.g. '24/20 13/8', 'bar/22', '6/off', '8/5(2)'."""
        mover, enemy = (arr[:] for arr in self.sides(who))  # work on copies, commit only if legal
        other = OPP if who == ME else ME

        def disp(p):  # mover's own numbering -> board numbering for messages
            return point_name(p) if (who == ME or p in (BAR, OFF)) else str(25 - p)

        hits = []
        for frm, to, starred in parse_move(text, who):
            if mover[frm] < 1:
                raise MoveError(f"{who} has no checker on {disp(frm)}")
            if frm != BAR and mover[BAR] > 0:
                raise MoveError(f"{who} has {mover[BAR]} checker(s) on the bar and must enter them first (bar/x)")
            if to != OFF:
                n_enemy = enemy[25 - to]
                if n_enemy > 1:
                    raise MoveError(f"point {disp(to)} is blocked by {n_enemy} {other} checkers")
                if starred and n_enemy != 1:
                    raise MoveError(f"'{disp(frm)}/{disp(to)}*' says hit, but there is no lone {other} checker on {disp(to)} - board out of sync?")
                if n_enemy == 1:  # hit
                    enemy[25 - to] = 0
                    enemy[BAR] += 1
                    hits.append(int(disp(to)))
            mover[frm] -= 1
            mover[to] += 1
        if who == ME:
            self.me, self.opp = mover, enemy
        else:
            self.opp, self.me = mover, enemy
        return hits  # board-numbered points where a hit happened

    # ---- gnubg position ID (me on roll) --------------------------------- #
    def position_id(self, on_roll=ME):
        bits = []
        order = (self.opp, self.me) if on_roll == ME else (self.me, self.opp)
        for arr in order:  # player NOT on roll first, player on roll second
            for p in range(1, 26):
                bits.extend([1] * arr[p])
                bits.append(0)
        if len(bits) > 80:
            raise MoveError("too many checkers on the board for a position ID")
        bits += [0] * (80 - len(bits))
        data = bytearray(10)
        for i, bit in enumerate(bits):
            if bit:
                data[i // 8] |= 1 << (i % 8)
        return base64.b64encode(bytes(data)).decode()[:14]

    @classmethod
    def from_position_id(cls, pid):
        pid = pid.strip()
        if len(pid) != 14:
            raise MoveError("a position ID is 14 characters")
        try:
            data = base64.b64decode(pid + "==")
        except Exception as e:
            raise MoveError(f"bad position ID: {e}")
        bits = [(data[i // 8] >> (i % 8)) & 1 for i in range(80)]
        b = cls.__new__(cls)
        b.me, b.opp = [0] * 26, [0] * 26
        i = 0
        for arr in (b.opp, b.me):
            for p in range(1, 26):
                while i < 80 and bits[i]:
                    arr[p] += 1
                    i += 1
                i += 1  # terminating zero
        for arr in (b.me, b.opp):
            arr[OFF] = max(0, 15 - sum(arr[1:26]))
        return b

    @staticmethod
    def pips(arr):
        """Pip count for one side (arr indexed in that side's own numbering; bar = 25)."""
        return sum(p * arr[p] for p in range(1, 26))

    # ---- rendering ------------------------------------------------------- #
    def render(self):
        """ASCII board from my point of view. X = me, O = opponent.
        I move 24 -> 1 (clockwise: top row right-to-left, then bottom row left-to-right)."""
        # cell(p) -> (symbol, count) for my point p
        def cell(p):
            if self.me[p]:
                return "X", self.me[p]
            if self.opp[25 - p]:
                return "O", self.opp[25 - p]
            return ".", 0

        top = list(range(13, 25))      # my 13..24, left to right
        bottom = list(range(12, 0, -1))  # my 12..1, left to right
        rows = 5

        def col_lines(points, from_top):
            lines = []
            for r in range(rows):
                cells = []
                for p in points:
                    sym, n = cell(p)
                    level = r if from_top else rows - 1 - r
                    if n > level:
                        cells.append(str(n) if (level == rows - 1 and n > rows) else sym)
                    else:
                        cells.append(" " if level > 0 else ".")
                    # (level==0 with no checker shows a dot as the point marker)
                lines.append(cells)
            return lines

        def fmt(cells):
            return " " + "  ".join(cells[:6]) + " |  " + "  ".join(cells[6:]) + " "

        out = []
        out.append("="*self.width)
        out.append(" "*self.margin +" "+" ".join(f"{p:2d}" for p in top[:6]) + " | " + " ".join(f"{p:2d}" for p in top[6:]))
        out.append(" "*self.margin+"+" + "-" * 17 + "+" + "-" * 19 + "+")
        for cells in col_lines(top, from_top=True):
            out.append(" "*self.margin+"|" + fmt(cells)[1:-1] + " |")
        out.append(" "*self.margin+"|" + " " * 17 + "|" + " " * 19 + "|")
        for cells in col_lines(bottom, from_top=False):
            out.append(" "*self.margin+"|" + fmt(cells)[1:-1] + " |")
        out.append(" "*self.margin+"+" + "-" * 17 + "+" + "-" * 19 + "+")
        out.append(" "*self.margin +" "+" ".join(f"{p:2d}" for p in bottom[:6]) + " | " + " ".join(f"{p:2d}" for p in bottom[6:]))
        out.append(f"  X = me  (bar {self.me[BAR]}, off {self.me[OFF]}, count {self.pips(self.me)})     "
                   f"O = opp  (bar {self.opp[BAR]}, off {self.opp[OFF]}, count {self.pips(self.opp)})")
        out.append("="*self.width)
        return "\n".join(out)


def mirror_move_text(text):
    """Convert a move written in the opponent's own numbering to board numbering (25 - p)."""
    out = []
    for tok in text.split():
        base, paren, rep = tok.partition("(")
        base = re.sub(r"\d+", lambda m: str(25 - int(m.group())), base)
        out.append(base + paren + rep)
    return " ".join(out)


def point_name(p):
    return "bar" if p == BAR else "off" if p == OFF else str(p)


_TOKEN = re.compile(r"^(bar|\d{1,2})((?:/(?:\d{1,2}|off)\*?)+)(?:\((\d+)\))?$", re.I)


def parse_move(text, who=ME):
    """'24/20 13/8' -> [(24,20,False),(13,8,False)]; third field = marked as a hit with '*'.
    Handles bar/off, chains (24/18/13) and repeats (8/5(2)).

    All moves are written in the board's numbering (mine). I move 24 -> 1; the opponent
    moves 1 -> 24 (e.g. 'opp 12/17', 'opp bar/3', 'opp 22/off'). Opponent points are
    converted to the opponent's own numbering (25 - p) for the internal board arrays."""
    steps = []
    for raw in re.split(r"[\s,]+", text.strip()):
        if not raw:
            continue
        tok = raw.lower()
        # allow 24-20 style too (but not inside "(2)")
        tok = re.sub(r"(?<=[\dr])-(?=(\d|off))", "/", tok)
        m = _TOKEN.match(tok)
        if not m:
            raise MoveError(f"can't parse move '{raw}' (expected e.g. 24/20, bar/22, 6/off, 8/5(2))")
        def conv(n):  # board number -> mover's own numbering
            return n if (who == ME or n in (BAR, OFF)) else 25 - n

        frm = BAR if m.group(1) == "bar" else int(m.group(1))
        dests = m.group(2).strip("/").split("/")
        count = int(m.group(3) or 1)
        chain = []
        cur = frm
        for d in dests:
            starred = d.endswith("*")
            d = d.rstrip("*")
            to = OFF if d == "off" else int(d)
            if cur != BAR and not 1 <= cur <= 24 or to != OFF and not 1 <= to <= 24:
                raise MoveError(f"point out of range in '{raw}'")
            if to != OFF and conv(to) >= (conv(cur) if cur != BAR else 25):
                direction = "24 -> 1" if who == ME else "1 -> 24"
                raise MoveError(f"'{raw}' moves the wrong way: {who} moves {direction} on this board")
            chain.append((conv(cur), conv(to), starred))
            cur = to
        steps.extend(chain)
        for _ in range(count - 1):  # a repeated move can only hit the first time, e.g. 8/5*(2)
            steps.extend((c, t, False) for c, t, _ in chain)
    if not steps:
        raise MoveError("empty move")
    return steps


# --------------------------------------------------------------------------- #
# gnubg driver
# --------------------------------------------------------------------------- #
class Gnubg:
    def __init__(self, exe, plies=2, verbose=False):
        self.exe = exe
        self.plies = plies
        self.verbose = verbose

    def _setup(self):
        return [
            "set player 0 human",
            "set player 1 human",
            "set automatic roll off",
            "set automatic game off",
            "set automatic move off",
            "set display off",
            "set confirm new off",
            f"set evaluation chequerplay evaluation plies {self.plies}",
            f"set evaluation cubedecision evaluation plies {self.plies}",
            "new game",
            "set turn 1",
        ]

    def run(self, commands):
        with tempfile.NamedTemporaryFile("w", suffix=".gnubg", delete=False) as f:
            f.write("\n".join(commands) + "\n")
            path = f.name
        try:
            proc = subprocess.run(
                [self.exe, "-t", "-q", "-c", path],
                capture_output=True, text=True, timeout=300,
                stdin=subprocess.DEVNULL,
            )
        finally:
            os.unlink(path)
        out = proc.stdout + ("\n" + proc.stderr if proc.stderr.strip() else "")
        if self.verbose:
            print("---- gnubg ----")
            print(out.rstrip())
            print("---------------")
        return out

    def move_hint(self, board, dice, who=ME):
        cmds = self._setup() + [
            f"set board {board.position_id(who)}",
            f"set dice {dice[0]} {dice[1]}",
            "hint",
        ]
        return parse_hint(self.run(cmds))

    def cube_hint(self, board):
        cmds = self._setup() + [f"set board {board.position_id()}", "hint"]
        return self.run(cmds)


def parse_hint(output):
    """Return (moves, raw). moves = [(rank, move_text, equity, diff_or_None, ply_label)]."""
    moves = []
    for line in output.splitlines():
        if "Eq.:" not in line and "MWC:" not in line:
            continue
        m = re.match(r"^\s*(\d+)\.\s+(.*?)\s+(?:Eq\.|MWC):\s*([+-]?\d+\.\d+)%?(?:\s*\(\s*([+-]?\d+\.\d+)%?\s*\))?", line)
        if not m:
            continue
        rank, left, eq, diff = m.groups()
        toks = left.split()
        # drop leading annotations like "Cubeful 2-ply" until the first move token
        ply = ""
        while toks and not re.match(r"^(bar|\d{1,2})/", toks[0], re.I):
            if re.fullmatch(r"\d-ply", toks[0]):
                ply = toks[0]
            toks.pop(0)
        if not toks:
            continue
        moves.append((int(rank), " ".join(toks), float(eq), float(diff) if diff else None, ply))
    return moves, output


# --------------------------------------------------------------------------- #
# Interactive loop
# --------------------------------------------------------------------------- #
HELP = """\
Commands. All moves use the board numbers as printed: you move 24 -> 1,
the opponent moves 1 -> 24 (enters at bar/1..6, bears off from 19-24).
  start <mine> <theirs>   opening roll, one die each, e.g.  start 5 3   (automatic in bot mode)
                    higher die goes first and plays both numbers; ties -> roll again
  opp <move>        opponent moved, e.g.  opp 1/5 12/17   opp bar/3*   opp 22/off
  opp pass          opponent had no legal move (danced)
  <dice>            your roll, e.g.  31  or  6 6  or  roll 4-2   -> prints recommendations
  roll              (bot mode) dice are automatic; 'roll' only forces a roll after 'undo' or 'mode'
  mode bot|manual   switch between playing the bot and tracking a real game
  play [n]          apply recommendation n (default 1) as your move
  me <move>         apply your own move instead, e.g.  me 8/5 6/5
  me pass           you had no legal move
  hint              re-run recommendations for the current roll
  cube              ask gnubg about the cube (double / take) in this position
  board             show the board          undo    revert the last board change
  id                print gnubg position ID (you on roll)
  setid <ID>        load a position from a gnubg position ID (you on roll)
  turn me|opp       fix whose turn it is    new     reset to the opening position
  raw               toggle printing gnubg's raw output
  help / quit
"""


def parse_dice(text):
    t = text.strip().lower()
    if t.startswith("roll"):
        t = t[4:]
    elif t.startswith("r ") or t == "r":
        t = t[1:]
    nums = re.findall(r"[1-6]", t)
    if len(nums) == 2 and re.fullmatch(r"[\s\-,/]*[1-6][\s\-,/]*[1-6][\s\-,/]*", t):
        return int(nums[0]), int(nums[1])
    return None


class Advisor:
    def __init__(self, engine, mode="manual"):
        self.engine = engine
        self.mode = mode  # "bot": gnubg plays the opponent; "manual": you type the opponent's moves
        self.board = Board()
        self.turn = ME
        self.dice = None
        self.last_moves = []
        self.history = []
        self.opening = True  # waiting for the opening roll

    def snapshot(self):
        self.history.append((self.board.copy(), self.turn, self.dice))
        del self.history[:-50]

    def prompt(self):
        if self.opening:
            return "[opening roll: 'start <your die> <their die>'] > "
        if self.turn == ME:
            d = f"{self.dice[0]}-{self.dice[1]}" if self.dice else "to roll"
            return f"[me {d}] > "
        return "[bot to move: 'roll'] > " if self.mode == "bot" else "[opp to move] > "

    @staticmethod
    def roll_dice():
        return random.randint(1, 6), random.randint(1, 6)

    def check_game_over(self):
        for who, mover, loser in ((ME, self.board.me, self.board.opp), (OPP, self.board.opp, self.board.me)):
            if mover[OFF] == 15:
                kind = "single game"
                if loser[OFF] == 0:
                    # backgammon if the loser still has a checker on the bar or in the winner's home board
                    kind = "BACKGAMMON" if (loser[BAR] or sum(loser[19:25])) else "GAMMON"
                print(f"\n*** Game over: {'you win' if who == ME else 'the bot wins'} ({kind}). 'new' to play again. ***")
                self.dice, self.last_moves = None, []
                return True
        return False

    def bot_move(self, dice):
        """gnubg plays the opponent's roll and the move is applied to the board."""
        print(f"Bot rolls {dice[0]}-{dice[1]}, thinking...", flush=True)
        try:
            moves, raw = self.engine.move_hint(self.board, dice, OPP)
        except subprocess.TimeoutExpired:
            print("gnubg timed out")
            return
        self.snapshot()
        if not moves:
            if re.search(r"no legal moves|cannot move", raw, re.I):
                print(f"Bot has no legal move with {dice[0]}-{dice[1]}")
            else:
                print("gnubg gave no move list; raw output:")
                print(raw.rstrip())
        else:
            mv = mirror_move_text(moves[0][1])
            try:
                hits = self.board.apply(OPP, mv)
            except MoveError as e:
                print(f"BUG: could not apply gnubg's move '{moves[0][1]}' (board numbering '{mv}'): {e}")
                print("Board unchanged; use 'raw' and report this. Bot's turn is skipped.")
                hits = []
            else:
                print(f"Bot plays {dice[0]}-{dice[1]}: {mv}" + (f"  (hits you on {', '.join(map(str, hits))})" if hits else ""))
        self.turn, self.dice, self.last_moves = ME, None, []
        # print(self.board.render())
        if not self.check_game_over() and self.mode == "bot":
            self.my_auto_roll()

    def my_auto_roll(self):
        self.dice = self.roll_dice()
        print(f"You roll {self.dice[0]}-{self.dice[1]}")
        print(self.board.render())
        # self.show_hint()

    def after_my_move(self):
        """Bot mode: once I've moved, the bot rolls and plays, then my next roll is made."""
        if not self.check_game_over() and self.mode == "bot":
            self.bot_move(self.roll_dice())

    def choose_mode(self):
        while True:
            try:
                ans = input("Play against the bot, or track a real game and type the opponent's moves? [bot/manual] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                return False
            if ans in ("b", "bot"):
                self.mode = "bot"
                return True
            if ans in ("m", "manual", "man"):
                self.mode = "manual"
                return True
            print("type 'bot' or 'manual'")

    def show_hint(self):
        if not self.dice:
            print("Tell me your roll first, e.g. 31")
            return
        print(f"thinking ({self.engine.plies}-ply)...", flush=True)
        try:
            moves, raw = self.engine.move_hint(self.board, self.dice)
        except subprocess.TimeoutExpired:
            print("gnubg timed out")
            return
        self.last_moves = moves
        if not moves:
            if re.search(r"no legal moves|cannot move", raw, re.I):
                print(f"You have no legal move with {self.dice[0]}-{self.dice[1]} - turn passes")
                self.turn, self.dice = OPP, None
                self.after_my_move()
            else:
                print("gnubg gave no move list; raw output:")
                print(raw.rstrip())
            return
        best = moves[0][2]
        print(f"Recommended for {self.dice[0]}-{self.dice[1]}:")
        for rank, mv, eq, diff, ply in moves[:8]:
            d = diff if diff is not None else eq - best
            tag = "" if rank == 1 else f"  ({d:+.3f})"
            print(f"  {rank}. {mv:<28} Eq. {eq:+.3f}{tag:<11} {ply}")
        if moves and moves[0][4] == "0-ply" and self.engine.plies > 0:
            print("  (0-ply shown: gnubg skips the deeper search when one move is clearly best)")
        print("  -> 'play' to apply #1, 'play n' for another, or 'me <move>' for your own")

    def run(self):
        print("Backgammon advisor (GNU Backgammon). Type 'help' for commands.\n")
        if self.mode is None and not self.choose_mode():
            return
        print(self.board.render())
        if self.mode == "bot":
            print("\nYou play X against gnubg (O). Dice are rolled automatically for both sides.")
            print("On your turn answer with 'play' (top recommendation), 'play n', or 'me <move>'.\n")
            self.handle("start")
        else:
            print("\nOpening roll: each side rolls one die. Enter both, yours first, e.g.  start 5 3")
            print("The higher die goes first and plays both numbers. Ties: roll again and re-enter.")
            print("(Game already under way? Just type your roll, e.g. 31, or the opponent's move, e.g. opp 1/5 12/17)\n")
        while True:
            try:
                line = input(self.prompt()).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if not line:
                continue
            try:
                if not self.handle(line):
                    return
            except MoveError as e:
                print(f"error: {e}")

    def handle(self, line):
        cmd, _, arg = line.partition(" ")
        cmd = cmd.lower()
        arg = arg.strip()

        if cmd in ("start", "open", "first"):
            nums = re.findall(r"[1-6]", arg)
            if len(nums) == 2:
                mine, theirs = int(nums[0]), int(nums[1])
            elif not nums and self.mode == "bot":
                mine, theirs = self.roll_dice()
                while mine == theirs:
                    print(f"Both rolled {mine} - rolling again")
                    mine, theirs = self.roll_dice()
                print(f"Opening roll: you {mine}, bot {theirs}")
            else:
                raise MoveError("give both opening dice, yours first, e.g.  start 5 3")
            if mine == theirs:
                print(f"Both rolled {mine} - roll again and enter the new dice")
                return True
            self.opening = False
            if mine > theirs:
                print(f"You go first with {mine}-{theirs}")
                self.turn, self.dice = ME, (mine, theirs)
            elif self.mode == "bot":
                self.bot_move((theirs, mine))
            else:
                print(f"Opponent goes first with {theirs}-{mine} - enter their move, e.g.  opp 1/5 12/17")
                self.turn, self.dice, self.last_moves = OPP, None, []
            return True

        if cmd in ("roll", "r", "dice") and not re.search(r"[1-6]", arg):
            if self.mode != "bot":
                print("enter the two dice, e.g.  31  or  roll 3 1")
                return True
            self.opening = False
            dice = self.roll_dice()
            if self.turn == OPP:
                self.bot_move(dice)
            else:
                self.dice = dice
                print(f"You roll {dice[0]}-{dice[1]}")
                print(self.board.render())
            return True

        if cmd == "mode":
            if arg.lower() in ("bot", "manual"):
                self.mode = arg.lower()
                print(f"mode: {self.mode}")
            else:
                raise MoveError("mode bot | mode manual")
            return True

        dice = parse_dice(line)
        if dice:
            self.opening = False
            if self.turn == OPP and self.mode == "bot":
                self.bot_move(dice)
                return True
            if self.turn != ME:
                print("(setting turn to me)")
                self.turn = ME
            self.dice = dice
            self.show_hint()
            return True

        if cmd in ("q", "quit", "exit"):
            return False
        if cmd in ("h", "help", "?"):
            print(HELP)
        elif cmd in ("b", "board", "show"):
            print(self.board.render())
        elif cmd in ("n", "new", "reset"):
            self.snapshot()
            self.board, self.turn, self.dice, self.last_moves = Board(), ME, None, []
            self.opening = True
            print(self.board.render())
            if self.mode == "bot":
                self.handle("start")
        elif cmd in ("o", "opp"):
            self.snapshot()
            self.opening = False
            if arg and arg.lower() not in ("pass", "none", "dance", "-"):
                hits = self.board.apply(OPP, arg)
                if hits:
                    print(f"opp hit you on {', '.join(map(str, hits))}")
            elif not arg:
                raise MoveError("give the opponent's move, or 'opp pass'")
            self.turn, self.dice, self.last_moves = ME, None, []
            print(self.board.render())
            print("Your turn - enter your roll (e.g. 31)")
        elif cmd in ("m", "me"):
            self.snapshot()
            self.opening = False
            if arg and arg.lower() not in ("pass", "none", "dance", "-"):
                hits = self.board.apply(ME, arg)
                if hits:
                    print(f"you hit on {', '.join(map(str, hits))}")
            elif not arg:
                raise MoveError("give your move, or 'me pass'")
            self.turn, self.dice, self.last_moves = OPP, None, []
            # print(self.board.render())
            self.after_my_move()
        elif cmd in ("p", "play", "ok"):
            if not self.last_moves:
                raise MoveError("no recommendation to play yet - enter your roll first")
            n = int(arg) if arg.isdigit() else 1
            match = [m for m in self.last_moves if m[0] == n]
            if not match:
                raise MoveError(f"no recommendation #{n}")
            mv = match[0][1]
            self.snapshot()
            hits = self.board.apply(ME, mv)
            print(f"played: {mv}" + (f"  (hit on {', '.join(map(str, hits))})" if hits else ""))
            self.turn, self.dice, self.last_moves = OPP, None, []
            # print(self.board.render())
            self.after_my_move()
        elif cmd == "hint":
            self.show_hint()
        elif cmd == "cube":
            print("thinking...", flush=True)
            out = self.engine.cube_hint(self.board)
            lines = [l for l in out.splitlines() if l.strip()]
            # show from the "Cube analysis" section onward if we can find it
            starts = [i for i, l in enumerate(lines) if l.lower().startswith("cube analysis")]
            if starts:
                lines = lines[starts[-1]:]
            print("\n".join(lines[-25:]))
        elif cmd in ("id", "posid"):
            print(self.board.position_id())
        elif cmd == "setid":
            self.snapshot()
            self.board = Board.from_position_id(arg)
            self.turn, self.dice, self.last_moves = ME, None, []
            print(self.board.render())
        elif cmd == "turn":
            if arg.lower() in ("me", "opp"):
                self.turn, self.dice, self.last_moves = arg.lower(), None, []
            else:
                raise MoveError("turn me | turn opp")
        elif cmd == "undo":
            if not self.history:
                raise MoveError("nothing to undo")
            self.board, self.turn, self.dice = self.history.pop()
            self.last_moves = []
            print(self.board.render())
        elif cmd == "raw":
            self.engine.verbose = not self.engine.verbose
            print(f"raw gnubg output {'on' if self.engine.verbose else 'off'}")
        else:
            print(f"unknown command '{cmd}' - type 'help'")
        return True


def selftest():
    b = Board()
    assert b.position_id() == START_ID, b.position_id()
    assert Board.from_position_id(START_ID).me == b.me
    b.apply(OPP, "1/5 12/17")  # board numbers: opp's 24/20 13/8 in their own terms
    assert b.opp[20] == 1 and b.opp[24] == 1 and b.opp[8] == 4 and b.opp[13] == 4
    assert b.apply(ME, "13/5*") == [5]  # lone opp checker on board point 5: hit
    assert b.opp[BAR] == 1 and b.opp[20] == 0 and b.me[5] == 1
    assert b.apply(OPP, "bar/5*") == [5] and b.me[BAR] == 1 and b.opp[20] == 1
    b3 = Board(); b3.opp[6] = 0; b3.opp[1] = 5  # opp has 5 on their 1-point == board 24
    b3.apply(OPP, "24/off"); assert b3.opp[OFF] == 1
    for who, bad in ((OPP, "17/12"), (ME, "12/17")):
        try:
            parse_move(bad, who)
        except MoveError:
            pass
        else:
            raise AssertionError(bad)
    b2 = Board.from_position_id(b.position_id())
    assert b2.me == b.me and b2.opp == b.opp
    assert parse_move("8/5(2)") == [(8, 5, False), (8, 5, False)]
    assert parse_move("24/18/13") == [(24, 18, False), (18, 13, False)]
    assert parse_move("bar/22* 6/off") == [(BAR, 22, True), (6, OFF, False)]
    bb = b.copy(); bb.opp[BAR] += 1
    for bad in ("1/2", "bar/3*"):  # bar checker must enter first; starred non-hit
        try:
            bb.copy().apply(OPP, bad)
        except MoveError:
            pass
        else:
            raise AssertionError(bad)
    d = Board(); d.me[21] = 1; d.me[24] = 1; d.opp[7] = 2  # opp has two on board point 18
    assert d.apply(OPP, "18/21*(2)") == [21] and d.opp[4] == 2 and d.me[BAR] == 1
    before = (d.me[:], d.opp[:])
    try:
        d.apply(ME, "24/23 6/1")  # second step blocked -> whole move rejected, board untouched
    except MoveError:
        pass
    assert (d.me, d.opp) == before
    assert mirror_move_text("24/20 13/8") == "1/5 12/17"
    assert mirror_move_text("bar/22* 8/5(2) 6/off") == "bar/3* 17/20(2) 19/off"
    sw = Board(); sw.apply(OPP, "1/5 12/17")
    # opp on roll: the ID must equal the mirrored position with me on roll
    mirrored = Board(); mirrored.apply(ME, "24/20 13/8")
    assert sw.position_id(OPP) == mirrored.position_id(ME)
    assert parse_dice("31") == (3, 1) and parse_dice("roll 6 6") == (6, 6) and parse_dice("4-2") == (4, 2)
    assert parse_dice("me 6/3") is None and parse_dice("24/20 13/8") is None
    sample = ("    1. Cubeful 2-ply    24/20 13/8                  Eq.:  +0.023\n"
              "       0.520 0.146 0.007 - 0.480 0.129 0.004\n"
              "    2. Cubeful 2-ply    13/9 13/8                   Eq.:  -0.011 ( -0.034)\n"
              "    3. Cubeful 0-ply    bar/22* 6/off              Eq.:  -0.100 ( -0.123)\n")
    mv, _ = parse_hint(sample)
    assert mv == [(1, "24/20 13/8", 0.023, None, "2-ply"), (2, "13/9 13/8", -0.011, -0.034, "2-ply"),
                  (3, "bar/22* 6/off", -0.1, -0.123, "0-ply")], mv
    print("selftest ok")
    print(b.render())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gnubg", default=None, help="path to the gnubg executable (default: search PATH)")
    ap.add_argument("--plies", type=int, default=2, help="evaluation depth, 0-3 (default 2 = world class)")
    ap.add_argument("--verbose", action="store_true", help="print gnubg's raw output")
    ap.add_argument("--mode", choices=["bot", "manual"], default=None,
                    help="bot: play against gnubg; manual: type the opponent's moves (default: ask)")
    ap.add_argument("--selftest", action="store_true", help="run internal checks (no gnubg needed)")
    args = ap.parse_args()

    if args.selftest:
        selftest()
        return

    exe = args.gnubg or shutil.which("gnubg")
    if not exe or not os.path.exists(exe):
        sys.exit("gnubg not found. Install it with:  sudo apt install gnubg   (or pass --gnubg PATH)")

    Advisor(Gnubg(exe, args.plies, args.verbose), args.mode).run()


if __name__ == "__main__":
    main()
