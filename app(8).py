import os
import random
import string
import threading
import time
import uuid
from fractions import Fraction

from flask import Flask, Response, jsonify, request, session

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "b9f8d2a7c4e1g6h3j0k9l8m2n5p7q1r4")

# ---------------- CONFIG ----------------
TILES = [
    ("1/2", "3/6"), ("12/24", "4/10"), ("6/21", "4/2"), ("10/5", "6/9"),
    ("1/2", "6/12"), ("2/3", "7/7"), ("5/5", "11/33"), ("4/12", "1/4"),
    ("5/20", "1/3"), ("5/15", "2/5"), ("8/20", "4/11"), ("8/22", "3/3"),
    ("12/12", "9/12"), ("30/40", "3/5"), ("15/25", "1/7"), ("2/14", "50/100"),
    ("9/18", "3/21"), ("2/14", "6/3"), ("20/10", "4/6"), ("10/15", "12/12"),
    ("9/9", "3/9"), ("10/30", "2/10"), ("5/25", "1/7"), ("11/77", "4/6"),
    ("22/33", "2/8"), ("7/28", "4/8"), ("7/14", "14/14"), ("21/21", "7/21"),
    ("6/18", "6/8"), ("3/4", "2/4"), ("15/30", "20/30"), ("14/21", "1/5"),
    ("20/100", "40/100"), ("4/10", "3/9"), ("9/27", "14/7"), ("18/9", "15/30"),
    ("35/70", "2/4"),
]

WIN_SCORE = 10
TIMER_OPTIONS = (20, 30, 40)
MAX_PLAYERS = 15
ROOM_TTL = 6 * 3600
COLORS = ["#2A7FFF", "#FF6A2A", "#22A559", "#B34BE0", "#E0B400", "#E0446B", "#00A5B5", "#7A7A7A",
          "#8B4513", "#6B8E23", "#FF1493", "#4B0082", "#20B2AA", "#DC143C", "#556B2F"]

rooms = {}
lock = threading.RLock()


# ---------------- LOGICA ----------------
def tile_dict(t):
    return {"left": t[0], "right": t[1]}


