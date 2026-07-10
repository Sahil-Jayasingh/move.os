"""
SWEATNET — Desktop CCTV Uplink
World Health Authority · Ministry of Human Performance

A standalone webcam client that talks to the same real backend as the
browser dashboard (sweatnet/backend/main.py). Use this for a "local CCTV
kiosk" demo variant — a live OpenCV window with skeleton overlay, running
outside the browser.

Run:
    pip install -r requirements.txt
    python main.py                          # talks to http://localhost:8000
    python main.py --api-base http://host:8000
    python main.py --camera 1                # pick a different webcam index
"""

import argparse
import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List

import cv2
import mediapipe as mp
import requests

# ==================== API CONNECTOR ====================


class APIConnector:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.session_id = None
        self.target_reps = 0

    def start_session(self) -> str:
        try:
            res = requests.post(f"{self.base_url}/session/start", timeout=5)
            res.raise_for_status()
            data = res.json()
            self.session_id = data["session_id"]
            return data["citizen_id"]
        except Exception as e:
            raise SystemExit(
                f"CRITICAL ERROR: Could not connect to API at {self.base_url} ({e}).\n"
                f"Start it first: cd backend && uvicorn main:app --reload --port 8000"
            )

    def assign_exercise(self) -> str:
        res = requests.post(
            f"{self.base_url}/exercise/random", params={"session_id": self.session_id}, timeout=5
        )
        res.raise_for_status()
        data = res.json()
        self.target_reps = data["target_reps"]
        return data["label"]  # "SQUATS", "JUMPING JACKS", or "HIGH KNEES"

    def _post_async(self, endpoint: str, payload: dict):
        def task():
            try:
                requests.post(f"{self.base_url}{endpoint}", json=payload, timeout=3)
            except Exception:
                pass  # Fail silently on the edge client to avoid stalling the camera loop

        threading.Thread(target=task, daemon=True).start()

    def send_telemetry(self, t: "Telemetry", is_complete: bool = False):
        payload = {
            "session_id": self.session_id,
            "exercise": t.target_exercise,
            "state": t.state,
            "rep_count": t.rep_count,
            "target": self.target_reps,
            "tracking": t.tracking_status,
            "tracking_confidence": t.tracking_confidence,
            "movement_quality": float(t.movement_quality),
            "symmetry": float(t.symmetry),
            "liveness": t.liveness_status,
            "compliance": t.compliance_score,
            "credits": t.credits,
            "threat": t.threat_level,
            "observation": t.observations[-1] if t.observations else "Monitoring nominal.",
            "session_complete": is_complete,
        }
        self._post_async("/telemetry", payload)

    def send_event(self, severity: str, description: str):
        payload = {"session_id": self.session_id, "severity": severity, "description": description}
        self._post_async("/events", payload)


# ==================== DATA MODELS & ENGINES ====================


@dataclass
class Telemetry:
    state: str
    rep_count: int
    target_exercise: str
    movement_quality: int
    compliance_score: int
    tracking_status: str
    tracking_confidence: float
    symmetry: float
    liveness_status: str
    threat_level: str
    observations: List[str]
    credits: int
    timestamp: str


class AngleEngine:
    @staticmethod
    def calculate_angle(landmarks, p1, p2, p3) -> float:
        a = [landmarks[p1].x, landmarks[p1].y]
        b = [landmarks[p2].x, landmarks[p2].y]
        c = [landmarks[p3].x, landmarks[p3].y]
        ba = [a[0] - b[0], a[1] - b[1]]
        bc = [c[0] - b[0], c[1] - b[1]]
        denominator = math.hypot(*ba) * math.hypot(*bc) + 1e-6
        cosine = (ba[0] * bc[0] + ba[1] * bc[1]) / denominator
        return math.degrees(math.acos(max(min(cosine, 1.0), -1.0)))


class SymmetryEngine:
    @staticmethod
    def calculate(left: float, right: float) -> float:
        score = 100 - abs(left - right) * 0.8
        return max(0, min(100, score))


