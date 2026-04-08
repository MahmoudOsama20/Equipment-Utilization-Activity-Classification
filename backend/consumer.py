import os
import json
import time
import psycopg2
from kafka import KafkaConsumer

# ==============================
# 🔥 CONFIG (Docker + Local)
# ==============================
DB_HOST = os.getenv("DB_HOST", "localhost")          # docker: postgres | local: localhost
KAFKA_SERVER = os.getenv("KAFKA_SERVER", "localhost:9092")

DB_NAME = "equipment_db"
DB_USER = "postgres"
DB_PASS = "0000"

# ==============================
# 🔥 STATE STORAGE
# ==============================
time_stats = {}
activity_stats = {}

# ==============================
# 🔥 CONNECT TO DATABASE (RETRY)
# ==============================
conn = None

for i in range(15):
    try:
        conn = psycopg2.connect(
            dbname=DB_NAME,
            user=DB_USER,
            password=DB_PASS,
            host=DB_HOST,
            port="5432"
        )
        print("✅ Connected to PostgreSQL")
        break
    except Exception as e:
        print(f"⏳ Waiting for PostgreSQL... ({e})")
        time.sleep(3)

if conn is None:
    raise Exception("❌ Could not connect to PostgreSQL")

cursor = conn.cursor()

# ==============================
# 🔥 CREATE TABLE
# ==============================
cursor.execute("""
CREATE TABLE IF NOT EXISTS equipment_logs (
    id SERIAL PRIMARY KEY,
    frame_id INT,
    equipment_id VARCHAR(20),
    state VARCHAR(20),
    activity VARCHAR(20),
    motion_source VARCHAR(20),
    motion_upper FLOAT,
    motion_lower FLOAT,
    utilization FLOAT,
    active_time FLOAT,
    idle_time FLOAT,
    utilization_percent FLOAT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
""")
conn.commit()

# ==============================
# 🔥 CONNECT TO KAFKA (RETRY)
# ==============================
consumer = None

for i in range(20):
    try:
        consumer = KafkaConsumer(
            "equipment_topic",
            bootstrap_servers=KAFKA_SERVER,
            value_deserializer=lambda m: json.loads(m.decode("utf-8")),
            auto_offset_reset="latest",
            enable_auto_commit=True
        )
        print("✅ Connected to Kafka")
        break
    except Exception as e:
        print(f"⏳ Waiting for Kafka... ({e})")
        time.sleep(5)

if consumer is None:
    raise Exception("❌ Could not connect to Kafka")

print("👂 Listening to Kafka...\n")

# ==============================
# 🔥 MAIN LOOP
# ==============================
for msg in consumer:
    data = msg.value

    track_id = data["equipment_id"]
    state = data["state"]
    activity = data["activity"]

    dt = 1 / 25

    # ------------------------------
    # Initialize stats
    # ------------------------------
    if track_id not in time_stats:
        time_stats[track_id] = {"active": 0.0, "idle": 0.0}

    if track_id not in activity_stats:
        activity_stats[track_id] = {
            "DIGGING": 0.0,
            "SWINGING": 0.0,
            "DUMPING": 0.0
        }

    # ------------------------------
    # Update time
    # ------------------------------
    if state == "ACTIVE":
        time_stats[track_id]["active"] += dt
    else:
        time_stats[track_id]["idle"] += dt

    if activity in activity_stats[track_id]:
        activity_stats[track_id][activity] += dt

    # ------------------------------
    # Compute utilization
    # ------------------------------
    total = time_stats[track_id]["active"] + time_stats[track_id]["idle"]
    utilization = 0 if total == 0 else (time_stats[track_id]["active"] / total) * 100

    # ------------------------------
    # Debug output
    # ------------------------------
    print(f"""
🚜 Equipment: {track_id}
Active: {time_stats[track_id]['active']:.2f}s
Idle: {time_stats[track_id]['idle']:.2f}s
Utilization: {utilization:.2f}%

Activity:
DIGGING: {activity_stats[track_id]['DIGGING']:.2f}s
SWINGING: {activity_stats[track_id]['SWINGING']:.2f}s
DUMPING: {activity_stats[track_id]['DUMPING']:.2f}s
""")

    # ------------------------------
    # Insert into DB
    # ------------------------------
    cursor.execute("""
    INSERT INTO equipment_logs (
        frame_id,
        equipment_id,
        state,
        activity,
        motion_source,
        motion_upper,
        motion_lower,
        utilization,
        active_time,
        idle_time,
        utilization_percent
    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, (
        data["frame_id"],
        data["equipment_id"],
        data["state"],
        data["activity"],
        data["motion_source"],
        data["motion_upper"],
        data["motion_lower"],
        data["utilization"],
        time_stats[track_id]["active"],
        time_stats[track_id]["idle"],
        utilization
    ))

    conn.commit()