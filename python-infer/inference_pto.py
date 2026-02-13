import os
# 屏蔽烦人的 GPU 和 OpenCV 警告
os.environ["ORT_LOG_LEVEL"] = "4"
os.environ["OPENCV_LOG_LEVEL"] = "0"

import cv2
import time
import math
import psutil
import numpy as np
import multiprocessing as mp
from multiprocessing import shared_memory
from flask import Flask, Response, render_template_string
from ultralytics import YOLO
import hyperlpr3 # 引入车牌识别库

# ================= 1. Configuration =================
# 视频路径配置
BASE_DIR = "/home/pi/raspi-edge-ai/python-infer" 
VIDEO_DIR = os.path.join(BASE_DIR, "videos")

VIDEOS = [
    os.path.join(VIDEO_DIR, "video1.mp4"),
    os.path.join(VIDEO_DIR, "video2.mp4"),
    os.path.join(VIDEO_DIR, "video3.mp4"),
    os.path.join(VIDEO_DIR, "video4.mp4")
]

FRAME_W, FRAME_H = 640, 360
SHM_SIZE = FRAME_W * FRAME_H * 3

# === 性能与视觉配置 ===
# AI检测频率：每隔几帧跑一次YOLO跟踪 (影响跟踪精度和负载)
AI_SKIP_FRAMES = 3          
# 推理分辨率：越小越快，但远距离小目标检测越差
YOLO_IMG_SIZE = 320      
# 视频读取跳帧：为了加快播放速度，每读1帧，跳过N帧不处理 (物理加速)
VIDEO_READ_SKIP = 2      

PIXELS_PER_METER = 20    # 虚拟标定
LINE_POS_RATIO = 0.6     # 检测线位置比例

# 个性化车辆颜色池 (霓虹风格)
NEON_COLORS = [
    (0, 255, 255), (255, 0, 255), (0, 255, 0), 
    (255, 255, 0), (0, 165, 255), (180, 105, 255)
]

# 仪表盘高度
LOG_AREA_H = 100 
TOTAL_H = FRAME_H + LOG_AREA_H
TOTAL_SHM_SIZE = FRAME_W * TOTAL_H * 3