class MovementQualityEngine:
    @staticmethod
    def compute(knee_avg: float, symmetry: float, confidence: float, smoothness: float) -> int:
        rom = max(40, min(100, 100 - abs(knee_avg - 90) * 0.7))
        quality = (rom * 0.40) + (symmetry * 0.25) + (confidence * 100 * 0.20) + (smoothness * 0.15)
        return int(max(0, min(100, quality)))


class LivenessEngine:
    def __init__(self):
        self.history = deque(maxlen=8)
        self.last_motion = time.time()

    def verify(self, landmarks):
        key_joints = [11, 12, 23, 24, 25, 26, 27, 28]
        conf = sum(landmarks[i].visibility for i in key_joints) / len(key_joints)

        self.history.append((landmarks, time.time()))
        if len(self.history) < 3:
            return "Verified", conf

        motion = any(abs(landmarks[i].y - self.history[-2][0][i].y) > 0.015 for i in key_joints)
        if motion:
            self.last_motion = time.time()

        status = "Suspicious" if (time.time() - self.last_motion > 2.0) else "Verified"
        return status, conf


class StateMachineEngine:
    def __init__(self, exercise_type: str):
        self.exercise_type = exercise_type
        self.state = "Ready"
        self.rep_count = 0
        self.stage_buffer = deque(maxlen=3)
        self.last_rep_time = 0.0
        self.cooldown = 0.5

        self.valid_transitions = {
            "Ready": "Movement Started",
            "Movement Started": "Depth Verified",
            "Depth Verified": "Movement Returning",
            "Movement Returning": "Ready",
        }

    def transition(self, metrics: Dict) -> str:
        current_time = time.time()
        if current_time - self.last_rep_time < self.cooldown:
            return "Cooldown"

        candidate = self._get_candidate_stage(metrics)
        self.stage_buffer.append(candidate)

        if len(self.stage_buffer) == 3 and len(set(self.stage_buffer)) == 1:
            confirmed_stage = self.stage_buffer[0]
            expected_next = self.valid_transitions.get(self.state)

            if confirmed_stage == expected_next:
                self.state = confirmed_stage
            elif confirmed_stage == "Ready" and self.state != "Ready":
                if self.state == "Movement Returning":
                    self.rep_count += 1
                    self.last_rep_time = current_time
                    self.state = "Ready"
                    return "Rep Completed"
                else:
                    self.state = "Ready"
        return self.state

    def _get_candidate_stage(self, metrics):
        if self.exercise_type == "Squats":
            knee_avg = metrics.get("knee_avg", 180)
            if knee_avg < 90:
                return "Depth Verified"
            if knee_avg < 140:
                return "Movement Started"
            if self.state == "Depth Verified" and knee_avg > 110:
                return "Movement Returning"
            if knee_avg > 160:
                return "Ready"

        elif self.exercise_type == "JumpingJacks":
            wrist_dist = metrics.get("wrist_dist", 0)
            ankle_dist = metrics.get("ankle_dist", 0)
            if wrist_dist > 0.5 and ankle_dist > 0.3:
                return "Depth Verified"
            if wrist_dist > 0.2 and ankle_dist > 0.15:
                return "Movement Started"
            if self.state == "Depth Verified" and wrist_dist < 0.4:
                return "Movement Returning"
            if wrist_dist < 0.2 and ankle_dist < 0.15:
                return "Ready"

        elif self.exercise_type == "HighKnees":
            left_raised = metrics.get("left_knee_y", 1) < metrics.get("left_hip_y", 0)
            right_raised = metrics.get("right_knee_y", 1) < metrics.get("right_hip_y", 0)
            if left_raised or right_raised:
                return "Depth Verified"
            if self.state == "Depth Verified" and not (left_raised or right_raised):
                return "Movement Returning"
            return "Ready"

        return self.state


class ThreatAssessmentEngine:
    def __init__(self):
        self.failures = 0.0
        self.level = "LOW"

    def update(self, liveness: str, tracking_conf: float, violation_triggered: bool) -> str:
        if liveness == "Suspicious" or tracking_conf < 0.50 or violation_triggered:
            self.failures += 3
        else:
            self.failures = max(0, self.failures - 0.5)

        self.level = "HIGH" if self.failures > 15 else "MEDIUM" if self.failures > 8 else "LOW"
        return self.level


