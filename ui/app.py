import streamlit as st
import pandas as pd
import time
import os
from sqlalchemy import create_engine, text

st.set_page_config(layout="wide")
st.title("🚜 Equipment Utilization Dashboard (DB)")

# ==============================
# 🔥 CONFIG (LOCAL + DOCKER)
# ==============================
DB_HOST = os.getenv("DB_HOST", "localhost")  # 🔥 key fix
DB_USER = "postgres"
DB_PASS = os.getenv("DB_PASSWORD", "0000")
DB_NAME = "equipment_db"

# ==============================
# 🔥 CONNECT TO DATABASE (RETRY)
# ==============================
engine = None

for _ in range(10):
    try:
        engine = create_engine(
            f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:5432/{DB_NAME}"
        )

        # test connection
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))

        break
    except Exception as e:
        st.warning(f"Waiting for database... ({e})")
        time.sleep(2)

if engine is None:
    st.error("❌ Could not connect to database")
    st.stop()

# ==============================
# 🔥 ENSURE TABLE EXISTS
# ==============================
with engine.connect() as conn:
    conn.execute(text("""
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
    """))

# ==============================
# 🔥 FETCH DATA
# ==============================
query = """
SELECT *
FROM equipment_logs
ORDER BY id DESC
LIMIT 500
"""

try:
    df = pd.read_sql(query, engine)
except Exception as e:
    st.error(f"Database error: {e}")
    st.stop()

if df.empty:
    st.warning("No data yet... (Kafka/consumer not sending)")
    st.stop()

# ==============================
# 🔥 PROCESS PER EQUIPMENT
# ==============================
st.subheader("📊 Live Equipment Metrics")

equipment_ids = df["equipment_id"].unique()

for eq in equipment_ids:

    sub = df[df["equipment_id"] == eq].sort_values("id")

    st.markdown(f"### 🚜 {eq}")

    # latest row
    latest = sub.iloc[-1]

    active_time = latest["active_time"]
    idle_time = latest["idle_time"]
    utilization = latest["utilization_percent"]

    col1, col2, col3 = st.columns(3)

    col1.metric("Active Time (s)", f"{active_time:.2f}")
    col2.metric("Idle Time (s)", f"{idle_time:.2f}")
    col3.metric("Utilization (%)", f"{utilization:.2f}")

    # ------------------------------
    # Activity distribution
    # ------------------------------
    st.write("Activity Distribution")
    activity_counts = sub["activity"].value_counts()
    st.bar_chart(activity_counts)

    # ------------------------------
    # Utilization over time
    # ------------------------------
    st.write("Utilization Over Time")
    st.line_chart(sub.set_index("id")["utilization_percent"])

    st.divider()

# ==============================
# 🔥 AUTO REFRESH
# ==============================
time.sleep(1)
st.rerun()