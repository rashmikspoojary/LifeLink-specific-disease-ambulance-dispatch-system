from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_file
import mysql.connector
from flask_socketio import SocketIO, emit, join_room
from datetime import datetime
import smtplib
import re
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
import os
import secrets
import base64
import traceback
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.exceptions import HTTPException



app = Flask(__name__)
app.secret_key = os.getenv("LIFELINK_SECRET_KEY", "lifelink_secret_key")
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.config['SESSION_COOKIE_PARTITIONED'] = False
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# Compatibility patch for 'partitioned' cookie error in some Flask/Werkzeug environments
from flask.wrappers import Response
original_set_cookie = Response.set_cookie
def patched_set_cookie(self, *args, **kwargs):
    kwargs.pop('partitioned', None)
    return original_set_cookie(self, *args, **kwargs)
Response.set_cookie = patched_set_cookie

original_delete_cookie = Response.delete_cookie
def patched_delete_cookie(self, *args, **kwargs):
    kwargs.pop('partitioned', None)
    return original_delete_cookie(self, *args, **kwargs)
Response.delete_cookie = patched_delete_cookie

EMAIL_ADDRESS = os.getenv("LIFELINK_EMAIL_ADDRESS", "lifelinkresq@gmail.com")
EMAIL_PASSWORD = os.getenv("LIFELINK_EMAIL_PASSWORD", "")
DOCTOR_CONTACT = os.getenv("LIFELINK_DOCTOR_CONTACT", "7204212318")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER", "")
DEFAULT_ADMIN_NAME = os.getenv("LIFELINK_DEFAULT_ADMIN_NAME", "Admin")
DEFAULT_ADMIN_EMAIL = os.getenv("LIFELINK_DEFAULT_ADMIN_EMAIL", "admin@lifelink.com").strip().lower()
DEFAULT_ADMIN_PASSWORD = os.getenv("LIFELINK_DEFAULT_ADMIN_PASSWORD", "admin123")
DEFAULT_DRIVER_PASSWORD = os.getenv("LIFELINK_DEFAULT_DRIVER_PASSWORD", "driver123")

ACTIVE_BOOKING_STATUSES = ("requested", "pending", "accepted", "on-the-way", "reached")
TRACKABLE_BOOKING_STATUSES = ("requested", "pending", "accepted", "on-the-way", "reached", "completed")

# ---------------- DATABASE ----------------
DB_CONFIG = {
    "host": os.getenv("LIFELINK_DB_HOST", "localhost"),
    "user": os.getenv("LIFELINK_DB_USER", "root"),
    "password": os.getenv("LIFELINK_DB_PASSWORD", "tiger"),
    "database": os.getenv("LIFELINK_DB_NAME", "lifelink"),
}

db = None
_global_cursor = None

class ThreadSafeCursor:
    def __getattr__(self, name):
        from flask import g, has_request_context
        # Use per-request cursor if inside a Flask request
        if has_request_context():
            if 'db_cursor' not in g:
                if not ensure_db_connection():
                    raise mysql.connector.Error("Database connection failed")
                g.db_cursor = db.cursor(dictionary=True)
            return getattr(g.db_cursor, name)
        
        # Fallback for background/startup tasks
        global _global_cursor
        if _global_cursor is None:
            if not ensure_db_connection():
                raise mysql.connector.Error("Database connection failed")
        return getattr(_global_cursor, name)

cursor = ThreadSafeCursor()
schema_initialized = False


def ensure_db_connection():
    global db, _global_cursor

    try:
        if db is not None:
            db.ping(reconnect=True, attempts=1, delay=0)
            if db.is_connected():
                if _global_cursor is None:
                    _global_cursor = db.cursor(dictionary=True)
                return True
    except Exception:
        pass

    try:
        db = mysql.connector.connect(**DB_CONFIG)
        _global_cursor = db.cursor(dictionary=True)
        return True
    except mysql.connector.Error as e:
        db = None
        _global_cursor = None
        print("Database connection error:", e)
        return False


@app.teardown_appcontext
def close_request_cursor(error):
    from flask import g
    if 'db_cursor' in g:
        try:
            g.db_cursor.close()
        except Exception:
            pass


def retry_db_once(operation, fallback=None):
    try:
        return operation()
    except mysql.connector.Error:
        if not ensure_db_connection():
            return fallback
        try:
            return operation()
        except mysql.connector.Error:
            return fallback


def parse_coordinate(value):
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return round(float(raw), 7)
    except (TypeError, ValueError):
        return None


def calculate_distance_km(lat1, lon1, lat2, lon2):
    from math import radians, sin, cos, sqrt, atan2

    if None in {lat1, lon1, lat2, lon2}:
        return None

    try:
        lat1, lon1, lat2, lon2 = map(float, (lat1, lon1, lat2, lon2))
    except (ValueError, TypeError):
        return None

    earth_radius_km = 6371
    d_lat = radians(lat2 - lat1)
    d_lon = radians(lon2 - lon1)
    a = (
        sin(d_lat / 2) ** 2 +
        cos(radians(lat1)) * cos(radians(lat2)) * sin(d_lon / 2) ** 2
    )
    c = 2 * atan2(sqrt(a), sqrt(1 - a))
    return earth_radius_km * c


def estimate_arrival_minutes(distance_km):
    if distance_km is None:
        return None
    return max(5, int(round((distance_km / 35) * 60)))


def driver_required():
    return session.get("role") == "driver" and bool(session.get("driver_id"))


def check_timeout_and_reassign(booking_id):
    cursor.execute("SELECT id, status, assigned_at, rejected_drivers, driver_id, user_id, pickup_latitude, pickup_longitude, pickup_location, recommended_ambulance_type FROM bookings WHERE id=%s", (booking_id,))
    booking = cursor.fetchone()
    if not booking or booking["status"] != "pending":
        return

    assigned_at = booking["assigned_at"]
    if not assigned_at:
        return

    time_diff = (datetime.now() - assigned_at).total_seconds()
    if time_diff >= 30:
        # Timeout occurred! Reassign
        rejected_list = booking["rejected_drivers"].split(',') if booking["rejected_drivers"] else []
        if booking["driver_id"] and str(booking["driver_id"]) not in rejected_list:
            rejected_list.append(str(booking["driver_id"]))
            
        new_rejected_str = ",".join(rejected_list)
        cursor.execute("UPDATE bookings SET rejected_drivers=%s WHERE id=%s", (new_rejected_str, booking_id))
        
        # Free up the old driver/ambulance
        if booking["driver_id"]:
            cursor.execute("UPDATE drivers SET status='available' WHERE id=%s", (booking["driver_id"],))
        
        db.commit()

        # Reassign to next driver
        dispatch_result = assign_hospital_dispatch(
            booking_id,
            booking["user_id"],
            pickup_latitude=booking.get("pickup_latitude"),
            pickup_longitude=booking.get("pickup_longitude"),
            pickup_location_text=booking.get("pickup_location", ""),
            recommended_ambulance_type=booking.get("recommended_ambulance_type"),
            rejected_drivers_list=[int(d) for d in rejected_list if d.strip()]
        )
        
        if not dispatch_result.get("assignment"):
            cursor.execute("UPDATE bookings SET status='failed' WHERE id=%s", (booking_id,))
            db.commit()


def booking_keeps_resources_busy(status):
    return (status or "").strip().lower() in ACTIVE_BOOKING_STATUSES


def booking_status_label(status):
    status_value = (status or "").strip().lower()
    labels = {
        "requested": "Pending Assignment",
        "pending": "Pending Driver Acceptance",
        "accepted": "Accepted",
        "on-the-way": "On The Way",
        "reached": "Reached",
        "completed": "Completed",
        "rejected": "Rejected",
    }
    return labels.get(status_value, status_value.title() or "Unknown")


def get_recommended_ambulance_type(disease_type="", emergency_level="medium", medical_notes=""):
    combined_text = " ".join(
        [
            str(disease_type or "").strip().lower(),
            str(emergency_level or "").strip().lower(),
            str(medical_notes or "").strip().lower(),
        ]
    )

    ventilator_keywords = {"ventilator", "breathing", "breath", "asthma", "oxygen", "respiratory", "lungs"}
    icu_keywords = {"heart", "cardiac", "stroke", "brain", "icu", "critical", "seizure", "unconscious"}
    trauma_keywords = {"accident", "trauma", "fracture", "injury", "bleeding", "burn"}

    if any(keyword in combined_text for keyword in ventilator_keywords):
        return "Ventilator"
    if any(keyword in combined_text for keyword in icu_keywords):
        return "ICU"
    if any(keyword in combined_text for keyword in trauma_keywords):
        return "Trauma"
    if (emergency_level or "").strip().lower() == "critical":
        return "ICU"
    return "Basic"


def ambulance_type_preference_score(ambulance_type, recommended_type):
    normalized_type = str(ambulance_type or "").strip().lower()
    normalized_recommended = str(recommended_type or "").strip().lower()

    if not normalized_recommended:
        return 0
    if normalized_type == normalized_recommended:
        return 0
    if normalized_recommended == "trauma" and normalized_type == "icu":
        return 1
    if normalized_recommended == "icu" and normalized_type == "ventilator":
        return 1
    if normalized_recommended == "basic":
        return 1
    return 2


def select_nearest_hospital(user_latitude=None, user_longitude=None, user_location_text=""):
    cursor.execute(
        """
        SELECT
            id,
            name,
            location_text,
            latitude,
            longitude,
            emergency_contact
        FROM hospitals
        ORDER BY id ASC
        """
    )
    hospitals = cursor.fetchall()
    if not hospitals:
        return None

    best_hospital = None
    best_distance = None
    normalized_location = " ".join(
        token for token in re.split(r"[^a-z0-9]+", str(user_location_text or "").lower()) if len(token) > 2
    )
    user_tokens = set(normalized_location.split())

    for hospital in hospitals:
        distance_km = None
        if user_latitude is not None and user_longitude is not None:
            distance_km = calculate_distance_km(
                user_latitude,
                user_longitude,
                float(hospital["latitude"]) if hospital.get("latitude") is not None else None,
                float(hospital["longitude"]) if hospital.get("longitude") is not None else None,
            )
        elif user_tokens:
            hospital_text = " ".join(
                token for token in re.split(
                    r"[^a-z0-9]+",
                    " ".join([
                        str(hospital.get("name") or ""),
                        str(hospital.get("location_text") or "")
                    ]).lower()
                ) if len(token) > 2
            )
            hospital_tokens = set(hospital_text.split())
            overlap = len(user_tokens.intersection(hospital_tokens))
            distance_km = 1000 - overlap
        if distance_km is None:
            distance_km = 999999

        if best_hospital is None or distance_km < best_distance:
            best_hospital = hospital
            best_distance = distance_km

    if not best_hospital:
        return None

    best_hospital["distance_km"] = None if best_distance == 999999 else round(best_distance, 2)
    return best_hospital


def assign_hospital_dispatch(
    booking_id,
    user_id,
    pickup_latitude=None,
    pickup_longitude=None,
    pickup_location_text="",
    recommended_ambulance_type=None,
    rejected_drivers_list=None
):
    if rejected_drivers_list is None:
        rejected_drivers_list = []
    hospital = select_nearest_hospital(pickup_latitude, pickup_longitude, pickup_location_text)
    if not hospital:
        return {"hospital": None, "assignment": None, "reason": "no_hospital"}

    cursor.execute(
        "UPDATE bookings SET hospital_id=%s WHERE id=%s",
        (hospital["id"], booking_id)
    )

    cursor.execute(
        """
        SELECT
            a.id,
            a.vehicle_number,
            a.type,
            a.current_latitude,
            a.current_longitude
        FROM ambulances a
        WHERE a.hospital_id=%s
          AND LOWER(TRIM(a.status))='available'
        ORDER BY a.id ASC
        """,
        (hospital["id"],)
    )
    ambulance_options = cursor.fetchall()
    if not ambulance_options:
        db.commit()
        return {"hospital": hospital, "assignment": None, "reason": "no_resource_in_hospital"}

    best_option = None
    best_distance = None
    best_type_score = None

    def fetch_driver_for_ambulance(ambulance_id):
        base_query = "SELECT id, name, phone FROM drivers WHERE LOWER(TRIM(status))='available'"
        params = []
        
        exclude_clause = ""
        if rejected_drivers_list:
            exclude_clause = f" AND id NOT IN ({','.join(['%s']*len(rejected_drivers_list))})"
            params.extend(rejected_drivers_list)
            
        # Try linked driver
        cursor.execute(base_query + " AND ambulance_id=%s" + exclude_clause + " LIMIT 1", tuple([ambulance_id] + params))
        linked_driver = cursor.fetchone()
        if linked_driver:
            return linked_driver

        # Try unlinked driver
        cursor.execute(base_query + " AND ambulance_id IS NULL" + exclude_clause + " ORDER BY id ASC LIMIT 1", tuple(params))
        unlinked_driver = cursor.fetchone()
        if unlinked_driver:
            cursor.execute("UPDATE drivers SET ambulance_id=%s WHERE id=%s", (ambulance_id, unlinked_driver["id"]))
            return unlinked_driver

        # Try fallback driver
        cursor.execute(base_query + exclude_clause + " ORDER BY id ASC LIMIT 1", tuple(params))
        fallback_driver = cursor.fetchone()
        if fallback_driver:
            cursor.execute("UPDATE drivers SET ambulance_id=%s WHERE id=%s", (ambulance_id, fallback_driver["id"]))
            return fallback_driver

        return None

    for ambulance in ambulance_options:
        driver = fetch_driver_for_ambulance(ambulance["id"])
        if not driver:
            continue

        option = {
            "id": ambulance["id"],
            "vehicle_number": ambulance["vehicle_number"],
            "type": ambulance.get("type"),
            "current_latitude": ambulance.get("current_latitude"),
            "current_longitude": ambulance.get("current_longitude"),
            "driver_id": driver["id"],
            "driver_name": driver["name"],
            "driver_phone": driver["phone"],
        }

        option_distance = None
        if pickup_latitude is not None and pickup_longitude is not None:
            option_distance = calculate_distance_km(
                pickup_latitude,
                pickup_longitude,
                float(option.get("current_latitude")) if option.get("current_latitude") is not None else None,
                float(option.get("current_longitude")) if option.get("current_longitude") is not None else None
            )
        if option_distance is None:
            option_distance = 999999

        option_type_score = ambulance_type_preference_score(option.get("type"), recommended_ambulance_type)
        if (
            best_option is None
            or option_type_score < best_type_score
            or (option_type_score == best_type_score and option_distance < best_distance)
        ):
            best_option = option
            best_distance = option_distance
            best_type_score = option_type_score

    if not best_option:
        db.commit()
        return {"hospital": hospital, "assignment": None, "reason": "no_driver_available"}

    eta_minutes = estimate_arrival_minutes(None if best_distance == 999999 else best_distance)
    cursor.execute(
        """
        UPDATE bookings
        SET status='pending',
            hospital_id=%s,
            ambulance_id=%s,
            driver_id=%s,
            estimated_arrival_minutes=%s,
            assigned_at=%s
        WHERE id=%s
        """,
        (hospital["id"], best_option["id"], best_option["driver_id"], eta_minutes, datetime.now(), booking_id)
    )
    cursor.execute("UPDATE ambulances SET status='assigned' WHERE id=%s", (best_option["id"],))
    cursor.execute("UPDATE drivers SET status='busy' WHERE id=%s", (best_option["driver_id"],))
    db.commit()

    assignment = {
        "hospital_id": hospital["id"],
        "hospital_name": hospital["name"],
        "hospital_contact": hospital.get("emergency_contact") or "",
        "hospital_distance_km": hospital.get("distance_km"),
        "ambulance_id": best_option["id"],
        "vehicle_number": best_option["vehicle_number"],
        "ambulance_type": best_option.get("type") or "Basic",
        "driver_id": best_option["driver_id"],
        "driver_name": best_option["driver_name"],
        "driver_phone": best_option["driver_phone"],
        "distance_km": None if best_distance == 999999 else round(best_distance, 2),
        "eta_minutes": eta_minutes,
    }
    return {"hospital": hospital, "assignment": assignment, "reason": "assigned"}