# ==================== MAIN GVU PROCESSOR ====================


class GovernmentVerificationUnit:
    def __init__(self, api_client: APIConnector, internal_exercise_name: str, api_exercise_label: str):
        self.api = api_client
        self.mp_pose = mp.solutions.pose
        self.drawing_utils = mp.solutions.drawing_utils
        self.pose = self.mp_pose.Pose(min_detection_confidence=0.7, min_tracking_confidence=0.7)

        self.liveness = LivenessEngine()
        self.threat = ThreatAssessmentEngine()
        self.state_machine = StateMachineEngine(internal_exercise_name)

        self.api_exercise_label = api_exercise_label
        self.compliance_score = 0
        self.credits = 0
        self.last_knee_avg = 180.0

    def _log_violation(self, severity, desc):
        self.api.send_event(severity, desc)
        return desc

    def generate_observations(self, quality, symmetry, conf, state) -> List[str]:
        obs = []
        if conf < 0.6:
            obs.append("[WARNING] Citizen partially occluded.")
        if symmetry < 75:
            obs.append("[OBSERVATION] Left/right balance degraded.")
        if quality < 70 and state == "Depth Verified":
            obs.append("[CORRECTION] Form deviation detected.")
        if quality > 90 and state == "Rep Completed":
            obs.append("[SYSTEM] Biomechanical compliance verified.")
        return obs

    def process_frame(self, frame):
        if frame is None:
            return None, None

        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        rgb.flags.writeable = False
        results = self.pose.process(rgb)
        rgb.flags.writeable = True

        violation_triggered = False
        observations: List[str] = []
        telemetry = Telemetry(
            state="Citizen Out Of View",
            rep_count=self.state_machine.rep_count,
            target_exercise=self.api_exercise_label,
            movement_quality=0,
            compliance_score=self.compliance_score,
            tracking_status="Lost",
            tracking_confidence=0.0,
            symmetry=0.0,
            liveness_status="Inactive",
            threat_level=self.threat.level,
            observations=[],
            credits=self.credits,
            timestamp=datetime.now().isoformat(),
        )

        if not results.pose_landmarks:
            desc = self._log_violation("WARNING", "Citizen out of frame. Verification suspended.")
            telemetry.observations.append(desc)
            return frame, telemetry

        landmarks = results.pose_landmarks.landmark
        l_status, conf = self.liveness.verify(landmarks)

        if conf < 0.4:
            desc = self._log_violation("WARNING", "Low tracking confidence.")
            observations.append(desc)
            violation_triggered = True

        ae = AngleEngine()
        left_k = ae.calculate_angle(
            landmarks, self.mp_pose.PoseLandmark.LEFT_HIP, self.mp_pose.PoseLandmark.LEFT_KNEE,
            self.mp_pose.PoseLandmark.LEFT_ANKLE,
        )
        right_k = ae.calculate_angle(
            landmarks, self.mp_pose.PoseLandmark.RIGHT_HIP, self.mp_pose.PoseLandmark.RIGHT_KNEE,
            self.mp_pose.PoseLandmark.RIGHT_ANKLE,
        )
        knee_avg = (left_k + right_k) / 2

        wrist_dist = abs(
            landmarks[self.mp_pose.PoseLandmark.LEFT_WRIST].x
            - landmarks[self.mp_pose.PoseLandmark.RIGHT_WRIST].x
        )
        ankle_dist = abs(
            landmarks[self.mp_pose.PoseLandmark.LEFT_ANKLE].x
            - landmarks[self.mp_pose.PoseLandmark.RIGHT_ANKLE].x
        )
        symmetry = SymmetryEngine.calculate(left_k, right_k)
        smoothness = max(0, 100 - abs(knee_avg - self.last_knee_avg) * 2)
        self.last_knee_avg = knee_avg

        metrics = {
            "knee_avg": knee_avg,
            "wrist_dist": wrist_dist,
            "ankle_dist": ankle_dist,
            "left_knee_y": landmarks[self.mp_pose.PoseLandmark.LEFT_KNEE].y,
            "right_knee_y": landmarks[self.mp_pose.PoseLandmark.RIGHT_KNEE].y,
            "left_hip_y": landmarks[self.mp_pose.PoseLandmark.LEFT_HIP].y,
            "right_hip_y": landmarks[self.mp_pose.PoseLandmark.RIGHT_HIP].y,
        }

        state = self.state_machine.transition(metrics)
        telemetry.state = state
        telemetry.rep_count = self.state_machine.rep_count

        quality = MovementQualityEngine.compute(knee_avg, symmetry, conf, smoothness)

        if "Rep Completed" in state:
            if quality < 50:
                desc = self._log_violation("VIOLATION", "Biomechanical threshold failed. Rep rejected.")
                observations.append(desc)
                violation_triggered = True
            else:
                self.compliance_score = min(100, self.compliance_score + 10)
                self.credits += 10
                self.api.send_event("INFO", f"Valid rep registered. Quality: {quality}%")

        telemetry.movement_quality = quality
        telemetry.symmetry = round(symmetry, 1)
        telemetry.tracking_confidence = round(conf, 2)
        telemetry.tracking_status = "Excellent" if conf > 0.8 else "Acceptable" if conf > 0.5 else "Poor"
        telemetry.liveness_status = l_status
        telemetry.threat_level = self.threat.update(l_status, conf, violation_triggered)
        telemetry.compliance_score = self.compliance_score
        telemetry.credits = self.credits

        ai_obs = self.generate_observations(quality, symmetry, conf, state)
        telemetry.observations.extend(observations + ai_obs)

        self.drawing_utils.draw_landmarks(frame, results.pose_landmarks, self.mp_pose.POSE_CONNECTIONS)
        return frame, telemetry


