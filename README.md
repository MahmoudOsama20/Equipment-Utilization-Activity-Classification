# Equipment Utilization & Activity Classification

A real-time, microservices-based pipeline that processes video footage of construction equipment, tracks machine utilization states (ACTIVE / INACTIVE), classifies work activities (Digging, Swinging, Dumping, Waiting), and streams live results to a dashboard through Apache Kafka.

> **Note:** This is a working prototype. The model, data pipeline, preprocessing, and Docker setup are all intentionally kept simple for this stage. Planned improvements are listed in the [Roadmap](#roadmap) section at the bottom.

---

## Table of Contents

- [System Architecture](#system-architecture)
- [Project Structure](#project-structure)
- [How It Works](#how-it-works)
- [Setup & Installation](#setup--installation)
- [Running the System](#running-the-system)
- [Kaggle Training Notebook](#kaggle-training-notebook)
- [Output Format](#output-format)
- [Dashboard](#dashboard)
- [Current State — Prototype](#current-state--prototype)
- [Roadmap](#roadmap)
- [Tech Stack](#tech-stack)

---

## System Architecture

```
YouTube Videos
      │
      ▼
 yt-dlp download
      │
      ▼
┌─────────────────────────────────┐
│         cv_service              │
│                                 │
│  YOLOv8  ──▶  Optical Flow     │
│  (detect)     (articulated)     │
│                    │            │
│          SimpleVideoModel        │
│          (ResNet18 + AvgPool)   │
│                    │            │
│         Kafka Producer          │
└──────────────┬──────────────────┘
               │  topic: equipment_topic
               ▼
        ┌─────────────┐
        │    Kafka    │
        └──────┬──────┘
               │
       ┌───────┴────────┐
       ▼                ▼
┌─────────────┐  ┌─────────────┐
│   backend   │  │  (future:   │
│  consumer   │  │   alerts)   │
│      │      │  └─────────────┘
│      ▼      │
│ PostgreSQL  │
└──────┬──────┘
       │
       ▼
┌─────────────┐
│  Streamlit  │
│  Dashboard  │
└─────────────┘
```

All services run inside Docker Compose.

---

## Project Structure

```
project/
│
├── docker-compose.yml          # Orchestrates all services
│
├── cv_service/                 # Computer vision + Kafka producer
│   ├── main.py
│   ├── Dockerfile
│   └── requirements.txt
│
├── backend/                    # Kafka consumer + PostgreSQL writer
│   ├── consumer.py
│   ├── Dockerfile
│   └── requirements.txt
│
├── ui/                         # Streamlit live dashboard
│   ├── app.py
│   ├── Dockerfile
│   └── requirements.txt
│
├── models/                     # Trained model weights
│   └── activity_model.pth
│
├── videos/                     # Input video files
│   └── video_00001.mp4
│
└── cv-notebook.ipynb           # Full Kaggle training notebook
```

---

## How It Works

### Step 1 — Detection (YOLOv8)

Each video frame is passed through YOLOv8m with ByteTrack enabled. The tracker assigns a persistent ID to each detected machine across frames, so we can track one machine across thousands of frames even if it briefly disappears.

```
Classes accepted: truck, car, bus
Confidence threshold: 0.3
Tracker: ByteTrack (persist=True)
```

> **Why not a custom excavator class?** The standard `yolov8m.pt` model is trained on COCO and has no "excavator" class. The closest classes are `truck` and `car`, which are sufficient for construction site footage. A fine-tuned model on construction data would improve precision.

---

### Step 2 — Articulated Motion Detection (Optical Flow)

This is the core challenge of the assessment: an excavator arm can be actively digging while the machine body and tracks stay completely still. A naive whole-frame motion detector would incorrectly classify this as INACTIVE.

**Solution: Region-based Farneback optical flow**

The bounding box of each detected machine is split horizontally into two regions:

```
┌─────────────────┐
│   UPPER (arm)   │  ← computes flow_upper
├─────────────────┤
│  LOWER (tracks) │  ← computes flow_lower
└─────────────────┘
```

Both regions are analyzed independently. State is assigned by combining both signals:

| Condition | State | Motion Source |
|---|---|---|
| `flow_upper > 0.3` AND `flow_lower > 0.5` | ACTIVE | both |
| `flow_upper > 0.3` only | ACTIVE | arm_only |
| `flow_lower > 0.5` only | ACTIVE | tracks_only |
| neither | INACTIVE | none |

This correctly identifies an excavator digging (arm_only) as ACTIVE even when the machine body is stationary.

---

### Step 3 — Activity Classification (SimpleVideoModel — ResNet18 + Temporal AvgPool)

For each ACTIVE machine, a sliding window of 8 frames is fed into the activity model to classify the current work action.

**Architecture:**

```
Input: (B, C=3, T=8, H=112, W=112)
    │
    ▼
Reshape → (B×T, C, H, W)
    │
    ▼
ResNet18 backbone (frozen, pretrained on ImageNet)
    │
    ▼
Feature vectors: (B×T, 512)
    │
    ▼
Reshape → (B, T, 512)
    │
    ▼
Temporal average pool → (B, 512)
    │
    ▼
Linear classifier → (B, 3)
    │
    ▼
Output: DIGGING | SWINGING | DUMPING
```

**Why SimpleVideoModel (AvgPool) instead of LSTM?**

Two architectures were trained and compared on the same data:

| Model | Val Accuracy |
|---|---|
| ResNet18 + LSTM | 57.9% |
| ResNet18 + TemporalAvgPool (`SimpleVideoModel`) | **63.2%** ✅ |

The LSTM underperformed because the training set is small (~500 clips). LSTMs need substantially more sequence data to learn meaningful temporal patterns. The simpler AvgPool model generalized better at this stage and is also faster at inference, so `SimpleVideoModel` was chosen as the production model. As more data is collected, the LSTM will be revisited.

**Activity labels:**

- `DIGGING` — arm moving downward into ground
- `SWINGING` — rotating to load or unload
- `DUMPING` — arm raised and releasing material
- `WAITING` — machine is INACTIVE (assigned directly, model not called)

---

### Step 4 — Time Tracking & Utilization

Per-machine time stats accumulate every frame using the actual video FPS:

```
dt = 1.0 / video_fps

if state == "ACTIVE":
    active_time += dt
else:
    idle_time += dt

utilization_percent = 100 * active_time / (active_time + idle_time)
```

---

### Step 5 — Kafka Streaming

The CV service publishes one JSON message per detected machine per frame to the `equipment_topic` Kafka topic:

```json
{
  "frame_id": 450,
  "equipment_id": "EX-0001",
  "label": "truck",
  "state": "ACTIVE",
  "activity": "DIGGING",
  "motion_source": "arm_only",
  "motion_upper": 1.4709,
  "motion_lower": 0.3815,
  "utilization": 57.14,
  "active_time": 12.5,
  "idle_time": 9.2
}
```

---

### Step 6 — Storage (PostgreSQL)

The backend consumer reads from Kafka and inserts every event into the `equipment_logs` table:

```sql
CREATE TABLE equipment_logs (
    id                  SERIAL PRIMARY KEY,
    frame_id            INT,
    equipment_id        VARCHAR(20),
    state               VARCHAR(20),
    activity            VARCHAR(20),
    motion_source       VARCHAR(20),
    motion_upper        FLOAT,
    motion_lower        FLOAT,
    utilization         FLOAT,
    active_time         FLOAT,
    idle_time           FLOAT,
    utilization_percent FLOAT,
    timestamp           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

---

## Setup & Installation

### Prerequisites

- Docker Desktop (Windows/Mac) or Docker Engine (Linux)
- At least 8 GB RAM available for Docker
- GPU optional (CPU works but is slower for cv_service)

### 1. Clone the repository

```bash
git clone https://github.com/your-username/equipment-utilization.git
cd equipment-utilization
```

### 2. Add your video and model files

Place your video file and trained model weights in the correct folders:

```
project/videos/video_00001.mp4
project/models/activity_model.pth
```

> To train the model yourself, see the [Kaggle Training Notebook](#kaggle-training-notebook) section below.

### 3. Configure the video path

In `docker-compose.yml`, update the volume mounts to point to your actual folders:

```yaml
cv_service:
  volumes:
    - "C:/your/path/to/videos:/videos"
    - "C:/your/path/to/models:/models"
```

### 4. Build and run

```bash
docker compose up --build
```

This starts all 5 services: Zookeeper, Kafka, PostgreSQL, cv_service, backend, and the Streamlit UI.

---

## Running the System

Once all containers are running, open the dashboard at:

```
http://localhost:8501
```

To check if data is flowing through the pipeline:

```bash
# Check all containers are running
docker ps

# Watch cv_service logs
docker logs cv_service -f

# Watch backend/consumer logs
docker logs backend -f

# Check if data reached PostgreSQL
docker exec -it postgres psql -U postgres -d equipment_db \
  -c "SELECT COUNT(*) FROM equipment_logs;"

# Check if Kafka has messages
docker exec -it kafka kafka-console-consumer \
  --bootstrap-server localhost:9092 \
  --topic equipment_topic \
  --from-beginning \
  --max-messages 3
```

To stop everything:

```bash
docker compose down
```

To stop and remove all data (full reset):

```bash
docker compose down -v
```

---

## Kaggle Training Notebook

The full model training pipeline is in `cv-notebook.ipynb`. It is designed to run on a Kaggle GPU notebook (T4).

### Sections in the notebook

| Section | Description |
|---|---|
| 1–2 | Install dependencies, imports, configuration |
| 3 | Download videos from YouTube via yt-dlp |
| 4 | Load YOLOv8m |
| 5 | Extract frames at 2fps, create 16-frame clips, filter by equipment presence |
| 6 | Articulated optical flow motion analysis per clip |
| 7 | Auto-label INACTIVE clips as WAITING, generate manual review file for ACTIVE clips |
| 8 | Merge auto + manual labels, build training dataset |
| 9 | Train and compare ResNet+LSTM vs SimpleVideoModel (ResNet+AvgPool) — SimpleVideoModel selected as best |
| 10 | Full inference pipeline with YOLO + optical flow + activity model |
| 11 | Results, plots, and save to JSON/CSV |

### Labeling strategy

Labeling was done in two passes:

1. **Automatic:** All INACTIVE clips (detected by optical flow) were labeled `WAITING` automatically — no manual effort needed.
2. **Manual:** All ACTIVE clips were exported to `labels_manual.csv`, manually reviewed and assigned one of `DIGGING`, `SWINGING`, or `DUMPING`, then uploaded as a separate Kaggle dataset and merged back.

### Training data

| Class | Clips |
|---|---|
| SWINGING | ~220 |
| DIGGING | ~190 |
| DUMPING | ~93 |
| WAITING | auto-labeled |
| **Total** | **~503 clips** |

---

## Output Format

The inference pipeline produces an `output.json` file (one record per line):

```json
{"frame_id": 8, "equipment_id": "EX-9565", "label": "truck", "state": "INACTIVE", "activity": "WAITING", "motion_source": "none", "motion_upper": 0.1123, "motion_lower": 0.4237, "utilization": 0.0, "active_time": 0.0, "idle_time": 0.267}
{"frame_id": 9, "equipment_id": "EX-9565", "label": "truck", "state": "ACTIVE",   "activity": "DIGGING",  "motion_source": "both",     "motion_upper": 0.419,  "motion_lower": 1.6488, "utilization": 50.0, "active_time": 0.033, "idle_time": 0.267}
```

Sample results from `video_00001.mp4`:

```
Total frames processed : 35,011
Total detection records: 39,597

State distribution:
  ACTIVE      25,446  (64.3%)
  INACTIVE    14,151  (35.7%)

Activity distribution:
  WAITING     14,151
  SWINGING    11,073
  DIGGING      9,470
  DUMPING      4,903
```

---

## Dashboard

The Streamlit dashboard at `localhost:8501` auto-refreshes every second and displays:

- **Per-machine metrics** — Active Time, Idle Time, Utilization %
- **Activity distribution** — bar chart of DIGGING / SWINGING / DUMPING / WAITING
- **Utilization over time** — line chart of utilization percentage across frames

---

## Current State — Prototype

This is an end-to-end working prototype. Every component functions correctly but is intentionally kept simple. The focus at this stage was getting the full pipeline running — from video input to a live dashboard — rather than maximizing performance.

What works right now:
- Full pipeline runs end-to-end via Docker Compose
- YOLO detects and tracks machines with persistent IDs across frames
- Articulated motion correctly identifies arm-only movement as ACTIVE
- Activity model classifies DIGGING, SWINGING, DUMPING, and WAITING
- Results stream through Kafka and appear live in the Streamlit dashboard
- All events are persisted to PostgreSQL

What is not yet production-ready:
- The activity model achieved 63% validation accuracy on a small dataset of 503 clips
- YOLO uses generic COCO classes instead of a fine-tuned construction equipment model
- The Docker setup requires manual volume path configuration and has not been fully tested across all environments
- Optical flow thresholds are manually set and not auto-calibrated per video

---

## Roadmap

**Data:**
- Add more YouTube construction site videos (target: 10+ videos, 3,000+ labeled clips)
- Balance the dataset — Dumping clips are underrepresented (~93 clips vs ~220 Swinging)
- Auto-augment clips with flips, brightness/contrast shifts, and speed changes

**Model:**
- Re-evaluate ResNet18 + LSTM once the dataset grows beyond 2,000 clips
- Experiment with a fine-tuned C3D or SlowFast model for better temporal modeling
- Fine-tune YOLOv8 on a construction equipment dataset (Roboflow has labeled datasets) for proper `excavator` and `dump_truck` class detection instead of using generic COCO classes

**Preprocessing:**
- Add a bounding box size filter to reject small detections (passing cars, people)
- Auto-calibrate optical flow thresholds per video instead of using fixed values
- Improve the ROI split — instead of a simple top/bottom 50% split, use keypoint-based splitting to better separate the arm from the tracks

**Infrastructure:**
- Simplify the Docker volume configuration so it works out of the box without manual path edits
- Add a Kafka health check so cv_service waits properly before producing
- Add frame overlay output: draw bounding boxes, state labels, and utilization % directly on the video
- Replace the hardcoded `dt = 1/25` in `consumer.py` with the actual frame timing from the Kafka payload

---

## Tech Stack

| Component | Technology |
|---|---|
| Object detection | YOLOv8m (Ultralytics) |
| Object tracking | ByteTrack |
| Motion analysis | Farneback Optical Flow (OpenCV) |
| Activity model | SimpleVideoModel — ResNet18 + Temporal AvgPool (PyTorch) |
| Message broker | Apache Kafka 7.5.0 |
| Database | PostgreSQL 15 |
| Dashboard | Streamlit |
| Containerization | Docker Compose |
| Training environment | Kaggle GPU (T4) |
