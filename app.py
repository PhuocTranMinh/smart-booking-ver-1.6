from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Union

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

try:
    from google.cloud import firestore
    from google.oauth2 import service_account
except ImportError:
    firestore = None
    service_account = None

try:
    from google import genai as google_genai
except ImportError:
    google_genai = None

BASE = Path(__file__).parent
DB_PATH = Path(os.getenv("ROOM_DB", str(BASE / "rooms.db")))
SECRET = os.getenv("QR_SECRET", "demo-only-change-this-secret").encode()
LOCK = threading.RLock()
CHAT: dict[str, dict[str, Any]] = {}
AI_CHAT: dict[str, dict[str, Any]] = {}
SESSIONS: dict[str, dict[str, str]] = {}
MQTT_CLIENT = None
MQTT_CONNECTED = False
FIRESTORE = None
FIREBASE_PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "smart-booking-82438")
LIFECYCLE_STOP = threading.Event()
LIFECYCLE_THREAD = None
app = FastAPI(title="Smart Study Room API", version="0.2.0")
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


def now() -> datetime:
    return datetime.now(timezone.utc)


def stamp(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="minutes")


@contextmanager
def db():
    conn = sqlite3.connect(str(DB_PATH), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS rooms (
          id INTEGER PRIMARY KEY, name TEXT NOT NULL, capacity INTEGER NOT NULL,
          amenities TEXT NOT NULL, hourly_rate INTEGER NOT NULL,
          online INTEGER NOT NULL DEFAULT 1, occupied INTEGER NOT NULL DEFAULT 0,
          light_on INTEGER NOT NULL DEFAULT 0, fan_on INTEGER NOT NULL DEFAULT 0,
          speaker_on INTEGER NOT NULL DEFAULT 0, last_seen TEXT
        );
        CREATE TABLE IF NOT EXISTS bookings (
          id TEXT PRIMARY KEY, room_id INTEGER NOT NULL REFERENCES rooms(id),
          user_id TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL,
          people INTEGER NOT NULL, cost INTEGER NOT NULL, status TEXT NOT NULL,
          qr_exp TEXT NOT NULL, idempotency_key TEXT UNIQUE, created TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, booking_id TEXT, event TEXT NOT NULL,
          at TEXT NOT NULL, detail TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit (
          id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL,
          action TEXT NOT NULL, detail TEXT NOT NULL, at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS devices (
          id TEXT PRIMARY KEY, room_id INTEGER NOT NULL REFERENCES rooms(id),
          device TEXT NOT NULL, name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
          UNIQUE(room_id,device)
        );
        """)
        columns = {row[1] for row in c.execute("PRAGMA table_info(rooms)").fetchall()}
        if "speaker_on" not in columns:
            c.execute("ALTER TABLE rooms ADD COLUMN speaker_on INTEGER NOT NULL DEFAULT 0")
        if "last_seen" not in columns:
            c.execute("ALTER TABLE rooms ADD COLUMN last_seen TEXT")
        if c.execute("SELECT COUNT(*) FROM rooms").fetchone()[0] == 0:
            c.executemany("INSERT INTO rooms(id,name,capacity,amenities,hourly_rate) VALUES(?,?,?,?,?)", [
                (1, "Phòng học A101", 8, '["Máy chiếu","Bảng trắng","Điều hòa"]', 40000),
                (2, "Phòng thảo luận B204", 6, '["Màn hình","Bảng trắng","Điều hòa"]', 35000),
                (3, "Phòng lab C301", 20, '["Máy chiếu","Máy tính","Điều hòa"]', 80000),
                (4, "Phòng nhóm D102", 4, '["Màn hình","Ổ cắm"]', 25000),
            ])
        device_names = {"lamp": "Đèn", "fan": "Quạt", "speaker": "Loa"}
        for room in c.execute("SELECT id FROM rooms").fetchall():
            for device, label in device_names.items():
                c.execute("INSERT OR IGNORE INTO devices(id,room_id,device,name,enabled) VALUES(?,?,?,?,1)", ("%s_%s" % (room["id"], device), room["id"], device, label))


def init_firestore() -> None:
    global FIRESTORE
    credentials_json = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
    credentials_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if not credentials_json and not credentials_path:
        return
    if firestore is None:
        raise RuntimeError("Thiếu google-cloud-firestore; hãy cài requirements.txt")
    if credentials_json:
        info = json.loads(credentials_json)
        credentials = service_account.Credentials.from_service_account_info(info)
        project_id = os.getenv("FIREBASE_PROJECT_ID", info.get("project_id", FIREBASE_PROJECT_ID))
        FIRESTORE = firestore.Client(project=project_id, credentials=credentials)
    else:
        FIRESTORE = firestore.Client(project=FIREBASE_PROJECT_ID)
    seed_firestore_rooms()
    seed_firestore_users_and_devices()


def seed_firestore_rooms() -> None:
    collection = FIRESTORE.collection("rooms")
    if any(collection.limit(1).stream()):
        return
    defaults = [
        (1, "Phòng học A101", 8, ["Máy chiếu", "Bảng trắng", "Điều hòa"], 40000),
        (2, "Phòng thảo luận B204", 6, ["Màn hình", "Bảng trắng", "Điều hòa"], 35000),
        (3, "Phòng lab C301", 20, ["Máy chiếu", "Máy tính", "Điều hòa"], 80000),
        (4, "Phòng nhóm D102", 4, ["Màn hình", "Ổ cắm"], 25000),
    ]
    migrated_rooms = []
    migrated_bookings = []
    migrated_audit = []
    migrated_events = []
    if DB_PATH.exists():
        try:
            with db() as c:
                migrated_rooms = [dict(row) for row in c.execute("SELECT * FROM rooms").fetchall()]
                migrated_bookings = [dict(row) for row in c.execute("SELECT * FROM bookings").fetchall()]
                migrated_audit = [dict(row) for row in c.execute("SELECT * FROM audit").fetchall()]
                migrated_events = [dict(row) for row in c.execute("SELECT * FROM events").fetchall()]
        except sqlite3.Error:
            migrated_rooms = []
    batch = FIRESTORE.batch()
    for raw in migrated_rooms:
        room = dict(raw)
        room["amenities"] = json.loads(room["amenities"]) if isinstance(room.get("amenities"), str) else room.get("amenities", [])
        room.setdefault("speaker_on", 0)
        for key in ("online", "occupied", "light_on", "fan_on", "speaker_on"):
            room[key] = bool(room.get(key, key == "online"))
        batch.set(collection.document(str(room["id"])), room)
    if not migrated_rooms:
        for room_id, name, capacity, amenities, rate in defaults:
            batch.set(collection.document(str(room_id)), {
                "id": room_id, "name": name, "capacity": capacity, "amenities": amenities,
                "hourly_rate": rate, "online": True, "occupied": False,
                "light_on": False, "fan_on": False, "speaker_on": False,
            })
    batch.commit()
    for collection_name, rows in (("bookings", migrated_bookings), ("audit", migrated_audit), ("booking_events", migrated_events)):
        chunk_size = 200 if collection_name == "bookings" else 400
        for offset in range(0, len(rows), chunk_size):
            migration_batch = FIRESTORE.batch()
            for raw in rows[offset:offset + chunk_size]:
                item = dict(raw)
                item.pop("id", None)
                migration_batch.set(FIRESTORE.collection(collection_name).document(str(raw.get("id") or uuid.uuid4().hex)), item)
                if collection_name == "bookings" and raw.get("idempotency_key"):
                    idem_id = hashlib.sha256(raw["idempotency_key"].encode()).hexdigest()
                    migration_batch.set(FIRESTORE.collection("idempotency").document(idem_id), {"booking_id": str(raw["id"])})
            migration_batch.commit()


def seed_firestore_users_and_devices() -> None:
    users = FIRESTORE.collection("users")
    for user_id, username, role, name in (
        ("demo-user", "user", "user", "Người dùng demo"),
        ("demo-admin", "admin", "admin", "Quản trị viên"),
    ):
        ref = users.document(user_id)
        if not ref.get().exists:
            ref.set({"user_id": user_id, "username": username, "role": role, "name": name, "demo": True})
    device_labels = {"lamp": "Đèn", "fan": "Quạt", "speaker": "Loa"}
    batch = FIRESTORE.batch()
    count = 0
    for room in fs_rooms():
        for device, label in device_labels.items():
            ref = FIRESTORE.collection("devices").document("%s_%s" % (room["id"], device))
            snapshot = ref.get()
            payload = {
                "id": "%s_%s" % (room["id"], device), "room_id": room["id"],
                "device": device,
                "on": bool(room.get(DEVICE_COLUMNS[device], False)),
                "topic_command": "rooms/%s/devices/%s/command" % (room["id"], device),
                "topic_status": "rooms/%s/devices/%s/status" % (room["id"], device),
            }
            if not snapshot.exists:
                payload.update({"name": label, "enabled": True})
            batch.set(ref, payload, merge=True)
            count += 1
            if count == 450:
                batch.commit()
                batch = FIRESTORE.batch()
                count = 0
    if count:
        batch.commit()


def fs_room(room_id: int) -> Optional[dict[str, Any]]:
    snapshot = FIRESTORE.collection("rooms").document(str(room_id)).get()
    if not snapshot.exists:
        return None
    room = snapshot.to_dict()
    room.setdefault("id", room_id)
    room.setdefault("amenities", [])
    for key in ("online", "occupied", "light_on", "fan_on", "speaker_on"):
        room.setdefault(key, False if key != "online" else True)
    return room


def fs_rooms() -> list[dict[str, Any]]:
    rooms = [fs_room(int(doc.id)) for doc in FIRESTORE.collection("rooms").stream()]
    return sorted((room for room in rooms if room), key=lambda room: room["id"])


def fs_add_audit(actor: str, action: str, detail: str) -> None:
    FIRESTORE.collection("audit").add({"actor": actor, "action": action, "detail": detail, "at": stamp(now())})


def fs_add_event(booking_id: str, event: str, detail: str) -> None:
    FIRESTORE.collection("booking_events").add({"booking_id": booking_id, "event": event, "detail": detail, "at": stamp(now())})


def fs_update_room(room_id: int, updates: dict[str, Any]) -> bool:
    ref = FIRESTORE.collection("rooms").document(str(room_id))
    if not ref.get().exists:
        return False
    ref.update(updates)
    device_updates = {device: updates[column] for device, column in DEVICE_COLUMNS.items() if column in updates}
    if device_updates:
        for device, value in device_updates.items():
            FIRESTORE.collection("devices").document("%s_%s" % (room_id, device)).set({"on": bool(value)}, merge=True)
    return True


def transition_firestore_booking(booking_id: str, expected: str, target: str) -> bool:
    booking_ref = FIRESTORE.collection("bookings").document(booking_id)
    row_result = {"changed": False}

    @firestore.transactional
    def transition(transaction):
        snapshot = fs_transaction_get_one(transaction, booking_ref)
        if snapshot is None or not snapshot.exists or snapshot.to_dict().get("status") != expected:
            return False
        booking = snapshot.to_dict()
        room_ref = FIRESTORE.collection("rooms").document(str(booking["room_id"]))
        room_snapshot = None
        if target == "COMPLETED":
            room_snapshot = fs_transaction_get_one(transaction, room_ref)
        transaction.update(booking_ref, {"status": target})
        if target == "COMPLETED" and room_snapshot is not None and room_snapshot.exists:
            transaction.update(room_ref, {"occupied": False})
        event_ref = FIRESTORE.collection("booking_events").document()
        transaction.set(event_ref, {"booking_id": booking_id, "event": target, "at": stamp(now()), "detail": "Tự động chuyển trạng thái theo thời gian booking"})
        audit_ref = FIRESTORE.collection("audit").document()
        transaction.set(audit_ref, {"actor": "booking-lifecycle", "action": target, "detail": booking_id, "at": stamp(now())})
        return True

    row_result["changed"] = transition(FIRESTORE.transaction())
    return row_result["changed"]


def process_booking_lifecycle() -> None:
    current = now()
    if FIRESTORE is not None:
        bookings = fs_bookings()
        for booking in bookings:
            status = booking.get("status")
            try:
                if status == "CONFIRMED" and current > parse_dt(booking["end"]) + timedelta(minutes=15):
                    transition_firestore_booking(booking["id"], "CONFIRMED", "NO_SHOW")
                elif status == "CHECKED_IN" and current >= parse_dt(booking["end"]):
                    if transition_firestore_booking(booking["id"], "CHECKED_IN", "COMPLETED"):
                        transport = publish_room_command(booking["room_id"], {"lamp": False, "fan": False, "speaker": False})
                        if transport["transport"] != "mqtt" or not transport["published"]:
                            fs_update_room(booking["room_id"], {"light_on": False, "fan_on": False, "speaker_on": False})
            except Exception as exc:
                print("Booking lifecycle error %s: %s" % (type(exc).__name__, str(exc)), flush=True)
        for room in fs_rooms():
            last_seen = room.get("last_seen")
            if last_seen and room.get("online"):
                try:
                    if current - parse_dt(last_seen) > timedelta(seconds=60):
                        fs_update_room(room["id"], {"online": False})
                        fs_add_audit("room-monitor", "CONTROLLER_OFFLINE", "room=%s; last_seen=%s" % (room["id"], last_seen))
                except Exception:
                    continue
        return

    with LOCK, db() as c:
        c.execute("BEGIN IMMEDIATE")
        no_show = c.execute("SELECT * FROM bookings WHERE status='CONFIRMED'").fetchall()
        checked_in = c.execute("SELECT * FROM bookings WHERE status='CHECKED_IN'").fetchall()
        for row in no_show:
            if current > parse_dt(row["end"]) + timedelta(minutes=15):
                changed = c.execute("UPDATE bookings SET status='NO_SHOW' WHERE id=? AND status='CONFIRMED'", (row["id"],)).rowcount
                if changed:
                    c.execute("INSERT INTO events(booking_id,event,at,detail) VALUES(?,?,?,?)", (row["id"], "NO_SHOW", stamp(current), "Booking quá hạn nhưng chưa check-in"))
                    audit(c, "booking-lifecycle", "NO_SHOW", row["id"])
        completed_ids = []
        for row in checked_in:
            if current >= parse_dt(row["end"]):
                changed = c.execute("UPDATE bookings SET status='COMPLETED' WHERE id=? AND status='CHECKED_IN'", (row["id"],)).rowcount
                if changed:
                    c.execute("UPDATE rooms SET occupied=0,light_on=0,fan_on=0,speaker_on=0 WHERE id=?", (row["room_id"],))
                    c.execute("INSERT INTO events(booking_id,event,at,detail) VALUES(?,?,?,?)", (row["id"], "COMPLETED", stamp(current), "Hết giờ; tự tắt thiết bị mô phỏng"))
                    audit(c, "booking-lifecycle", "COMPLETED", row["id"])
                    completed_ids.append((row["id"], row["room_id"]))
        room_rows = c.execute("SELECT id,online,last_seen FROM rooms WHERE online=1 AND last_seen IS NOT NULL").fetchall()
        for room in room_rows:
            try:
                if current - parse_dt(room["last_seen"]) > timedelta(seconds=60):
                    c.execute("UPDATE rooms SET online=0 WHERE id=? AND online=1", (room["id"],))
                    audit(c, "room-monitor", "CONTROLLER_OFFLINE", "room=%s; last_seen=%s" % (room["id"], room["last_seen"]))
            except Exception:
                continue
        c.execute("COMMIT")
    for _, room_id in completed_ids:
        publish_room_command(room_id, {"lamp": False, "fan": False, "speaker": False})


def lifecycle_worker() -> None:
    while not LIFECYCLE_STOP.wait(15):
        try:
            process_booking_lifecycle()
        except Exception as exc:
            print("Booking lifecycle worker error %s: %s" % (type(exc).__name__, str(exc)), flush=True)


def fs_bookings() -> list[dict[str, Any]]:
    result = []
    for doc in FIRESTORE.collection("bookings").stream():
        item = doc.to_dict()
        item.setdefault("id", doc.id)
        result.append(item)
    return result


def fs_transaction_get_one(transaction, reference):
    """Transaction.get yields snapshots in the Python Firestore client."""
    return next(iter(transaction.get(reference)), None)


def fs_booking_conflict(room_id: int, start: str, end: str, exclude_id: Optional[str] = None) -> bool:
    query = FIRESTORE.collection("bookings").where("room_id", "==", room_id)
    for snapshot in query.stream():
        booking = snapshot.to_dict()
        booking.setdefault("id", snapshot.id)
        if booking.get("id") == exclude_id or booking.get("room_id") != room_id:
            continue
        if booking.get("status") not in ("CONFIRMED", "CHECKED_IN"):
            continue
        if booking.get("start", "") < end and booking.get("end", "") > start:
            return True
    return False


def fs_create_booking(data: "BookingIn", identity: dict[str, str], start: datetime, end: datetime) -> dict[str, Any]:
    booking_id = str(uuid.uuid4())
    booking_ref = FIRESTORE.collection("bookings").document(booking_id)
    room_ref = FIRESTORE.collection("rooms").document(str(data.room_id))
    idem_ref = None
    if data.idempotency_key:
        idem_id = hashlib.sha256(data.idempotency_key.encode()).hexdigest()
        idem_ref = FIRESTORE.collection("idempotency").document(idem_id)
    expiration = end + timedelta(minutes=15)
    booking = None

    @firestore.transactional
    def commit(transaction):
        nonlocal booking
        if idem_ref is not None:
            existing_idempotency = fs_transaction_get_one(transaction, idem_ref)
            if existing_idempotency is not None and existing_idempotency.exists:
                existing_data = existing_idempotency.to_dict() or {}
                existing_booking_id = existing_data.get("booking_id")
                if existing_booking_id:
                    existing_booking = fs_transaction_get_one(
                        transaction,
                        FIRESTORE.collection("bookings").document(existing_booking_id),
                    )
                    if existing_booking is not None and existing_booking.exists:
                        booking = {**existing_booking.to_dict(), "id": existing_booking.id}
                        return booking
        room_snapshot = fs_transaction_get_one(transaction, room_ref)
        if room_snapshot is None or not room_snapshot.exists:
            raise HTTPException(404, "Không tìm thấy phòng")
        room = room_snapshot.to_dict()
        if data.people > int(room.get("capacity", 0)):
            raise HTTPException(400, "Số người vượt sức chứa phòng")
        room_bookings = transaction.get(FIRESTORE.collection("bookings").where("room_id", "==", data.room_id))
        for snapshot in room_bookings:
            other = snapshot.to_dict()
            if other.get("status") in ("CONFIRMED", "CHECKED_IN") and other.get("start", "") < stamp(end) and other.get("end", "") > stamp(start):
                raise HTTPException(409, "Phòng vừa được đặt trong khoảng thời gian này")
        cost = round(int(room.get("hourly_rate", 0)) * (end - start).total_seconds() / 3600)
        booking = {
            "id": booking_id, "room_id": data.room_id, "user_id": identity["user_id"],
            "start": stamp(start), "end": stamp(end), "people": data.people,
            "cost": cost, "status": "CONFIRMED", "qr_exp": stamp(expiration),
            "idempotency_key": data.idempotency_key, "created": stamp(now()),
        }
        transaction.create(booking_ref, booking)
        transaction.update(room_ref, {"booking_version": int(room.get("booking_version", 0)) + 1})
        event_ref = FIRESTORE.collection("booking_events").document()
        transaction.set(event_ref, {"booking_id": booking_id, "event": "PENDING", "at": stamp(now()), "detail": "Yêu cầu đã qua bước xác nhận của người dùng"})
        confirmed_event_ref = FIRESTORE.collection("booking_events").document()
        transaction.set(confirmed_event_ref, {"booking_id": booking_id, "event": "CONFIRMED", "at": stamp(now()), "detail": "Booking đã xác nhận"})
        audit_ref = FIRESTORE.collection("audit").document()
        transaction.set(audit_ref, {"actor": identity["user_id"], "action": "BOOKING_CREATED", "detail": booking_id, "at": stamp(now())})
        if idem_ref is not None:
            transaction.set(idem_ref, {"booking_id": booking_id})
        return booking

    try:
        return booking_view(commit(FIRESTORE.transaction()))
    except HTTPException:
        raise
    except Exception as exc:
        import traceback

        print("========== FIRESTORE BOOKING ERROR ==========")
        print("ERROR TYPE:", type(exc).__name__)
        print("ERROR:", repr(exc))
        traceback.print_exc()
        print("==============================================", flush=True)

        raise HTTPException(
            503,
            f"Firestore error: {type(exc).__name__}: {str(exc)}"
        ) from exc

@app.on_event("startup")
def startup() -> None:
    global LIFECYCLE_THREAD
    init_db()
    init_firestore()
    start_mqtt()
    LIFECYCLE_STOP.clear()
    if LIFECYCLE_THREAD is None or not LIFECYCLE_THREAD.is_alive():
        LIFECYCLE_THREAD = threading.Thread(target=lifecycle_worker, name="booking-lifecycle", daemon=True)
        LIFECYCLE_THREAD.start()


@app.on_event("shutdown")
def shutdown() -> None:
    LIFECYCLE_STOP.set()
    if MQTT_CLIENT is not None:
        MQTT_CLIENT.loop_stop()


DEVICE_COLUMNS = {"lamp": "light_on", "fan": "fan_on", "speaker": "speaker_on"}


def mqtt_on_connect(client, userdata, flags, reason_code, properties=None):
    global MQTT_CONNECTED
    MQTT_CONNECTED = int(reason_code) == 0
    if MQTT_CONNECTED:
        client.subscribe("rooms/+/telemetry", qos=1)
        client.subscribe("rooms/+/status", qos=1)
        client.subscribe("rooms/+/devices/+/status", qos=1)


def mqtt_on_disconnect(client, userdata, disconnect_flags, reason_code, properties=None):
    global MQTT_CONNECTED
    MQTT_CONNECTED = False


def apply_device_message(room_id: int, payload: dict[str, Any], actor: str = "mqtt-controller") -> None:
    assignments = []
    values = []
    if "online" in payload:
        assignments.append("online=?")
        values.append(int(bool(payload["online"])))
    elif actor == "mqtt-controller":
        assignments.append("online=?")
        values.append(1)
    if "occupied" in payload:
        assignments.append("occupied=?")
        values.append(int(bool(payload["occupied"])))
    devices = payload.get("devices", {})
    if not isinstance(devices, dict):
        devices = {}
    if "device" in payload and "on" in payload:
        devices = dict(devices, **{payload["device"]: payload["on"]})
    for device, state in devices.items():
        column = DEVICE_COLUMNS.get(device)
        if column and isinstance(state, bool):
            assignments.append(column + "=?")
            values.append(int(state))
    if actor == "mqtt-controller":
        assignments.append("last_seen=?")
        values.append(stamp(now()))
    if not assignments:
        return
    if FIRESTORE is not None:
        field_updates = {}
        for assignment, value in zip(assignments, values):
            field = assignment.split("=")[0]
            field_updates[field] = bool(value) if field in ("online", "occupied", "light_on", "fan_on", "speaker_on") else value
        fs_update_room(room_id, field_updates)
        fs_add_audit(actor, "MQTT_STATUS", "room=%s; %s" % (room_id, json.dumps(payload, ensure_ascii=False)))
        return
    with db() as c:
        values.append(room_id)
        c.execute("UPDATE rooms SET " + ",".join(assignments) + " WHERE id=?", values)
        audit(c, actor, "MQTT_STATUS", "room=%s; %s" % (room_id, json.dumps(payload, ensure_ascii=False)))


def mqtt_on_message(client, userdata, message):
    try:
        parts = message.topic.split("/")
        room_id = int(parts[1])
        payload = json.loads(message.payload.decode("utf-8"))
        if len(parts) == 5 and parts[2] == "devices" and parts[4] == "status":
            payload = dict(payload, device=parts[3])
        apply_device_message(room_id, payload)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return
    except Exception:
        return


def start_mqtt() -> None:
    global MQTT_CLIENT
    broker = os.getenv("MQTT_BROKER", "").strip()
    if not broker or mqtt is None:
        return
    try:
        port = int(os.getenv("MQTT_PORT", "8883" if os.getenv("MQTT_TLS", "true").lower() == "true" else "1883"))
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=os.getenv("MQTT_CLIENT_ID", "smart-room-backend"))
        username = os.getenv("MQTT_USERNAME", "")
        if username:
            client.username_pw_set(username, os.getenv("MQTT_PASSWORD", ""))
        if os.getenv("MQTT_TLS", "true").lower() == "true":
            client.tls_set()
        client.on_connect = mqtt_on_connect
        client.on_disconnect = mqtt_on_disconnect
        client.on_message = mqtt_on_message
        client.connect_async(broker, port, keepalive=30)
        client.loop_start()
        MQTT_CLIENT = client
    except Exception:
        MQTT_CLIENT = None


def publish_room_command(room_id: int, devices: dict[str, bool]) -> dict[str, Any]:
    payload = {"command_id": uuid.uuid4().hex, "devices": devices, "at": stamp(now())}
    topic = "rooms/%s/command" % room_id
    if MQTT_CLIENT is not None and MQTT_CONNECTED:
        result = MQTT_CLIENT.publish(topic, json.dumps(payload), qos=1, retain=False)
        return {"transport": "mqtt", "topic": topic, "published": result.rc == mqtt.MQTT_ERR_SUCCESS}
    return {"transport": "http-simulation", "topic": topic, "published": False}


def audit(c: sqlite3.Connection, actor: str, action: str, detail: str) -> None:
    c.execute("INSERT INTO audit(actor,action,detail,at) VALUES(?,?,?,?)", (actor, action, detail, stamp(now())))


def require_admin(role: Optional[str]) -> None:
    if role != "admin":
        raise HTTPException(403, "Yêu cầu quyền Administrator")


class BookingIn(BaseModel):
    room_id: int
    start: datetime
    end: datetime
    people: int = Field(ge=1, le=100)
    user_id: str = "demo-user"
    idempotency_key: Optional[str] = None


class ChatIn(BaseModel):
    message: str
    session_id: str = "default"


class QRIn(BaseModel):
    token: str
    room_id: int


class TelemetryIn(BaseModel):
    room_id: int
    occupied: bool
    online: bool = True
    devices: Optional[dict[str, bool]] = None


class DeviceCommandIn(BaseModel):
    devices: dict[str, bool]


class DeviceConfigIn(BaseModel):
    name: Optional[str] = None
    enabled: Optional[bool] = None


class LoginIn(BaseModel):
    username: str
    password: str


DEMO_ACCOUNTS = {
    "user": {"password": "demo123", "user_id": "demo-user", "role": "user", "name": "Người dùng demo"},
    "admin": {"password": "admin123", "user_id": "demo-admin", "role": "admin", "name": "Quản trị viên"},
}


def current_user(authorization: Optional[str] = Header(default=None)) -> dict[str, str]:
    scheme, _, token = (authorization or "").partition(" ")
    identity = SESSIONS.get(token) if scheme.lower() == "bearer" else None
    if not identity:
        raise HTTPException(401, "Vui lòng đăng nhập")
    return identity


def parse_dt(raw: str) -> datetime:
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    except ValueError as exc:
        raise HTTPException(400, "Thời gian phải theo ISO 8601") from exc


def free_rooms(start: datetime, end: datetime, people: int, amenities: Optional[list[str]] = None) -> list[dict[str, Any]]:
    if FIRESTORE is not None:
        requested = {a.casefold() for a in (amenities or [])}
        result = []
        for room in fs_rooms():
            if int(room.get("capacity", 0)) < people:
                continue
            current_amenities = room.get("amenities", [])
            if requested and not requested.issubset({a.casefold() for a in current_amenities}):
                continue
            if not fs_booking_conflict(room["id"], stamp(start), stamp(end)):
                result.append(room)
        return sorted(result, key=lambda room: room.get("hourly_rate", 0))
    with db() as c:
        rows = c.execute("SELECT * FROM rooms WHERE capacity>=? ORDER BY hourly_rate", (people,)).fetchall()
        result = []
        for row in rows:
            room = dict(row)
            room["amenities"] = json.loads(room["amenities"])
            if amenities and not {a.casefold() for a in amenities}.issubset({a.casefold() for a in room["amenities"]}):
                continue
            conflict = c.execute("""SELECT 1 FROM bookings WHERE room_id=?
              AND status IN ('CONFIRMED','CHECKED_IN') AND start<? AND end>? LIMIT 1""",
              (room["id"], stamp(end), stamp(start))).fetchone()
            if not conflict:
                result.append(room)
        return result


def sign_payload(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    body = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    sig = hmac.new(SECRET, body.encode(), hashlib.sha256).hexdigest()
    return body + "." + sig


def decode_token(token: str) -> dict[str, Any]:
    try:
        body, signature = token.split(".", 1)
        expected = hmac.new(SECRET, body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("signature")
        return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except Exception as exc:
        raise HTTPException(400, "QR không hợp lệ hoặc đã bị thay đổi") from exc


def booking_view(row: Union[sqlite3.Row, dict]) -> dict[str, Any]:
    booking = dict(row)
    booking["qr_token"] = sign_payload({"booking_id": booking["id"], "room_id": booking["room_id"], "exp": booking["qr_exp"]})
    return booking


@app.get("/")
def home():
    return FileResponse(str(BASE / "static" / "index.html"))


@app.get("/api/health")
def health():
    ai_status = {"ai_provider": "gemini", "ai_enabled": bool(os.getenv("GEMINI_API_KEY", "").strip() and google_genai is not None), "ai_model": GEMINI_MODEL}
    if FIRESTORE is None:
        return {"status": "ok", "database": "sqlite", "firestore_connected": False, **ai_status}
    try:
        next(iter(FIRESTORE.collection("rooms").limit(1).stream()), None)
        return {"status": "ok", "database": "firestore", "firestore_connected": True, **ai_status}
    except Exception as exc:
        print("Firestore health check failed: %s: %s" % (type(exc).__name__, str(exc)), flush=True)
        return {"status": "degraded", "database": "firestore", "firestore_connected": False, "error": type(exc).__name__, **ai_status}


@app.post("/api/auth/login")
def login(data: LoginIn):
    account = DEMO_ACCOUNTS.get(data.username)
    if not account or not hmac.compare_digest(data.password, account["password"]):
        raise HTTPException(401, "Tên đăng nhập hoặc mật khẩu không đúng")
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = {"user_id": account["user_id"], "role": account["role"], "name": account["name"]}
    return {"token": token, "user": SESSIONS[token]}


@app.get("/api/auth/me")
def whoami(identity: dict[str, str] = Depends(current_user)):
    return identity


@app.get("/api/rooms")
def rooms(identity: dict[str, str] = Depends(current_user)):
    if FIRESTORE is not None:
        return [{**r, "devices": {"lamp": bool(r["light_on"]), "fan": bool(r["fan_on"]), "speaker": bool(r["speaker_on"])}} for r in fs_rooms()]
    with db() as c:
        return [{**dict(r), "amenities": json.loads(r["amenities"]), "devices": {"lamp": bool(r["light_on"]), "fan": bool(r["fan_on"]), "speaker": bool(r["speaker_on"])}} for r in c.execute("SELECT * FROM rooms ORDER BY id").fetchall()]


@app.get("/api/availability")
def availability(start: str, end: str, people: int = 1, amenities: str = "", identity: dict[str, str] = Depends(current_user)):
    s, e = parse_dt(start), parse_dt(end)
    if s >= e or s < now() - timedelta(minutes=1):
        raise HTTPException(400, "Khoảng thời gian không hợp lệ")
    return free_rooms(s, e, people, [x.strip() for x in amenities.split(",") if x.strip()])


@app.post("/api/bookings")
def create_booking(data: BookingIn, identity: dict[str, str] = Depends(current_user)):
    start = data.start.replace(tzinfo=timezone.utc) if data.start.tzinfo is None else data.start.astimezone(timezone.utc)
    end = data.end.replace(tzinfo=timezone.utc) if data.end.tzinfo is None else data.end.astimezone(timezone.utc)
    if start >= end or start < now() - timedelta(minutes=1):
        raise HTTPException(400, "Khoảng thời gian không hợp lệ")
    if FIRESTORE is not None:
        return fs_create_booking(data, identity, start, end)
    with LOCK, db() as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            if data.idempotency_key:
                existing = c.execute("SELECT * FROM bookings WHERE idempotency_key=?", (data.idempotency_key,)).fetchone()
                if existing:
                    c.execute("COMMIT")
                    return booking_view(existing)
            room = c.execute("SELECT * FROM rooms WHERE id=?", (data.room_id,)).fetchone()
            if not room:
                raise HTTPException(404, "Không tìm thấy phòng")
            if data.people > room["capacity"]:
                raise HTTPException(400, "Số người vượt sức chứa phòng")
            conflict = c.execute("""SELECT 1 FROM bookings WHERE room_id=?
              AND status IN ('CONFIRMED','CHECKED_IN') AND start<? AND end>? LIMIT 1""",
              (data.room_id, stamp(end), stamp(start))).fetchone()
            if conflict:
                raise HTTPException(409, "Phòng vừa được đặt trong khoảng thời gian này")
            booking_id = str(uuid.uuid4())
            cost = round(room["hourly_rate"] * (end - start).total_seconds() / 3600)
            expiration = end + timedelta(minutes=15)
            c.execute("INSERT INTO bookings VALUES(?,?,?,?,?,?,?,?,?,?,?)", (booking_id, data.room_id, identity["user_id"], stamp(start), stamp(end), data.people, cost, "CONFIRMED", stamp(expiration), data.idempotency_key, stamp(now())))
            c.execute("INSERT INTO events(booking_id,event,at,detail) VALUES(?,?,?,?)", (booking_id, "PENDING", stamp(now()), "Yêu cầu đã qua bước xác nhận của người dùng"))
            c.execute("INSERT INTO events(booking_id,event,at,detail) VALUES(?,?,?,?)", (booking_id, "CONFIRMED", stamp(now()), "Booking đã xác nhận"))
            audit(c, identity["user_id"], "BOOKING_CREATED", booking_id)
            c.execute("COMMIT")
            return booking_view(c.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone())
        except Exception:
            c.execute("ROLLBACK")
            raise


@app.get("/api/bookings")
def list_bookings(identity: dict[str, str] = Depends(current_user)):
    if FIRESTORE is not None:
        items = [b for b in fs_bookings() if b.get("user_id") == identity["user_id"]]
        return [booking_view(b) for b in sorted(items, key=lambda b: b.get("created", ""), reverse=True)]
    with db() as c:
        return [booking_view(r) for r in c.execute("SELECT * FROM bookings WHERE user_id=? ORDER BY created DESC", (identity["user_id"],)).fetchall()]


@app.get("/api/bookings/{booking_id}")
def get_booking(booking_id: str, identity: dict[str, str] = Depends(current_user)):
    if FIRESTORE is not None:
        snapshot = FIRESTORE.collection("bookings").document(booking_id).get()
        if not snapshot.exists:
            raise HTTPException(404, "Không tìm thấy booking")
        row = {**snapshot.to_dict(), "id": snapshot.id}
    else:
        with db() as c:
            snapshot = c.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
            if not snapshot:
                raise HTTPException(404, "Không tìm thấy booking")
            row = dict(snapshot)
    if identity["role"] != "admin" and row.get("user_id") != identity["user_id"]:
        raise HTTPException(403, "Không có quyền xem booking này")
    return booking_view(row)


@app.get("/api/bookings/{booking_id}/events")
def booking_events(booking_id: str, identity: dict[str, str] = Depends(current_user)):
    booking = get_booking(booking_id, identity)
    if FIRESTORE is not None:
        events = [doc.to_dict() for doc in FIRESTORE.collection("booking_events").where("booking_id", "==", booking_id).stream()]
    else:
        with db() as c:
            events = [dict(row) for row in c.execute("SELECT * FROM events WHERE booking_id=? ORDER BY id", (booking_id,)).fetchall()]
    return {"booking_id": booking["id"], "events": sorted(events, key=lambda event: event.get("at", ""))}


@app.post("/api/bookings/{booking_id}/cancel")
def cancel_booking(booking_id: str, identity: dict[str, str] = Depends(current_user)):
    if FIRESTORE is not None:
        ref = FIRESTORE.collection("bookings").document(booking_id)
        @firestore.transactional
        def cancel(transaction):
            snapshot = fs_transaction_get_one(transaction, ref)
            if snapshot is None or not snapshot.exists:
                raise HTTPException(404, "Không tìm thấy booking")
            row = snapshot.to_dict()
            if identity["role"] != "admin" and row.get("user_id") != identity["user_id"]:
                raise HTTPException(403, "Không có quyền hủy booking này")
            if row.get("status") != "CONFIRMED":
                raise HTTPException(409, "Chỉ hủy được booking CONFIRMED")
            transaction.update(ref, {"status": "CANCELLED"})
            event_ref = FIRESTORE.collection("booking_events").document()
            audit_ref = FIRESTORE.collection("audit").document()
            transaction.set(event_ref, {"booking_id": booking_id, "event": "CANCELLED", "detail": "Người dùng hủy booking", "at": stamp(now())})
            transaction.set(audit_ref, {"actor": identity["user_id"], "action": "BOOKING_CANCELLED", "detail": booking_id, "at": stamp(now())})
            return True
        cancel(FIRESTORE.transaction())
        return {"ok": True, "status": "CANCELLED"}
    with LOCK, db() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
        if not row:
            c.execute("ROLLBACK")
            raise HTTPException(404, "Không tìm thấy booking")
        if identity["role"] != "admin" and row["user_id"] != identity["user_id"]:
            c.execute("ROLLBACK")
            raise HTTPException(403, "Không có quyền hủy booking này")
        if row["status"] != "CONFIRMED":
            c.execute("ROLLBACK")
            raise HTTPException(409, "Chỉ hủy được booking CONFIRMED")
        c.execute("UPDATE bookings SET status='CANCELLED' WHERE id=? AND status='CONFIRMED'", (booking_id,))
        c.execute("INSERT INTO events(booking_id,event,at,detail) VALUES(?,?,?,?)", (booking_id, "CANCELLED", stamp(now()), "Người dùng hủy booking"))
        audit(c, identity["user_id"], "BOOKING_CANCELLED", booking_id)
        c.execute("COMMIT")
        return {"ok": True, "status": "CANCELLED"}


@app.post("/api/qr/validate")
def validate_qr(data: QRIn, identity: dict[str, str] = Depends(current_user)):
    payload = decode_token(data.token)
    if payload.get("room_id") != data.room_id:
        raise HTTPException(403, "QR không thuộc phòng này")
    if now() > parse_dt(payload.get("exp", "")):
        raise HTTPException(403, "QR đã hết hạn")
    if FIRESTORE is not None:
        booking_ref = FIRESTORE.collection("bookings").document(payload.get("booking_id", ""))
        room_ref = FIRESTORE.collection("rooms").document(str(data.room_id))

        @firestore.transactional
        def checkin(transaction):
            booking_snapshot = fs_transaction_get_one(transaction, booking_ref)
            if booking_snapshot is None or not booking_snapshot.exists:
                raise HTTPException(404, "Không tìm thấy booking phù hợp")
            row = booking_snapshot.to_dict()
            if row.get("room_id") != data.room_id:
                raise HTTPException(404, "Không tìm thấy booking phù hợp")
            if identity["role"] != "admin" and row.get("user_id") != identity["user_id"]:
                raise HTTPException(403, "QR không thuộc booking của tài khoản này")
            if row.get("status") != "CONFIRMED":
                raise HTTPException(409, "Booking không còn ở trạng thái CONFIRMED")
            if now() < parse_dt(row["start"]) - timedelta(minutes=15):
                raise HTTPException(403, "Check-in quá sớm")
            if now() > parse_dt(row["end"]) + timedelta(minutes=15):
                raise HTTPException(403, "Đã quá thời gian check-in")
            room_snapshot = fs_transaction_get_one(transaction, room_ref)
            if room_snapshot is None or not room_snapshot.exists:
                raise HTTPException(404, "Không tìm thấy phòng")
            transaction.update(booking_ref, {"status": "CHECKED_IN"})
            transaction.update(room_ref, {"occupied": True})
            event_ref = FIRESTORE.collection("booking_events").document()
            transaction.set(event_ref, {"booking_id": row["id"], "event": "CHECKED_IN", "at": stamp(now()), "detail": "QR hợp lệ; gửi lệnh bật đèn, quạt, loa"})
            audit_ref = FIRESTORE.collection("audit").document()
            transaction.set(audit_ref, {"actor": row["user_id"], "action": "CHECK_IN", "detail": row["id"], "at": stamp(now())})
            return row

        row = checkin(FIRESTORE.transaction())
        transport = publish_room_command(data.room_id, {"lamp": True, "fan": True, "speaker": True})
        if transport["transport"] != "mqtt" or not transport["published"]:
            fs_update_room(data.room_id, {"light_on": True, "fan_on": True, "speaker_on": True})
        return {"ok": True, "booking_id": row["id"], "status": "CHECKED_IN", "devices": {"lamp": True, "fan": True, "speaker": True}, **transport}
    with db() as c:
        row = c.execute("SELECT * FROM bookings WHERE id=?", (payload.get("booking_id"),)).fetchone()
        if not row or row["room_id"] != data.room_id:
            raise HTTPException(404, "Không tìm thấy booking phù hợp")
        if identity["role"] != "admin" and row["user_id"] != identity["user_id"]:
            raise HTTPException(403, "QR không thuộc booking của tài khoản này")
        if row["status"] != "CONFIRMED":
            raise HTTPException(409, "Booking không còn ở trạng thái CONFIRMED")
        if now() < parse_dt(row["start"]) - timedelta(minutes=15):
            raise HTTPException(403, "Check-in quá sớm")
        if now() > parse_dt(row["end"]) + timedelta(minutes=15):
            raise HTTPException(403, "Đã quá thời gian check-in")
        c.execute("BEGIN IMMEDIATE")
        c.execute("UPDATE bookings SET status='CHECKED_IN' WHERE id=? AND status='CONFIRMED'", (row["id"],))
        if c.execute("SELECT changes()").fetchone()[0] == 0:
            c.execute("ROLLBACK")
            raise HTTPException(409, "Booking đã được xử lý")
        c.execute("UPDATE rooms SET occupied=1 WHERE id=?", (data.room_id,))
        c.execute("INSERT INTO events(booking_id,event,at,detail) VALUES(?,?,?,?)", (row["id"], "CHECKED_IN", stamp(now()), "QR hợp lệ; bật thiết bị mô phỏng"))
        audit(c, row["user_id"], "CHECK_IN", row["id"])
        c.execute("COMMIT")
    transport = publish_room_command(data.room_id, {"lamp": True, "fan": True, "speaker": True})
    if transport["transport"] != "mqtt" or not transport["published"]:
        with db() as c:
            c.execute("UPDATE rooms SET light_on=1,fan_on=1,speaker_on=1 WHERE id=?", (data.room_id,))
    return {"ok": True, "booking_id": row["id"], "status": "CHECKED_IN", "devices": {"lamp": True, "fan": True, "speaker": True}, **transport}


@app.get("/api/qr/image")
def qr_image(token: str):
    import qrcode
    image = qrcode.make(token)
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    stream.seek(0)
    return StreamingResponse(stream, media_type="image/png")


@app.post("/api/iot/telemetry")
def telemetry(data: TelemetryIn):
    if FIRESTORE is not None:
        room = fs_room(data.room_id)
        if not room:
            raise HTTPException(404, "Không tìm thấy phòng")
        devices = data.devices or {"lamp": data.occupied, "fan": data.occupied, "speaker": data.occupied}
        updates = {"online": data.online, "occupied": data.occupied}
        updates["last_seen"] = stamp(now())
        for device, state in devices.items():
            column = DEVICE_COLUMNS.get(device)
            if not column:
                raise HTTPException(400, "Thiết bị hợp lệ: lamp, fan, speaker")
            updates[column] = state
        fs_update_room(data.room_id, updates)
        fs_add_audit("http-controller", "TELEMETRY", "room=%s; occupied=%s; online=%s; devices=%s" % (data.room_id, data.occupied, data.online, json.dumps(devices)))
        if not data.occupied:
            for booking in fs_bookings():
                if booking.get("room_id") == data.room_id and booking.get("status") == "CHECKED_IN" and booking.get("end", "") <= stamp(now()):
                    FIRESTORE.collection("bookings").document(booking["id"]).update({"status": "COMPLETED"})
        return {"ok": True, "room_id": data.room_id, "online": data.online, "occupied": data.occupied, "devices": devices, "transport": "http-simulation"}
    with db() as c:
        room = c.execute("SELECT id FROM rooms WHERE id=?", (data.room_id,)).fetchone()
        if not room:
            raise HTTPException(404, "Không tìm thấy phòng")
        devices = data.devices or {"lamp": data.occupied, "fan": data.occupied, "speaker": data.occupied}
        assignments = ["online=?", "occupied=?", "last_seen=?"]
        values = [int(data.online), int(data.occupied), stamp(now())]
        for device, state in devices.items():
            column = DEVICE_COLUMNS.get(device)
            if not column:
                raise HTTPException(400, "Thiết bị hợp lệ: lamp, fan, speaker")
            assignments.append(column + "=?")
            values.append(int(state))
        values.append(data.room_id)
        c.execute("UPDATE rooms SET " + ",".join(assignments) + " WHERE id=?", values)
        c.execute("INSERT INTO audit(actor,action,detail,at) VALUES(?,?,?,?)", ("http-controller", "TELEMETRY", f"room={data.room_id}; occupied={data.occupied}; online={data.online}; devices={json.dumps(devices)}", stamp(now())))
        if not data.occupied:
            c.execute("UPDATE bookings SET status='COMPLETED' WHERE room_id=? AND status='CHECKED_IN' AND end<=?", (data.room_id, stamp(now())))
        return {"ok": True, "room_id": data.room_id, "online": data.online, "occupied": data.occupied, "devices": devices, "transport": "http-simulation"}


@app.post("/api/admin/rooms/{room_id}/devices")
def command_devices(room_id: int, data: DeviceCommandIn, identity: dict[str, str] = Depends(current_user)):
    require_admin(identity["role"])
    if not data.devices or set(data.devices) - set(DEVICE_COLUMNS):
        raise HTTPException(400, "Thiết bị hợp lệ: lamp, fan, speaker")
    if FIRESTORE is not None:
        room = fs_room(room_id)
        if not room:
            raise HTTPException(404, "Không tìm thấy phòng")
        for device in data.devices:
            config = FIRESTORE.collection("devices").document("%s_%s" % (room_id, device)).get()
            if config.exists and not config.to_dict().get("enabled", True):
                raise HTTPException(409, "Thiết bị %s đang bị Admin vô hiệu hóa" % device)
    else:
        with db() as c:
            if not c.execute("SELECT id FROM rooms WHERE id=?", (room_id,)).fetchone():
                raise HTTPException(404, "Không tìm thấy phòng")
            placeholders = ",".join("?" for _ in data.devices)
            disabled = c.execute("SELECT device FROM devices WHERE room_id=? AND device IN (%s) AND enabled=0" % placeholders, (room_id, *data.devices)).fetchall()
            if disabled:
                raise HTTPException(409, "Thiết bị %s đang bị Admin vô hiệu hóa" % disabled[0]["device"])
    transport = publish_room_command(room_id, data.devices)
    if FIRESTORE is not None:
        if not fs_room(room_id):
            raise HTTPException(404, "Không tìm thấy phòng")
        if transport["transport"] != "mqtt" or not transport["published"]:
            fs_update_room(room_id, {DEVICE_COLUMNS[key]: value for key, value in data.devices.items()})
        fs_add_audit(identity["user_id"], "DEVICE_COMMAND", "room=%s; %s" % (room_id, json.dumps(data.devices)))
        return {"ok": True, "room_id": room_id, "devices": data.devices, **transport}
    with db() as c:
        if not c.execute("SELECT id FROM rooms WHERE id=?", (room_id,)).fetchone():
            raise HTTPException(404, "Không tìm thấy phòng")
        if transport["transport"] != "mqtt" or not transport["published"]:
            assignments = [DEVICE_COLUMNS[key] + "=?" for key in data.devices]
            c.execute("UPDATE rooms SET " + ",".join(assignments) + " WHERE id=?", (*[int(v) for v in data.devices.values()], room_id))
        audit(c, identity["user_id"], "DEVICE_COMMAND", "room=%s; %s" % (room_id, json.dumps(data.devices)))
    return {"ok": True, "room_id": room_id, "devices": data.devices, **transport}


GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
AI_TOOLS = [
    {"type": "function", "name": "get_room_catalog", "description": "Lấy loại phòng, sức chứa, tiện nghi, giá và trạng thái thiết bị từ database hiện tại.",
     "parameters": {"type": "object", "properties": {}, "required": []}},
    {"type": "function", "name": "find_available_rooms", "description": "Tìm phòng trống thật theo thời gian, số người và tiện nghi; chỉ dùng khi người dùng hỏi tình trạng còn phòng.",
     "parameters": {"type": "object", "properties": {"start": {"type": "string", "description": "ISO 8601 có múi giờ"}, "end": {"type": "string", "description": "ISO 8601 có múi giờ"}, "people": {"type": "integer"}, "amenities": {"type": "array", "items": {"type": "string"}}}, "required": ["start", "end", "people", "amenities"]}},
    {"type": "function", "name": "prepare_booking", "description": "Kiểm tra lại phòng còn trống và chuẩn bị đề xuất đặt phòng. Không tạo booking; phải chờ người dùng xác nhận ở lượt riêng.",
     "parameters": {"type": "object", "properties": {"start": {"type": "string", "description": "ISO 8601 có múi giờ"}, "end": {"type": "string", "description": "ISO 8601 có múi giờ"}, "people": {"type": "integer"}, "room_id": {"type": "integer"}, "amenities": {"type": "array", "items": {"type": "string"}}}, "required": ["start", "end", "people", "amenities"]}},
    {"type": "function", "name": "confirm_booking", "description": "Tạo booking chỉ sau khi người dùng xác nhận đề xuất ở lượt hiện tại.",
     "parameters": {"type": "object", "properties": {}, "required": []}},
    {"type": "function", "name": "get_my_bookings", "description": "Xem booking của tài khoản đang đăng nhập.",
     "parameters": {"type": "object", "properties": {}, "required": []}},
    {"type": "function", "name": "cancel_my_booking", "description": "Hủy booking của tài khoản đang đăng nhập, chỉ khi người dùng yêu cầu hủy rõ ràng ở lượt hiện tại.",
     "parameters": {"type": "object", "properties": {"booking_id": {"type": "string"}}, "required": ["booking_id"]}},
]


def _ai_room_summary(room: dict[str, Any]) -> dict[str, Any]:
    return {"id": room["id"], "name": room["name"], "capacity": room["capacity"],
            "amenities": room.get("amenities", []), "hourly_rate": room["hourly_rate"],
            "occupied": bool(room.get("occupied", False)), "online": bool(room.get("online", False)),
            "devices": {"lamp": bool(room.get("light_on", False)), "fan": bool(room.get("fan_on", False)), "speaker": bool(room.get("speaker_on", False))}}


def _ai_available(args: dict[str, Any]) -> tuple[datetime, datetime, list[dict[str, Any]]]:
    start, end = parse_dt(args["start"]), parse_dt(args["end"])
    people = int(args["people"])
    if people < 1 or people > 100:
        raise HTTPException(400, "Số người phải từ 1 đến 100")
    if start >= end or start <= now():
        raise HTTPException(400, "Ngày giờ đặt phòng phải nằm trong tương lai")
    minutes = (end - start).total_seconds() / 60
    if minutes < 15 or minutes > 12 * 60:
        raise HTTPException(400, "Thời lượng đặt phòng phải từ 15 phút đến 12 tiếng")
    choices = free_rooms(start, end, people, args.get("amenities", []))
    return start, end, choices


def _is_explicit_booking_confirmation(message: str) -> bool:
    return bool(re.fullmatch(r"\s*(?:(?:vâng|ừ|ok(?:ay)?|được|đồng ý|xác nhận|chốt|đặt luôn)(?:\s+(?:nhé|nha|đi|ạ|giúp tôi|giúp mình))?)[.!\s]*", message.casefold()))


def _ai_tool(name: str, args: dict[str, Any], identity: dict[str, str], state: dict[str, Any], message: str) -> Any:
    if name == "get_room_catalog":
        rooms_now = fs_rooms() if FIRESTORE is not None else rooms(identity)
        return [_ai_room_summary(room) for room in rooms_now]
    if name == "find_available_rooms":
        start, end, choices = _ai_available(args)
        return {"start": stamp(start), "end": stamp(end), "people": args["people"], "available": [_ai_room_summary(room) for room in choices[:8]]}
    if name == "prepare_booking":
        start, end, choices = _ai_available(args)
        room_id = args.get("room_id")
        if room_id is not None:
            choices = [room for room in choices if int(room["id"]) == int(room_id)]
        if not choices:
            return {"ok": False, "message": "Không có phòng phù hợp còn trống; đưa ra các lựa chọn khác hoặc hỏi người dùng đổi thời gian/tiện nghi."}
        room = choices[0]
        state["pending_booking"] = {"room_id": int(room["id"]), "start": stamp(start), "end": stamp(end), "people": int(args["people"]), "cost": round(room["hourly_rate"] * (end - start).total_seconds() / 3600)}
        pending = state["pending_booking"]
        return {"ok": True, "requires_confirmation": True, "booking": {**pending, "room_name": room["name"]}, "message": "Chỉ trình bày đề xuất và hỏi xác nhận. Chưa có booking nào được tạo."}
    if name == "confirm_booking":
        explicit_yes = _is_explicit_booking_confirmation(message)
        pending = state.get("pending_booking")
        if not explicit_yes or not pending:
            return {"ok": False, "message": "Chưa được phép tạo booking: cần có đề xuất đang chờ và lời xác nhận rõ ràng từ người dùng ở lượt này."}
        current_rooms = fs_rooms() if FIRESTORE is not None else rooms(identity)
        room_now = next((room for room in current_rooms if int(room["id"]) == int(pending["room_id"])), None)
        if room_now is None:
            state.pop("pending_booking", None)
            return {"ok": False, "message": "Phòng này không còn trong hệ thống. Hãy tìm phòng khác."}
        current_cost = round(room_now["hourly_rate"] * (parse_dt(pending["end"]) - parse_dt(pending["start"])).total_seconds() / 3600)
        if current_cost != pending["cost"]:
            pending["cost"] = current_cost
            return {"ok": False, "requires_confirmation": True, "message": "Giá phòng đã thay đổi. Báo giá mới cho người dùng và xin xác nhận lại; chưa tạo booking."}
        idem = "ai-chat-" + hashlib.sha256((identity["user_id"] + state["session_key"] + json.dumps(pending, sort_keys=True)).encode()).hexdigest()
        try:
            booking = create_booking(BookingIn(room_id=pending["room_id"], start=parse_dt(pending["start"]), end=parse_dt(pending["end"]), people=pending["people"], idempotency_key=idem), identity)
        except HTTPException as exc:
            state.pop("pending_booking", None)
            return {"ok": False, "message": str(exc.detail)}
        state.pop("pending_booking", None)
        state["created_booking"] = booking
        return {"ok": True, "booking_id": booking["id"], "room_id": booking["room_id"], "status": booking["status"], "cost": booking["cost"]}
    if name == "get_my_bookings":
        items = list_bookings(identity)
        return [{key: row.get(key) for key in ("id", "room_id", "start", "end", "status", "cost", "people")} for row in items[:10]]
    if name == "cancel_my_booking":
        if not re.search(r"\b(hủy|huy|cancel)\b", message.casefold()) or re.search(r"\b(không|đừng|chưa)\s+(?:hủy|huy|cancel)\b", message.casefold()):
            return {"ok": False, "message": "Chỉ hủy khi người dùng yêu cầu hủy rõ ràng ở lượt hiện tại."}
        try:
            return cancel_booking(args["booking_id"], identity)
        except HTTPException as exc:
            return {"ok": False, "message": str(exc.detail)}
    return {"ok": False, "message": "Function không được hỗ trợ."}


def gemini_chat_reply(message: str, identity: dict[str, str], session_id: str) -> Optional[dict[str, Any]]:
    if google_genai is None:
        return None
    cache_key = identity["user_id"] + ":" + hashlib.sha256(session_id.encode()).hexdigest()
    state = AI_CHAT.setdefault(cache_key, {"steps": [], "pending_booking": None, "session_key": cache_key})
    if state.get("pending_booking") and not _is_explicit_booking_confirmation(message):
        state.pop("pending_booking", None)
    if len(state["steps"]) > 32:
        state["steps"] = []
    state["steps"].append({"type": "user_input", "content": [{"type": "text", "text": message[:2000]}]})
    instructions = (
        "Bạn là trợ lý đặt phòng học Smart Study Room, trả lời tiếng Việt thân thiện, ngắn gọn. "
        "Luôn dùng công cụ để lấy giá/phòng/trạng thái thực; không tự đoán dữ liệu. Đọc các lượt trước để hỏi tiếp các thông tin còn thiếu. "
        "Thời gian hiện tại UTC: " + stamp(now()) + ". Múi giờ đặt phòng là Việt Nam UTC+07:00; gửi thời gian công cụ ISO 8601 có offset +07:00. Hỏi đủ ngày tương lai, giờ, thời lượng 15 phút-12 tiếng, số người và tiện nghi cần có. "
        "Ngày trong quá khứ hoặc thời điểm bắt đầu đã qua phải từ chối. Khi người dùng muốn đặt, gọi prepare_booking sau khi có đủ thông tin; chỉ hỏi xác nhận, tuyệt đối không khẳng định đã đặt trước khi confirm_booking trả thành công. "
        "Chỉ gọi confirm_booking khi user hiện tại trả lời rõ ràng xác nhận. Hủy chỉ theo yêu cầu rõ ràng. Không hiển thị booking của người khác. "
        "Giới thiệu phòng/giá bằng get_room_catalog; hỏi tình trạng còn phòng bằng find_available_rooms."
    )
    client = google_genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    interaction = client.interactions.create(model=GEMINI_MODEL, system_instruction=instructions, tools=AI_TOOLS, input=state["steps"], store=False, generation_config={"max_output_tokens": 400}, timeout=60.0)
    made_booking = None
    for _ in range(4):
        interaction_steps = interaction.steps or []
        state["steps"].extend([item.model_dump(mode="json", exclude_none=True) for item in interaction_steps])
        calls = [item for item in interaction_steps if item.type == "function_call"]
        if not calls:
            break
        tool_results = []
        for call in calls:
            try:
                args = call.arguments if isinstance(call.arguments, dict) else json.loads(call.arguments or "{}")
                result = _ai_tool(call.name, args, identity, state, message)
            except HTTPException as exc:
                result = {"ok": False, "message": str(exc.detail)}
            except Exception as exc:
                result = {"ok": False, "message": "Không xử lý được yêu cầu (%s)." % type(exc).__name__}
            tool_results.append({"type": "function_result", "name": call.name, "call_id": call.id, "result": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, default=str)}]})
            if call.name == "confirm_booking" and result.get("ok"):
                made_booking = state.pop("created_booking", None)
        state["steps"].extend(tool_results)
        try:
            interaction = client.interactions.create(model=GEMINI_MODEL, system_instruction=instructions, tools=AI_TOOLS, input=state["steps"], store=False, generation_config={"max_output_tokens": 400}, timeout=60.0)
        except Exception:
            if made_booking:
                return {"reply": "Đặt phòng thành công. Mã booking %s, phòng #%s, trạng thái %s." % (made_booking["id"][:8], made_booking["room_id"], made_booking["status"]), "booking": made_booking}
            raise
    if made_booking:
        return {"reply": "Đặt phòng thành công. Mã booking %s, phòng #%s, trạng thái %s." % (made_booking["id"][:8], made_booking["room_id"], made_booking["status"]), "booking": made_booking}
    answer = (interaction.output_text or "Mình chưa hiểu rõ yêu cầu. Bạn có thể nói ngày, giờ, thời lượng và số người cần phòng nhé.").strip()
    return {"reply": answer}


@app.post("/api/chat")
def chat(data: ChatIn, identity: dict[str, str] = Depends(current_user)):
    message = data.message.strip()
    state = CHAT.setdefault(identity["user_id"] + ":" + data.session_id, {})
    normalized = message.casefold()
    ai_key = identity["user_id"] + ":" + hashlib.sha256(data.session_id.encode()).hexdigest()
    ai_state = AI_CHAT.get(ai_key)
    if ai_state and ai_state.get("pending_booking") and not _is_explicit_booking_confirmation(message):
        ai_state.pop("pending_booking", None)

    # Short greetings and acknowledgements are fixed UI copy: answer locally, without an AI call.
    if re.fullmatch(r"\s*(xin chào|chào(?: bạn| ad)?|hello|hi|hey)[!.\s]*", normalized):
        return {"reply": "Chào bạn! 👋 Mình có thể giới thiệu phòng, báo giá hoặc giúp bạn tìm phòng phù hợp. Bạn muốn xem dịch vụ, loại phòng, bảng giá hay bắt đầu đặt phòng?"}
    if re.fullmatch(r"\s*(cảm ơn|cam on|cảm ơn nhé|thanks|thank you)[!.\s]*", normalized):
        return {"reply": "Rất vui được hỗ trợ bạn! Nếu cần, mình có thể xem loại phòng, giá hoặc tìm phòng còn trống."}
    if re.fullmatch(r"\s*(tìm phòng phù hợp|tìm phòng|bắt đầu đặt phòng)[!.\s]*", normalized):
        state["local_booking_flow"] = True
        return {"reply": "Mình sẽ tìm phòng phù hợp cho bạn. Trước tiên, bạn cần phòng cho bao nhiêu người? Sau đó cho mình ngày, giờ bắt đầu và thời lượng." , "needs": ["people", "date", "time", "duration"]}

    # Carry room type and amenity preferences across turns just like the other booking slots.
    available_rooms = fs_rooms() if FIRESTORE is not None else None
    if available_rooms is None:
        with db() as c:
            available_rooms = [dict(row) for row in c.execute("SELECT id,name,amenities FROM rooms").fetchall()]
            for room in available_rooms:
                room["amenities"] = json.loads(room["amenities"])
    amenity_aliases = {
        "máy chiếu": ("máy chiếu", "projector"), "bảng trắng": ("bảng trắng",),
        "điều hòa": ("điều hòa", "điều hoa", "máy lạnh"), "màn hình": ("màn hình", "man hinh"),
        "máy tính": ("máy tính", "computer", "pc"), "ổ cắm": ("ổ cắm", "o cam"),
    }
    requested_amenities = set(state.get("amenities", []))
    for amenity, aliases in amenity_aliases.items():
        if any(alias in normalized for alias in aliases):
            requested_amenities.add(amenity)
    if requested_amenities:
        state["amenities"] = sorted(requested_amenities)
    room_aliases = {"lab": ("lab", "phòng thí nghiệm"), "thảo luận": ("thảo luận", "thao luan"),
                    "nhóm": ("phòng nhóm", "phòng cho nhóm"), "học": ("phòng học",)}
    room_filter = state.get("room_filter")
    code_match = re.search(r"\b([a-d]\d{3})\b", normalized)
    detected_filter = code_match.group(1).upper() if code_match else None
    if not detected_filter:
        for key, aliases in room_aliases.items():
            if any(alias in normalized for alias in aliases):
                detected_filter = key
                break
    if detected_filter:
        room_filter = detected_filter
        state["room_filter"] = room_filter

    if re.search(r"(trạng thái|tình trạng).*(booking|đặt phòng)|booking.*(của tôi|nào|trạng thái)|đặt phòng của tôi", normalized):
        state.pop("pending_confirm", None)
        items = [b for b in fs_bookings() if b.get("user_id") == identity["user_id"]] if FIRESTORE is not None else list_bookings(identity)
        items = sorted(items, key=lambda booking: booking.get("created", ""), reverse=True)[:5]
        if not items:
            return {"reply": "Bạn chưa có booking nào."}
        lines = ["• #%s · phòng %s · %s · %s" % (b["id"][:8], b["room_id"], b["status"], parse_dt(b["start"]).astimezone().strftime("%d/%m %H:%M")) for b in items]
        return {"reply": "Booking gần đây của bạn:\n" + "\n".join(lines)}

    if re.search(r"(hủy|huy).*(booking|đặt phòng|phòng)|((booking|đặt phòng).*(hủy|huy))", normalized):
        state.pop("pending_confirm", None)
        items = [b for b in fs_bookings() if b.get("user_id") == identity["user_id"] and b.get("status") == "CONFIRMED"] if FIRESTORE is not None else [b for b in list_bookings(identity) if b.get("status") == "CONFIRMED"]
        short_id = re.search(r"\b([0-9a-f]{8})\b", normalized)
        if short_id:
            items = [b for b in items if b["id"].startswith(short_id.group(1))]
        if len(items) == 1:
            try:
                cancel_booking(items[0]["id"], identity)
                return {"reply": "Đã hủy booking #%s." % items[0]["id"][:8]}
            except HTTPException as exc:
                return {"reply": "Mình chưa thể hủy booking: " + str(exc.detail)}
        if items:
            return {"reply": "Bạn có nhiều booking chưa check-in. Hãy gửi 8 ký tự đầu của mã booking muốn hủy:\n" + "\n".join("• #%s · phòng %s · %s" % (b["id"][:8], b["room_id"], parse_dt(b["start"]).astimezone().strftime("%d/%m %H:%M")) for b in items[:6])}
        return {"reply": "Hiện không có booking CONFIRMED nào để hủy."}

    if re.search(r"(dịch vụ|bạn.*giúp.*gì|bạn làm được gì|có thể giúp|có những chức năng gì|cách đặt|check.?in.*qr)", normalized):
        state.pop("pending_confirm", None)
        return {"reply": "Mình có thể giúp bạn:\n• Giới thiệu phòng, tiện nghi và giá hiện tại.\n• Tìm phòng còn trống theo số người, ngày, giờ và thời lượng.\n• Tạo booking và cấp QR check-in.\n• Điều khiển thiết bị phòng gồm đèn, quạt và loa. Hệ thống nhận telemetry/cập nhật trạng thái qua MQTT; nếu chưa cấu hình broker thì demo mô phỏng qua HTTP.\n\nBạn muốn xem dịch vụ thiết bị, loại phòng, bảng giá hay bắt đầu tìm phòng?"}

    if re.search(r"(bảng giá|chi phí|mức phí|bao nhiêu (?:tiền|đồng|một giờ)|giá\s*(?:phòng|bao nhiêu|thế nào|ra sao)|phòng\s+[a-d]\d{3}\s+giá)", normalized):
        if FIRESTORE is not None:
            rows = sorted(fs_rooms(), key=lambda room: room.get("hourly_rate", 0))
            room_code = re.search(r"\b([a-d]\d{3})\b", normalized)
            if room_code:
                rows = [room for room in rows if room_code.group(1).upper() in room.get("name", "").upper()]
            state.pop("pending_confirm", None)
            if not rows:
                return {"reply": "Mình không tìm thấy phòng đó. Bạn có thể nhắn ‘bảng giá’ để xem giá các phòng hiện có."}
            lines = [f"• {r['name']}: {r['hourly_rate']:,}₫/giờ · tối đa {r['capacity']} người" for r in rows]
            return {"reply": "Giá phòng hiện tại (theo cấu hình hệ thống):\n" + "\n".join(lines) + "\nBạn muốn mình tìm phòng còn trống vào ngày, giờ và thời lượng nào?"}
        with db() as c:
            rows = c.execute("SELECT name,capacity,amenities,hourly_rate FROM rooms ORDER BY hourly_rate").fetchall()
        room_code = re.search(r"\b([a-d]\d{3})\b", normalized)
        if room_code:
            rows = [r for r in rows if room_code.group(1).upper() in r["name"].upper()]
        state.pop("pending_confirm", None)
        if not rows:
            return {"reply": "Mình không tìm thấy phòng đó. Bạn có thể nhắn ‘bảng giá’ để xem giá các phòng hiện có."}
        lines = [f"• {r['name']}: {r['hourly_rate']:,}₫/giờ · tối đa {r['capacity']} người" for r in rows]
        return {"reply": "Giá phòng hiện tại (theo cấu hình hệ thống):\n" + "\n".join(lines) + "\nBạn muốn mình tìm phòng còn trống vào ngày, giờ và thời lượng nào?"}

    # Informational room questions use the database as the source of truth.
    if re.search(r"(các loại|loại.*phòng|phòng.*loại nào|có.*phòng.*(?:gì|nào)|giới thiệu.*phòng|thông tin.*phòng|danh sách phòng|phòng.*(?:tiện nghi|sức chứa))", normalized):
        state.pop("pending_confirm", None)
        if FIRESTORE is not None:
            rows = sorted(fs_rooms(), key=lambda room: room.get("capacity", 0))
            descriptions = [f"• {r['name']}: tối đa {r['capacity']} người; tiện nghi {', '.join(r.get('amenities', []))}; {r['hourly_rate']:,}₫/giờ" for r in rows]
            return {"reply": "Hiện có các phòng sau (thông tin lấy từ hệ thống):\n" + "\n".join(descriptions) + "\nBạn muốn đặt phòng nào? Hãy cho mình biết số người, ngày, giờ bắt đầu và thời lượng."}
        with db() as c:
            rows = c.execute("SELECT name,capacity,amenities,hourly_rate FROM rooms ORDER BY capacity").fetchall()
        descriptions = [f"• {r['name']}: tối đa {r['capacity']} người; tiện nghi {', '.join(json.loads(r['amenities']))}; {r['hourly_rate']:,}₫/giờ" for r in rows]
        return {"reply": "Hiện có các phòng sau (thông tin lấy từ hệ thống):\n" + "\n".join(descriptions) + "\nBạn muốn đặt phòng nào? Hãy cho mình biết số người, ngày, giờ bắt đầu và thời lượng."}

    # Rule based answers above stay local. Call Gemini only for free-form requests or
    # an AI conversation already in progress; structured booking slots use our parser.
    has_booking_slot = bool(
        re.search(r"\b\d{1,2}\s*(?:người|nguoi|pax)\b", normalized)
        or re.search(r"\b(?:hôm nay|hom nay|ngày mai|ngay mai|mai|ngày kia|ngay kia|mốt|mot|20\d{2}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[/-]\d{1,2}(?:[/-]20\d{2})?|thứ\s*[2-7]|chủ nhật)\b", normalized)
        or re.search(r"\b\d{1,2}\s*(?:h|:)\s*\d{0,2}\b", normalized)
        or re.search(r"\d+(?:[.,]\d+)?\s*(?:tiếng|giờ|hours|phút|phut|min|minutes)", normalized)
    )
    if os.getenv("GEMINI_API_KEY", "").strip() and not state.get("local_booking_flow") and (bool(ai_state and ai_state.get("steps")) or _is_explicit_booking_confirmation(message) or not has_booking_slot):
        try:
            ai_reply = gemini_chat_reply(message, identity, data.session_id)
            if ai_reply is not None:
                return ai_reply
        except Exception as exc:
            # Keep local responses available when the external AI service is unavailable.
            print("Gemini chat unavailable (%s): %s" % (type(exc).__name__, str(exc)), flush=True)

    if has_booking_slot:
        state["local_booking_flow"] = True

    people_match = re.search(r"\b(\d{1,2})\s*(?:người|nguoi|pax)\b", normalized)
    if people_match:
        state["people"] = int(people_match.group(1))

    local_today = now().astimezone().date()
    requested_date = None
    relative = re.search(r"\b(hôm nay|hom nay|ngày mai|ngay mai|mai|ngày kia|ngay kia|mốt|mot)\b", normalized)
    if relative:
        word = relative.group(1)
        offset = 0 if word in ("hôm nay", "hom nay") else (2 if word in ("ngày kia", "ngay kia", "mốt", "mot") else 1)
        requested_date = local_today + timedelta(days=offset)
    else:
        iso_match = re.search(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b", normalized)
        dmy_match = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](20\d{2})\b", normalized)
        short_dmy_match = re.search(r"\b(\d{1,2})[/-](\d{1,2})(?![/-]\d{2,4})\b", normalized)
        vn_match = re.search(r"ngày\s+(\d{1,2})\s+tháng\s+(\d{1,2})(?:\s+năm\s+(20\d{2}))?", normalized)
        weekday_match = re.search(r"\b(thứ\s*([2-7])|chủ nhật)\b", normalized)
        try:
            if iso_match:
                requested_date = date(int(iso_match.group(1)), int(iso_match.group(2)), int(iso_match.group(3)))
            elif dmy_match:
                requested_date = date(int(dmy_match.group(3)), int(dmy_match.group(2)), int(dmy_match.group(1)))
            elif short_dmy_match:
                requested_date = date(local_today.year, int(short_dmy_match.group(2)), int(short_dmy_match.group(1)))
            elif vn_match:
                requested_date = date(int(vn_match.group(3) or local_today.year), int(vn_match.group(2)), int(vn_match.group(1)))
            elif weekday_match:
                target_weekday = 6 if weekday_match.group(1) == "chủ nhật" else int(weekday_match.group(2)) - 2
                offset = (target_weekday - local_today.weekday()) % 7 or 7
                requested_date = local_today + timedelta(days=offset)
        except ValueError:
            return {"reply": "Ngày bạn nhập không hợp lệ. Hãy ghi ngày/tháng/năm, ví dụ 05/10/2026.", "needs": ["date"]}
    if requested_date:
        if requested_date < local_today:
            state.pop("date", None); state.pop("start", None); state.pop("end", None); state.pop("pending_confirm", None)
            return {"reply": "Ngày đó đã qua nên mình không thể tạo booking. Hãy chọn hôm nay hoặc một ngày trong tương lai.", "needs": ["date"]}
        state["date"] = requested_date.isoformat()

    time_match = re.search(r"\b(?:lúc\s*)?(\d{1,2})\s*(?:h|:)(\d{2})?\b", normalized)
    if time_match:
        hour, minute = int(time_match.group(1)), int(time_match.group(2) or 0)
        if hour > 23 or minute > 59:
            return {"reply": "Giờ không hợp lệ. Hãy nhập giờ từ 00:00 đến 23:59.", "needs": ["time"]}
        state["time"] = f"{hour:02d}:{minute:02d}"

    duration_match = re.search(r"(?:trong\s*)?(?:(\d+(?:[.,]\d+)?)\s*(tiếng|giờ|hours)(?:\s*(?:và\s*)?(\d+)\s*(?:phút|phut))?|(\d+)\s*(phút|phut|min|minutes))", normalized)
    if duration_match:
        if duration_match.group(1):
            minutes = round(float(duration_match.group(1).replace(",", ".")) * 60) + int(duration_match.group(3) or 0)
        else:
            minutes = int(duration_match.group(4))
        if minutes < 15 or minutes > 12 * 60:
            return {"reply": "Thời lượng đặt phòng phải từ 15 phút đến 12 tiếng. Bạn muốn dùng phòng bao lâu?", "needs": ["duration"]}
        state["duration_minutes"] = minutes

    if not state.get("people"):
        return {"reply": "Bạn cần phòng cho bao nhiêu người?", "needs": ["people"]}
    if not state.get("date"):
        return {"reply": "Bạn muốn đặt phòng ngày nào? Hãy ghi ngày/tháng/năm hoặc nói hôm nay/ngày mai.", "needs": ["date"]}
    if not state.get("time"):
        return {"reply": "Bạn muốn bắt đầu lúc mấy giờ? Ví dụ 14:30.", "needs": ["time"]}
    if not state.get("duration_minutes"):
        return {"reply": "Bạn muốn sử dụng phòng trong bao lâu? Ví dụ 90 phút, 2 tiếng hoặc 2 tiếng 30 phút.", "needs": ["duration"]}

    start_local = datetime.combine(date.fromisoformat(state["date"]), datetime.strptime(state["time"], "%H:%M").time()).astimezone()
    if start_local <= now().astimezone():
        state.pop("time", None); state.pop("start", None); state.pop("end", None); state.pop("pending_confirm", None)
        return {"reply": "Giờ bắt đầu đó đã qua. Hãy chọn một giờ trong tương lai (hoặc đổi sang ngày khác).", "needs": ["time"]}
    state["start"] = start_local.astimezone(timezone.utc)
    state["end"] = state["start"] + timedelta(minutes=state["duration_minutes"])
    choices = free_rooms(state["start"], state["end"], state["people"], state.get("amenities"))
    if room_filter:
        choices = [room for room in choices if (room_filter.casefold() in room.get("name", "").casefold())]
    if not choices:
        requirements = []
        if state.get("amenities"):
            requirements.append("tiện nghi " + ", ".join(state["amenities"]))
        if room_filter:
            requirements.append("loại/phòng " + room_filter)
        return {"reply": "Mình chưa tìm thấy phòng trống phù hợp" + (" với " + " và ".join(requirements) if requirements else "") + ". Bạn thử đổi tiện nghi, loại phòng, giờ hoặc số người nhé.", "rooms": []}
    room = choices[0]
    duration_hours = (state["end"] - state["start"]).total_seconds() / 3600
    state.update({"room_id": room["id"], "cost": round(room["hourly_rate"] * duration_hours)})
    signature = (state["people"], state["date"], state["time"], state["duration_minutes"], tuple(state.get("amenities", [])), room_filter)
    if state.get("context_signature") is not None and state["context_signature"] != signature:
        state.pop("pending_confirm", None)
    state["context_signature"] = signature
    start_local, end_local = state["start"].astimezone(), state["end"].astimezone()
    summary = (f"{room['name']} cho {state['people']} người, ngày {start_local:%d/%m/%Y}, {start_local:%H:%M}–{end_local:%H:%M} "
               f"dự kiến {state['cost']:,}₫. Phòng đủ sức chứa, còn trống và có giá thấp nhất trong các lựa chọn.")
    if state.get("amenities"):
        summary += " Đáp ứng tiện nghi: " + ", ".join(state["amenities"]) + "."
    if re.search(r"\b(xác nhận|đồng ý|đặt phòng|confirm)\b", message, re.I):
        if state.get("pending_confirm"):
            try:
                booking = create_booking(BookingIn(room_id=room["id"], start=state["start"], end=state["end"], people=state["people"], idempotency_key="chat-" + uuid.uuid4().hex), identity)
            except HTTPException as exc:
                return {"reply": "Không thể tạo booking: " + str(exc.detail), "rooms": choices}
            state.clear()
            return {"reply": "Đã tạo booking " + booking["id"][:8] + ". Trạng thái CONFIRMED.", "booking": booking}
        state["pending_confirm"] = True
        return {"reply": "Trước khi đặt, hãy xác nhận đề xuất: " + summary + " Nhắn ‘xác nhận’ để tiếp tục.", "rooms": choices}
    state["pending_confirm"] = True
    return {"reply": "Đề xuất: " + summary + " Nếu phù hợp, nhắn ‘xác nhận’ để đặt.", "rooms": choices}


@app.get("/api/admin/audit")
def get_audit(identity: dict[str, str] = Depends(current_user)):
    require_admin(identity["role"])
    if FIRESTORE is not None:
        items = [doc.to_dict() for doc in FIRESTORE.collection("audit").stream()]
        return sorted(items, key=lambda item: item.get("at", ""), reverse=True)[:200]
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 200").fetchall()]


@app.get("/api/admin/devices")
def admin_devices(identity: dict[str, str] = Depends(current_user)):
    require_admin(identity["role"])
    if FIRESTORE is not None:
        return sorted([doc.to_dict() for doc in FIRESTORE.collection("devices").stream()], key=lambda row: (row.get("room_id", 0), row.get("device", "")))
    with db() as c:
        result = []
        for row in c.execute("SELECT d.*,r." + ",r.".join(DEVICE_COLUMNS.values()) + " FROM devices d JOIN rooms r ON r.id=d.room_id ORDER BY d.room_id,d.device").fetchall():
            item = dict(row)
            item["on"] = bool(item.pop(DEVICE_COLUMNS[item["device"]]))
            item["enabled"] = bool(item["enabled"])
            result.append(item)
        return result


@app.patch("/api/admin/devices/{room_id}/{device}")
def configure_device(room_id: int, device: str, values: DeviceConfigIn, identity: dict[str, str] = Depends(current_user)):
    require_admin(identity["role"])
    if device not in DEVICE_COLUMNS:
        raise HTTPException(404, "Không tìm thấy loại thiết bị")
    updates = values.model_dump(exclude_unset=True)
    if not updates or ("name" in updates and (not updates["name"].strip() or len(updates["name"]) > 40)):
        raise HTTPException(400, "Cần tên thiết bị hợp lệ hoặc trạng thái enabled")
    if FIRESTORE is not None:
        ref = FIRESTORE.collection("devices").document("%s_%s" % (room_id, device))
        if not ref.get().exists:
            raise HTTPException(404, "Không tìm thấy thiết bị")
        ref.update(updates)
        fs_add_audit(identity["user_id"], "DEVICE_CONFIG_UPDATED", "room=%s; device=%s; %s" % (room_id, device, json.dumps(updates, ensure_ascii=False)))
    else:
        with db() as c:
            columns = ",".join(key + "=?" for key in updates)
            result = c.execute("UPDATE devices SET " + columns + " WHERE room_id=? AND device=?", (*updates.values(), room_id, device))
            if result.rowcount == 0:
                raise HTTPException(404, "Không tìm thấy thiết bị")
            audit(c, identity["user_id"], "DEVICE_CONFIG_UPDATED", "room=%s; device=%s; %s" % (room_id, device, json.dumps(updates, ensure_ascii=False)))
    return {"ok": True, "room_id": room_id, "device": device, **updates}


@app.patch("/api/admin/rooms/{room_id}")
def update_room(room_id: int, values: dict[str, Any], identity: dict[str, str] = Depends(current_user)):
    require_admin(identity["role"])
    allowed = {"name", "capacity", "amenities", "hourly_rate"}
    if not values or set(values) - allowed:
        raise HTTPException(400, "Chỉ cập nhật name, capacity, amenities, hourly_rate")
    if "amenities" in values and not isinstance(values["amenities"], list):
        raise HTTPException(400, "amenities phải là danh sách")
    if ("capacity" in values and int(values["capacity"]) < 1) or ("hourly_rate" in values and int(values["hourly_rate"]) < 0):
        raise HTTPException(400, "Sức chứa hoặc giá không hợp lệ")
    if FIRESTORE is not None:
        if not fs_update_room(room_id, values):
            raise HTTPException(404, "Không tìm thấy phòng")
        fs_add_audit("admin", "ROOM_UPDATED", "room=" + str(room_id))
        return {"ok": True, "room_id": room_id}
    update = {k: json.dumps(v, ensure_ascii=False) if k == "amenities" else v for k, v in values.items()}
    clause = ",".join(k + "=?" for k in update)
    with db() as c:
        result = c.execute("UPDATE rooms SET " + clause + " WHERE id=?", (*update.values(), room_id))
        if result.rowcount == 0:
            raise HTTPException(404, "Không tìm thấy phòng")
        audit(c, "admin", "ROOM_UPDATED", "room=" + str(room_id))
        return {"ok": True, "room_id": room_id}


@app.get("/api/admin/bookings")
def admin_bookings(identity: dict[str, str] = Depends(current_user)):
    require_admin(identity["role"])
    if FIRESTORE is not None:
        rooms_by_id = {r["id"]: r["name"] for r in fs_rooms()}
        items = [dict(b, room_name=rooms_by_id.get(b.get("room_id"), "Phòng")) for b in fs_bookings()]
        return sorted(items, key=lambda booking: booking.get("created", ""), reverse=True)
    with db() as c:
        return [dict(r) for r in c.execute("SELECT b.*,r.name room_name FROM bookings b JOIN rooms r ON r.id=b.room_id ORDER BY b.created DESC").fetchall()]