# ==================== MAIN EXECUTION LOOP ====================


def main():
    parser = argparse.ArgumentParser(description="SWEATNET Desktop CCTV Uplink")
    parser.add_argument("--api-base", default="http://localhost:8000", help="SWEATNET backend URL")
    parser.add_argument("--camera", type=int, default=0, help="Webcam device index")
    args = parser.parse_args()

    print("--- INITIATING WHA VISION ENGINE ---")

    api = APIConnector(base_url=args.api_base)
    citizen_id = api.start_session()
    api_exercise_label = api.assign_exercise()

    exercise_map = {"SQUATS": "Squats", "JUMPING JACKS": "JumpingJacks", "HIGH KNEES": "HighKnees"}
    internal_name = exercise_map.get(api_exercise_label, "Squats")

    print("[*] API Uplink Established.")
    print(f"[*] Citizen ID: {citizen_id}")
    print(f"[*] Target Assignment: {api_exercise_label} (Goal: {api.target_reps})")
    print("[*] Starting CCTV Feed... (press 'q' to quit)\n")

    gvu = GovernmentVerificationUnit(api, internal_name, api_exercise_label)

    cap = cv2.VideoCapture(args.camera)
    if not cap.isOpened():
        raise SystemExit(f"CRITICAL ERROR: Could not open camera index {args.camera}.")

    last_telemetry_time = time.time()
    session_completed = False

    try:
        while cap.isOpened():
            ret, frame = cap.read()
            if not ret:
                break

            processed_frame, telemetry = gvu.process_frame(frame)

            current_time = time.time()
            if current_time - last_telemetry_time > 0.25:
                if telemetry.credits >= 100 and not session_completed:
                    session_completed = True
                    api.send_event("INFO", "Citizen has reached 100% compliance.")

                api.send_telemetry(telemetry, is_complete=session_completed)
                last_telemetry_time = current_time

            cv2.putText(processed_frame, f"TARGET: {api_exercise_label}", (10, 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
            cv2.putText(processed_frame, f"REPS: {telemetry.rep_count}/{api.target_reps}", (10, 70),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
            cv2.putText(processed_frame, f"CREDITS: {telemetry.credits}", (10, 110),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 0), 2)

            cv2.imshow("WHA CCTV Uplink (Local Monitor)", processed_frame)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break
    finally:
        cap.release()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
