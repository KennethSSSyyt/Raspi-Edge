import zmq
import json
import time
import cv2
import numpy as np
import warnings
import psutil
import threading
import torch
import csv
import os
import math
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from queue import Queue, Empty
import hyperlpr3
from ultralytics import YOLO

# === 1. 全局配置 ===
PI_IP = '192.168.137.166'  # ⚠️ 请确保这是树莓派的 IP
PULL_PORT = "5555"
PUSH_PORT = "5556"

warnings.filterwarnings("ignore")

# === 2. 高性能日志记录模块 (MetricLogger) ===
class MetricLogger:
    def __init__(self, filename="rsu_performance.csv"):
        self.q = Queue()
        self.filename = filename
        self.running = True
        
        # 初始化 CSV 文件头 (增加了 Speed 和 Flow 字段)
        if not os.path.exists(self.filename):
            with open(self.filename, mode='w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    "Timestamp", "Unix_Time", "Cam_ID", 
                    "Pi_CPU", "PC_CPU", "Latency_ms", 
                    "Object_Count", "Avg_Speed", "Traffic_Flow",
                    "Plate_Detected", "Queue_Backlog"
                ])
        
        # 启动独立写入线程 (Daemon 守护线程)
        self.thread = threading.Thread(target=self._writer_loop, daemon=True)
        self.thread.start()
        print(f"📊 [Logger] Active. Saving to {self.filename}")

    def log(self, cam_id, pi_cpu, latency, obj_count, speed, flow, plate, q_size):
        """将数据推入队列，非阻塞"""
        data = {
            "time": datetime.now(),
            "cam": cam_id,
            "pi": pi_cpu,
            "pc": psutil.cpu_percent(),
            "lat": latency,
            "cnt": obj_count,
            "spd": speed,
            "flow": flow,
            "plate": plate,
            "q": q_size
        }
        self.q.put(data)

    def _writer_loop(self):
        """后台写入循环，批量写入减少IO开销"""
        with open(self.filename, mode='a', newline='') as f:
            writer = csv.writer(f)
            while self.running:
                try:
                    item = self.q.get(timeout=2.0)
                    writer.writerow([
                        item["time"].strftime("%H:%M:%S.%f")[:-3],
                        f"{item['time'].timestamp():.3f}",
                        item["cam"],
                        f"{item['pi']:.1f}",
                        f"{item['pc']:.1f}",
                        f"{item['lat']:.1f}",
                        item["cnt"],
                        item["spd"],
                        item["flow"],
                        item["plate"],
                        item["q"]
                    ])
                    # 确保数据不丢失
                    if self.q.empty(): f.flush()
                    self.q.task_done()
                except Empty:
                    continue
                except Exception as e:
                    print(f"Logger Error: {e}")

# === 3. 交通分析核心类 (TrafficAnalyst) ===
class TrafficAnalyst:
    def __init__(self):
        self.tracks = {}
        self.total_flow = set()
        self.px_to_m = 20.0 / 640.0 
        self.lock = threading.Lock()

    def get_known_plate(self, track_id):
        """快速查询该ID是否已有车牌记录，避免重复OCR"""
        with self.lock:
            if track_id in self.tracks:
                return self.tracks[track_id]['plate']
        return "--"

    def update(self, track_id, box, new_plate_text):
        x1, y1, x2, y2 = box
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        now = time.time()
        
        speed = 0.0
        # 优先使用历史识别到的车牌
        final_plate = new_plate_text
        
        with self.lock:
            self.total_flow.add(track_id)
            
            if track_id in self.tracks:
                last_data = self.tracks[track_id]
                
                # 逻辑修正：如果历史记录里有车牌，且当前传入的是无效值，则保持历史值
                if last_data['plate'] != "--":
                    final_plate = last_data['plate']
                
                # 速度计算
                dt = now - last_data['time']
                if dt > 0.05:
                    dx = cx - last_data['pos'][0]
                    dy = cy - last_data['pos'][1]
                    dist_px = math.sqrt(dx**2 + dy**2)
                    dist_m = dist_px * self.px_to_m
                    raw_speed = (dist_m / dt) * 3.6 
                    speed = 0.6 * raw_speed + 0.4 * last_data['speed'] # 系数调整更平滑
            
            self.tracks[track_id] = {
                'pos': (cx, cy),
                'time': now,
                'speed': speed,
                'plate': final_plate
            }
            
            # 清理过期ID
            if len(self.tracks) > 200:
                old_ids = [k for k, v in self.tracks.items() if now - v['time'] > 10.0]
                for k in old_ids: del self.tracks[k]

        return int(speed), final_plate, len(self.total_flow)

# === 4. 全局资源与初始化 ===
PERF_LOGGER = MetricLogger() # 启动日志记录器
ANALYSTS = {f"CAM-{i:02d}": TrafficAnalyst() for i in range(1, 5)} # 4路分析器
YOLO_MODELS = {}
YOLO_LOCK = threading.Lock()
LPR_MODEL = None
RESULT_QUEUE = Queue(maxsize=200)

def init_global_resources():
    global LPR_MODEL
    try:
        LPR_MODEL = hyperlpr3.LicensePlateCatcher()
        print("✅ [Init] HyperLPR model loaded.")
    except Exception as e:
        print(f"❌ [Init] HyperLPR Failed: {e}")

