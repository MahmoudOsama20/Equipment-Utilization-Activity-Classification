import os
import cv2
import torch
import numpy as np
import json
from kafka import KafkaProducer
from torchvision import models
import torch.nn as nn
from ultralytics import YOLO

# ==============================
# 🔥 CONFIG
# ==============================
VIDEO_PATH = "E:\\CV Project\\project\\videos\\video_00001.mp4"
MODEL_PATH = "E:\\CV Project\\project\\models\\activity_model.pth"
KAFKA_TOPIC = "equipment_topic"
KAFKA_SERVER = "localhost:9092"
DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")
yolo_model = YOLO("E:\\CV Project\\project\\cv_service\\yolov8m.pt")

# ==============================
# 🔥 MODEL DEFINITION
# ==============================
class SimpleVideoModel(nn.Module): # RESNET18 + Temporal AvgPool
    def __init__(self, num_classes=3):
        super().__init__()

        self.backbone = models.resnet18(pretrained=True)
        self.backbone.fc = nn.Identity()

        self.classifier = nn.Linear(512, num_classes)

    def forward(self, x):  # (B, C, T, H, W)
        B, C, T, H, W = x.shape

        x = x.permute(0,2,1,3,4)      # (B, T, C, H, W)
        x = x.reshape(B*T, C, H, W)   # (B*T, C, H, W)

        feats = self.backbone(x)      # (B*T, 512)
        feats = feats.view(B, T, -1)  # (B, T, 512)

        feats = feats.mean(dim=1)     # 🔥 temporal average

        return self.classifier(feats)

# ==============================
# 🔥 LOAD MODEL
# ==============================
model = SimpleVideoModel(num_classes=3).to(DEVICE)
model.load_state_dict(torch.load(MODEL_PATH, map_location=DEVICE))
model.eval()

ACTIVITY_LABELS = ["DIGGING", "SWINGING", "DUMPING"]



# ==============================
# 🔥 KAFKA PRODUCER
# ==============================
producer = KafkaProducer(
    # bootstrap_servers=KAFKA_SERVER,
    bootstrap_servers=os.getenv("KAFKA_SERVER", "localhost:9092"),
    value_serializer=lambda v: json.dumps(v).encode("utf-8")
)

# ==============================
# 🔥 HELPERS
# ==============================
frame_buffers = {}
motion_history = {}
time_stats = {}

def update_buffer(track_id, roi, num_frames=8):
    if track_id not in frame_buffers:
        frame_buffers[track_id] = []

    roi = cv2.resize(roi, (112, 112))
    roi = roi.astype(np.float32) / 255.0

    buffer = frame_buffers[track_id]
    buffer.append(roi)

    if len(buffer) > num_frames:
        buffer.pop(0)

    return buffer

def compute_optical_flow(prev, curr):
    prev = cv2.GaussianBlur(prev, (5, 5), 0)
    curr = cv2.GaussianBlur(curr, (5, 5), 0)

    flow = cv2.calcOpticalFlowFarneback(
        prev, curr, None,
        0.5, 3, 15, 3, 5, 1.2, 0
    )

    magnitude = np.sqrt(flow[..., 0]**2 + flow[..., 1]**2)
    return float(np.mean(magnitude))