def update_booking_eta_for_driver(driver_id):
    cursor.execute(
        """
        SELECT
            b.id,
            b.pickup_latitude,
            b.pickup_longitude,
            d.current_latitude,
            d.current_longitude
        FROM bookings b
        JOIN drivers d ON d.id = b.driver_id
        WHERE b.driver_id=%s AND b.status IN ('pending', 'accepted', 'on-the-way', 'reached')
        ORDER BY b.id DESC
        LIMIT 1
        """,
        (driver_id,)
    )
    row = cursor.fetchone()
    if not row:
        return

    distance_km = calculate_distance_km(
        float(row["pickup_latitude"]) if row.get("pickup_latitude") is not None else None,
        float(row["pickup_longitude"]) if row.get("pickup_longitude") is not None else None,
        float(row["current_latitude"]) if row.get("current_latitude") is not None else None,
        float(row["current_longitude"]) if row.get("current_longitude") is not None else None
    )
    eta_minutes = estimate_arrival_minutes(distance_km)
    cursor.execute(
        "UPDATE bookings SET estimated_arrival_minutes=%s WHERE id=%s",
        (eta_minutes, row["id"])
    )


def assign_nearest_resources(
    booking_id,
    user_id,
    pickup_latitude=None,
    pickup_longitude=None,
    recommended_ambulance_type=None
):
    cursor.execute(
        """
        SELECT
            a.id,
            a.vehicle_number,
            a.type,
            a.current_latitude,
            a.current_longitude,
            d.id AS driver_id,
            d.name AS driver_name,
            d.phone AS driver_phone,
            d.current_latitude AS driver_latitude,
            d.current_longitude AS driver_longitude
        FROM ambulances a
        JOIN drivers d ON LOWER(TRIM(d.status))='available'
        WHERE LOWER(TRIM(a.status))='available'
        ORDER BY a.id ASC, d.id ASC
        """
    )
    options = cursor.fetchall()
    if not options:
        return None

    best_option = None
    best_distance = None
    best_type_score = None

    for option in options:
        option_distance = None
        if pickup_latitude is not None and pickup_longitude is not None:
            option_distance = calculate_distance_km(
                pickup_latitude,
                pickup_longitude,
                float(option.get("current_latitude")) if option.get("current_latitude") is not None else None,
                float(option.get("current_longitude")) if option.get("current_longitude") is not None else None
            )
        if option_distance is None:
            option_distance = 999999

        option_type_score = ambulance_type_preference_score(option.get("type"), recommended_ambulance_type)

        if (
            best_option is None
            or option_type_score < best_type_score
            or (option_type_score == best_type_score and option_distance < best_distance)
        ):
            best_option = option
            best_distance = option_distance
            best_type_score = option_type_score

    if not best_option:
        return None

    eta_minutes = estimate_arrival_minutes(None if best_distance == 999999 else best_distance)
    cursor.execute(
        """
        UPDATE bookings
        SET status='pending',
            ambulance_id=%s,
            driver_id=%s,
            estimated_arrival_minutes=%s
        WHERE id=%s
        """,
        (best_option["id"], best_option["driver_id"], eta_minutes, booking_id)
    )
    cursor.execute("UPDATE ambulances SET status='on-duty' WHERE id=%s", (best_option["id"],))
    cursor.execute("UPDATE drivers SET status='busy' WHERE id=%s", (best_option["driver_id"],))
    db.commit()
    return {
        "ambulance_id": best_option["id"],
        "vehicle_number": best_option["vehicle_number"],
        "ambulance_type": best_option.get("type") or "Basic",
        "driver_id": best_option["driver_id"],
        "driver_name": best_option["driver_name"],
        "driver_phone": best_option["driver_phone"],
        "distance_km": None if best_distance == 999999 else round(best_distance, 2),
        "eta_minutes": eta_minutes
    }


def table_has_column(table_name, column_name):
    cursor.execute("""
        SELECT COUNT(*) AS c
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = %s
          AND column_name = %s
    """, (table_name, column_name))
    return (cursor.fetchone() or {}).get("c", 0) > 0


