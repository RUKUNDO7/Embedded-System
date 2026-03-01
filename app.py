import json
from pathlib import Path
import re
import sqlite3
import uuid
from threading import Lock

from flask import Flask, jsonify, render_template, request
from flask_socketio import SocketIO

try:
    import paho.mqtt.client as mqtt
except ModuleNotFoundError:
    mqtt = None

app = Flask(__name__, template_folder=".")
socketio = SocketIO(app, cors_allowed_origins="*")

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = str(BASE_DIR / "rukundo.db")


@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    return response

DEFAULT_MENU_ITEMS = [
    {"id": "avocado_toast", "name": "Avocado Toast", "category": "Breakfast", "price": 2800},
    {"id": "chapati_beans", "name": "Chapati & Beans", "category": "Breakfast", "price": 2200},
    {"id": "banana_pancakes", "name": "Banana Pancakes", "category": "Breakfast", "price": 2600},
    {"id": "grilled_tilapia", "name": "Grilled Tilapia", "category": "Lunch", "price": 5200},
    {"id": "beef_brochette", "name": "Beef Brochette", "category": "Lunch", "price": 4700},
    {"id": "veggie_pilau", "name": "Veggie Pilau", "category": "Lunch", "price": 3900},
    {"id": "goat_stew", "name": "Goat Stew", "category": "Dinner", "price": 5600},
    {"id": "chicken_curry", "name": "Chicken Curry", "category": "Dinner", "price": 5100},
    {"id": "passion_mojito", "name": "Passion Mojito", "category": "Beverages", "price": 1800},
    {"id": "ginger_tea", "name": "Ginger Tea", "category": "Beverages", "price": 900},
    {"id": "mango_lassi", "name": "Mango Lassi", "category": "Beverages", "price": 1600},
    {"id": "fruit_parfait", "name": "Fruit Parfait", "category": "Dessert", "price": 2000},
    {"id": "choco_brownie", "name": "Chocolate Brownie", "category": "Dessert", "price": 1700},
]

# Global state tracking pending checkout and lock for cross-thread safety.
checkout_lock = Lock()
checkout_queue = {"active": False, "session_id": None, "amount": 0, "items": []}


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS cards (
            uid TEXT PRIMARY KEY,
            balance INTEGER,
            last_seen DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid TEXT,
            amount INTEGER,
            type TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS menu_items (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            price INTEGER NOT NULL CHECK (price > 0),
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cursor.execute("SELECT COUNT(*) AS c FROM menu_items")
    if cursor.fetchone()["c"] == 0:
        cursor.executemany(
            "INSERT INTO menu_items (id, name, category, price) VALUES (:id, :name, :category, :price)",
            DEFAULT_MENU_ITEMS,
        )
    conn.commit()
    conn.close()


def parse_int(value, fallback=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def slugify(value):
    base = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return base or "item"


def get_menu_items():
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, name, category, price
        FROM menu_items
        ORDER BY category ASC, name ASC
        """
    )
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows


def ensure_menu_items():
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) AS c FROM menu_items")
    count = cursor.fetchone()["c"]
    if count == 0:
        cursor.executemany(
            "INSERT INTO menu_items (id, name, category, price) VALUES (:id, :name, :category, :price)",
            DEFAULT_MENU_ITEMS,
        )
        conn.commit()
    conn.close()


def get_menu_index():
    return {item["id"]: item for item in get_menu_items()}


def unique_item_id(base):
    candidate = base
    suffix = 1
    conn = get_conn()
    cursor = conn.cursor()
    while True:
        cursor.execute("SELECT 1 FROM menu_items WHERE id = ?", (candidate,))
        if not cursor.fetchone():
            conn.close()
            return candidate
        suffix += 1
        candidate = f"{base}_{suffix}"


def upsert_card(uid, balance):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO cards (uid, balance, last_seen)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(uid) DO UPDATE SET
            balance = excluded.balance,
            last_seen = CURRENT_TIMESTAMP
        """,
        (uid, balance),
    )
    conn.commit()
    conn.close()


def create_transaction(uid, amount, tx_type):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO transactions (uid, amount, type) VALUES (?, ?, ?)",
        (uid, amount, tx_type),
    )
    conn.commit()
    conn.close()


def get_summary():
    conn = get_conn()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) AS c FROM cards")
    total_cards = cursor.fetchone()["c"]

    cursor.execute(
        """
        SELECT COALESCE(SUM(CASE WHEN type = 'checkout' THEN -amount ELSE 0 END), 0) AS revenue
        FROM transactions
        """
    )
    total_revenue = cursor.fetchone()["revenue"]

    cursor.execute(
        """
        SELECT COALESCE(SUM(CASE WHEN type = 'topup_command' THEN amount ELSE 0 END), 0) AS topups
        FROM transactions
        """
    )
    total_topups = cursor.fetchone()["topups"]

    cursor.execute(
        """
        SELECT uid, balance, last_seen
        FROM cards
        ORDER BY datetime(last_seen) DESC
        LIMIT 8
        """
    )
    recent_cards = [dict(row) for row in cursor.fetchall()]

    cursor.execute(
        """
        SELECT uid, amount, type, timestamp
        FROM transactions
        ORDER BY datetime(timestamp) DESC
        LIMIT 12
        """
    )
    recent_transactions = [dict(row) for row in cursor.fetchall()]

    conn.close()
    return {
        "total_cards": total_cards,
        "total_revenue": total_revenue,
        "total_topups": total_topups,
        "recent_cards": recent_cards,
        "recent_transactions": recent_transactions,
    }