# ================= 2. Traffic Analyst (算法核心+LPR) =================
class TrafficAnalyst:
    def __init__(self):
        self.track_history = {} 
        self.speeds = {}        
        self.line_y = int(FRAME_H * LINE_POS_RATIO)
        self.logs = []
        self.latest_plate = "--"
        self.triggered_frames = 0 # 用于控制线的变色状态
        
    def update(self, tracks, frame, lpr_instance):
        current_time = time.time()
        current_ids = []
        
        # 线的触发状态递减
        if self.triggered_frames > 0: self.triggered_frames -= 1

        # 1. 计算拥堵指数
        congestion = min(10.0, (len(tracks) / 15.0) * 10)
        status = "FREE"
        if congestion > 7: status = "JAM"
        elif congestion > 4: status = "BUSY"

        for box in tracks:
            if len(box) < 5: continue
            x1, y1, x2, y2, obj_id = map(int, box[:5])
            cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
            current_ids.append(obj_id)
            
            # 记录轨迹
            if obj_id not in self.track_history:
                self.track_history[obj_id] = []
            self.track_history[obj_id].append((cx, cy, current_time))
            if len(self.track_history[obj_id]) > 5: self.track_history[obj_id].pop(0)
            
            # 2. 速度估算
            if len(self.track_history[obj_id]) >= 3:
                last_pt = self.track_history[obj_id][-1]
                prev_pt = self.track_history[obj_id][0]
                dist_px = math.sqrt((last_pt[0] - prev_pt[0])**2 + (last_pt[1] - prev_pt[1])**2)
                time_diff = last_pt[2] - prev_pt[2]
                if time_diff > 0.01:
                    # 注意：如果视频加速播放，计算出的速度会比实际快
                    speed_kmh = (dist_px / PIXELS_PER_METER) / time_diff * 3.6
                    self.speeds[obj_id] = int(speed_kmh)

            # 3. 过线检测 + LPR触发
            prev_y = self.track_history[obj_id][0][1] if len(self.track_history[obj_id]) > 0 else cy
            direction = None
            if prev_y < self.line_y and cy >= self.line_y: direction = "Down"
            elif prev_y > self.line_y and cy <= self.line_y: direction = "Up"
            
            if direction:
                self.triggered_frames = 10 # 触发状态持续10帧
                
                # === LPR 核心逻辑：仅在过线瞬间识别 ===
                plate_text = "Unknown"
                # 稍微扩大裁剪范围，提高识别率
                pad = 5
                crop_x1, crop_y1 = max(0, x1-pad), max(0, y1-pad)
                crop_x2, crop_y2 = min(FRAME_W, x2+pad), min(FRAME_H, y2+pad)
                vehicle_crop = frame[crop_y1:crop_y2, crop_x1:crop_x2]
                
                if vehicle_crop.size > 0 and vehicle_crop.shape[0] > 20:
                    try:
                        # HyperLPR 识别
                        res = lpr_instance(vehicle_crop)
                        if res:
                            text, conf, _ = res[0]
                            # 简单过滤置信度过低的结果
                            if conf > 0.75 or len(text) > 6:
                                plate_text = text
                                self.latest_plate = plate_text
                    except: pass
                # ====================================

                # 生成日志
                spd = self.speeds.get(obj_id, 0)
                log = f"ID:{obj_id} {direction} Spd:{spd} LPR:{plate_text}"
                if not self.logs or self.logs[-1] != log: 
                    self.logs.append(log)
                    if len(self.logs) > 4: self.logs.pop(0)

        # 清理
        valid_keys = set(current_ids)
        self.track_history = {k:v for k,v in self.track_history.items() if k in valid_keys}
        self.speeds = {k:v for k,v in self.speeds.items() if k in valid_keys}
        avg_speed = int(sum(self.speeds.values()) / len(self.speeds)) if self.speeds else 0
            
        return {
            "idx": congestion, "status": status, "avg_spd": avg_speed,
            "speeds": self.speeds, "logs": self.logs, "plate": self.latest_plate,
            "triggered": self.triggered_frames > 0
        }

