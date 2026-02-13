import cv2
import imagezmq
import json
import time
import math
import psutil
import numpy as np
from ultralytics import YOLO
import hyperlpr3

# ================= 配置 =================
MODEL_PATH = "yolov8n.pt" 
PIXELS_PER_METER = 25 

# ================= 环境感知 =================
class EnvironmentAnalyst:
    def analyze(self, frame):
        # 降采样极速分析
        small = cv2.resize(frame, (64, 36))
        hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
        brightness = hsv[..., 2].mean()
        contrast = small.std()
        
        time_day = "DAY" if brightness > 60 else "NIGHT"
        weather = "CLEAR"
        if time_day == "DAY":
            if contrast < 30: weather = "FOGGY/RAIN"
            elif brightness > 140: weather = "SUNNY"
            else: weather = "CLOUDY"
        
        return {"time": time_day, "weather": weather}

# ================= 交通分析 =================
class TrafficAnalyst:
    def __init__(self):
        self.track_history = {} 
        self.speeds = {}        
        self.logs = []
        self.line_y_ratio = 0.6
        self.latest_plate = "--"
        self.triggered = False
        self.trigger_timer = 0

    def update(self, tracks, frame_img, lpr_instance):
        h, w = frame_img.shape[:2]
        line_y = int(h * self.line_y_ratio)
        current_time = time.time()
        
        if self.trigger_timer > 0: self.trigger_timer -= 1
        else: self.triggered = False

        congestion = min(10.0, (len(tracks) / 12.0) * 10)
        status = "FREE"
        if congestion > 8: status = "JAM"
        elif congestion > 5: status = "BUSY"

        current_ids = []
        for box in tracks:
            if len(box) < 5: continue
            x1, y1, x2, y2, obj_id = box
            cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
            current_ids.append(obj_id)

            if obj_id not in self.track_history: self.track_history[obj_id] = []
            self.track_history[obj_id].append((cx, cy, current_time))
            if len(self.track_history[obj_id]) > 10: self.track_history[obj_id].pop(0)

            if len(self.track_history[obj_id]) >= 3:
                last = self.track_history[obj_id][-1]
                first = self.track_history[obj_id][0]
                dist = math.sqrt((last[0]-first[0])**2 + (last[1]-first[1])**2)
                t_diff = last[2] - first[2]
                if t_diff > 0.05:
                    speed = int((dist / PIXELS_PER_METER) / t_diff * 3.6)
                    self.speeds[obj_id] = speed

            prev_y = self.track_history[obj_id][0][1] if len(self.track_history[obj_id]) > 0 else cy
            direction = None
            if prev_y < line_y and cy >= line_y: direction = "Down"
            elif prev_y > line_y and cy <= line_y: direction = "Up"

            if direction:
                self.triggered = True
                self.trigger_timer = 5
                pad = 10
                crop = frame_img[max(0, y1-pad):min(h, y2+pad), max(0, x1-pad):min(w, x2+pad)]
                if crop.size > 0:
                    try:
                        res = lpr_instance(crop)
                        if res:
                            txt, conf, _ = res[0]
                            if conf > 0.7: self.latest_plate = txt
                    except: pass
                
                log = f"ID:{obj_id} {direction} {self.speeds.get(obj_id,0)}km {self.latest_plate}"
                if not self.logs or self.logs[-1] != log:
                    self.logs.append(log)
                    if len(self.logs) > 5: self.logs.pop(0)

        valid = set(current_ids)
        self.track_history = {k:v for k,v in self.track_history.items() if k in valid}
        avg_spd = int(sum(self.speeds.values())/len(self.speeds)) if self.speeds else 0
        
        return {
            "idx": congestion, "status": status, "avg_spd": avg_spd,
            "speeds": self.speeds, "logs": self.logs, "plate": self.latest_plate,
            "triggered": self.triggered
        }

def main():
    print("="*50)
    print("🚀 PC CLOUD BRAIN V6 (Data Integrity)")
    print("="*50)
    
    model = YOLO(MODEL_PATH)
    lpr = hyperlpr3.LicensePlateCatcher()
    env_analyst = EnvironmentAnalyst()
    analysts = {}
    image_hub = imagezmq.ImageHub(open_port='tcp://*:5555')
    
    while True:
        cam_id, jpg_bytes = image_hub.recv_jpg()
        try:
            frame = cv2.imdecode(np.frombuffer(jpg_bytes, dtype='uint8'), -1)
            if cam_id not in analysts: analysts[cam_id] = TrafficAnalyst()
            
            # 1. 环境感知
            env_info = env_analyst.analyze(frame)
            
            # 2. YOLO
            results = model.track(frame, persist=True, verbose=False, 
                                classes=[2, 3, 5, 7], tracker="bytetrack.yaml")
            
            formatted_tracks = []
            if results[0].boxes.id is not None:
                boxes = results[0].boxes.xyxy.cpu().numpy().astype(int)
                ids = results[0].boxes.id.cpu().numpy().astype(int)
                for box, obj_id in zip(boxes, ids):
                    formatted_tracks.append([int(b) for b in box] + [int(obj_id)])
            
            # 3. 交通分析
            metrics = analysts[cam_id].update(formatted_tracks, frame, lpr)
            
            # 4. PC CPU
            pc_cpu = psutil.cpu_percent()
            
            response = {
                "tracks": formatted_tracks,
                "metrics": metrics,
                "env": env_info,
                "pc_cpu": pc_cpu
            }
            
            image_hub.send_reply(json.dumps(response).encode('utf-8'))
            print(f"\r⚡ {cam_id}: {env_info['weather']} | {metrics['status']}   ", end="")
            
        except Exception as e:
            image_hub.send_reply(b"{}")

if __name__ == '__main__':
    main()