def build_checkout(items):
    if not isinstance(items, list) or not items:
        return None, "Cart is empty."

    normalized = []
    total = 0
    menu_index = get_menu_index()
    for entry in items:
        item_id = str(entry.get("id", "")).strip()
        qty = parse_int(entry.get("qty"), 0)
        item = menu_index.get(item_id)
        if not item:
            return None, f"Unknown item: {item_id}"
        if qty < 1 or qty > 50:
            return None, f"Invalid quantity for {item['name']}."

        line_total = item["price"] * qty
        normalized.append(
            {
                "id": item["id"],
                "name": item["name"],
                "qty": qty,
                "unit_price": item["price"],
                "line_total": line_total,
            }
        )
        total += line_total

    return {"items": normalized, "total": total}, None


init_db()

# --- MQTT SETUP ---
MQTT_BROKER = "157.173.101.159"
TEAM_ID = "team_rukundo_20266"
TOPIC_STATUS = f"rfid/{TEAM_ID}/card/status"
TOPIC_TOPUP = f"rfid/{TEAM_ID}/card/topup"
TOPIC_BALANCE = f"rfid/{TEAM_ID}/card/balance"


def on_connect(client, userdata, flags, rc):
    client.subscribe(TOPIC_STATUS)
    client.subscribe(TOPIC_BALANCE)


def on_message(client, userdata, msg):
    global checkout_queue
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return

    uid = str(payload.get("uid", "")).strip()
    if not uid:
        return

    balance = parse_int(payload.get("balance", payload.get("new balance")), 0)
    upsert_card(uid, balance)

    socketio.emit(
        "card_tapped",
        {"uid": uid, "balance": balance, "source_topic": msg.topic},
    )

    with checkout_lock:
        if not checkout_queue["active"]:
            return

        pending = dict(checkout_queue)
        checkout_queue = {"active": False, "session_id": None, "amount": 0, "items": []}

    if balance >= pending["amount"]:
        deduction = -pending["amount"]
        if mqtt_enabled:
            client.publish(TOPIC_TOPUP, json.dumps({"uid": uid, "amount": deduction}))
        create_transaction(uid, deduction, "checkout")
        socketio.emit(
            "checkout_result",
            {
                "status": "success",
                "session_id": pending["session_id"],
                "uid": uid,
                "charged": pending["amount"],
                "new_balance": balance + deduction,
                "items": pending["items"],
            },
        )
    else:
        socketio.emit(
            "checkout_result",
            {
                "status": "insufficient",
                "session_id": pending["session_id"],
                "uid": uid,
                "needed": pending["amount"],
                "balance": balance,
            },
        )


mqtt_client = None
mqtt_enabled = False
if mqtt is not None:
    mqtt_client = mqtt.Client()
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message
    mqtt_enabled = True
    try:
        mqtt_client.connect(MQTT_BROKER, 1883, 60)
        mqtt_client.loop_start()
    except Exception:
        # Keep dashboard routes available even if MQTT is unreachable.
        mqtt_enabled = False