# ================= 3. UI 绘制 (新需求实现) =================
def draw_dashboard(frame, cam_id, metrics, cpu_load, fps):
    canvas = np.zeros((TOTAL_H, FRAME_W, 3), dtype=np.uint8)
    canvas[:FRAME_H, :FRAME_W] = frame 
    
    line_y = int(FRAME_H * LINE_POS_RATIO)
    overlay = canvas.copy()
    
    # === 新需求：线上方蓝色，线下方玫粉色，半透明 ===
    # 上方蓝色 (Blue-ish)
    cv2.rectangle(overlay, (0, line_y-30), (FRAME_W, line_y), (255, 100, 0), -1) 
    # 下方玫粉色 (Magenta-ish)
    cv2.rectangle(overlay, (0, line_y), (FRAME_W, line_y+30), (180, 105, 255), -1) 
    # 应用半透明
    cv2.addWeighted(overlay, 0.4, canvas, 0.6, 0, canvas)
    
    # === 新需求：中间线平时黄色，通过变白色 ===
    line_color = (255, 255, 255) if metrics['triggered'] else (0, 255, 255)
    cv2.line(canvas, (0, line_y), (FRAME_W, line_y), line_color, 2)

    # 左上角状态面板
    cv2.rectangle(canvas, (5, 5), (220, 110), (20, 20, 20), -1)
    cv2.rectangle(canvas, (5, 5), (220, 110), (0, 255, 255), 1)
    
    cv2.putText(canvas, f"{cam_id} | CPU: {cpu_load}%", (15, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
    
    status_color = (0, 255, 0)
    if metrics['idx'] > 7: status_color = (0, 0, 255)
    elif metrics['idx'] > 4: status_color = (0, 165, 255)
    
    cv2.putText(canvas, f"Status: {metrics['status']} (Idx:{metrics['idx']:.1f})", (15, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.5, status_color, 1)
    cv2.putText(canvas, f"Avg Spd: {metrics['avg_spd']} km/h", (15, 75), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 0), 1)
    # 显示最近识别的车牌
    cv2.putText(canvas, f"LPR: {metrics['plate']}", (15, 100), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 0, 255), 2)
    
    # FPS
    cv2.putText(canvas, f"FPS: {fps}", (FRAME_W - 80, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 1)

    # 底部日志区
    log_y = FRAME_H + 20
    cv2.putText(canvas, "EVENT LOGS & LPR:", (10, log_y), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1)
    for i, log in enumerate(reversed(metrics['logs'])):
        if i >= 3: break 
        # 由于OpenCV不支持中文，HyperLPR识别出的中文首字可能会显示为问号，这是正常的
        cv2.putText(canvas, log, (130, log_y), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)
        log_y += 20

    return canvas

# ================= 4. Worker 进程 (极速版) =================
def worker_process(index, video_path, shm_name):
    try:
        existing_shm = shared_memory.SharedMemory(name=shm_name)
        shared_frame = np.ndarray((TOTAL_H, FRAME_W, 3), dtype=np.uint8, buffer=existing_shm.buf)
    except Exception as e:
        print(f"SHM Error: {e}")
        return

    # 初始化模型
    print(f"Worker {index}: Loading YOLO...", end="", flush=True)
    model = YOLO("yolov8n.pt")
    print("Done. Loading LPR...", end="", flush=True)
    lpr = hyperlpr3.LicensePlateCatcher()
    print("Done.")
    
    analyst = TrafficAnalyst()
    cap = cv2.VideoCapture(video_path)
    proc = psutil.Process(os.getpid()) 
    
    cam_id = f"CAM-{index+1:02d}"
    frame_cnt = 0
    fps_start = time.time()
    real_fps = 0
    
    cached_boxes = []
    cached_ids = []
    metrics = {"idx": 0, "status": "INIT", "avg_spd": 0, "speeds": {}, "logs": [], "plate": "--", "triggered": False}
    
    while True:
        # === 极速优化：物理跳帧 ===
        # 连续读取并丢弃几帧，加快视频播放进度，减少解码压力
        for _ in range(VIDEO_READ_SKIP):
            cap.read()
            
        ret, frame = cap.read()
        if not ret:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            continue

        frame = cv2.resize(frame, (FRAME_W, FRAME_H))
        frame_cnt += 1
        
        # === 核心处理 (稀疏执行) ===
        if frame_cnt % AI_SKIP_FRAMES == 0:
            # 1. AI 推理
            results = model.track(frame, persist=True, verbose=False, 
                                classes=[2, 3, 5, 7], tracker="bytetrack.yaml", imgsz=YOLO_IMG_SIZE)
            
            if results[0].boxes.id is not None:
                boxes = results[0].boxes.xyxy.cpu().numpy().astype(int)
                ids = results[0].boxes.id.cpu().numpy().astype(int)
                
                track_data = []
                for b, i in zip(boxes, ids):
                    track_data.append([b[0], b[1], b[2], b[3], i])
                
                # 2. 算法更新 (传入原图用于LPR)
                metrics = analyst.update(track_data, frame, lpr)
                cached_boxes = boxes
                cached_ids = ids
            else:
                cached_boxes = []

        # === 绘制车辆框 (每帧) - 新需求：自身对应颜色的框 ===
        for box, obj_id in zip(cached_boxes, cached_ids):
            x1, y1, x2, y2 = box
            
            # 根据ID获取固定颜色
            color = NEON_COLORS[obj_id % len(NEON_COLORS)]
            
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
            spd = metrics["speeds"].get(obj_id, 0)
            cv2.putText(frame, f"ID:{obj_id} {spd}km/h", (x1, y1-5), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)

        # === 绘制仪表盘 ===
        if frame_cnt % 15 == 0: # 降低FPS计算频率
            dt = time.time() - fps_start
            real_fps = round(15 / dt, 1) if dt > 0 else 0
            fps_start = time.time()
            
        cpu_load = int(proc.cpu_percent())
        final_canvas = draw_dashboard(frame, cam_id, metrics, cpu_load, real_fps)

        # 写入共享内存
        np.copyto(shared_frame, final_canvas)
        # 极速模式下减少休眠时间
        time.sleep(0.005)

# ================= 5. Flask App =================
app = Flask(__name__)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>RoadOS Turbo LPR Analytics</title>
    <style>
        body { background-color: #0d0d0d; color: #fff; font-family: monospace; margin: 0; padding: 20px; }
        .header { text-align: center; margin-bottom: 20px; border-bottom: 1px solid #333; padding-bottom: 10px; }
        h1 { color: #00f3ff; margin: 0; }
        p { color: #ff00ff; }
        .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; max-width: 1300px; margin: 0 auto; }
        .cam-container { border: 1px solid #333; background: #000; }
        img { width: 100%; display: block; }
    </style>
</head>
<body>
    <div class="header">
        <h1>RoadOS Edge Analytics (Pi 5 Turbo)</h1>
        <p>High-Speed Playback | LPR Integrated | Dynamic UI</p>
    </div>
    <div class="grid">
        <div class="cam-container"><img src="/video_feed/0"></div>
        <div class="cam-container"><img src="/video_feed/1"></div>
        <div class="cam-container"><img src="/video_feed/2"></div>
        <div class="cam-container"><img src="/video_feed/3"></div>
    </div>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

def generate_feed(index):
    shm_name = f"psm_cam_{index}"
    try:
        existing_shm = shared_memory.SharedMemory(name=shm_name)
        shm_view = np.ndarray((TOTAL_H, FRAME_W, 3), dtype=np.uint8, buffer=existing_shm.buf)
    except: return

    while True:
        # 降低JPEG质量以提高网络传输速度
        ret, buffer = cv2.imencode('.jpg', shm_view, [cv2.IMWRITE_JPEG_QUALITY, 70])
        yield (b'--frame\r\n' b'Content-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')
        # 提高推流帧率上限
        time.sleep(0.03) 

@app.route('/video_feed/<int:cam_id>')
def video_feed(cam_id):
    return Response(generate_feed(cam_id), mimetype='multipart/x-mixed-replace; boundary=frame')

# ================= 6. Main =================
if __name__ == '__main__':
    mp.set_start_method('spawn', force=True)
    
    if not os.path.exists(VIDEOS[0]):
        print(f"❌ Error: Videos not found in {VIDEO_DIR}")
        exit()

    shm_handlers = []
    for i in range(4):
        try: shared_memory.SharedMemory(name=f"psm_cam_{i}").unlink()
        except: pass
        shm = shared_memory.SharedMemory(name=f"psm_cam_{i}", create=True, size=TOTAL_SHM_SIZE)
        shm_handlers.append(shm)

    processes = []
    for i in range(4):
        p = mp.Process(target=worker_process, args=(i, VIDEOS[i], f"psm_cam_{i}"))
        p.daemon = True
        p.start()
        processes.append(p)
        print(f"🚀 Worker {i+1} starting...")

    # 等待所有进程初始化模型完毕后再启动Web服务
    time.sleep(10) 
    
    try:
        print("✅ Web Server Running on http://<PI_IP>:5000")
        # 使用多线程模式运行Flask
        app.run(host='0.0.0.0', port=5000, threaded=True, use_reloader=False)
    finally:
        for shm in shm_handlers: 
            try: shm.unlink()
            except: pass