def make_code():
    while True:
        code = "".join(random.choice("ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(4))
        if code not in rooms:
            return code


def cleanup_rooms():
    now = time.time()
    for code in [c for c, r in rooms.items() if now - r["created"] > ROOM_TTL]:
        del rooms[code]


def add_log(room, text):
    room["log"].append(text)
    del room["log"][:-6]


def new_shelf():
    """Ogni giocatore ha le stesse tessere, in ordine diverso."""
    tiles = [dict(tile_dict(t), used=False) for t in TILES]
    random.shuffle(tiles)
    return tiles


def start_round(room):
    t = random.choice(TILES)
    room["board"] = [{"id": 1, "x": 0, "y": 0, "o": "h", "a": t[0], "b": t[1]}]
    room["next_id"] = 2
    room["last_id"] = 1
    room["game"] = room.get("game", 0) + 1
    room["version"] = 1
    room["winner"] = None
    room["log"] = []
    for p in room["players"].values():
        p["score"] = 0
        p["shelf"] = new_shelf()
    room["status"] = "playing"
    room["deadline"] = time.time() + room["timer"]
    add_log(room, "La partita è iniziata!")


DIRS = {"right": (1, 0), "left": (-1, 0), "down": (0, 1), "up": (0, -1)}


def tile_cells(t):
    """Le due celle (x, y, valore) occupate da una tessera sul campo."""
    if t["o"] == "h":
        return [(t["x"], t["y"], t["a"]), (t["x"] + 1, t["y"], t["b"])]
    return [(t["x"], t["y"], t["a"]), (t["x"], t["y"] + 1, t["b"])]


def occupancy(board):
    return {(x, y): v for t in board for x, y, v in tile_cells(t)}


def make_board_tile(room, cx, cy, d, first, second):
    """Tessera attaccata alla cella (cx, cy) nella direzione d.
    `first` va nella cella adiacente (deve essere equivalente alla cella),
    `second` in quella più lontana."""
    dx, dy = DIRS[d]
    n1 = (cx + dx, cy + dy)
    n2 = (cx + 2 * dx, cy + 2 * dy)
    if d == "right":
        t = {"x": n1[0], "y": n1[1], "o": "h", "a": first, "b": second}
    elif d == "left":
        t = {"x": n2[0], "y": n2[1], "o": "h", "a": second, "b": first}
    elif d == "down":
        t = {"x": n1[0], "y": n1[1], "o": "v", "a": first, "b": second}
    else:
        t = {"x": n2[0], "y": n2[1], "o": "v", "a": second, "b": first}
    t["id"] = room["next_id"]
    room["next_id"] += 1
    room["last_id"] = t["id"]
    return t


def add_jolly(room):
    """Aggiunge sul campo una tessera che combacia, in un punto libero a caso."""
    occ = occupancy(room["board"])
    slots = []
    for (x, y), v in occ.items():
        for d, (dx, dy) in DIRS.items():
            if (x + dx, y + dy) not in occ and (x + 2 * dx, y + 2 * dy) not in occ:
                slots.append((x, y, d, v))
    random.shuffle(slots)
    tiles = TILES[:]
    random.shuffle(tiles)
    for x, y, d, v in slots:
        for t in tiles:
            if Fraction(t[0]) == Fraction(v):
                first, second = t[0], t[1]
            elif Fraction(t[1]) == Fraction(v):
                first, second = t[1], t[0]
            else:
                continue
            nt = make_board_tile(room, x, y, d, first, second)
            nt["jolly"] = True
            room["board"].append(nt)
            return


def check_timeout(room, now):
    """Tempo scaduto: nessuno ha piazzato -> arriva una tessera nuova sul campo."""
    if room["status"] != "playing" or now < room["deadline"]:
        return
    add_jolly(room)
    room["version"] += 1
    room["deadline"] = now + room["timer"]
    add_log(room, "Tempo scaduto! Nuova tessera sul campo.")


def current():
    code = session.get("room")
    pid = session.get("pid")
    room = rooms.get(code)
    if not room or pid not in room["players"]:
        return None, None
    return room, room["players"][pid]


def clean_name(raw):
    return " ".join(str(raw or "").split())[:20]


def add_player(room, pid, name):
    idx = len(room["players"])
    room["players"][pid] = {
        "name": name, "score": 0, "shelf": [], "color": COLORS[idx % len(COLORS)],
    }


def name_taken(room, name, pid):
    return any(p["name"].lower() == name.lower() for k, p in room["players"].items() if k != pid)


def fail(msg, code=200):
    return jsonify({"result": "fail", "message": msg}), code


# ---------------- ROUTE ----------------
@app.route("/")
def index():
    return Response(HTML_PAGE, mimetype="text/html")


@app.route("/api/create", methods=["POST"])
def create():
    data = request.get_json(silent=True) or {}
    name = clean_name(data.get("name"))
    if not name:
        return fail("Inserisci il tuo nome.")
    timer = data.get("timer")
    if timer not in TIMER_OPTIONS:
        timer = 30
    with lock:
        cleanup_rooms()
        code = make_code()
        pid = str(uuid.uuid4())
        rooms[code] = {
            "code": code, "created": time.time(), "host": pid, "status": "lobby",
            "timer": timer, "players": {}, "board": [], "version": 0,
            "deadline": 0, "winner": None, "log": [],
        }
        add_player(rooms[code], pid, name)
        session["room"], session["pid"] = code, pid
    return jsonify({"result": "success", "code": code})


@app.route("/api/join", methods=["POST"])
def join():
    data = request.get_json(silent=True) or {}
    name = clean_name(data.get("name"))
    code = str(data.get("code") or "").strip().upper()
    if not name:
        return fail("Inserisci il tuo nome.")
    with lock:
        room = rooms.get(code)
        if not room:
            return fail("Stanza non trovata. Controlla il codice.")
        pid = session.get("pid")
        if session.get("room") == code and pid in room["players"]:
            room["players"][pid]["name"] = name
            return jsonify({"result": "success", "code": code})
        if room["status"] != "lobby":
            return fail("La partita è già iniziata.")
        if len(room["players"]) >= MAX_PLAYERS:
            return fail("La stanza è piena.")
        pid = str(uuid.uuid4())
        if name_taken(room, name, pid):
            return fail("Questo nome è già in uso nella stanza.")
        add_player(room, pid, name)
        session["room"], session["pid"] = code, pid
    return jsonify({"result": "success", "code": code})


@app.route("/api/timer", methods=["POST"])
def set_timer():
    data = request.get_json(silent=True) or {}
    with lock:
        room, me = current()
        if not room or room["host"] != session["pid"] or room["status"] != "lobby":
            return fail("Non puoi cambiare il timer.")
        if data.get("timer") not in TIMER_OPTIONS:
            return fail("Valore non valido.")
        room["timer"] = data["timer"]
    return jsonify({"result": "success"})


@app.route("/api/start", methods=["POST"])
def start():
    with lock:
        room, me = current()
        if not room or room["host"] != session["pid"]:
            return fail("Solo chi ha creato la stanza può avviare.")
        if room["status"] != "lobby":
            return fail("Partita già avviata.")
        start_round(room)
    return jsonify({"result": "success"})


@app.route("/api/restart", methods=["POST"])
def restart():
    with lock:
        room, me = current()
        if not room or room["host"] != session["pid"]:
            return fail("Solo chi ha creato la stanza può riavviare.")
        room["status"] = "lobby"
        room["winner"] = None
        room["board"] = []
        room["log"] = []
        for p in room["players"].values():
            p["score"] = 0
            p["shelf"] = []
    return jsonify({"result": "success"})


@app.route("/api/leave", methods=["POST"])
def leave():
    with lock:
        room, me = current()
        if room:
            pid = session["pid"]
            del room["players"][pid]
            if not room["players"]:
                rooms.pop(room["code"], None)
            elif room["host"] == pid:
                room["host"] = next(iter(room["players"]))
        session.pop("room", None)
        session.pop("pid", None)
    return jsonify({"result": "success"})


@app.route("/api/state")
def state():
    with lock:
        room, me = current()
        if not room:
            return jsonify({"room": None})
        now = time.time()
        check_timeout(room, now)
        pid = session["pid"]
        players = [
            {
                "name": p["name"], "score": p["score"], "color": p["color"],
                "you": k == pid, "host": k == room["host"],
            }
            for k, p in room["players"].items()
        ]
        winner = room.get("winner_name") if room["winner"] else None
        return jsonify({
            "room": room["code"],
            "status": room["status"],
            "timer": room["timer"],
            "remaining": max(0, room["deadline"] - now) if room["status"] == "playing" else room["timer"],
            "is_host": room["host"] == pid,
            "players": players,
            "board": room["board"],
            "last_id": room.get("last_id"),
            "game": room.get("game", 0),
            "version": room["version"],
            "shelf": [dict(t, idx=i) for i, t in enumerate(me["shelf"]) if not t["used"]],
            "winner": winner,
            "winner_is_you": room["winner"] == pid,
            "win_score": WIN_SCORE,
            "log": room["log"],
        })


@app.route("/api/place", methods=["POST"])
def place():
    data = request.get_json(silent=True) or {}
    idx, version = data.get("idx"), data.get("version")
    cx, cy, d = data.get("x"), data.get("y"), data.get("dir")
    side = data.get("side")  # metà della tessera che deve combaciare col campo
    with lock:
        room, me = current()
        if not room:
            return fail("Non sei in nessuna stanza.")
        now = time.time()
        check_timeout(room, now)
        if room["status"] != "playing":
            return fail("La partita non è in corso.")
        if d not in DIRS or side not in ("left", "right") or not all(isinstance(v, int) for v in (idx, cx, cy)) or not 0 <= idx < len(me["shelf"]):
            return fail("Mossa non valida.")
        # Gara: se qualcuno ha già piazzato, l'estremità scelta non c'è più
        if version != room["version"]:
            return fail("Troppo tardi! Qualcuno ha già piazzato una tessera.")
        tile = me["shelf"][idx]
        if tile["used"]:
            return fail("Tessera già usata!")

        board = room["board"]
        occ = occupancy(board)
        if (cx, cy) not in occ:
            return fail("Posizione non valida.")
        dx, dy = DIRS[d]
        if (cx + dx, cy + dy) in occ or (cx + 2 * dx, cy + 2 * dy) in occ:
            return fail("Non c'è spazio libero da quella parte.")
        target = Fraction(occ[(cx, cy)])
        other = "right" if side == "left" else "left"
        first, second = tile[side], tile[other]
        if Fraction(first) != target:
            return fail("Le frazioni non sono equivalenti!")
        board.append(make_board_tile(room, cx, cy, d, first, second))

        tile["used"] = True
        me["score"] += 1
        room["version"] += 1
        room["deadline"] = now + room["timer"]
        add_log(room, f"{me['name']} ha piazzato una tessera ({me['score']}/{WIN_SCORE})")
        if me["score"] >= WIN_SCORE:
            room["status"] = "finished"
            room["winner"] = session["pid"]
            room["winner_name"] = me["name"]
            add_log(room, f"{me['name']} ha vinto la partita!")
    return jsonify({"result": "success", "message": "Accoppiamento corretto!"})


# ---------------- HTML ----------------
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Domino Frazioni</title>
<style>
*{box-sizing:border-box}
body{font-family:Arial,sans-serif;margin:0;padding:0;background:#f5f7fb;color:#222;text-align:center}
h1{margin:10px 0}
.view{display:none;max-width:1100px;margin:0 auto;padding:12px}
.card{background:#fff;border:2px solid #ddd;border-radius:12px;padding:16px;margin:12px auto}
input,select{padding:10px;font-size:16px;border:2px solid #bbb;border-radius:8px}
button{padding:10px 18px;font-size:15px;border:none;border-radius:8px;background:#2A7FFF;color:#fff;cursor:pointer;font-weight:bold}
button.sec{background:#777}
button:disabled{opacity:.5;cursor:default}
.code{font-size:44px;letter-spacing:8px;font-weight:bold;color:#2A7FFF}
.err{color:#d11;font-weight:bold;min-height:24px}
.players{display:flex;flex-wrap:wrap;gap:10px;justify-content:center;margin:10px 0}
.pcard{border:3px solid;border-radius:10px;padding:8px 12px;min-width:140px;background:#fff}
.pcard.you{box-shadow:0 0 8px rgba(0,0,0,.35)}
.bar{height:8px;background:#e3e3e3;border-radius:4px;margin-top:6px;overflow:hidden}
.bar>div{height:100%}
#timer{font-size:26px;font-weight:bold}
#timer.low{color:#d11}
#msg{min-height:26px;font-weight:bold;color:#d11}
#msg.ok{color:#1a8f3c}
#field{border:3px solid #333;border-radius:12px;background:#fff;padding:10px;margin:8px 0;overflow:auto;max-height:62vh}
#board{display:grid;grid-template-columns:repeat(var(--cols,3),64px);grid-auto-rows:64px;width:max-content;margin:0 auto}
.btile{display:flex;margin:2px;border:2px solid #000;border-radius:6px;background:#fff}
.btile.v{flex-direction:column}
.btile.jolly{border-style:dashed;background:#fff8dc}
.btile.last{box-shadow:0 0 8px #22A559}
.half{position:relative;flex:1;display:flex;align-items:center;justify-content:center;font-size:12px;user-select:none}
.btile.h .half:first-child{border-right:1px solid #000}
.btile.v .half:first-child{border-bottom:1px solid #000}
.mk{position:absolute;border:none;padding:0;margin:0;background:rgba(42,127,255,.18);color:#2A7FFF;font-size:9px;line-height:1;cursor:pointer;border-radius:4px;display:flex;align-items:center;justify-content:center;opacity:.6;font-weight:normal}
.mk:hover{opacity:1;background:rgba(42,127,255,.45)}
.mk.on{opacity:1;background:#22A559;color:#fff}
.mk.up,.mk.down{left:50%;transform:translateX(-50%);width:28px;height:11px}
.mk.up{top:0}.mk.down{bottom:0}
.mk.left,.mk.right{top:50%;transform:translateY(-50%);width:9px;height:24px}
.mk.left{left:0}.mk.right{right:0}
.row{display:flex;flex-wrap:wrap;justify-content:center;align-items:center}
.tile{display:flex;margin:5px;border:2px solid #000;border-radius:5px;background:#fff}
.tile.jolly{border-style:dashed;background:#fff8dc}
.side{padding:10px 6px;width:56px;text-align:center;border-right:1px solid #000;user-select:none}
.side:last-child{border-right:none}
.end{cursor:pointer;background:#eef4ff}
.end:hover{background:#cfe0ff}
.side.sel{background:rgba(42,127,255,.35);outline:3px solid #22A559;outline-offset:-3px}
#shelf{border:3px solid #2A7FFF;border-radius:12px;background:#fff;padding:10px}
#shelf .tile{border-color:#2A7FFF}
#shelf .side{cursor:pointer}
#shelf .side:hover{background:#eef4ff}
#shelf .side.sel{background:rgba(42,127,255,.35)}
#shelf .tile.sel{background:rgba(42,127,255,.3);box-shadow:0 0 8px #22A559;border-color:#22A559}
#log{font-size:14px;color:#555;min-height:20px}
#overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.65);z-index:10;align-items:center;justify-content:center}
#overlay .box{background:#fff;border-radius:16px;padding:30px 40px;max-width:90%}
@keyframes flash{0%{color:gold;text-shadow:0 0 5px gold}50%{color:red;text-shadow:0 0 20px red}100%{color:gold;text-shadow:0 0 5px gold}}
#wintext{font-size:30px;font-weight:bold;animation:flash 1s infinite}
.hint{color:#666;font-size:14px}
</style>
</head>
<body>

<!-- ENTRA / CREA -->
<div id="v-join" class="view">
  <h1>Domino Frazioni</h1>
  <p class="hint">Abbina le frazioni equivalenti. Vince chi piazza per primo 10 tessere!</p>
  <div class="card">
    <h3>Il tuo nome</h3>
    <input id="name" maxlength="20" placeholder="Nome giocatore">
  </div>
  <div class="card">
    <h3>Crea una nuova stanza</h3>
    Tempo per turno:
    <select id="timer-new"><option>20</option><option selected>30</option><option>40</option></select> secondi<br><br>
    <button onclick="createRoom()">CREA STANZA</button>
  </div>
  <div class="card">
    <h3>Entra in una stanza</h3>
    <input id="code" maxlength="4" placeholder="CODICE" style="text-transform:uppercase;width:130px;text-align:center">
    <button onclick="joinRoom()">ENTRA</button>
  </div>
  <div id="join-err" class="err"></div>
</div>

<!-- SALA D'ATTESA -->
<div id="v-lobby" class="view">
  <h1>Sala d'attesa</h1>
  <div class="card">
    Codice stanza<br><span class="code" id="l-code"></span><br>
    <span class="hint">Link da condividere: <span id="l-link"></span></span><br><br>
    <button class="sec" onclick="copyLink()">Copia link</button>
  </div>
  <div class="card">
    <h3>Giocatori (<span id="l-count">0</span>)</h3>
    <div id="l-players" class="players"></div>
    <div id="l-host">
      Tempo per turno:
      <select id="timer-lobby" onchange="changeTimer()"><option>20</option><option>30</option><option>40</option></select> secondi<br><br>
      <button onclick="startGame()">AVVIA PARTITA</button>
    </div>
    <div id="l-wait" class="hint">In attesa che chi ha creato la stanza avvii la partita…</div>
    <div>Tempo per turno: <b id="l-timer"></b> s</div>
  </div>
  <button class="sec" onclick="leaveRoom()">Esci</button>
</div>

<!-- GIOCO -->
<div id="v-game" class="view">
  <div class="row" style="justify-content:space-between">
    <div class="hint">Stanza <b id="g-code"></b></div>
    <h1 style="margin:0">Domino Frazioni</h1>
    <button class="sec" onclick="leaveRoom()">Esci</button>
  </div>
  <div id="players" class="players"></div>
  <div id="timer">Timer: 30s</div>
  <div id="msg"></div>
  <div id="log"></div>
  <h3 style="margin:8px">Campo di gioco</h3>
  <div class="hint">Clicca la metà della tua tessera (sinistra o destra) equivalente a una frazione del campo, poi la freccetta azzurra (▲ ▼ ◀ ▶) di quella frazione: la metà scelta si attacca lì e l'altra metà resta all'esterno. Puoi attaccare in orizzontale o in verticale, anche a tessere in mezzo al campo. Chi è più veloce la piazza!</div>
  <div id="field"><div id="board"></div></div>
  <h3 style="margin:8px">La tua collezione</h3>
  <div id="shelf"><div id="shelf-tiles" class="row"></div></div>
</div>

<div id="overlay"><div class="box">
  <div id="wintext"></div>
  <div id="ranking" style="margin:14px 0;text-align:left;display:inline-block"></div><br>
  <div id="restart-wrap"><button onclick="restartGame()">NUOVA PARTITA</button></div>
  <div id="restart-wait" class="hint">In attesa che l'host avvii una nuova partita…</div>
</div></div>

<script>
let S = null, selIdx = null, selSide = null, selSlot = null, msgTimer = null;
let lastBoardKey = null, lastShelfKey = null, lastGame = null, lastVersionSeen = -1;
const DIRS = {up: [0, -1], down: [0, 1], left: [-1, 0], right: [1, 0]};
const ARROWS = {up: "▲", down: "▼", left: "◀", right: "▶"};

const $ = id => document.getElementById(id);
async function api(path, body) {
  const r = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body || {})});
  return r.json();
}
function showView(id) {
  ["v-join", "v-lobby", "v-game"].forEach(v => $(v).style.display = v === id ? "block" : "none");
}
function toast(text, ok) {
  const m = $("msg");
  m.textContent = text;
  m.className = ok ? "ok" : "";
  clearTimeout(msgTimer);
  msgTimer = setTimeout(() => { m.textContent = ""; }, 2500);
}

// ---- ingresso ----
async function createRoom() {
  const r = await api("/api/create", {name: $("name").value, timer: parseInt($("timer-new").value)});
  if (r.result !== "success") { $("join-err").textContent = r.message; return; }
  poll();
}
async function joinRoom() {
  const r = await api("/api/join", {name: $("name").value, code: $("code").value});
  if (r.result !== "success") { $("join-err").textContent = r.message; return; }
  history.replaceState(null, "", location.pathname);
  poll();
}
async function leaveRoom() { await api("/api/leave"); S = null; poll(); }
async function changeTimer() { await api("/api/timer", {timer: parseInt($("timer-lobby").value)}); }
async function startGame() { const r = await api("/api/start"); if (r.result !== "success") alert(r.message); poll(); }
async function restartGame() { await api("/api/restart"); poll(); }
function copyLink() {
  const link = location.origin + location.pathname + "?room=" + S.room;
  navigator.clipboard && navigator.clipboard.writeText(link);
  toast("Link copiato!", true);
}

// ---- disegno ----
function playerCards(container) {
  container.innerHTML = "";
  S.players.forEach(p => {
    const d = document.createElement("div");
    d.className = "pcard" + (p.you ? " you" : "");
    d.style.borderColor = p.color;
    const n = document.createElement("strong");
    n.textContent = p.name + (p.you ? " (tu)" : "") + (p.host ? " ★" : "");
    d.appendChild(n);
    if (S.status !== "lobby") {
      const s = document.createElement("div");
      s.textContent = "Tessere: " + p.score + "/" + S.win_score;
      d.appendChild(s);
      const bar = document.createElement("div"); bar.className = "bar";
      const fill = document.createElement("div");
      fill.style.width = (100 * p.score / S.win_score) + "%";
      fill.style.background = p.color;
      bar.appendChild(fill); d.appendChild(bar);
    }
    container.appendChild(d);
  });
}

function makeTile(t, cls) {
  const d = document.createElement("div");
  d.className = "tile" + (cls ? " " + cls : "") + (t.jolly ? " jolly" : "");
  ["left", "right"].forEach(side => {
    const s = document.createElement("div");
    s.className = "side";
    s.textContent = t[side];
    d.appendChild(s);
  });
  return d;
}

function cellsOf(t) {
  return t.o === "h"
    ? [[t.x, t.y, t.a], [t.x + 1, t.y, t.b]]
    : [[t.x, t.y, t.a], [t.x, t.y + 1, t.b]];
}

function renderBoard() {
  const key = S.room + "|" + S.game + "|" + S.version + "|" + JSON.stringify(selSlot);
  if (key === lastBoardKey) return;
  lastBoardKey = key;
  const b = $("board");
  const occ = {};
  let minX = 1e9, minY = 1e9, maxX = -1e9, maxY = -1e9;
  S.board.forEach(t => cellsOf(t).forEach(([x, y]) => {
    occ[x + "," + y] = 1;
    minX = Math.min(minX, x); maxX = Math.max(maxX, x);
    minY = Math.min(minY, y); maxY = Math.max(maxY, y);
  }));
  const free = (x, y) => !occ[x + "," + y];
  b.style.setProperty("--cols", maxX - minX + 3);
  b.innerHTML = "";
  S.board.forEach(t => {
    const d = document.createElement("div");
    d.className = "btile " + t.o + (t.jolly ? " jolly" : "") + (t.id === S.last_id ? " last" : "");
    d.dataset.id = t.id;
    d.style.gridColumn = (t.x - minX + 2) + " / span " + (t.o === "h" ? 2 : 1);
    d.style.gridRow = (t.y - minY + 2) + " / span " + (t.o === "h" ? 1 : 2);
    cellsOf(t).forEach(([x, y, val]) => {
      const h = document.createElement("div");
      h.className = "half";
      h.appendChild(document.createTextNode(val));
      ["up", "down", "left", "right"].forEach(dir => {
        const [dx, dy] = DIRS[dir];
        if (!free(x + dx, y + dy) || !free(x + 2 * dx, y + 2 * dy)) return;
        const on = selSlot && selSlot.x === x && selSlot.y === y && selSlot.dir === dir;
        const m = document.createElement("button");
        m.className = "mk " + dir + (on ? " on" : "");
        m.textContent = ARROWS[dir];
        m.onclick = ev => {
          ev.stopPropagation();
          selSlot = on ? null : {x, y, dir, version: S.version};
          lastBoardKey = null;
          renderBoard();
          tryPlace();
        };
        h.appendChild(m);
      });
      d.appendChild(h);
    });
    b.appendChild(d);
  });
}

function scrollToLast() {
  if (S.version === lastVersionSeen) return;
  lastVersionSeen = S.version;
  const el = document.querySelector('.btile[data-id="' + S.last_id + '"]');
  if (!el) return;
  const f = $("field"), r = el.getBoundingClientRect(), fr = f.getBoundingClientRect();
  if (r.left < fr.left || r.right > fr.right || r.top < fr.top || r.bottom > fr.bottom) {
    f.scrollLeft += (r.left + r.width / 2) - (fr.left + fr.width / 2);
    f.scrollTop += (r.top + r.height / 2) - (fr.top + fr.height / 2);
  }
}

function renderShelf() {
  const key = S.room + "|" + S.game + "|" + S.shelf.length + "|" + selIdx + "|" + selSide;
  if (key === lastShelfKey) return;
  lastShelfKey = key;
  const sh = $("shelf-tiles");
  sh.innerHTML = "";
  S.shelf.forEach(t => {
    const d = makeTile(t, "");
    ["left", "right"].forEach((side, k) => {
      const el = d.children[k];
      if (t.idx === selIdx && side === selSide) el.classList.add("sel");
      el.onclick = () => {
        if (selIdx === t.idx && selSide === side) { selIdx = null; selSide = null; }
        else { selIdx = t.idx; selSide = side; }
        lastShelfKey = null; renderShelf(); tryPlace();
      };
    });
    sh.appendChild(d);
  });
}

async function tryPlace() {
  if (selIdx === null || !selSlot) return;
  const payload = {idx: selIdx, side: selSide, x: selSlot.x, y: selSlot.y, dir: selSlot.dir, version: selSlot.version};
  selIdx = null; selSide = null; selSlot = null;
  lastBoardKey = null; lastShelfKey = null;
  const r = await api("/api/place", payload);
  toast(r.message, r.result === "success");
  poll();
}

function render() {
  if (!S || !S.room) { showView("v-join"); $("overlay").style.display = "none"; return; }
  if (S.status === "lobby") {
    showView("v-lobby");
    $("overlay").style.display = "none";
    $("l-code").textContent = S.room;
    $("l-link").textContent = location.origin + location.pathname + "?room=" + S.room;
    $("l-count").textContent = S.players.length;
    playerCards($("l-players"));
    $("l-host").style.display = S.is_host ? "block" : "none";
    $("l-wait").style.display = S.is_host ? "none" : "block";
    $("l-timer").textContent = S.timer;
    if (document.activeElement !== $("timer-lobby")) $("timer-lobby").value = S.timer;
    return;
  }
  showView("v-game");
  if (S.game !== lastGame) { lastGame = S.game; selIdx = null; selSide = null; selSlot = null; lastVersionSeen = -1; }
  if (selSlot && selSlot.version !== S.version) { selSlot = null; }
  $("g-code").textContent = S.room;
  playerCards($("players"));
  const t = Math.ceil(S.remaining);
  $("timer").textContent = S.status === "playing" ? "Timer: " + t + "s" : "Partita terminata – ha vinto " + S.winner;
  $("timer").className = (S.status === "playing" && t <= 5) ? "low" : "";
  $("log").textContent = S.log.slice(-3).join("  •  ");
  renderBoard();
  scrollToLast();
  renderShelf();
  const ov = $("overlay");
  if (S.status === "finished") {
    ov.style.display = "flex";
    $("wintext").textContent = "🎉 " + S.winner + " ha vinto la partita!" + (S.winner_is_you ? " (sei tu!)" : "") + " 🎉";
    const rk = $("ranking");
    rk.innerHTML = "";
    S.players.slice().sort((a, b) => b.score - a.score).forEach((p, i) => {
      const line = document.createElement("div");
      line.textContent = (i + 1) + ". " + p.name + " – " + p.score + " tessere";
      rk.appendChild(line);
    });
    $("restart-wrap").style.display = S.is_host ? "block" : "none";
    $("restart-wait").style.display = S.is_host ? "none" : "block";
  } else {
    ov.style.display = "none";
  }
}

let polling = false;
async function poll() {
  if (polling) return;
  polling = true;
  try {
    S = await (await fetch("/api/state")).json();
    render();
  } catch (e) {}
  polling = false;
}

const urlRoom = new URLSearchParams(location.search).get("room");
if (urlRoom) $("code").value = urlRoom.toUpperCase();
poll();
setInterval(poll, 700);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