@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/bootstrap", methods=["GET"])
def bootstrap():
    ensure_menu_items()
    return jsonify({"menu": get_menu_items(), "summary": get_summary()})


@app.route("/api/summary", methods=["GET"])
def summary():
    return jsonify(get_summary())


@app.route("/api/menu", methods=["GET"])
def menu_list():
    ensure_menu_items()
    return jsonify({"menu": get_menu_items()})


@app.route("/api/menu", methods=["POST"])
def menu_create():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    category = str(data.get("category", "")).strip()
    price = parse_int(data.get("price"), 0)

    if not name:
        return jsonify({"status": "error", "message": "Name is required."}), 400
    if not category:
        return jsonify({"status": "error", "message": "Category is required."}), 400
    if price <= 0:
        return jsonify({"status": "error", "message": "Price must be above zero."}), 400

    item_id = unique_item_id(slugify(name))
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO menu_items (id, name, category, price) VALUES (?, ?, ?, ?)",
        (item_id, name, category, price),
    )
    conn.commit()
    conn.close()
    return jsonify({"status": "created", "item": {"id": item_id, "name": name, "category": category, "price": price}})


@app.route("/api/menu/<item_id>", methods=["PUT"])
def menu_update(item_id):
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    category = str(data.get("category", "")).strip()
    price = parse_int(data.get("price"), 0)

    if not name:
        return jsonify({"status": "error", "message": "Name is required."}), 400
    if not category:
        return jsonify({"status": "error", "message": "Category is required."}), 400
    if price <= 0:
        return jsonify({"status": "error", "message": "Price must be above zero."}), 400

    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE menu_items SET name = ?, category = ?, price = ? WHERE id = ?",
        (name, category, price, item_id),
    )
    if cursor.rowcount == 0:
        conn.close()
        return jsonify({"status": "error", "message": "Item not found."}), 404
    conn.commit()
    conn.close()
    return jsonify({"status": "updated", "item": {"id": item_id, "name": name, "category": category, "price": price}})


@app.route("/api/menu/<item_id>", methods=["DELETE"])
def menu_delete(item_id):
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM menu_items WHERE id = ?", (item_id,))
    if cursor.rowcount == 0:
        conn.close()
        return jsonify({"status": "error", "message": "Item not found."}), 404
    conn.commit()
    conn.close()
    return jsonify({"status": "deleted", "id": item_id})


@app.route("/api/menu/reset", methods=["POST"])
def menu_reset():
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM menu_items")
    cursor.executemany(
        "INSERT INTO menu_items (id, name, category, price) VALUES (:id, :name, :category, :price)",
        DEFAULT_MENU_ITEMS,
    )
    conn.commit()
    conn.close()
    return jsonify({"status": "reset", "count": len(DEFAULT_MENU_ITEMS)})


@app.route("/api/checkout", methods=["POST"])
def start_checkout():
    global checkout_queue
    data = request.get_json(silent=True) or {}
    checkout, error = build_checkout(data.get("items", []))
    if error:
        return jsonify({"status": "error", "message": error}), 400

    session_id = str(uuid.uuid4())
    with checkout_lock:
        checkout_queue = {
            "active": True,
            "session_id": session_id,
            "amount": checkout["total"],
            "items": checkout["items"],
        }

    return jsonify(
        {
            "status": "waiting_for_tap",
            "session_id": session_id,
            "total": checkout["total"],
            "items": checkout["items"],
        }
    )


@app.route("/api/topup", methods=["POST"])
def topup():
    data = request.get_json(silent=True) or {}
    uid = str(data.get("uid", "")).strip()
    amount = parse_int(data.get("amount"), 0)

    if not uid:
        return jsonify({"status": "error", "message": "Card UID is required."}), 400
    if amount <= 0:
        return jsonify({"status": "error", "message": "Amount must be above zero."}), 400

    if not mqtt_enabled:
        return jsonify({"status": "error", "message": "RFID broker is offline. Try again later."}), 503

    mqtt_client.publish(TOPIC_TOPUP, json.dumps({"uid": uid, "amount": amount}))
    create_transaction(uid, amount, "topup_command")
    socketio.emit("topup_sent", {"uid": uid, "amount": amount})
    return jsonify({"status": "command_sent", "uid": uid, "amount": amount})


if __name__ == "__main__":
    socketio.run(app, debug=True, host="0.0.0.0", port=6000)