def split_roi(roi):
    h, w = roi.shape[:2]
    return roi[:h//2, :], roi[h//2:, :]

def compute_articulated_motion(track_id, roi):
    roi = cv2.resize(roi, (128, 128))

    upper, lower = split_roi(roi)

    gray_upper = cv2.cvtColor(upper, cv2.COLOR_BGR2GRAY)
    gray_lower = cv2.cvtColor(lower, cv2.COLOR_BGR2GRAY)

    if track_id not in motion_history:
        motion_history[track_id] = (gray_upper, gray_lower)
        return 0, 0

    prev_upper, prev_lower = motion_history[track_id]

    m_up = compute_optical_flow(prev_upper, gray_upper)
    m_low = compute_optical_flow(prev_lower, gray_lower)

    motion_history[track_id] = (gray_upper, gray_lower)

    return m_up, m_low

def get_state_articulated(m_up, m_low, th_arm=0.3, th_tracks=0.5):
    if m_up > th_arm and m_low > th_tracks:
        return "ACTIVE", "both"
    if m_up > th_arm:
        return "ACTIVE", "arm_only"
    if m_low > th_tracks:
        return "ACTIVE", "tracks_only"
    return "INACTIVE", "none"

def predict_activity(buffer, model, device, state):
    if state == "INACTIVE":
        return "WAITING"

    if len(buffer) < 8:
        return "WARMING_UP"

    clip = np.array(buffer)
    clip = clip.transpose(3, 0, 1, 2)

    clip = torch.tensor(clip).unsqueeze(0).float().to(device)

    with torch.no_grad():
        out = model(clip)
        pred = out.argmax(1).item()

    return ACTIVITY_LABELS[pred]

def update_time(track_id, state, dt):
    if track_id not in time_stats:
        time_stats[track_id] = {"active": 0.0, "idle": 0.0}

    if state == "ACTIVE":
        time_stats[track_id]["active"] += dt
    else:
        time_stats[track_id]["idle"] += dt

def compute_utilization(track_id):
    stats = time_stats.get(track_id, {"active": 0, "idle": 0})
    total = stats["active"] + stats["idle"]

    if total == 0:
        return 0

    return 100 * stats["active"] / total

# ==============================
# 🔥 YOLO (IMPORT YOUR MODEL)
# ==============================
# from ultralytics import YOLO
# yolo_model = YOLO("yolov8m.pt")

def detect_and_track(frame):
    results = yolo_model.track(frame, persist=True, verbose=False)[0]

    detections = []

    if results.boxes is None:
        return detections

    for box in results.boxes:
        if box.id is None:
            continue

        cls = int(box.cls[0])
        label = yolo_model.names[cls]

        if label not in ["truck", "car", "bus"]:
            continue

        track_id = int(box.id.item())
        x1, y1, x2, y2 = map(int, box.xyxy[0])

        detections.append({
            "id": track_id,
            "bbox": (x1, y1, x2, y2),
            "label": label
        })

    return detections


# ==============================
# 🔥 MAIN LOOP
# ==============================
def main():
    cap = cv2.VideoCapture(VIDEO_PATH)

    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    dt = 1.0 / fps

    frame_id = 0

    print("🚀 CV Service Started...")

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        detections = detect_and_track(frame)

        for det in detections:
            track_id = det["id"]
            x1, y1, x2, y2 = det["bbox"]

            roi = frame[y1:y2, x1:x2]
            if roi.shape[0] < 20:
                continue

            # Motion
            m_up, m_low = compute_articulated_motion(track_id, roi)
            state, source = get_state_articulated(m_up, m_low)

            # Activity
            buffer = update_buffer(track_id, roi)
            if len(buffer) < 8:
                continue

            activity = predict_activity(buffer, model, DEVICE, state)

            # Time + Utilization
            update_time(track_id, state, dt)
            utilization = compute_utilization(track_id)

            _, buffer = cv2.imencode('.jpg', frame)
            frame_bytes = buffer.tobytes()

            # 🔥 Send to Kafka
            data = {
                "frame_id": frame_id,
                "equipment_id": f"EX-{track_id:04d}",
                "state": state,
                "activity": activity,
                "motion_source": source,
                "motion_upper": round(m_up, 4),
                "motion_lower": round(m_low, 4),
                "utilization": round(utilization, 2),
                "frame": frame_bytes.hex()
            }

            producer.send(KAFKA_TOPIC, value=data)

        frame_id += 1

    cap.release()
    print("✅ Done streaming.")

# ==============================
# 🔥 RUN
# ==============================
if __name__ == "__main__":
    main()