def get_yolo_model(cam_id):
    with YOLO_LOCK:
        if cam_id not in YOLO_MODELS:
            print(f"🔄 [YOLO] Init Tracker for {cam_id}")
            device = 'cuda' if torch.cuda.is_available() else 'cpu'
            print(f"   👉 Device: {device}")
            model = YOLO("yolov8n.pt")
            # 模型预热 (Warmup) 消除首帧卡顿
            try:
                model(np.zeros((640, 640, 3), dtype=np.uint8), device=device, verbose=False)
            except: pass
            YOLO_MODELS[cam_id] = model
        return YOLO_MODELS[cam_id]

# === 5. 核心处理线程 ===
def process_frame_thread(meta_data_json, jpg_bytes):
    t_start = time.time()
    cam_id = meta_data_json.get("cam_id", "UNK")
    pi_cpu = meta_data_json.get("pi_cpu", 0.0)

    try:
        if not jpg_bytes: return
        nparr = np.frombuffer(jpg_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is None: return
    except: return

    # 2. 推理
    model = get_yolo_model(cam_id)
    # verbose=False 关闭日志能稍微提升速度
    results = model.track(img, classes=[2,3,5,7], conf=0.5, persist=True, verbose=False)
    
    tracks_list = []
    
    if cam_id not in ANALYSTS: ANALYSTS[cam_id] = TrafficAnalyst()
    analyst = ANALYSTS[cam_id]
    
    current_flow = 0
    total_speed = 0
    vehicle_count = 0
    final_plate_log = "--"

    for r in results:
        if r.boxes.id is None: continue 
        
        boxes = r.boxes.xyxy.cpu().numpy()
        ids = r.boxes.id.cpu().numpy()
        
        h, w, _ = img.shape
        center_y_min, center_y_max = h * 0.3, h * 0.7 # 定义黄金识别区域

        for box, track_id in zip(boxes, ids):
            x1, y1, x2, y2 = box.astype(int).tolist()
            track_id = int(track_id)
            cy = (y1 + y2) / 2
            
            # === 优化核心：先查缓存，再决定是否跑 OCR ===
            known_plate = analyst.get_known_plate(track_id)
            plate_text = "--"

            # 只有当：
            # 1. 这个 ID 还没识别出车牌 (known_plate == "--")
            # 2. 车辆够大 (宽度 > 100)
            # 3. 车辆在画面中心区域 (避免边缘畸变和只拍到一半)
            # 才运行 OCR
            if known_plate == "--" and (x2 - x1) > 100 and (center_y_min < cy < center_y_max):
                pad = 10
                roi = img[max(0,y1-pad):min(h,y2+pad), max(0,x1-pad):min(w,x2+pad)]
                if roi.size > 0:
                    try:
                        res = LPR_MODEL(roi)
                        # 提高置信度阈值，减少误读
                        if res and res[0][1] > 0.75: 
                            plate_text = res[0][0]
                    except: pass
            
            # 更新状态 (如果 plate_text 是 "--"，update 内部会自动保留历史 known_plate)
            speed, current_id_plate, flow = analyst.update(track_id, (x1,y1,x2,y2), plate_text)
            
            current_flow = flow
            if speed > 0:
                total_speed += speed
                vehicle_count += 1
            if current_id_plate != "--":
                final_plate_log = current_id_plate
            
            tracks_list.append([x1, y1, x2, y2, track_id, current_id_plate, speed])

    avg_spd = int(total_speed / vehicle_count) if vehicle_count > 0 else 0
    latency = (time.time() - t_start) * 1000
    
    PERF_LOGGER.log(cam_id, pi_cpu, latency, len(tracks_list), avg_spd, current_flow, final_plate_log, RESULT_QUEUE.qsize())

    response = {
        "cam_id": cam_id,
        "tracks": tracks_list,
        "flow": current_flow,
        "avg_spd": avg_spd,
        "pi_cpu": pi_cpu,
        "latency_ms": latency,
        "offload_ratio": 0 
    }
    
    try: RESULT_QUEUE.put(response, timeout=0.01) # 缩短 timeout
    except: pass

# === 6. 主循环 ===
def main():
    print(f"🚀 PC Cloud Service Starting...")
    init_global_resources()
    
    context = zmq.Context()
    receiver = context.socket(zmq.PULL)
    receiver.bind(f"tcp://*:{PULL_PORT}")
    
    sender = context.socket(zmq.PUSH)
    sender.connect(f"tcp://{PI_IP}:{PUSH_PORT}")
    
    poller = zmq.Poller()
    poller.register(receiver, zmq.POLLIN)
    
    # 线程池大小建议：物理核数 + 2
    max_workers = psutil.cpu_count(logical=True) + 2
    print(f"⚙️  Thread Pool: {max_workers} workers")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        while True:
            try:
                socks = dict(poller.poll(10))
                if receiver in socks:
                    try:
                        # 接收 Multipart 消息
                        meta = receiver.recv_json(zmq.SNDMORE)
                        img = receiver.recv(0)
                        executor.submit(process_frame_thread, meta, img)
                    except Exception as e:
                        print(f"Recv Error: {e}")

                # 发送结果回树莓派
                while True:
                    try:
                        res = RESULT_QUEUE.get_nowait()
                        sender.send_json(res, zmq.DONTWAIT)
                        
                        # 简化控制台日志
                        print(f"\r⚡ {res['cam_id']} | LAT:{res['latency_ms']:3.0f}ms | SPD:{res['avg_spd']} | FLOW:{res['flow']}", end="")
                        
                        RESULT_QUEUE.task_done()
                    except Empty: break
                        
            except KeyboardInterrupt: break
            except Exception: time.sleep(0.1)

if __name__ == "__main__":
    main()