def ensure_schema():
    global schema_initialized
    if schema_initialized:
        return

    # Core tables for first-run stability.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(120) NOT NULL,
            email VARCHAR(180) NOT NULL UNIQUE,
            password VARCHAR(255) NOT NULL,
            role VARCHAR(30) DEFAULT 'user',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            phone VARCHAR(20) NULL,
            location_text VARCHAR(255) NULL,
            medical_info TEXT NULL,
            latitude DECIMAL(10,7) NULL,
            longitude DECIMAL(10,7) NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS drivers (
            id INT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(120) NOT NULL,
            email VARCHAR(180) NULL,
            password VARCHAR(255) NULL,
            phone VARCHAR(20) NOT NULL,
            license_no VARCHAR(80) NOT NULL,
            ambulance_id INT NULL,
            status VARCHAR(30) DEFAULT 'available',
            current_latitude DECIMAL(10,7) NULL,
            current_longitude DECIMAL(10,7) NULL,
            current_location_text VARCHAR(255) NULL,
            tracking_token VARCHAR(80) NULL,
            last_location_update DATETIME NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ambulances (
            id INT AUTO_INCREMENT PRIMARY KEY,
            vehicle_number VARCHAR(50) NOT NULL,
            type VARCHAR(80) DEFAULT 'Basic',
            status VARCHAR(30) DEFAULT 'available',
            hospital_id INT NULL,
            current_latitude DECIMAL(10,7) NULL,
            current_longitude DECIMAL(10,7) NULL,
            current_location_text VARCHAR(255) NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS hospitals (
            id INT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(150) NOT NULL,
            location_text VARCHAR(255) NULL,
            latitude DECIMAL(10,7) NULL,
            longitude DECIMAL(10,7) NULL,
            emergency_contact VARCHAR(20) NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bookings (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL,
            hospital_id INT NULL,
            ambulance_id INT NULL,
            driver_id INT NULL,
            status VARCHAR(30) DEFAULT 'requested',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            pickup_location VARCHAR(255) NULL,
            pickup_latitude DECIMAL(10,7) NULL,
            pickup_longitude DECIMAL(10,7) NULL,
            disease_type VARCHAR(120) NULL,
            emergency_level VARCHAR(30) NULL,
            medical_notes TEXT NULL,
            estimated_arrival_minutes INT NULL,
            assigned_at DATETIME NULL,
            rejected_drivers VARCHAR(255) DEFAULT '',
            recommended_ambulance_type VARCHAR(50) NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NULL,
            driver_id INT NULL,
            message TEXT NOT NULL,
            role VARCHAR(20) DEFAULT 'user',
            status VARCHAR(20) DEFAULT 'unread',
            is_read BOOLEAN DEFAULT FALSE,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS feedback (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL,
            rating INT NOT NULL,
            message TEXT NOT NULL,
            admin_reply TEXT NULL,
            replied_at DATETIME NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL,
            amount DECIMAL(10,2) DEFAULT 0.00,
            status VARCHAR(30) DEFAULT 'success',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # user created_at for admin user listing
    if not table_has_column("users", "created_at"):
        cursor.execute("ALTER TABLE users ADD COLUMN created_at DATETIME DEFAULT CURRENT_TIMESTAMP")
    if not table_has_column("users", "phone"):
        cursor.execute("ALTER TABLE users ADD COLUMN phone VARCHAR(20) NULL")
    if not table_has_column("users", "location_text"):
        cursor.execute("ALTER TABLE users ADD COLUMN location_text VARCHAR(255) NULL")
    if not table_has_column("users", "medical_info"):
        cursor.execute("ALTER TABLE users ADD COLUMN medical_info TEXT NULL")
    if not table_has_column("users", "latitude"):
        cursor.execute("ALTER TABLE users ADD COLUMN latitude DECIMAL(10,7) NULL")
    if not table_has_column("users", "longitude"):
        cursor.execute("ALTER TABLE users ADD COLUMN longitude DECIMAL(10,7) NULL")

    if not table_has_column("drivers", "current_latitude"):
        cursor.execute("ALTER TABLE drivers ADD COLUMN current_latitude DECIMAL(10,7) NULL")
    if not table_has_column("drivers", "current_longitude"):
        cursor.execute("ALTER TABLE drivers ADD COLUMN current_longitude DECIMAL(10,7) NULL")
    if not table_has_column("drivers", "current_location_text"):
        cursor.execute("ALTER TABLE drivers ADD COLUMN current_location_text VARCHAR(255) NULL")
    if not table_has_column("drivers", "tracking_token"):
        cursor.execute("ALTER TABLE drivers ADD COLUMN tracking_token VARCHAR(80) NULL")
    if not table_has_column("drivers", "last_location_update"):
        cursor.execute("ALTER TABLE drivers ADD COLUMN last_location_update DATETIME NULL")
    if not table_has_column("drivers", "email"):
        cursor.execute("ALTER TABLE drivers ADD COLUMN email VARCHAR(180) NULL")
    if not table_has_column("drivers", "password"):
        cursor.execute("ALTER TABLE drivers ADD COLUMN password VARCHAR(255) NULL")
    if not table_has_column("drivers", "ambulance_id"):
        cursor.execute("ALTER TABLE drivers ADD COLUMN ambulance_id INT NULL")
    cursor.execute("ALTER TABLE drivers MODIFY COLUMN status VARCHAR(30) DEFAULT 'available'")

    if not table_has_column("hospitals", "location_text"):
        cursor.execute("ALTER TABLE hospitals ADD COLUMN location_text VARCHAR(255) NULL")
    if not table_has_column("hospitals", "latitude"):
        cursor.execute("ALTER TABLE hospitals ADD COLUMN latitude DECIMAL(10,7) NULL")
    if not table_has_column("hospitals", "longitude"):
        cursor.execute("ALTER TABLE hospitals ADD COLUMN longitude DECIMAL(10,7) NULL")
    if not table_has_column("hospitals", "emergency_contact"):
        cursor.execute("ALTER TABLE hospitals ADD COLUMN emergency_contact VARCHAR(20) NULL")
    if not table_has_column("hospitals", "created_at"):
        cursor.execute("ALTER TABLE hospitals ADD COLUMN created_at DATETIME DEFAULT CURRENT_TIMESTAMP")

    if not table_has_column("ambulances", "hospital_id"):
        cursor.execute("ALTER TABLE ambulances ADD COLUMN hospital_id INT NULL")
    if not table_has_column("ambulances", "current_latitude"):
        cursor.execute("ALTER TABLE ambulances ADD COLUMN current_latitude DECIMAL(10,7) NULL")
    if not table_has_column("ambulances", "current_longitude"):
        cursor.execute("ALTER TABLE ambulances ADD COLUMN current_longitude DECIMAL(10,7) NULL")
    if not table_has_column("ambulances", "current_location_text"):
        cursor.execute("ALTER TABLE ambulances ADD COLUMN current_location_text VARCHAR(255) NULL")
    cursor.execute("ALTER TABLE ambulances MODIFY COLUMN status VARCHAR(30) DEFAULT 'available'")

    if not table_has_column("bookings", "hospital_id"):
        cursor.execute("ALTER TABLE bookings ADD COLUMN hospital_id INT NULL")
    if not table_has_column("bookings", "pickup_location"):
        cursor.execute("ALTER TABLE bookings ADD COLUMN pickup_location VARCHAR(255) NULL")
    if not table_has_column("bookings", "pickup_latitude"):
        cursor.execute("ALTER TABLE bookings ADD COLUMN pickup_latitude DECIMAL(10,7) NULL")
    if not table_has_column("bookings", "pickup_longitude"):
        cursor.execute("ALTER TABLE bookings ADD COLUMN pickup_longitude DECIMAL(10,7) NULL")
    if not table_has_column("bookings", "disease_type"):
        cursor.execute("ALTER TABLE bookings ADD COLUMN disease_type VARCHAR(120) NULL")
    if not table_has_column("bookings", "emergency_level"):
        cursor.execute("ALTER TABLE bookings ADD COLUMN emergency_level VARCHAR(30) NULL")
    if not table_has_column("bookings", "medical_notes"):
        cursor.execute("ALTER TABLE bookings ADD COLUMN medical_notes TEXT NULL")
    if not table_has_column("bookings", "estimated_arrival_minutes"):
        cursor.execute("ALTER TABLE bookings ADD COLUMN estimated_arrival_minutes INT NULL")
    if not table_has_column("bookings", "assigned_at"):
        cursor.execute("ALTER TABLE bookings ADD COLUMN assigned_at DATETIME NULL")
    if not table_has_column("bookings", "rejected_drivers"):
        cursor.execute("ALTER TABLE bookings ADD COLUMN rejected_drivers VARCHAR(255) DEFAULT ''")
    if not table_has_column("bookings", "recommended_ambulance_type"):
        cursor.execute("ALTER TABLE bookings ADD COLUMN recommended_ambulance_type VARCHAR(50) NULL")
    cursor.execute("ALTER TABLE bookings MODIFY COLUMN status VARCHAR(30) DEFAULT 'requested'")

    # notifications read state for popup flow
    if not table_has_column("notifications", "is_read"):
        cursor.execute("ALTER TABLE notifications ADD COLUMN is_read BOOLEAN DEFAULT FALSE")
    if not table_has_column("notifications", "driver_id"):
        cursor.execute("ALTER TABLE notifications ADD COLUMN driver_id INT NULL")
    if not table_has_column("notifications", "role"):
        cursor.execute("ALTER TABLE notifications ADD COLUMN role VARCHAR(20) DEFAULT 'user'")
    if not table_has_column("notifications", "status"):
        cursor.execute("ALTER TABLE notifications ADD COLUMN status VARCHAR(20) DEFAULT 'unread'")
    cursor.execute("ALTER TABLE notifications MODIFY COLUMN user_id INT NULL")
    cursor.execute("ALTER TABLE notifications MODIFY COLUMN status VARCHAR(20) DEFAULT 'unread'")

    # admin feedback reply support
    if not table_has_column("feedback", "admin_reply"):
        cursor.execute("ALTER TABLE feedback ADD COLUMN admin_reply TEXT NULL")
    if not table_has_column("feedback", "replied_at"):
        cursor.execute("ALTER TABLE feedback ADD COLUMN replied_at DATETIME NULL")

    # login activity tracking
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS login_activity (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL,
            user_name VARCHAR(100),
            user_email VARCHAR(150),
            user_role VARCHAR(30),
            login_type VARCHAR(40) DEFAULT 'email',
            login_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_login_user_id (user_id),
            INDEX idx_login_at (login_at)
        )
    """)

    # internal admin bug tracker
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bug_tickets (
            id INT AUTO_INCREMENT PRIMARY KEY,
            title VARCHAR(200) NOT NULL,
            description TEXT,
            priority ENUM('low','medium','high','critical') DEFAULT 'medium',
            status ENUM('open','in-progress','resolved','closed') DEFAULT 'open',
            assigned_to VARCHAR(120) DEFAULT '',
            created_by VARCHAR(120) DEFAULT '',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX idx_bug_status (status),
            INDEX idx_bug_priority (priority),
            INDEX idx_bug_created_at (created_at)
        )
    """)
    db.commit()
    cursor.execute("SELECT id, tracking_token FROM drivers")
    for driver_row in cursor.fetchall():
        if not driver_row.get("tracking_token"):
            cursor.execute(
                "UPDATE drivers SET tracking_token=%s WHERE id=%s",
                (secrets.token_urlsafe(24), driver_row["id"])
            )
    cursor.execute("SELECT id, email, password FROM drivers")
    for driver_row in cursor.fetchall():
        updated_email = driver_row.get("email")
        updated_password = driver_row.get("password")
        if not updated_email:
            updated_email = f"driver{driver_row['id']}@lifelink.com"
        if not updated_password:
            updated_password = generate_password_hash(DEFAULT_DRIVER_PASSWORD)
        cursor.execute(
            "UPDATE drivers SET email=%s, password=%s WHERE id=%s",
            (updated_email, updated_password, driver_row["id"])
        )
    cursor.execute(
        """
        UPDATE notifications
        SET role=COALESCE(NULLIF(role, ''), 'user'),
            status=CASE
                WHEN is_read=TRUE OR is_read=1 THEN 'read'
                ELSE 'unread'
            END
        """
    )
    db.commit()
    ensure_default_admin()
    schema_initialized = True


def ensure_default_admin():
    cursor.execute("SELECT id, email, role FROM users WHERE role='admin' ORDER BY id ASC LIMIT 1")
    admin_user = cursor.fetchone()
    if admin_user:
        return

    cursor.execute("SELECT id, role FROM users WHERE email=%s LIMIT 1", (DEFAULT_ADMIN_EMAIL,))
    existing = cursor.fetchone()
    if existing:
        cursor.execute(
            "UPDATE users SET role='admin', password=%s WHERE id=%s",
            (generate_password_hash(DEFAULT_ADMIN_PASSWORD), existing["id"])
        )
        db.commit()
        print(f"Recovered admin access for existing user: {DEFAULT_ADMIN_EMAIL}")
        return

    cursor.execute(
        "INSERT INTO users (name, email, password, role) VALUES (%s, %s, %s, %s)",
        (DEFAULT_ADMIN_NAME, DEFAULT_ADMIN_EMAIL, generate_password_hash(DEFAULT_ADMIN_PASSWORD), "admin")
    )
    db.commit()
    print(f"Created default admin user: {DEFAULT_ADMIN_EMAIL}")


def auto_record_bug(title, description, priority="high"):
    try:
        if not ensure_db_connection():
            return
        ensure_schema()
        cursor.execute(
            """
            INSERT INTO bug_tickets (title, description, priority, status, assigned_to, created_by, created_at, updated_at)
            VALUES (%s, %s, %s, 'open', %s, %s, %s, %s)
            """,
            (
                title[:200],
                description[:5000],
                priority if priority in {"low", "medium", "high", "critical"} else "high",
                "",
                "System",
                datetime.now(),
                datetime.now(),
            )
        )
        db.commit()
    except Exception:
        # Never allow bug logging to crash the app again.
        pass


@app.before_request
def require_database():
    if request.endpoint in {"static", "logout"}:
        return None

    if ensure_db_connection():
        ensure_schema()
        return None

    return render_template(
        "shared/message.html",
        type="error",
        heading="Database Unavailable",
        message="Could not connect to database. Please check MySQL and try again.",
        redirect_url="/login"
    ), 503


@app.after_request
def disable_cache(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.errorhandler(Exception)
def handle_runtime_error(error):
    if isinstance(error, HTTPException):
        return error

    route_name = request.path if request else "unknown-route"
    method_name = request.method if request else "unknown-method"
    trace = traceback.format_exc()
    title = f"Runtime error on {route_name}"
    description = (
        f"Method: {method_name}\n"
        f"Error Type: {error.__class__.__name__}\n"
        f"Message: {str(error)}\n\n"
        f"Traceback:\n{trace}"
    )
    auto_record_bug(title, description, priority="high")

    redirect_target = "/login"
    if session.get("role") == "admin":
        redirect_target = "/admin/dashboard"
    elif session.get("user_id"):
        redirect_target = "/home"

    return render_template(
        "shared/message.html",
        type="error",
        heading="Something Went Wrong",
        message="The issue was recorded in Bug Tracker. Please try again.",
        redirect_url=redirect_target,
        button_text="Go Back"
    ), 500

# ---------------- AUTH ----------------
@app.route("/", methods=["GET", "POST"])
@app.route("/login", methods=["GET", "POST"])
def login():
    if driver_required():
        return redirect(url_for("driver_dashboard"))

    if request.method == "POST":
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        user = retry_db_once(
            lambda: (
                cursor.execute("SELECT * FROM users WHERE email=%s", (email,)),
                cursor.fetchone()
            )[1]
        )

        if user and verify_user_password(user, password):
            # Auto-recover admin access for default admin account.
            if email == DEFAULT_ADMIN_EMAIL and user.get("role") != "admin":
                cursor.execute("UPDATE users SET role='admin' WHERE id=%s", (user["id"],))
                db.commit()
                user["role"] = "admin"
            return login_user(user, login_type="email")

        # If default admin account was deleted, recreate it on demand.
        if email == DEFAULT_ADMIN_EMAIL and password == DEFAULT_ADMIN_PASSWORD:
            ensure_default_admin()
            cursor.execute("SELECT * FROM users WHERE email=%s", (email,))
            user = cursor.fetchone()
            if user and verify_user_password(user, password):
                return login_user(user, login_type="email")

        return render_template("shared/message.html", message="Invalid login")

    return render_template("auth/login.html")


@app.route("/driver/login", methods=["GET", "POST"])
def driver_login():
    if driver_required():
        return redirect(url_for("driver_dashboard"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        driver = retry_db_once(
            lambda: (
                cursor.execute("SELECT * FROM drivers WHERE LOWER(TRIM(email))=%s LIMIT 1", (email,)),
                cursor.fetchone()
            )[1]
        )

        if driver and verify_driver_password(driver, password):
            return login_driver(driver)

        return render_template("shared/message.html", message="Invalid driver login.")

    return render_template("driver/login.html", default_driver_password=DEFAULT_DRIVER_PASSWORD)


def log_login_activity(user, login_type):
    retry_db_once(
        lambda: (
            cursor.execute(
                """
                INSERT INTO login_activity (user_id, user_name, user_email, user_role, login_type, login_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    user["id"],
                    user.get("name", ""),
                    user.get("email", ""),
                    user.get("role", "user"),
                    login_type,
                    datetime.now(),
                )
            ),
            db.commit()
        )
    )


def login_user(user, login_type="email"):
    session.clear()
    session["user_id"] = user["id"]
    session["role"] = user["role"]
    session["name"] = user["name"]
    session["login_type"] = login_type
    log_login_activity(user, login_type)

    if user["role"] == "admin":
        return redirect(url_for("admin_dashboard"))
    return redirect(url_for("home"))


def login_driver(driver):
    session.clear()
    session["driver_id"] = driver["id"]
    session["role"] = "driver"
    session["name"] = driver["name"]
    session["driver_email"] = driver.get("email") or ""
    session["login_type"] = "driver"
    return redirect(url_for("driver_dashboard"))


def verify_user_password(user, plain_password):
    stored_password = (user or {}).get("password")
    if not stored_password:
        return False

    is_hashed_format = stored_password.startswith(("pbkdf2:", "scrypt:"))

    if is_hashed_format:
        try:
            return check_password_hash(stored_password, plain_password)
        except ValueError:
            return False

    if stored_password == plain_password:
        # Backward compatibility: upgrade plain-text passwords on successful login.
        cursor.execute(
            "UPDATE users SET password=%s WHERE id=%s",
            (generate_password_hash(plain_password), user["id"])
        )
        db.commit()
        return True

    return False


def verify_driver_password(driver, plain_password):
    stored_password = (driver or {}).get("password")
    if not stored_password:
        return False

    is_hashed_format = stored_password.startswith(("pbkdf2:", "scrypt:"))
    if is_hashed_format:
        try:
            return check_password_hash(stored_password, plain_password)
        except ValueError:
            return False

    if stored_password == plain_password:
        cursor.execute(
            "UPDATE drivers SET password=%s WHERE id=%s",
            (generate_password_hash(plain_password), driver["id"])
        )
        db.commit()
        return True

    return False


def get_or_create_user(name, email, role="user", password=None):
    user = retry_db_once(
        lambda: (
            cursor.execute("SELECT * FROM users WHERE email=%s", (email,)),
            cursor.fetchone()
        )[1]
    )
    if user:
        return user

    safe_password = password or secrets.token_urlsafe(16)
    created_user = retry_db_once(
        lambda: (
            cursor.execute(
                "INSERT INTO users (name, email, password, role) VALUES (%s, %s, %s, %s)",
                (name, email, generate_password_hash(safe_password), role)
            ),
            db.commit(),
            cursor.execute("SELECT * FROM users WHERE id=%s", (cursor.lastrowid,)),
            cursor.fetchone()
        )[3]
    )
    return created_user


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        phone = request.form.get("phone", "").strip()
        location_text = request.form.get("location_text", "").strip()
        medical_info = request.form.get("medical_info", "").strip()
        latitude = parse_coordinate(request.form.get("latitude"))
        longitude = parse_coordinate(request.form.get("longitude"))
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not name or not email or not password:
            return render_template("shared/message.html", message="All fields are required.")

        if password != confirm_password:
            return render_template("shared/message.html", message="Passwords do not match.")

        cursor.execute("SELECT id FROM users WHERE email=%s", (email,))
        existing = cursor.fetchone()
        if existing:
            return render_template("shared/message.html", message="Email already registered. Please login.")

        cursor.execute(
            """
            INSERT INTO users (name, email, phone, location_text, medical_info, latitude, longitude, password, role)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                name,
                email,
                phone or None,
                location_text or None,
                medical_info or None,
                latitude,
                longitude,
                generate_password_hash(password),
                "user"
            )
        )
        db.commit()
        cursor.execute("SELECT * FROM users WHERE id=%s", (cursor.lastrowid,))
        user = cursor.fetchone()
        return login_user(user, login_type="signup")

    return render_template("auth/signup.html")


@app.route("/login/guest")
def login_guest():
    guest_token = datetime.now().strftime("%Y%m%d%H%M%S")
    guest_name = f"Guest {guest_token[-4:]}"
    guest_email = f"guest_{guest_token}@guest.lifelink"
    guest_user = get_or_create_user(guest_name, guest_email, role="user")
    session["is_guest"] = True
    return login_user(guest_user, login_type="guest")


@app.route("/login/social/<provider>")
def login_social(provider):
    provider = (provider or "").strip().lower()
    allowed = {"google", "github", "facebook"}
    if provider not in allowed:
        return render_template("shared/message.html", message="Unsupported social provider.")

    # Demo social login for college project UI without OAuth credentials.
    social_email = f"{provider}_demo@oauth.lifelink"
    social_name = f"{provider.title()} User"
    social_user = get_or_create_user(social_name, social_email, role="user")
    session["social_provider"] = provider
    return login_user(social_user, login_type=provider)

def get_user_by_email(email):
    conn = mysql.connector.connect(...)
    cursor = conn.cursor(dictionary=True)

    cursor.execute("SELECT * FROM users WHERE email=%s", (email,))
    user = cursor.fetchone()

    return user

@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()

        cursor.execute("SELECT * FROM users WHERE email=%s", (email,))
        user = cursor.fetchone()

        if user:
            session['reset_email'] = email
            return redirect('/reset-password')
        else:
            return render_template("shared/message.html", message="Email not found")

    return render_template("auth/forgot_password.html")

@app.route('/reset-password', methods=['GET', 'POST'])
def reset_password():
    if 'reset_email' not in session:
        return redirect('/login')

    if request.method == 'POST':
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')

        if password != confirm_password:
            return render_template(
                "shared/message.html",
                message="Passwords do not match"
            )

        # 🔐 Hash password (your project already supports hashing)
        hashed_password = generate_password_hash(password)

        cursor.execute(
            "UPDATE users SET password=%s WHERE email=%s",
            (hashed_password, session['reset_email'])
        )
        db.commit()

        session.pop('reset_email', None)

        return render_template(
            "shared/message.html",
            message="Password updated successfully! Please login."
        )

    return render_template("auth/reset_password.html")

@app.route("/logout")
def logout():
    try:
        session.clear()
        return redirect(url_for("login"))
    except Exception as e:
        print(f"Logout error: {e}")
        return redirect("/")


def fetch_driver_active_booking(driver_id):
    cursor.execute(
        """
        SELECT
            b.id,
            b.status,
            b.created_at,
            b.assigned_at,
            b.pickup_location,
            h.name AS hospital_name,
            b.disease_type,
            b.emergency_level,
            b.medical_notes,
            b.estimated_arrival_minutes,
            u.name AS patient_name,
            u.phone AS patient_phone,
            a.vehicle_number
        FROM bookings b
        JOIN users u ON u.id = b.user_id
        LEFT JOIN hospitals h ON h.id = b.hospital_id
        LEFT JOIN ambulances a ON a.id = b.ambulance_id
        WHERE b.driver_id=%s AND b.status IN ('pending', 'accepted', 'on-the-way', 'reached')
        ORDER BY FIELD(b.status, 'pending', 'accepted', 'on-the-way', 'reached'), b.id DESC
        LIMIT 1
        """,
        (driver_id,)
    )
    booking = cursor.fetchone()
    if booking:
        if booking.get("assigned_at"):
            booking["assigned_at_iso"] = booking["assigned_at"].isoformat() + "Z"
        booking["server_time_iso"] = datetime.now().isoformat() + "Z"
    return booking


@app.route("/driver/dashboard")
def driver_dashboard():
    if not driver_required():
        return redirect(url_for("driver_login"))

    driver_id = session["driver_id"]
    active_booking = fetch_driver_active_booking(driver_id)

    cursor.execute(
        """
        SELECT id, message, created_at, status
        FROM notifications
        WHERE role='driver' AND driver_id=%s
        ORDER BY created_at DESC
        LIMIT 8
        """,
        (driver_id,)
    )
    notifications = cursor.fetchall()
    unread_notifications = sum(1 for item in notifications if (item.get("status") or "unread") == "unread")

    cursor.execute(
        """
        SELECT
            b.id,
            b.status,
            b.pickup_location,
            b.disease_type,
            b.emergency_level,
            u.name AS patient_name
        FROM bookings b
        JOIN users u ON u.id = b.user_id
        WHERE b.driver_id=%s
        ORDER BY b.id DESC
        LIMIT 8
        """,
        (driver_id,)
    )
    recent_bookings = cursor.fetchall()

    cursor.execute("SELECT tracking_token FROM drivers WHERE id=%s", (driver_id,))
    driver_meta = cursor.fetchone() or {}

    return render_template(
        "driver/dashboard.html",
        active_booking=active_booking,
        notifications=notifications,
        unread_notifications=unread_notifications,
        recent_bookings=recent_bookings,
        booking_status_label=booking_status_label,
        driver_tracking_token=driver_meta.get("tracking_token", "")
    )

@app.route("/driver/update_status", methods=["POST"])
def driver_update_status():
    if not driver_required():
        return redirect(url_for("driver_login"))

    booking_id = request.form.get("booking_id", "").strip()
    action = request.form.get("action", "").strip().lower()
    if not booking_id.isdigit():
        return render_template("shared/message.html", message="Invalid booking reference.")

    cursor.execute(
        """
        SELECT
            b.id,
            b.user_id,
            b.driver_id,
            b.ambulance_id,
            b.status,
            u.name AS user_name,
            d.name AS driver_name,
            a.vehicle_number
        FROM bookings b
        JOIN users u ON u.id = b.user_id
        LEFT JOIN drivers d ON d.id = b.driver_id
        LEFT JOIN ambulances a ON a.id = b.ambulance_id
        WHERE b.id=%s AND b.driver_id=%s
        LIMIT 1
        """,
        (int(booking_id), session["driver_id"])
    )
    booking = cursor.fetchone()
    if not booking:
        return render_template("shared/message.html", message="Assigned booking not found.")

    current_status = (booking.get("status") or "").strip().lower()
    next_status_map = {
        "accept": "accepted",
        "on-the-way": "on-the-way",
        "reached": "reached",
        "completed": "completed",
    }

    if action == "reject":
        cursor.execute(
            """
            UPDATE bookings
            SET status='requested', ambulance_id=NULL, driver_id=NULL, estimated_arrival_minutes=NULL
            WHERE id=%s
            """,
            (booking["id"],)
        )
        if booking.get("ambulance_id"):
            cursor.execute("UPDATE ambulances SET status='available' WHERE id=%s", (booking["ambulance_id"],))
        cursor.execute("UPDATE drivers SET status='available' WHERE id=%s", (session["driver_id"],))
        db.commit()
        create_notification(
            booking["user_id"],
            f"Driver could not take booking #{booking['id']}. We are reassigning a new ambulance team now."
        )
        create_admin_notification(
            f"Driver {session.get('name', 'Driver')} rejected booking #{booking['id']}. Booking returned to requested queue."
        )
        return redirect(url_for("driver_dashboard"))

    if action not in next_status_map:
        return render_template("shared/message.html", message="Invalid driver action.")

    new_status = next_status_map[action]
    valid_transitions = {
        "pending": {"accepted"},
        "accepted": {"on-the-way"},
        "on-the-way": {"reached"},
        "reached": {"completed"},
    }
    if new_status not in valid_transitions.get(current_status, set()):
        return render_template("shared/message.html", message="This status change is not allowed right now.")

    cursor.execute("UPDATE bookings SET status=%s WHERE id=%s", (new_status, booking["id"]))

    if booking.get("ambulance_id"):
        ambulance_status = "on-duty" if booking_keeps_resources_busy(new_status) else "available"
        cursor.execute("UPDATE ambulances SET status=%s WHERE id=%s", (ambulance_status, booking["ambulance_id"]))
    driver_status = "busy" if booking_keeps_resources_busy(new_status) else "available"
    cursor.execute("UPDATE drivers SET status=%s WHERE id=%s", (driver_status, session["driver_id"]))
    db.commit()

    if new_status == "accepted":
        user_message = f"Driver {session.get('name', 'Driver')} accepted booking #{booking['id']}."
        driver_message = f"You accepted booking #{booking['id']}. Start moving when ready."
        admin_message = f"Driver {session.get('name', 'Driver')} accepted booking #{booking['id']}."
    elif new_status == "on-the-way":
        user_message = f"Ambulance for booking #{booking['id']} is on the way."
        driver_message = f"Booking #{booking['id']} marked on the way."
        admin_message = f"Booking #{booking['id']} is on the way."
    elif new_status == "reached":
        user_message = f"Ambulance for booking #{booking['id']} has reached your pickup location."
        driver_message = f"Booking #{booking['id']} marked reached."
        admin_message = f"Booking #{booking['id']} reached pickup location."
    else:
        user_message = f"Booking #{booking['id']} completed. Thank you for using LifeLink."
        driver_message = f"Booking #{booking['id']} completed successfully."
        admin_message = f"Booking #{booking['id']} completed by driver {session.get('name', 'Driver')}."

    create_notification(booking["user_id"], user_message)
    create_driver_notification(session["driver_id"], driver_message)
    create_admin_notification(admin_message)
    return redirect(url_for("driver_dashboard"))

@app.route("/api/driver/active_trip")
def api_driver_active_trip():
    if "driver_id" not in session:
        return jsonify({"ok": False, "error": "Not logged in"}), 401

    driver_id = session["driver_id"]
    
    # Run the lazy reassign check for any pending trips assigned to this driver
    cursor.execute("SELECT id FROM bookings WHERE driver_id=%s AND status='pending'", (driver_id,))
    pending_bookings = cursor.fetchall()
    for pb in pending_bookings:
        check_timeout_and_reassign(pb["id"])

    # Now fetch the active trip
    cursor.execute(
        """
        SELECT id, status, assigned_at
        FROM bookings
        WHERE driver_id=%s
          AND status IN ('pending', 'accepted', 'on-the-way', 'reached')
        ORDER BY id DESC
        LIMIT 1
        """,
        (driver_id,)
    )
    active_booking = cursor.fetchone()
    
    if active_booking and active_booking["assigned_at"]:
        active_booking["assigned_at_iso"] = active_booking["assigned_at"].isoformat() + "Z"
        # We also need server time to calculate exact diff safely
        active_booking["server_time_iso"] = datetime.now().isoformat() + "Z"

    return jsonify({"ok": True, "data": active_booking})


@app.route("/api/driver/reject_trip", methods=["POST"])
def api_driver_reject_trip():
    if "driver_id" not in session:
        return jsonify({"ok": False, "error": "Not logged in"}), 401

    data = request.json or {}
    booking_id = data.get("booking_id")
    if not booking_id:
        return jsonify({"ok": False, "error": "No booking ID"}), 400

    driver_id = session["driver_id"]
    cursor.execute(
        "SELECT id, driver_id, status, rejected_drivers, user_id, pickup_latitude, pickup_longitude, pickup_location, recommended_ambulance_type FROM bookings WHERE id=%s",
        (booking_id,)
    )
    booking = cursor.fetchone()

    if not booking or booking["status"] != "pending" or booking["driver_id"] != driver_id:
        return jsonify({"ok": True, "message": "No action taken"})

    # Add this driver to rejected list
    rejected_list = booking["rejected_drivers"].split(',') if booking["rejected_drivers"] else []
    if str(driver_id) not in rejected_list:
        rejected_list.append(str(driver_id))

    new_rejected_str = ",".join(rejected_list)
    cursor.execute("UPDATE bookings SET rejected_drivers=%s WHERE id=%s", (new_rejected_str, booking_id))
    cursor.execute("UPDATE drivers SET status='available' WHERE id=%s", (driver_id,))
    db.commit()

    # Directly trigger reassignment (bypass the 15s check since this is a manual reject)
    rejected_ids = [int(d) for d in rejected_list if d.strip().isdigit()]
    dispatch_result = assign_hospital_dispatch(
        booking_id,
        booking["user_id"],
        pickup_latitude=booking.get("pickup_latitude"),
        pickup_longitude=booking.get("pickup_longitude"),
        pickup_location_text=booking.get("pickup_location", ""),
        recommended_ambulance_type=booking.get("recommended_ambulance_type"),
        rejected_drivers_list=rejected_ids
    )

    if not dispatch_result.get("assignment"):
        cursor.execute("UPDATE bookings SET status='failed' WHERE id=%s", (booking_id,))
        db.commit()
        return jsonify({"ok": True, "reassigned": False, "message": "No drivers available"})

    return jsonify({"ok": True, "reassigned": True})



@app.route('/driver/update_location', methods=['POST'])
def driver_update_location_post():
    if not driver_required():
        return {"error": "unauthorized"}

    data = request.json
    lat = data.get("lat")
    lon = data.get("lon")
    location_text = data.get("location_text", "")

    driver_id = session["driver_id"]

    # 1. Update driver's own location
    cursor.execute("""
        UPDATE drivers
        SET current_latitude=%s,
            current_longitude=%s,
            current_location_text=%s,
            last_location_update=NOW()
        WHERE id=%s
    """, (lat, lon, location_text, driver_id))

    # 2. Sync the linked ambulance's location so tracking APIs reflect live position
    cursor.execute("""
        UPDATE ambulances a
        JOIN drivers d ON d.ambulance_id = a.id
        SET a.current_latitude = %s,
            a.current_longitude = %s,
            a.current_location_text = %s
        WHERE d.id = %s
    """, (lat, lon, location_text, driver_id))

    # 3. Recalculate ETA for the driver's active booking
    update_booking_eta_for_driver(driver_id)

    db.commit()
    return {"status": "updated"}

@app.route("/home")
def home():
    if driver_required():
        return redirect(url_for("driver_dashboard"))
    if "user_id" not in session:
        return redirect(url_for("login"))
    recent_notifications = retry_db_once(
        lambda: (
            cursor.execute(
                """
                SELECT message, created_at, is_read
                FROM notifications
                WHERE user_id=%s AND role='user'
                ORDER BY created_at DESC
                LIMIT 5
                """,
                (session["user_id"],)
            ),
            cursor.fetchall()
        )[1],
        fallback=[]
    ) or []

    unread_count = sum(1 for n in recent_notifications if not n.get("is_read"))

    return render_template(
        "user/home.html",
        recent_notifications=recent_notifications,
        unread_count=unread_count
    )

# ---------------- USER SYMPTOMS ----------------
@app.route("/symptoms")
def symptoms():
    if "user_id" not in session:
        return redirect(url_for("login"))

    return render_template("user/symptoms.html")

# ---------------- USER BOOKING ----------------
@app.route("/booking", methods=["GET", "POST"])
def booking():
    if "user_id" not in session:
        return redirect(url_for("login"))

    cursor.execute(
        """
        SELECT id, name, phone, location_text, medical_info, latitude, longitude
        FROM users
        WHERE id=%s
        LIMIT 1
        """,
        (session["user_id"],)
    )
    user_profile = cursor.fetchone() or {}

    if request.method == "POST":
        user_id = session["user_id"]
        pickup_location = request.form.get("pickup_location", "").strip() or user_profile.get("location_text")
        medical_notes = request.form.get("medical_notes", "").strip() or user_profile.get("medical_info")
        disease_type = request.form.get("disease_type", "").strip() or "General Emergency"
        emergency_level = request.form.get("emergency_level", "").strip().lower() or "medium"
        if emergency_level not in {"low", "medium", "high", "critical"}:
            emergency_level = "medium"
        pickup_latitude = parse_coordinate(request.form.get("pickup_latitude"))
        pickup_longitude = parse_coordinate(request.form.get("pickup_longitude"))
        recommended_ambulance_type = get_recommended_ambulance_type(
            disease_type=disease_type,
            emergency_level=emergency_level,
            medical_notes=medical_notes,
        )

        if pickup_latitude is None:
            stored_latitude = user_profile.get("latitude")
            pickup_latitude = float(stored_latitude) if stored_latitude is not None else None
        if pickup_longitude is None:
            stored_longitude = user_profile.get("longitude")
            pickup_longitude = float(stored_longitude) if stored_longitude is not None else None

        # Create booking with defensive schema compatibility for older databases.
        booking_columns = ["user_id", "status", "created_at"]
        booking_values = ["%s", "%s", "%s"]
        booking_params = [user_id, "requested", datetime.now()]

        optional_booking_fields = [
            ("pickup_location", pickup_location),
            ("pickup_latitude", pickup_latitude),
            ("pickup_longitude", pickup_longitude),
            ("disease_type", disease_type),
            ("emergency_level", emergency_level),
            ("medical_notes", medical_notes),
            ("recommended_ambulance_type", recommended_ambulance_type),
        ]

        for column_name, column_value in optional_booking_fields:
            if table_has_column("bookings", column_name):
                booking_columns.append(column_name)
                booking_values.append("%s")
                booking_params.append(column_value)

        cursor.execute(
            f"""
            INSERT INTO bookings ({', '.join(booking_columns)})
            VALUES ({', '.join(booking_values)})
            """,
            tuple(booking_params)
        )

        db.commit()
        booking_id = cursor.lastrowid

        create_admin_notification(
            (
                f"New booking #{booking_id} created for user #{user_id}. Disease: {disease_type}. "
                f"Emergency: {emergency_level.title()}. Suggested ambulance type: {recommended_ambulance_type}."
            )
        )

        dispatch_result = assign_hospital_dispatch(
            booking_id,
            user_id,
            pickup_latitude,
            pickup_longitude,
            pickup_location,
            recommended_ambulance_type=recommended_ambulance_type,
        )
        selected_hospital = dispatch_result.get("hospital")
        assigned = dispatch_result.get("assignment")

        if assigned:
            create_notification(
                user_id,
                (
                    f"Booking #{booking_id} confirmed. Ambulance {assigned['vehicle_number']} "
                    f"({assigned['ambulance_type']}) and driver {assigned['driver_name']} assigned from "
                    f"{assigned['hospital_name']}. Driver phone: {assigned['driver_phone']}. "
                    "Waiting for driver acceptance."
                    + (f" ETA about {assigned['eta_minutes']} minutes." if assigned.get("eta_minutes") else "")
                )
            )
            create_driver_notification(
                assigned["driver_id"],
                (
                    f"New booking #{booking_id} assigned. Pickup: {pickup_location or 'User saved location'}. "
                    f"Disease: {disease_type}. Emergency: {emergency_level.title()}. "
                    f"Suggested ambulance type: {recommended_ambulance_type}. Hospital: {assigned['hospital_name']}."
                )
            )
            create_admin_notification(
                (
                    f"Booking #{booking_id} auto-assigned to driver {assigned['driver_name']} and "
                    f"ambulance {assigned['vehicle_number']} ({assigned['ambulance_type']}) from hospital {assigned['hospital_name']}."
                )
            )
            create_admin_notification(
                f"Hospital alert: {assigned['hospital_name']} has an incoming patient for booking #{booking_id}. Disease: {disease_type}."
            )
        else:
            if dispatch_result.get("reason") == "no_hospital":
                user_message = (
                    f"Booking #{booking_id} created, but no hospitals are registered yet. "
                    "Admin needs to add hospital records before dispatch."
                )
                admin_message = (
                    f"Booking #{booking_id} is waiting for manual assignment because no hospitals are registered. "
                    f"Disease: {disease_type}. Emergency: {emergency_level.title()}."
                )
            else:
                hospital_name = (selected_hospital or {}).get("name") or "nearest hospital"
                user_message = (
                    f"Booking #{booking_id} created. Nearest hospital selected: {hospital_name}, "
                    "but no matching ambulance-driver team is currently available. Admin will assign soon."
                )
                admin_message = (
                    f"Booking #{booking_id} selected hospital {hospital_name} but no matching ambulance-driver team "
                    f"is available there. Disease: {disease_type}. Emergency: {emergency_level.title()}. "
                    f"Suggested ambulance type: {recommended_ambulance_type}."
                )
            create_notification(
                user_id,
                user_message + f" For first-aid help call doctor at {DOCTOR_CONTACT}."
            )
            create_admin_notification(admin_message)

        return redirect("/my-booking")


    return render_template(
        "booking/booking.html",
        user_profile=user_profile,
        recommended_ambulance_type="Basic"
    )

# ---------------- USER BOOKING TRACKING ----------------
@app.route("/my-booking")
def my_booking():
    if "user_id" not in session:
        return redirect(url_for("login"))

    cursor.execute("""
        SELECT 
            b.id,
            b.status,
            b.created_at,
            b.pickup_location,
            b.driver_id,
            h.name AS hospital_name,
            b.disease_type,
            b.emergency_level,
            b.estimated_arrival_minutes,
            a.vehicle_number,
            d.name AS driver_name,
            d.phone AS driver_phone
        FROM bookings b
        LEFT JOIN hospitals h ON h.id = b.hospital_id
        LEFT JOIN ambulances a ON b.ambulance_id = a.id
        LEFT JOIN drivers d ON b.driver_id = d.id
        WHERE b.user_id = %s
        ORDER BY b.id DESC
        LIMIT 1
    """, (session["user_id"],))

    booking = cursor.fetchone()
    waiting_statuses = {"requested", "pending", "accepted", "on-the-way", "reached"}
    is_waiting = bool(booking and booking.get("status") in waiting_statuses)
    first_aid_tips = []

    if is_waiting:
        first_aid_tips = [
            "Keep the patient calm and seated/lying safely with fresh air.",
            "If breathing trouble: loosen tight clothes and keep patient upright.",
            "If bleeding: apply firm pressure with clean cloth.",
            "If fever/dehydration: give small sips of water or ORS if conscious.",
            "Do not give unknown medicines without medical advice.",
        ]

    return render_template(
        "booking/my_booking.html",
        booking=booking,
        doctor_contact=DOCTOR_CONTACT,
        is_waiting=is_waiting,
        first_aid_tips=first_aid_tips
    )


@app.route("/booking/cancel", methods=["POST"])
def cancel_booking():
    if "user_id" not in session:
        return redirect(url_for("login"))

    cancellable_statuses = {"requested", "pending", "accepted", "on-the-way", "reached"}

    cursor.execute(
        """
        SELECT id, status, driver_id
        FROM bookings
        WHERE user_id=%s
        ORDER BY id DESC
        LIMIT 1
        """,
        (session["user_id"],)
    )
    booking = cursor.fetchone()

    if not booking:
        return render_template("shared/message.html", message="No booking found to cancel.")

    if (booking.get("status") or "").lower() not in cancellable_statuses:
        return render_template("shared/message.html", message="Only active bookings can be cancelled.")

    if (booking.get("status") or "").lower() == "cancelled":
        return render_template("shared/message.html", message="This booking is already cancelled.")

    cursor.execute(
        "UPDATE bookings SET status=%s WHERE id=%s",
        ("cancelled", booking["id"])
    )
    db.commit()

    create_notification(
        session["user_id"],
        f"Booking #{booking['id']} has been cancelled."
    )
    create_admin_notification(
        f"Booking #{booking['id']} was cancelled by the user."
    )
    if booking.get("driver_id"):
        create_driver_notification(
            booking["driver_id"],
            f"Booking #{booking['id']} was cancelled by the user."
        )

    return redirect("/my-booking")


@app.route("/track-ambulance")
def track_ambulance_page():
    if "user_id" not in session:
        return redirect(url_for("login"))
    latest_booking = retry_db_once(
        lambda: (
            cursor.execute(
                """
                SELECT id
                FROM bookings
                WHERE user_id=%s
                ORDER BY id DESC
                LIMIT 1
                """,
                (session["user_id"],)
            ),
            cursor.fetchone()
        )[1],
        fallback=None
    )
    default_case_id = latest_booking["id"] if latest_booking else ""
    return render_template("user/track_ambulance.html", default_case_id=default_case_id)


@app.route("/api/track-booking/<booking_id>")
def api_track_booking(booking_id):
    if "user_id" not in session:
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    booking_id = str(booking_id).strip()
    if not booking_id.isdigit():
        return jsonify({"ok": False, "error": "Enter a valid numeric case ID."}), 400

    # Lazy check for timeout
    check_timeout_and_reassign(booking_id)

    booking = retry_db_once(
        lambda: (
            cursor.execute(
                """
                SELECT
                    b.id,
                    b.status,
                    b.created_at,
                    b.pickup_location,
                    h.name AS hospital_name,
                    b.pickup_latitude,
                    b.pickup_longitude,
                    b.disease_type,
                    b.emergency_level,
                    b.estimated_arrival_minutes,
                    a.vehicle_number,
                    a.current_latitude AS ambulance_latitude,
                    a.current_longitude AS ambulance_longitude,
                    d.name AS driver_name,
                    d.phone AS driver_phone
                FROM bookings b
                LEFT JOIN hospitals h ON h.id = b.hospital_id
                LEFT JOIN ambulances a ON a.id = b.ambulance_id
                LEFT JOIN drivers d ON d.id = b.driver_id
                WHERE b.id=%s AND b.user_id=%s
                LIMIT 1
                """,
                (int(booking_id), session["user_id"])
            ),
            cursor.fetchone()
        )[1],
        fallback=None
    )

    if not booking:
        return jsonify({"ok": False, "error": "Case ID not found for this user."}), 404

    return jsonify({
        "ok": True,
        "data": {
            "id": booking["id"],
            "status": booking.get("status", "requested"),
            "created_at": str(booking.get("created_at") or ""),
            "pickup_location": booking.get("pickup_location") or "Not provided",
            "hospital_name": booking.get("hospital_name") or "Not assigned yet",
            "disease_type": booking.get("disease_type") or "General Emergency",
            "emergency_level": booking.get("emergency_level") or "medium",
            "vehicle_number": booking.get("vehicle_number") or "Not assigned yet",
            "driver_name": booking.get("driver_name") or "Not assigned yet",
            "driver_phone": booking.get("driver_phone") or "",
            "estimated_arrival_minutes": booking.get("estimated_arrival_minutes"),
            "ambulance_name": f"Ambulance {booking.get('vehicle_number') or 'Not Assigned'}",
            "lat": float(booking["ambulance_latitude"]) if booking.get("ambulance_latitude") is not None else 13.02153,
            "lng": float(booking["ambulance_longitude"]) if booking.get("ambulance_longitude") is not None else 74.96866,
            "pickup_lat": float(booking["pickup_latitude"]) if booking.get("pickup_latitude") is not None else None,
            "pickup_lng": float(booking["pickup_longitude"]) if booking.get("pickup_longitude") is not None else None
        }
    })

@app.route("/api/all-drivers-location")
def api_all_drivers_location():
    # Only admins should ideally see this, but for the user requirement I will make it accessible or check session
    if not admin_required():
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    cursor.execute("""
        SELECT id, name, current_latitude, current_longitude, status 
        FROM drivers
    """)
    drivers = cursor.fetchall()
    
    result = []
    for d in drivers:
        result.append({
            "driver_id": d["id"],
            "name": d["name"],
            "lat": float(d["current_latitude"]) if d["current_latitude"] is not None else 13.02153,
            "lng": float(d["current_longitude"]) if d["current_longitude"] is not None else 74.96866,
            "status": d["status"]
        })
    
    return jsonify(result)


# ---------------- ADMIN GUARD ----------------
def admin_required():
    return "user_id" in session and session.get("role") == "admin"


def driver_has_active_booking(driver_id):
    cursor.execute(
        """
        SELECT COUNT(*) AS c
        FROM bookings
        WHERE driver_id=%s AND status IN ('pending', 'accepted', 'on-the-way', 'reached')
        """,
        (driver_id,)
    )
    return (cursor.fetchone() or {}).get("c", 0) > 0


def sync_resource_statuses():
    # Drivers with active booking must be busy.
    cursor.execute("""
        UPDATE drivers d
        SET d.status='busy'
        WHERE EXISTS (
            SELECT 1 FROM bookings b
            WHERE b.driver_id = d.id
              AND b.status IN ('pending', 'accepted', 'on-the-way', 'reached')
        )
    """)

    # Ambulances with active booking must be on-duty.
    cursor.execute("""
        UPDATE ambulances a
        SET a.status='assigned'
        WHERE EXISTS (
            SELECT 1 FROM bookings b
            WHERE b.ambulance_id = a.id
              AND b.status IN ('pending', 'accepted', 'on-the-way', 'reached')
        )
    """)
    db.commit()


def notify_driver(phone, message):
    if phone:
        send_sms(phone, message)


# ---------------- ADMIN DASHBOARD ----------------
@app.route("/admin/dashboard")
def admin_dashboard():
    if not admin_required():
        return redirect(url_for("login"))

    cursor.execute("SELECT COUNT(*) c FROM users")
    users = cursor.fetchone()["c"]

    cursor.execute("SELECT COUNT(*) c FROM drivers")
    drivers = cursor.fetchone()["c"]

    cursor.execute("SELECT COUNT(*) c FROM ambulances")
    ambulances = cursor.fetchone()["c"]

    cursor.execute("SELECT COUNT(*) c FROM hospitals")
    hospitals = cursor.fetchone()["c"]

    cursor.execute("SELECT COUNT(*) c FROM bookings")
    bookings = cursor.fetchone()["c"]

    cursor.execute("SELECT COUNT(*) c FROM bookings WHERE status IN ('requested', 'pending')")
    pending_bookings = cursor.fetchone()["c"]

    cursor.execute("SELECT COUNT(*) c FROM notifications WHERE role='admin' AND (is_read=FALSE OR status='unread')")
    unread_notifications = cursor.fetchone()["c"]

    cursor.execute("SELECT COUNT(*) c FROM feedback WHERE admin_reply IS NULL OR admin_reply=''")
    pending_feedback = cursor.fetchone()["c"]

    cursor.execute("SELECT AVG(rating) avg FROM feedback")
    avg_rating = cursor.fetchone()["avg"] or 0

    cursor.execute("SELECT COUNT(*) c FROM drivers WHERE LOWER(TRIM(status))='available'")
    free_drivers = cursor.fetchone()["c"]
    busy_drivers = max(drivers - free_drivers, 0)

    cursor.execute("SELECT COUNT(*) c FROM ambulances WHERE LOWER(TRIM(status))='available'")
    free_ambulances = cursor.fetchone()["c"]
    active_ambulances = max(ambulances - free_ambulances, 0)

    cursor.execute("SELECT COUNT(*) c FROM bug_tickets WHERE status IN ('open', 'in-progress')")
    open_bugs = cursor.fetchone()["c"]

    cursor.execute("""
        SELECT user_name, user_email, user_role, login_type, login_at
        FROM login_activity
        ORDER BY login_at DESC
        LIMIT 10
    """)
    recent_logins = cursor.fetchall()

    return render_template(
        "admin/dashboard.html",
        users=users,
        drivers=drivers,
        ambulances=ambulances,
        hospitals=hospitals,
        bookings=bookings,
        pending_bookings=pending_bookings,
        unread_notifications=unread_notifications,
        pending_feedback=pending_feedback,
        avg_rating=round(avg_rating, 1),
        recent_logins=recent_logins,
        db_status="Connected" if ensure_db_connection() else "Disconnected",
        free_drivers=free_drivers,
        busy_drivers=busy_drivers,
        free_ambulances=free_ambulances,
        active_ambulances=active_ambulances,
        open_bugs=open_bugs
    )


# ---------------- ADMIN USERS ----------------
@app.route("/admin/users")
def admin_users():
    if not admin_required():
        return redirect(url_for("login"))

    cursor.execute("""
        SELECT 
            u.id, u.name, u.email, u.phone, u.location_text, u.medical_info, u.latitude, u.longitude, u.role, u.created_at,
            la.login_type AS last_login_type,
            la.login_at AS last_login_at
        FROM users u
        LEFT JOIN (
            SELECT l1.user_id, l1.login_type, l1.login_at
            FROM login_activity l1
            JOIN (
                SELECT user_id, MAX(login_at) AS max_login_at
                FROM login_activity
                GROUP BY user_id
            ) m ON l1.user_id = m.user_id AND l1.login_at = m.max_login_at
        ) la ON la.user_id = u.id
        ORDER BY u.id DESC
    """)
    users = cursor.fetchall()
    return render_template("admin/users.html", users=users)


@app.route("/admin/users/update/<int:user_id>", methods=["POST"])
def update_user(user_id):
    if not admin_required():
        return redirect(url_for("login"))

    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip()
    role = request.form.get("role", "user").strip()
    phone = request.form.get("phone", "").strip()
    location_text = request.form.get("location_text", "").strip()
    medical_info = request.form.get("medical_info", "").strip()
    latitude = parse_coordinate(request.form.get("latitude"))
    longitude = parse_coordinate(request.form.get("longitude"))

    if not name or not email or role not in {"admin", "user", "driver"}:
        return render_template("shared/message.html", message="Invalid user update input.")

    cursor.execute(
        """
        UPDATE users
        SET name=%s, email=%s, phone=%s, location_text=%s, medical_info=%s, latitude=%s, longitude=%s, role=%s
        WHERE id=%s
        """,
        (name, email, phone or None, location_text or None, medical_info or None, latitude, longitude, role, user_id)
    )
    db.commit()
    return redirect(url_for("admin_users"))


@app.route("/admin/users/delete/<int:user_id>", methods=["POST"])
def delete_user(user_id):
    if not admin_required():
        return redirect(url_for("login"))

    cursor.execute("DELETE FROM users WHERE id=%s", (user_id,))
    db.commit()
    return redirect(url_for("admin_users"))


@app.route("/admin/users/clear", methods=["POST"])
def clear_users():
    if not admin_required():
        return redirect(url_for("login"))

    # Keep at least one admin account.
    cursor.execute("DELETE FROM users WHERE role <> 'admin'")
    db.commit()
    return redirect(url_for("admin_users"))


# ---------------- ADMIN DRIVERS ----------------
@app.route("/admin/drivers")
def admin_drivers():
    if not admin_required():
        return redirect(url_for("login"))

    sync_resource_statuses()

    cursor.execute("""
        SELECT
            d.id,
            d.name,
            d.email,
            d.phone,
            d.license_no,
            d.ambulance_id,
            a.vehicle_number AS ambulance_number,
            d.current_latitude,
            d.current_longitude,
            d.current_location_text,
            d.tracking_token,
            d.last_location_update,
            CASE
                WHEN LOWER(TRIM(d.status)) = 'available' THEN 'available'
                ELSE 'busy'
            END AS status,
            CASE
                WHEN EXISTS (
                    SELECT 1 FROM bookings b
                    WHERE b.driver_id = d.id
                      AND b.status IN ('pending', 'accepted', 'on-the-way', 'reached')
                ) THEN 1
                ELSE 0
            END AS has_active_booking
        FROM drivers d
        LEFT JOIN ambulances a ON a.id = d.ambulance_id
        ORDER BY d.id DESC
    """)
    drivers = cursor.fetchall()
    cursor.execute("SELECT id, vehicle_number FROM ambulances ORDER BY vehicle_number ASC")
    ambulances = cursor.fetchall()
    return render_template("admin/drivers.html", drivers=drivers, ambulances=ambulances)


@app.route("/admin/drivers/add", methods=["POST"])
def add_driver():
    if not admin_required():
        return redirect(url_for("login"))

    name = request.form["name"]
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "").strip() or DEFAULT_DRIVER_PASSWORD
    phone = request.form["phone"]
    license_no = request.form["license_no"]
    ambulance_id = request.form.get("ambulance_id") or None
    current_location_text = request.form.get("current_location_text", "").strip()
    current_latitude = parse_coordinate(request.form.get("current_latitude"))
    current_longitude = parse_coordinate(request.form.get("current_longitude"))

    if not email:
        cursor.execute("SELECT COALESCE(MAX(id), 0) + 1 AS next_id FROM drivers")
        next_row = cursor.fetchone() or {}
        email = f"driver{next_row.get('next_id', 1)}@lifelink.com"
    else:
        cursor.execute("SELECT id FROM drivers WHERE LOWER(TRIM(email))=%s LIMIT 1", (email,))
        if cursor.fetchone():
            return render_template("shared/message.html", message="Driver email already exists.")

    cursor.execute(
        """
        INSERT INTO drivers (name, email, password, phone, license_no, ambulance_id, status, current_latitude, current_longitude, current_location_text, tracking_token)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (
            name,
            email,
            generate_password_hash(password),
            phone,
            license_no,
            ambulance_id,
            "available",
            current_latitude,
            current_longitude,
            current_location_text or None,
            secrets.token_urlsafe(24)
        )
    )
    db.commit()
    return redirect(url_for("admin_drivers"))


@app.route("/admin/drivers/link/<int:driver_id>", methods=["POST"])
def link_driver_ambulance(driver_id):
    if not admin_required():
        return redirect(url_for("login"))

    ambulance_id = request.form.get("ambulance_id") or None
    cursor.execute("UPDATE drivers SET ambulance_id=%s WHERE id=%s", (ambulance_id, driver_id))
    db.commit()
    return redirect(url_for("admin_drivers"))


@app.route("/admin/drivers/status/<int:driver_id>/<status>")
def change_driver_status(driver_id, status):
    if not admin_required():
        return redirect(url_for("login"))

    status = (status or "").strip().lower()
    if status not in {"available", "busy"}:
        return render_template("shared/message.html", message="Invalid driver status.")

    if status == "available" and driver_has_active_booking(driver_id):
        return render_template(
            "shared/message.html",
            message="Driver has active booking and cannot be marked available yet."
        )

    cursor.execute(
        "UPDATE drivers SET status=%s WHERE id=%s",
        (status, driver_id)
    )
    db.commit()
    return redirect(url_for("admin_drivers"))


@app.route("/admin/drivers/delete/<int:driver_id>", methods=["POST"])
def delete_driver(driver_id):
    if not admin_required():
        return redirect(url_for("login"))

    cursor.execute("DELETE FROM drivers WHERE id=%s", (driver_id,))
    db.commit()
    return redirect(url_for("admin_drivers"))


@app.route("/admin/drivers/toggle/<int:driver_id>")
def toggle_driver_status(driver_id):
    if not admin_required():
        return redirect(url_for("login"))

    # If driver is assigned to active booking, keep busy.
    if driver_has_active_booking(driver_id):
        cursor.execute("UPDATE drivers SET status='busy' WHERE id=%s", (driver_id,))
        db.commit()
        return redirect(url_for("admin_drivers"))

    cursor.execute(
        """
        UPDATE drivers
        SET status = CASE
            WHEN LOWER(TRIM(status))='available' THEN 'busy'
            ELSE 'available'
        END
        WHERE id=%s
        """,
        (driver_id,)
    )
    db.commit()
    return redirect(url_for("admin_drivers"))


@app.route("/admin/drivers/update/<int:driver_id>", methods=["POST"])
def update_driver(driver_id):
    if not admin_required():
        return redirect(url_for("login"))

    name = request.form.get("name", "").strip()
    phone = request.form.get("phone", "").strip()
    license_no = request.form.get("license_no", "").strip()
    status = request.form.get("status", "available").strip().lower()

    if not name or not phone or not license_no or status not in {"available", "busy"}:
        return render_template("shared/message.html", message="Invalid driver update input.")

    if status == "available" and driver_has_active_booking(driver_id):
        return render_template(
            "shared/message.html",
            message="Driver has active booking and cannot be marked available yet."
        )

    cursor.execute(
        "UPDATE drivers SET name=%s, phone=%s, license_no=%s, status=%s WHERE id=%s",
        (name, phone, license_no, status, driver_id)
    )
    db.commit()
    return redirect(url_for("admin_drivers"))


@app.route("/admin/drivers/location/<int:driver_id>", methods=["POST"])
def admin_update_driver_location(driver_id):
    if not admin_required():
        return redirect(url_for("login"))

    current_location_text = request.form.get("current_location_text", "").strip()
    current_latitude = parse_coordinate(request.form.get("current_latitude"))
    current_longitude = parse_coordinate(request.form.get("current_longitude"))

    cursor.execute(
        """
        UPDATE drivers
        SET current_location_text=%s, current_latitude=%s, current_longitude=%s, last_location_update=%s
        WHERE id=%s
        """,
        (current_location_text or None, current_latitude, current_longitude, datetime.now(), driver_id)
    )
    cursor.execute(
        """
        UPDATE ambulances a
        JOIN bookings b ON b.ambulance_id = a.id
        SET a.current_latitude=%s, a.current_longitude=%s, a.current_location_text=%s
        WHERE b.driver_id=%s AND b.status IN ('pending', 'accepted', 'on-the-way', 'reached')
        """,
        (current_latitude, current_longitude, current_location_text or None, driver_id)
    )
    update_booking_eta_for_driver(driver_id)
    db.commit()
    return redirect(url_for("admin_drivers"))


@app.route("/driver/live/<tracking_token>")
def driver_live_location_page(tracking_token):
    cursor.execute(
        """
        SELECT id, name, phone, license_no, status, current_location_text, last_location_update
        FROM drivers
        WHERE tracking_token=%s
        LIMIT 1
        """,
        (tracking_token,)
    )
    driver = cursor.fetchone()
    if not driver:
        return render_template("shared/message.html", message="Driver live tracking link is invalid.")

    cursor.execute(
        """
        SELECT b.id, b.status, b.pickup_location, u.name AS user_name
        FROM bookings b
        JOIN users u ON u.id = b.user_id
        WHERE b.driver_id=%s AND b.status IN ('pending', 'accepted', 'on-the-way', 'reached')
        ORDER BY b.id DESC
        LIMIT 1
        """,
        (driver["id"],)
    )
    active_booking = cursor.fetchone()
    return render_template("driver/live_location.html", driver=driver, active_booking=active_booking, tracking_token=tracking_token)


@app.route("/api/driver-location/<tracking_token>", methods=["POST"])
def api_update_driver_location(tracking_token):
    cursor.execute(
        "SELECT id FROM drivers WHERE tracking_token=%s LIMIT 1",
        (tracking_token,)
    )
    driver = cursor.fetchone()
    if not driver:
        return jsonify({"ok": False, "error": "Invalid tracking token"}), 404

    payload = request.get_json(silent=True) or {}
    latitude = parse_coordinate(payload.get("latitude"))
    longitude = parse_coordinate(payload.get("longitude"))
    location_text = str(payload.get("location_text") or "").strip() or "Live GPS update"

    if latitude is None or longitude is None:
        return jsonify({"ok": False, "error": "Latitude and longitude are required"}), 400

    cursor.execute(
        """
        UPDATE drivers
        SET current_latitude=%s, current_longitude=%s, current_location_text=%s, last_location_update=%s
        WHERE id=%s
        """,
        (latitude, longitude, location_text, datetime.now(), driver["id"])
    )
    cursor.execute(
        """
        UPDATE ambulances a
        JOIN bookings b ON b.ambulance_id = a.id
        SET a.current_latitude=%s, a.current_longitude=%s, a.current_location_text=%s
        WHERE b.driver_id=%s AND b.status IN ('pending', 'accepted', 'on-the-way', 'reached')
        """,
        (latitude, longitude, location_text, driver["id"])
    )
    update_booking_eta_for_driver(driver["id"])
    db.commit()
    return jsonify({"ok": True})


@app.route("/admin/drivers/clear", methods=["POST"])
def clear_drivers():
    if not admin_required():
        return redirect(url_for("login"))

    cursor.execute("DELETE FROM drivers")
    db.commit()
    return redirect(url_for("admin_drivers"))


# ---------------- ADMIN AMBULANCES ----------------
@app.route("/admin/ambulances")
def admin_ambulances():
    if not admin_required():
        return redirect(url_for("login"))

    sync_resource_statuses()

    cursor.execute("""
        SELECT
            a.id,
            a.vehicle_number,
            a.type,
            a.hospital_id,
            h.name AS hospital_name,
            a.current_latitude,
            a.current_longitude,
            a.current_location_text,
            CASE
                WHEN LOWER(TRIM(a.status))='available' THEN 'available'
                ELSE LOWER(TRIM(a.status))
            END AS status,
            CASE
                WHEN EXISTS (
                    SELECT 1 FROM bookings b
                    WHERE b.ambulance_id = a.id
                      AND b.status IN ('pending', 'accepted', 'on-the-way', 'reached')
                ) THEN 1
                ELSE 0
            END AS has_active_booking
        FROM ambulances a
        LEFT JOIN hospitals h ON h.id = a.hospital_id
        ORDER BY a.id DESC
    """)
    ambulances = cursor.fetchall()
    cursor.execute("SELECT id, name FROM hospitals ORDER BY name ASC")
    hospitals = cursor.fetchall()
    return render_template("admin/ambulances.html", ambulances=ambulances, hospitals=hospitals)


@app.route("/admin/ambulances/add", methods=["POST"])
def add_ambulance():
    if not admin_required():
        return redirect(url_for("login"))

    vehicle_number = request.form["vehicle_number"]
    amb_type = request.form["type"]
    hospital_id = request.form.get("hospital_id") or None
    current_location_text = request.form.get("current_location_text", "").strip()
    current_latitude = parse_coordinate(request.form.get("current_latitude"))
    current_longitude = parse_coordinate(request.form.get("current_longitude"))

    cursor.execute(
        """
        INSERT INTO ambulances (vehicle_number, type, hospital_id, status, current_latitude, current_longitude, current_location_text)
        VALUES (%s,%s,%s,'available',%s,%s,%s)
        """,
        (vehicle_number, amb_type, hospital_id, current_latitude, current_longitude, current_location_text or None)
    )
    db.commit()
    return redirect(url_for("admin_ambulances"))


@app.route("/admin/ambulances/hospital/<int:amb_id>", methods=["POST"])
def assign_ambulance_hospital(amb_id):
    if not admin_required():
        return redirect(url_for("login"))

    hospital_id = request.form.get("hospital_id") or None
    cursor.execute("UPDATE ambulances SET hospital_id=%s WHERE id=%s", (hospital_id, amb_id))
    db.commit()
    return redirect(url_for("admin_ambulances"))


@app.route("/admin/ambulances/status/<int:amb_id>")
def toggle_ambulance_status(amb_id):
    if not admin_required():
        return redirect(url_for("login"))

    cursor.execute(
        """
        SELECT COUNT(*) AS c
        FROM bookings
        WHERE ambulance_id=%s AND status IN ('pending', 'accepted', 'on-the-way', 'reached')
        """,
        (amb_id,)
    )
    has_active = (cursor.fetchone() or {}).get("c", 0) > 0
    if has_active:
        cursor.execute("UPDATE ambulances SET status='assigned' WHERE id=%s", (amb_id,))
        db.commit()
        return redirect(url_for("admin_ambulances"))

    cursor.execute(
        "UPDATE ambulances SET status = IF(status='available','assigned','available') WHERE id=%s",
        (amb_id,)
    )
    db.commit()
    return redirect(url_for("admin_ambulances"))


@app.route("/admin/ambulances/delete/<int:amb_id>")
def delete_ambulance(amb_id):
    if not admin_required():
        return redirect(url_for("login"))

    cursor.execute("DELETE FROM ambulances WHERE id=%s", (amb_id,))
    db.commit()
    return redirect(url_for("admin_ambulances"))


@app.route("/admin/ambulances/update/<int:amb_id>", methods=["POST"])
def update_ambulance(amb_id):
    if not admin_required():
        return redirect(url_for("login"))

    vehicle_number = request.form.get("vehicle_number", "").strip()
    amb_type = request.form.get("type", "").strip()
    status = request.form.get("status", "available").strip()

    if not vehicle_number or not amb_type or status not in {"available", "assigned", "on-duty"}:
        return render_template("shared/message.html", message="Invalid ambulance update input.")

    cursor.execute(
        "UPDATE ambulances SET vehicle_number=%s, type=%s, status=%s WHERE id=%s",
        (vehicle_number, amb_type, status, amb_id)
    )
    db.commit()
    return redirect(url_for("admin_ambulances"))


@app.route("/admin/ambulances/location/<int:amb_id>", methods=["POST"])
def update_ambulance_location(amb_id):
    if not admin_required():
        return redirect(url_for("login"))

    current_location_text = request.form.get("current_location_text", "").strip()
    current_latitude = parse_coordinate(request.form.get("current_latitude"))
    current_longitude = parse_coordinate(request.form.get("current_longitude"))

    cursor.execute(
        """
        UPDATE ambulances
        SET current_location_text=%s, current_latitude=%s, current_longitude=%s
        WHERE id=%s
        """,
        (current_location_text or None, current_latitude, current_longitude, amb_id)
    )
    db.commit()
    return redirect(url_for("admin_ambulances"))


@app.route("/admin/ambulances/clear", methods=["POST"])
def clear_ambulances():
    if not admin_required():
        return redirect(url_for("login"))

    cursor.execute("DELETE FROM ambulances")
    db.commit()
    return redirect(url_for("admin_ambulances"))


# ---------------- ADMIN HOSPITALS ----------------
@app.route("/admin/hospitals")
def admin_hospitals():
    if not admin_required():
        return redirect(url_for("login"))

    cursor.execute(
        """
        SELECT
            h.id,
            h.name,
            h.location_text,
            h.latitude,
            h.longitude,
            h.emergency_contact,
            COUNT(a.id) AS ambulance_count
        FROM hospitals h
        LEFT JOIN ambulances a ON a.hospital_id = h.id
        GROUP BY h.id, h.name, h.location_text, h.latitude, h.longitude, h.emergency_contact
        ORDER BY h.name ASC
        """
    )
    hospitals = cursor.fetchall()
    return render_template("admin/hospitals.html", hospitals=hospitals)


@app.route("/admin/hospitals/add", methods=["POST"])
def add_hospital():
    if not admin_required():
        return redirect(url_for("login"))

    name = request.form.get("name", "").strip()
    location_text = request.form.get("location_text", "").strip()
    latitude = parse_coordinate(request.form.get("latitude"))
    longitude = parse_coordinate(request.form.get("longitude"))
    emergency_contact = request.form.get("emergency_contact", "").strip()

    if not name:
        return render_template("shared/message.html", message="Hospital name is required.")

    cursor.execute(
        """
        INSERT INTO hospitals (name, location_text, latitude, longitude, emergency_contact)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (name, location_text or None, latitude, longitude, emergency_contact or None)
    )
    db.commit()
    return redirect(url_for("admin_hospitals"))


@app.route("/admin/hospitals/update/<int:hospital_id>", methods=["POST"])
def update_hospital(hospital_id):
    if not admin_required():
        return redirect(url_for("login"))

    name = request.form.get("name", "").strip()
    location_text = request.form.get("location_text", "").strip()
    latitude = parse_coordinate(request.form.get("latitude"))
    longitude = parse_coordinate(request.form.get("longitude"))
    emergency_contact = request.form.get("emergency_contact", "").strip()

    if not name:
        return render_template("shared/message.html", message="Hospital name is required.")

    cursor.execute(
        """
        UPDATE hospitals
        SET name=%s, location_text=%s, latitude=%s, longitude=%s, emergency_contact=%s
        WHERE id=%s
        """,
        (name, location_text or None, latitude, longitude, emergency_contact or None, hospital_id)
    )
    db.commit()
    return redirect(url_for("admin_hospitals"))


@app.route("/admin/hospitals/delete/<int:hospital_id>", methods=["POST"])
def delete_hospital(hospital_id):
    if not admin_required():
        return redirect(url_for("login"))

    cursor.execute("UPDATE ambulances SET hospital_id=NULL WHERE hospital_id=%s", (hospital_id,))
    cursor.execute("UPDATE bookings SET hospital_id=NULL WHERE hospital_id=%s", (hospital_id,))
    cursor.execute("DELETE FROM hospitals WHERE id=%s", (hospital_id,))
    db.commit()
    return redirect(url_for("admin_hospitals"))


@app.route("/admin/hospitals/clear", methods=["POST"])
def clear_hospitals():
    if not admin_required():
        return redirect(url_for("login"))

    cursor.execute("UPDATE ambulances SET hospital_id=NULL")
    cursor.execute("UPDATE bookings SET hospital_id=NULL")
    cursor.execute("DELETE FROM hospitals")
    db.commit()
    return redirect(url_for("admin_hospitals"))

# ---------------- NOTIFICATIONS HELPER ----------------
def get_default_admin_user_id():
    cursor.execute("SELECT id FROM users WHERE role='admin' ORDER BY id ASC LIMIT 1")
    admin_user = cursor.fetchone() or {}
    return admin_user.get("id")


def create_role_notification(message, role="user", user_id=None, driver_id=None, send_sms_flag=True):
    normalized_role = (role or "user").strip().lower()
    is_read = False
    status = "unread"
    safe_user_id = user_id

    if safe_user_id is None:
        safe_user_id = get_default_admin_user_id()

    columns = []
    values = []
    params = []

    if table_has_column("notifications", "user_id"):
        columns.append("user_id")
        values.append("%s")
        params.append(safe_user_id)

    if table_has_column("notifications", "driver_id"):
        columns.append("driver_id")
        values.append("%s")
        params.append(driver_id)

    columns.append("message")
    values.append("%s")
    params.append(message)

    if table_has_column("notifications", "role"):
        columns.append("role")
        values.append("%s")
        params.append(normalized_role)

    if table_has_column("notifications", "status"):
        columns.append("status")
        values.append("%s")
        params.append(status)

    if table_has_column("notifications", "is_read"):
        columns.append("is_read")
        values.append("%s")
        params.append(is_read)

    if table_has_column("notifications", "created_at"):
        columns.append("created_at")
        values.append("%s")
        params.append(datetime.now())

    insert_sql = f"INSERT INTO notifications ({', '.join(columns)}) VALUES ({', '.join(values)})"
    cursor.execute(insert_sql, tuple(params))
    db.commit()

    if not send_sms_flag:
        return

    if normalized_role == "user" and user_id:
        cursor.execute("SELECT phone FROM users WHERE id=%s", (user_id,))
        user = cursor.fetchone() or {}
        user_phone = (user.get("phone") or "").strip()
        if user_phone:
            send_sms(user_phone, message)
    elif normalized_role == "driver" and driver_id:
        cursor.execute("SELECT phone FROM drivers WHERE id=%s", (driver_id,))
        driver = cursor.fetchone() or {}
        driver_phone = (driver.get("phone") or "").strip()
        if driver_phone:
            send_sms(driver_phone, message)


def create_notification(user_id, message):
    create_role_notification(message, role="user", user_id=user_id)


def create_driver_notification(driver_id, message):
    create_role_notification(message, role="driver", driver_id=driver_id)


def create_admin_notification(message):
    create_role_notification(message, role="admin", send_sms_flag=False)


def normalize_phone(phone):
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    if not digits:
        return ""
    if len(digits) == 10:
        return f"+91{digits}"
    if len(digits) == 12 and digits.startswith("91"):
        return f"+{digits}"
    if (phone or "").startswith("+"):
        return phone.strip()
    return f"+{digits}"


def send_sms(phone, message):
    to_number = normalize_phone(phone)
    if not to_number:
        print("SMS skipped: user phone not available.")
        return False
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER):
        print("SMS skipped: set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER.")
        return False

    payload = urlencode({
        "To": to_number,
        "From": TWILIO_FROM_NUMBER,
        "Body": message[:500]
    }).encode("utf-8")
    endpoint = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
    auth = base64.b64encode(f"{TWILIO_ACCOUNT_SID}:{TWILIO_AUTH_TOKEN}".encode("utf-8")).decode("ascii")
    req = Request(endpoint, data=payload, method="POST")
    req.add_header("Authorization", f"Basic {auth}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")

    try:
        with urlopen(req, timeout=15) as response:
            if 200 <= response.status < 300:
                print(f"SMS sent successfully to {to_number}")
                return True
            print(f"SMS failed with status {response.status}")
            return False
    except HTTPError as e:
        err = e.read().decode("utf-8", errors="ignore")
        print(f"SMS HTTP error ({e.code}): {err}")
        return False
    except URLError as e:
        print(f"SMS connection error: {e}")
        return False
    except Exception as e:
        print(f"SMS unexpected error: {e}")
        return False

def send_email(to_email, subject, body):
    try:
        msg = MIMEMultipart()
        msg["From"] = EMAIL_ADDRESS
        msg["To"] = to_email
        msg["Subject"] = subject

        msg.attach(MIMEText(body, "plain"))

        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
        server.send_message(msg)
        server.quit()

    except Exception as e:
        print("Email error:", e)
# ---------------- ADMIN BOOKINGS ----------------
@app.route("/admin/bookings")
def admin_bookings():
    if not admin_required():
        return redirect("/login")

    sync_resource_statuses()

    cursor.execute("""
        SELECT 
            b.id AS booking_id,
            b.user_id,
            u.name AS user_name,
            u.email,
            u.phone,
            h.name AS hospital_name,
            b.pickup_location,
            b.pickup_latitude,
            b.pickup_longitude,
            b.disease_type,
            b.emergency_level,
            b.medical_notes,
            b.estimated_arrival_minutes,
            b.ambulance_id,
            b.driver_id,
            a.vehicle_number AS ambulance_number,
            a.current_latitude AS ambulance_latitude,
            a.current_longitude AS ambulance_longitude,
            d.name AS driver_name,
            d.phone AS driver_phone,
            d.current_latitude AS driver_latitude,
            d.current_longitude AS driver_longitude,
            b.status
        FROM bookings b
        JOIN users u ON b.user_id = u.id
        LEFT JOIN hospitals h ON h.id = b.hospital_id
        LEFT JOIN ambulances a ON b.ambulance_id = a.id
        LEFT JOIN drivers d ON b.driver_id = d.id
        ORDER BY b.id DESC
    """)
    bookings = cursor.fetchall()

    cursor.execute("SELECT id, vehicle_number, status FROM ambulances ORDER BY id DESC")
    assign_ambulances = cursor.fetchall()
    cursor.execute("SELECT id, name, status FROM drivers ORDER BY id DESC")
    assign_drivers = cursor.fetchall()

    return render_template(
        "admin/bookings.html",
        bookings=bookings,
        assign_ambulances=assign_ambulances,
        assign_drivers=assign_drivers
    )


@app.route("/admin/live-tracking")
def admin_live_tracking():
    if not admin_required():
        return redirect(url_for("login"))
    return render_template("admin/live_tracking.html")

@app.route("/admin/fleet")
def admin_fleet_tracking():
    if not admin_required():
        return redirect(url_for("login"))
    return render_template("admin/admin_live_tracking.html")


@app.route("/admin/api/live-tracking")
def admin_live_tracking_api():
    if not admin_required():
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    cursor.execute(
        """
        SELECT
            b.id AS booking_id,
            b.status,
            h.name AS hospital_name,
            b.pickup_location,
            b.pickup_latitude,
            b.pickup_longitude,
            b.estimated_arrival_minutes,
            u.name AS user_name,
            u.phone AS user_phone,
            a.vehicle_number,
            a.current_latitude AS ambulance_latitude,
            a.current_longitude AS ambulance_longitude,
            a.current_location_text AS ambulance_location_text,
            d.name AS driver_name,
            d.phone AS driver_phone,
            d.current_latitude AS driver_latitude,
            d.current_longitude AS driver_longitude,
            d.current_location_text AS driver_location_text
        FROM bookings b
        JOIN users u ON u.id = b.user_id
        LEFT JOIN hospitals h ON h.id = b.hospital_id
        LEFT JOIN ambulances a ON a.id = b.ambulance_id
        LEFT JOIN drivers d ON d.id = b.driver_id
        WHERE b.status IN ('pending', 'accepted', 'on-the-way', 'reached')
        ORDER BY b.id DESC
        """
    )
    rows = cursor.fetchall()
    payload = []
    for row in rows:
        payload.append({
            "booking_id": row["booking_id"],
            "status": row["status"],
            "hospital_name": row.get("hospital_name") or "Not selected",
            "pickup_location": row.get("pickup_location") or "Not provided",
            "pickup_latitude": float(row["pickup_latitude"]) if row.get("pickup_latitude") is not None else None,
            "pickup_longitude": float(row["pickup_longitude"]) if row.get("pickup_longitude") is not None else None,
            "estimated_arrival_minutes": row.get("estimated_arrival_minutes"),
            "user_name": row.get("user_name") or "User",
            "user_phone": row.get("user_phone") or "",
            "vehicle_number": row.get("vehicle_number") or "Not assigned",
            "ambulance_latitude": float(row["ambulance_latitude"]) if row.get("ambulance_latitude") is not None else None,
            "ambulance_longitude": float(row["ambulance_longitude"]) if row.get("ambulance_longitude") is not None else None,
            "ambulance_location_text": row.get("ambulance_location_text") or "",
            "driver_name": row.get("driver_name") or "Not assigned",
            "driver_phone": row.get("driver_phone") or "",
            "driver_latitude": float(row["driver_latitude"]) if row.get("driver_latitude") is not None else None,
            "driver_longitude": float(row["driver_longitude"]) if row.get("driver_longitude") is not None else None,
            "driver_location_text": row.get("driver_location_text") or ""
        })
    return jsonify({"ok": True, "data": payload})


@app.route("/admin/bookings/assign/<int:booking_id>", methods=["POST"])
def assign_booking_resources(booking_id):
    if not admin_required():
        return redirect("/login")

    ambulance_id = request.form.get("ambulance_id")
    driver_id = request.form.get("driver_id")

    if not ambulance_id or not driver_id:
        return render_template("shared/message.html", message="Select both ambulance and driver.")

    cursor.execute("SELECT user_id, status FROM bookings WHERE id=%s", (booking_id,))
    booking = cursor.fetchone()
    if not booking:
        return render_template("shared/message.html", message="Booking not found.")

    cursor.execute("SELECT id FROM ambulances WHERE id=%s AND status='available'", (ambulance_id,))
    amb = cursor.fetchone()
    cursor.execute("SELECT id FROM drivers WHERE id=%s AND status='available'", (driver_id,))
    drv = cursor.fetchone()
    if not amb or not drv:
        return render_template("shared/message.html", message="Selected ambulance/driver is not available.")

    cursor.execute(
        """
        SELECT pickup_latitude, pickup_longitude
        FROM bookings
        WHERE id=%s
        """,
        (booking_id,)
    )
    pickup = cursor.fetchone() or {}
    cursor.execute(
        """
        SELECT current_latitude, current_longitude, vehicle_number, hospital_id
        FROM ambulances
        WHERE id=%s
        """,
        (ambulance_id,)
    )
    ambulance_data = cursor.fetchone() or {}
    distance_km = calculate_distance_km(
        float(pickup["pickup_latitude"]) if pickup.get("pickup_latitude") is not None else None,
        float(pickup["pickup_longitude"]) if pickup.get("pickup_longitude") is not None else None,
        float(ambulance_data["current_latitude"]) if ambulance_data.get("current_latitude") is not None else None,
        float(ambulance_data["current_longitude"]) if ambulance_data.get("current_longitude") is not None else None
    )
    eta_minutes = estimate_arrival_minutes(distance_km)

    new_status = "pending" if booking["status"] == "requested" else booking["status"]
    cursor.execute(
        """
        UPDATE bookings
        SET hospital_id=%s, ambulance_id=%s, driver_id=%s, status=%s, estimated_arrival_minutes=%s
        WHERE id=%s
        """,
        (ambulance_data.get("hospital_id"), ambulance_id, driver_id, new_status, eta_minutes, booking_id)
    )
    cursor.execute("UPDATE ambulances SET status='assigned' WHERE id=%s", (ambulance_id,))
    cursor.execute("UPDATE drivers SET status='busy' WHERE id=%s", (driver_id,))
    db.commit()

    create_notification(
            booking["user_id"],
            (
                f"Booking #{booking_id} assigned with ambulance {ambulance_data.get('vehicle_number', '')} "
                f"from hospital #{ambulance_data.get('hospital_id')}. Driver assigned and ready."
                " Waiting for driver acceptance."
                + (f" ETA about {eta_minutes} minutes." if eta_minutes else "")
            )
        )
    create_driver_notification(
        int(driver_id),
        f"LifeLink assigned booking #{booking_id} to you. Review patient details and accept or reject the booking."
    )
    create_admin_notification(
        f"Admin assigned booking #{booking_id} to driver #{driver_id} and ambulance {ambulance_data.get('vehicle_number', '')}."
    )
    return redirect("/admin/bookings")


@app.route("/admin/bookings/edit/<int:booking_id>", methods=["POST"])
def edit_booking(booking_id):
    if not admin_required():
        return redirect("/login")

    status = request.form.get("status", "").strip()
    if status not in {"requested", "pending", "accepted", "on-the-way", "reached", "completed"}:
        return render_template("shared/message.html", message="Invalid booking status.")

    cursor.execute(
        "SELECT user_id, ambulance_id, driver_id FROM bookings WHERE id=%s",
        (booking_id,)
    )
    booking = cursor.fetchone()
    if not booking:
        return render_template("shared/message.html", message="Booking not found.")

    cursor.execute("UPDATE bookings SET status=%s WHERE id=%s", (status, booking_id))

    # Keep resource statuses in sync with edited booking status.
    if booking.get("ambulance_id"):
        new_ambulance_status = "on-duty" if booking_keeps_resources_busy(status) else "available"
        cursor.execute("UPDATE ambulances SET status=%s WHERE id=%s", (new_ambulance_status, booking["ambulance_id"]))
    if booking.get("driver_id"):
        new_driver_status = "busy" if booking_keeps_resources_busy(status) else "available"
        cursor.execute("UPDATE drivers SET status=%s WHERE id=%s", (new_driver_status, booking["driver_id"]))

    db.commit()
    create_notification(booking["user_id"], f"Booking #{booking_id} status updated to {status}.")
    if booking.get("driver_id"):
        create_driver_notification(booking["driver_id"], f"Booking #{booking_id} status changed to {booking_status_label(status)} by admin.")
    create_admin_notification(f"Booking #{booking_id} status edited to {booking_status_label(status)}.")
    return redirect("/admin/bookings")


@app.route("/admin/bookings/clear-completed", methods=["POST"])
def clear_completed_bookings():
    if not admin_required():
        return redirect("/login")

    cursor.execute("DELETE FROM bookings WHERE status='completed'")
    db.commit()
    return redirect("/admin/bookings")

#------------------ BOOKING STATUS UPDATE (WITH RESOURCE ASSIGNMENT) ----------------
@app.route("/admin/bookings/update/<int:booking_id>")
def update_booking_status(booking_id):
    if not admin_required():
        return redirect("/login")

    cursor.execute("""
        SELECT status, user_id FROM bookings WHERE id=%s
    """, (booking_id,))
    booking = cursor.fetchone()

    if not booking:
        return redirect("/admin/bookings")

    current_status = booking["status"]

    # STATUS FLOW
    flow = ["requested", "pending", "accepted", "on-the-way", "reached", "completed"]

    if current_status == "requested":
        # 🚑 Find available ambulance
        cursor.execute("SELECT id FROM ambulances WHERE status='available' LIMIT 1")
        ambulance = cursor.fetchone()

        # 👨‍✈️ Find available driver
        cursor.execute("SELECT id FROM drivers WHERE status='available' LIMIT 1")
        driver = cursor.fetchone()

        if not ambulance or not driver:
            return render_template(
                "shared/message.html",
                type="error",
                heading="No Resources",
                message="No available ambulance or driver at the moment.",
                redirect_url="/admin/bookings"
            )

        # Assign both
        cursor.execute("""
            UPDATE bookings
            SET status='pending',
                ambulance_id=%s,
                driver_id=%s
            WHERE id=%s
        """, (ambulance["id"], driver["id"], booking_id))

        cursor.execute(
            "UPDATE ambulances SET status='on-duty' WHERE id=%s",
            (ambulance["id"],)
        )

        cursor.execute(
            "UPDATE drivers SET status='busy' WHERE id=%s",
            (driver["id"],)
        )
        create_notification(
            booking["user_id"],
            f"Booking #{booking_id} assigned. Driver and ambulance are ready. Waiting for driver acceptance."
        )
        create_driver_notification(driver["id"], f"LifeLink booking #{booking_id} assigned to you. Please accept or reject it from driver dashboard.")
        create_admin_notification(f"Booking #{booking_id} assigned through admin progress flow.")

    elif current_status == "pending":
        cursor.execute(
            "UPDATE bookings SET status='accepted' WHERE id=%s",
            (booking_id,)
        )
        create_notification(
            booking["user_id"],
            f"Booking #{booking_id}: Driver accepted the booking."
        )
        cursor.execute("SELECT driver_id FROM bookings WHERE id=%s", (booking_id,))
        booking_driver = cursor.fetchone()
        if booking_driver and booking_driver.get("driver_id"):
            create_driver_notification(booking_driver["driver_id"], f"Booking #{booking_id} accepted. Use the dashboard to mark when you are on the way.")
        create_admin_notification(f"Booking #{booking_id} moved to accepted.")

    elif current_status == "accepted":
        cursor.execute(
            "UPDATE bookings SET status='on-the-way' WHERE id=%s",
            (booking_id,)
        )
        create_notification(
            booking["user_id"],
            f"Booking #{booking_id}: Ambulance is on the way."
        )
        cursor.execute("SELECT driver_id FROM bookings WHERE id=%s", (booking_id,))
        booking_driver = cursor.fetchone()
        if booking_driver and booking_driver.get("driver_id"):
            create_driver_notification(booking_driver["driver_id"], f"Booking #{booking_id}: proceed to patient pickup now.")
        create_admin_notification(f"Booking #{booking_id} moved to on-the-way.")

    elif current_status == "on-the-way":
        cursor.execute(
            "UPDATE bookings SET status='reached' WHERE id=%s",
            (booking_id,)
        )
        create_notification(
            booking["user_id"],
            f"Booking #{booking_id}: Ambulance has reached your pickup location."
        )
        cursor.execute("SELECT driver_id FROM bookings WHERE id=%s", (booking_id,))
        booking_driver = cursor.fetchone()
        if booking_driver and booking_driver.get("driver_id"):
            create_driver_notification(booking_driver["driver_id"], f"Booking #{booking_id}: patient pickup point reached.")
        create_admin_notification(f"Booking #{booking_id} moved to reached.")

    elif current_status == "reached":
        cursor.execute(
            "UPDATE bookings SET status='completed' WHERE id=%s",
            (booking_id,)
        )

        # FREE ambulance + driver
        cursor.execute("""
            SELECT ambulance_id, driver_id FROM bookings WHERE id=%s
        """, (booking_id,))
        ids = cursor.fetchone()

        if ids["ambulance_id"]:
            cursor.execute(
                "UPDATE ambulances SET status='available' WHERE id=%s",
                (ids["ambulance_id"],)
            )

        if ids["driver_id"]:
            cursor.execute(
                "UPDATE drivers SET status='available' WHERE id=%s",
                (ids["driver_id"],)
            )
        create_notification(
            booking["user_id"],
            f"Booking #{booking_id} completed. Thank you for using LifeLink."
        )
        if ids["driver_id"]:
            create_driver_notification(ids["driver_id"], f"LifeLink booking #{booking_id} completed.")
        create_admin_notification(f"Booking #{booking_id} completed.")

    db.commit()
    return redirect("/admin/bookings")
@app.route("/admin/bookings/delete/<int:booking_id>")
def delete_booking(booking_id):
    if not admin_required():
        return redirect("/login")

    cursor.execute("DELETE FROM bookings WHERE id=%s", (booking_id,))
    db.commit()
    return redirect("/admin/bookings")

# ---------------- ADMIN NOTIFICATIONS ----------------
@app.route("/admin/notifications", methods=["GET", "POST"])
def admin_notifications():
    if not admin_required():
        return redirect(url_for("login"))

    if request.method == "POST":
        target_role = request.form.get("target_role", "user").strip().lower()
        target_user_id = request.form.get("user_id")
        target_driver_id = request.form.get("driver_id")
        message = request.form.get("message", "").strip()
        if target_role == "user" and target_user_id and message:
            create_notification(target_user_id, message)
        elif target_role == "driver" and target_driver_id and message:
            create_driver_notification(target_driver_id, message)
        elif target_role == "admin" and message:
            create_admin_notification(message)
        return redirect("/admin/notifications")

    cursor.execute("SELECT id, name, email FROM users WHERE role='user' ORDER BY id DESC")
    users = cursor.fetchall()
    cursor.execute("SELECT id, name, email FROM drivers ORDER BY id DESC")
    drivers = cursor.fetchall()

    cursor.execute("""
        SELECT 
            n.id,
            n.user_id,
            n.driver_id,
            n.role,
            n.status,
            u.name AS user_name,
            u.email AS user_email,
            d.name AS driver_name,
            d.email AS driver_email,
            n.message,
            n.is_read,
            n.created_at
        FROM notifications n
        LEFT JOIN users u ON n.user_id = u.id
        LEFT JOIN drivers d ON n.driver_id = d.id
        ORDER BY n.created_at DESC
    """)
    notifications = cursor.fetchall()

    return render_template(
        "admin/notifications.html",
        notifications=notifications,
        users=users,
        drivers=drivers
    )


@app.route("/admin/notifications/update/<int:notification_id>", methods=["POST"])
def update_notification(notification_id):
    if not admin_required():
        return redirect(url_for("login"))

    message = request.form.get("message", "").strip()
    if not message:
        return render_template("shared/message.html", message="Notification message cannot be empty.")

    cursor.execute("UPDATE notifications SET message=%s WHERE id=%s", (message, notification_id))
    db.commit()
    return redirect("/admin/notifications")


@app.route("/admin/notifications/delete/<int:notification_id>", methods=["POST"])
def delete_notification(notification_id):
    if not admin_required():
        return redirect(url_for("login"))

    cursor.execute("DELETE FROM notifications WHERE id=%s", (notification_id,))
    db.commit()
    return redirect("/admin/notifications")


@app.route("/admin/notifications/clear", methods=["POST"])
def clear_notifications():
    if not admin_required():
        return redirect(url_for("login"))

    cursor.execute("DELETE FROM notifications")
    db.commit()
    return redirect("/admin/notifications")

# ---------------- USER NOTIFICATIONS ----------------
@app.route("/notifications")
def user_notifications():
    if admin_required():
        return redirect("/admin/notifications")

    if driver_required():
        cursor.execute(
            """
            SELECT id, message, created_at, status
            FROM notifications
            WHERE role='driver' AND driver_id=%s
            ORDER BY created_at DESC
            """,
            (session["driver_id"],)
        )
        notifications = cursor.fetchall()
        cursor.execute(
            """
            UPDATE notifications
            SET is_read=TRUE, status='read'
            WHERE role='driver' AND driver_id=%s
            """,
            (session["driver_id"],)
        )
        db.commit()
        return render_template(
            "shared/notifications.html",
            notifications=notifications,
            page_title="Driver Notifications",
            back_url=url_for("driver_dashboard")
        )

    if "user_id" not in session:
        return redirect("/login")

    cursor.execute("""
        SELECT id, message, created_at, status
        FROM notifications
        WHERE user_id=%s AND role='user'
        ORDER BY created_at DESC
    """, (session["user_id"],))
    notifications = cursor.fetchall()

    cursor.execute("""
        UPDATE notifications SET is_read=TRUE, status='read' WHERE user_id=%s AND role='user'
    """, (session["user_id"],))
    db.commit()

    return render_template(
        "shared/notifications.html",
        notifications=notifications,
        page_title="My Notifications",
        back_url="/home"
    )


@app.route("/api/notifications/unread")
def api_unread_notifications():
    role = session.get("role")
    if admin_required():
        notification_sql = """
            SELECT id, message, created_at
            FROM notifications
            WHERE role='admin' AND (is_read=FALSE OR is_read=0 OR status='unread' OR is_read IS NULL)
            ORDER BY created_at ASC
        """
        params = ()
    elif role == "driver" and session.get("driver_id"):
        notification_sql = """
            SELECT id, message, created_at
            FROM notifications
            WHERE role='driver' AND driver_id=%s AND (is_read=FALSE OR is_read=0 OR status='unread' OR is_read IS NULL)
            ORDER BY created_at ASC
        """
        params = (session["driver_id"],)
    elif "user_id" in session:
        notification_sql = """
            SELECT id, message, created_at
            FROM notifications
            WHERE role='user' AND user_id=%s AND (is_read=FALSE OR is_read=0 OR status='unread' OR is_read IS NULL)
            ORDER BY created_at ASC
        """
        params = (session["user_id"],)
    else:
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    try:
        cursor.execute(notification_sql, params)
        rows = cursor.fetchall()
    except Exception:
        traceback.print_exc()
        if not ensure_db_connection():
            return jsonify({"ok": True, "data": []})
        try:
            cursor.execute(notification_sql, params)
            rows = cursor.fetchall()
        except Exception:
            traceback.print_exc()
            return jsonify({"ok": True, "data": []})
    data = []
    for row in rows:
        created_at_value = row.get("created_at")
        if hasattr(created_at_value, "strftime"):
            created_at_text = created_at_value.strftime("%Y-%m-%d %H:%M:%S")
        elif created_at_value is None:
            created_at_text = ""
        else:
            created_at_text = str(created_at_value)
        data.append(
            {
                "id": row["id"],
                "message": row["message"],
                "created_at": created_at_text,
            }
        )
    return jsonify({"ok": True, "data": data})


@app.route("/api/notifications/mark-read/<int:notification_id>", methods=["POST"])
def api_mark_notification_read(notification_id):
    role = session.get("role")
    if admin_required():
        update_sql = "UPDATE notifications SET is_read=TRUE, status='read' WHERE id=%s AND role='admin'"
        params = (notification_id,)
    elif role == "driver" and session.get("driver_id"):
        update_sql = "UPDATE notifications SET is_read=TRUE, status='read' WHERE id=%s AND driver_id=%s AND role='driver'"
        params = (notification_id, session["driver_id"])
    elif "user_id" in session:
        update_sql = "UPDATE notifications SET is_read=TRUE, status='read' WHERE id=%s AND user_id=%s AND role='user'"
        params = (notification_id, session["user_id"])
    else:
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    try:
        cursor.execute(update_sql, params)
        db.commit()
        return jsonify({"ok": True})
    except Exception:
        traceback.print_exc()
        try:
            if ensure_db_connection():
                cursor.execute(update_sql, params)
                db.commit()
        except Exception:
            traceback.print_exc()
        return jsonify({"ok": True})

# ---------------- FEEDBACK ----------------
@app.route("/admin/feedback")
def admin_feedback():
    if not admin_required():
        return redirect(url_for("login"))

    cursor.execute("""
        SELECT 
            f.id,
            f.user_id,
            u.name AS user_name,
            u.email AS user_email,
            f.rating,
            f.message,
            f.admin_reply,
            f.replied_at,
            f.created_at
        FROM feedback f
        JOIN users u ON f.user_id = u.id
        ORDER BY f.created_at DESC
    """)
    feedbacks = cursor.fetchall()

    return render_template(
        "admin/feedback.html",
        feedbacks=feedbacks
    )


@app.route("/admin/feedback/reply/<int:feedback_id>", methods=["POST"])
def reply_feedback(feedback_id):
    if not admin_required():
        return redirect(url_for("login"))

    admin_reply = request.form.get("admin_reply", "").strip()
    if not admin_reply:
        return render_template("shared/message.html", message="Reply cannot be empty.")

    cursor.execute("SELECT user_id FROM feedback WHERE id=%s", (feedback_id,))
    feedback = cursor.fetchone()
    if not feedback:
        return render_template("shared/message.html", message="Feedback not found.")

    cursor.execute(
        "UPDATE feedback SET admin_reply=%s, replied_at=%s WHERE id=%s",
        (admin_reply, datetime.now(), feedback_id)
    )
    db.commit()

    create_notification(
        feedback["user_id"],
        f"Admin replied to your feedback: {admin_reply}"
    )
    return redirect("/admin/feedback")


@app.route("/admin/feedback/clear", methods=["POST"])
def clear_feedback():
    if not admin_required():
        return redirect(url_for("login"))

    cursor.execute("DELETE FROM feedback")
    db.commit()
    return redirect("/admin/feedback")

@app.route("/feedback/submit", methods=["GET", "POST"])
def submit_feedback():
    if "user_id" not in session:
        return redirect(url_for("login"))

    if request.method == "POST":
        cursor.execute(
            "INSERT INTO feedback (user_id, rating, message, created_at) VALUES (%s,%s,%s,%s)",
            (session["user_id"], request.form["rating"], request.form["message"], datetime.now())
        )
        db.commit()
        return render_template("shared/message.html", message="Feedback submitted")

    return render_template("feedback/submit_feedback.html")


# ---------------- BUG TRACKER (ADMIN ONLY) ----------------
@app.route("/admin/bugs", methods=["GET", "POST"])
def admin_bugs():
    if not admin_required():
        return redirect(url_for("login"))

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        priority = request.form.get("priority", "medium").strip().lower()
        assigned_to = request.form.get("assigned_to", "").strip()
        created_by = session.get("name", "Admin")

        if not title:
            return render_template("shared/message.html", message="Bug title is required.")
        if priority not in {"low", "medium", "high", "critical"}:
            priority = "medium"

        try:
            cursor.execute(
                """
                INSERT INTO bug_tickets (title, description, priority, status, assigned_to, created_by, created_at, updated_at)
                VALUES (%s, %s, %s, 'open', %s, %s, %s, %s)
                """,
                (title, description, priority, assigned_to, created_by, datetime.now(), datetime.now())
            )
            db.commit()
        except mysql.connector.Error:
            if not ensure_db_connection():
                return render_template("shared/message.html", message="Database unavailable while saving bug.")
            cursor.execute(
                """
                INSERT INTO bug_tickets (title, description, priority, status, assigned_to, created_by, created_at, updated_at)
                VALUES (%s, %s, %s, 'open', %s, %s, %s, %s)
                """,
                (title, description, priority, assigned_to, created_by, datetime.now(), datetime.now())
            )
            db.commit()
        return redirect(url_for("admin_bugs"))

    cursor.execute("""
        SELECT id, title, description, priority, status, assigned_to, created_by, created_at, updated_at
        FROM bug_tickets
        ORDER BY
            CASE priority
                WHEN 'critical' THEN 1
                WHEN 'high' THEN 2
                WHEN 'medium' THEN 3
                ELSE 4
            END,
            created_at DESC
    """)
    bugs = cursor.fetchall()
    return render_template("admin/bugs.html", bugs=bugs)


@app.route("/admin/bugs/update/<int:bug_id>", methods=["POST"])
def update_bug(bug_id):
    if not admin_required():
        return redirect(url_for("login"))

    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    priority = request.form.get("priority", "medium").strip().lower()
    status = request.form.get("status", "open").strip().lower()
    assigned_to = request.form.get("assigned_to", "").strip()

    if not title:
        return render_template("shared/message.html", message="Bug title is required.")
    if priority not in {"low", "medium", "high", "critical"}:
        priority = "medium"
    if status not in {"open", "in-progress", "resolved", "closed"}:
        status = "open"

    cursor.execute(
        """
        UPDATE bug_tickets
        SET title=%s, description=%s, priority=%s, status=%s, assigned_to=%s, updated_at=%s
        WHERE id=%s
        """,
        (title, description, priority, status, assigned_to, datetime.now(), bug_id)
    )
    db.commit()
    return redirect(url_for("admin_bugs"))


@app.route("/admin/bugs/delete/<int:bug_id>", methods=["POST"])
def delete_bug(bug_id):
    if not admin_required():
        return redirect(url_for("login"))

    cursor.execute("DELETE FROM bug_tickets WHERE id=%s", (bug_id,))
    db.commit()
    return redirect(url_for("admin_bugs"))


@app.route("/admin/bugs/clear", methods=["POST"])
def clear_bugs():
    if not admin_required():
        return redirect(url_for("login"))

    cursor.execute("DELETE FROM bug_tickets")
    db.commit()
    return redirect(url_for("admin_bugs"))



# ---------------- REAL-TIME TRACKING (SocketIO) ----------------

@socketio.on('join')
def on_join(data):
    booking_id = data.get('booking_id')
    if booking_id:
        room = f"booking_{booking_id}"
        join_room(room)
        print(f"User joined room: {room}")

@socketio.on('update_location')
def handle_location_update(data):
    booking_id = data.get('booking_id')
    lat = data.get('lat')
    lng = data.get('lng')
    driver_id = data.get('driver_id')

    if booking_id and lat and lng:
        # Update driver and ambulance location in DB for persistence and polling fallback
        try:
            if driver_id:
                # Update driver table
                cursor.execute(
                    "UPDATE drivers SET current_latitude=%s, current_longitude=%s, last_location_update=%s WHERE id=%s",
                    (lat, lng, datetime.now(), driver_id)
                )
                
                # Update linked ambulance table
                cursor.execute(
                    "UPDATE ambulances SET current_latitude=%s, current_longitude=%s WHERE id = (SELECT ambulance_id FROM drivers WHERE id=%s)",
                    (lat, lng, driver_id)
                )
                db.commit()
        except Exception as e:
            print(f"DB Update Error: {e}")

        # Broadcast to anyone tracking this booking
        room = f"booking_{booking_id}"
        emit('location_update', {
            'booking_id': booking_id,
            'lat': lat,
            'lng': lng,
            'timestamp': datetime.now().strftime("%H:%M:%S")
        }, room=room)

@app.route("/track/<int:booking_id>")
def track_booking(booking_id):
    # Fetch booking details
    cursor.execute("""
        SELECT b.*, u.name as user_name, d.name as driver_name, d.phone as driver_phone, 
               a.vehicle_number, h.name as hospital_name
        FROM bookings b
        JOIN users u ON b.user_id = u.id
        LEFT JOIN drivers d ON b.driver_id = d.id
        LEFT JOIN ambulances a ON b.ambulance_id = a.id
        LEFT JOIN hospitals h ON b.hospital_id = h.id
        WHERE b.id = %s
    """, (booking_id,))
    booking = cursor.fetchone()
    
    if not booking:
        return "Booking not found", 404
        
    return render_template("booking/track.html", booking=booking)


# ---------------- RUN ----------------
if __name__ == "__main__":
    import os
    port = int(os.environ.get("LIFELINK_PORT", 5000))
    socketio.run(app, debug=True, host="127.0.0.1", port=port, allow_unsafe_werkzeug=True)
