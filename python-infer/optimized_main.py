import os
os.environ["ORT_LOG_LEVEL"] = "3"
import cv2
import time
import json
import numpy as np
import psutil
import multiprocessing as mp
from multiprocessing import shared_memory
from flask import Flask, Response, render_template
from ultralytics import YOLO
import hyperlpr3
from PIL import Image, ImageDraw, ImageFont  # 引入 PIL 处理中文

# ================= 1. 系统配置 =================
VIDEOS = [
    "/home/pi/raspi-edge-ai/python-infer/videos/video1.mp4",
    "/home/pi/raspi-edge-ai/python-infer/videos/video2.mp4",
    "/home/pi/raspi-edge-ai/python-infer/videos/video3.mp4",
    "/home/pi/raspi-edge-ai/python-infer/videos/video4.mp4"
]

PRIORITY_MAP = {0: "HIGH", 1: "HIGH", 2: "LOW", 3: "LOW"}
FRAME_W, FRAME_H = 640, 360
SHM_SIZE = FRAME_W * FRAME_H * 3
NEON_COLORS = [(0, 255, 255), (255, 0, 255), (0, 255, 0), (0, 165, 255)]

# ================= 2. 中文绘制工具 (PIL) =================
def cv2_add_chinese_text(img, text, position, text_color=(255, 255, 255), text_size=20):
    if (isinstance(img, np.ndarray)): 
        img = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    
    draw = ImageDraw.Draw(img)
    # 树莓派标准中文字体路径
    font_path = "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"
    try:
        font = ImageFont.truetype(font_path, text_size)
    except:
        # 如果找不到字体，使用默认（仍然不支持中文，但不会报错）
        font = ImageFont.load_default()
        print("Warning: Chinese font not found. Please install fonts-wqy-zenhei")
    
    draw.text(position, text, font=font, fill=text_color)
    return cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2BGR)

# ================= 3. 核心算法：自适应调度器 =================
class AdaptiveScheduler:
    def get_system_stress(self):
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory().percent
        stress_score = (cpu * 0.7 + mem * 0.3) / 100.0
        return stress_score, cpu

    def decide_strategy(self, stress, priority):
        skip = 3
        scale = 1.0
        if priority == "HIGH":
            if stress > 0.85: skip = 4
        else:
            if stress > 0.85: 
                skip = 10     
                scale = 0.5   
            elif stress > 0.7: 
                skip = 6
        return skip, scale

# ================= 4. 融合 UI 绘制 =================
def draw_osd_fusion(frame, cam_id, priority, fps, count, plate, cpu_load, status_msg):
    # 背景
    overlay = frame.copy()
    cv2.rectangle(overlay, (0, 0), (640, 40), (0, 0, 0), -1) 
    cv2.rectangle(overlay, (0, 320), (220, 360), (0, 0, 0), -1) 
    cv2.addWeighted(overlay, 0.7, frame, 0.3, 0, frame)

    # 英文信息用 OpenCV 画（快）
    color_p = (0, 255, 0) if priority == "HIGH" else (0, 255, 255)
    cv2.putText(frame, f"CAM-{cam_id+1} [{priority}]", (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color_p, 2)
    
    color_cpu = (0, 0, 255) if cpu_load > 90 else (255, 255, 255)
    cv2.putText(frame, f"CPU:{cpu_load}%", (200, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color_cpu, 1)
    cv2.putText(frame, status_msg, (350, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)

    cv2.putText(frame, f"FPS: {fps}", (10, 345), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 1)
    cv2.putText(frame, f"Count: {count}", (100, 345), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 1)
    
    # === 关键修改：中文车牌用 PIL 画 ===
    if plate != "--":
        # 绘制车牌背景
        cv2.rectangle(frame, (450, 310), (640, 360), (255, 255, 255), -1)
        # 用 PIL 绘制中文车牌 (蓝色字体)
        frame = cv2_add_chinese_text(frame, plate, (460, 315), (200, 0, 0), 30)

    return frame

class SmartCounter:
    def __init__(self):
        self.count = 0
        self.crossed_ids = set()
        self.last_cy = {} 
        self.last_plate = "--"
        self.line_y = int(FRAME_H * 0.6)

    def update(self, boxes, ids, frame, lpr_instance):
        if boxes is None: return
        current_ids = []
        for box, obj_id in zip(boxes, ids):
            current_ids.append(obj_id)
            x1, y1, x2, y2 = box
            cy = int((y1 + y2) / 2)
            prev_cy = self.last_cy.get(obj_id, cy)
            self.last_cy[obj_id] = cy
            
            if obj_id in self.crossed_ids: continue
            if prev_cy < self.line_y and cy >= self.line_y:
                self.count += 1
                self.crossed_ids.add(obj_id)
                
                # 车牌识别触发
                crop = frame[y1:y2, x1:x2]
                if crop.size > 0:
                    try:
                        res = lpr_instance(crop)
                        if res: self.last_plate = res[0][0]
                    except: pass
        if len(self.last_cy) > 50:
            self.last_cy = {k:v for k,v in self.last_cy.items() if k in current_ids}

# ================= 5. Worker 进程 =================
def worker_process(index, video_path, shm_name, global_config):
    try:
        existing_shm = shared_memory.SharedMemory(name=shm_name)
        shared_frame = np.ndarray((FRAME_H, FRAME_W, 3), dtype=np.uint8, buffer=existing_shm.buf)
    except: return

    model = YOLO("yolov8n.pt")
    lpr = hyperlpr3.LicensePlateCatcher()
    counter = SmartCounter()
    scheduler = AdaptiveScheduler()
    cap = cv2.VideoCapture(video_path)
    
    my_priority = PRIORITY_MAP.get(index, "LOW")
    cached_boxes = [] 
    cached_ids = []
    
    frame_cnt = 0
    fps_start = time.time()
    real_fps = 0
    current_skip = 3
    current_scale = 1.0
    
    while True:
        ret, frame = cap.read()
        if not ret:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            continue

        frame = cv2.resize(frame, (FRAME_W, FRAME_H))
        frame_cnt += 1
        
        # 调度逻辑
        if frame_cnt % 30 == 0:
            stress, cpu_load = scheduler.get_system_stress()
            current_skip, current_scale = scheduler.decide_strategy(stress, my_priority)
            global_config[index] = {"cpu": int(cpu_load), "mode": f"Sk:{current_skip} Sc:{current_scale}"}

        # AI 推理
        if frame_cnt % current_skip == 0:
            infer_w = int(640 * current_scale)
            results = model.track(frame, imgsz=infer_w, persist=True, verbose=False, 
                                classes=[2,3,5,7], conf=0.4, tracker="bytetrack.yaml")
            
            if results[0].boxes.id is not None:
                scale_factor = 1.0 / current_scale
                boxes = results[0].boxes.xyxy.cpu().numpy() * scale_factor
                cached_boxes = boxes.astype(int)
                cached_ids = results[0].boxes.id.cpu().numpy().astype(int)
                counter.update(cached_boxes, cached_ids, frame, lpr)
            else:
                cached_boxes = []

        # 绘图层
        cv2.line(frame, (0, counter.line_y), (FRAME_W, counter.line_y), (0, 0, 255), 1)
        
        for box, obj_id in zip(cached_boxes, cached_ids):
            x1, y1, x2, y2 = box
            color = NEON_COLORS[obj_id % len(NEON_COLORS)]
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)

        if frame_cnt % 10 == 0:
            dt = time.time() - fps_start
            real_fps = round(10 / dt, 1) if dt > 0 else 0
            fps_start = time.time()

        status = global_config.get(index, {"cpu":0, "mode":"Init"})
        
        # 调用支持中文的绘制函数
        frame = draw_osd_fusion(frame, index, my_priority, real_fps, 
                              counter.count, counter.last_plate, 
                              status["cpu"], status["mode"])

        np.copyto(shared_frame, frame)
        sleep_time = 0.01 if my_priority == "HIGH" else 0.02
        time.sleep(sleep_time)

# ================= 6. Flask & Main =================
app = Flask(__name__)

@app.route('/')
def index():
    return render_template('index.html')

def generate_feed(index):
    shm_name = f"psm_cam_{index}"
    try:
        existing_shm = shared_memory.SharedMemory(name=shm_name)
        shm_view = np.ndarray((FRAME_H, FRAME_W, 3), dtype=np.uint8, buffer=existing_shm.buf)
    except: return

    while True:
        ret, buffer = cv2.imencode('.jpg', shm_view, [cv2.IMWRITE_JPEG_QUALITY, 85])
        yield (b'--frame\r\n' b'Content-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')
        time.sleep(0.05)

@app.route('/video_feed/<int:cam_id>')
def video_feed(cam_id):
    return Response(generate_feed(cam_id), mimetype='multipart/x-mixed-replace; boundary=frame')

if __name__ == '__main__':
    mp.set_start_method('spawn', force=True)
    manager = mp.Manager()
    global_config = manager.dict()

    shm_handlers = []
    for i in range(4):
        try: shared_memory.SharedMemory(name=f"psm_cam_{i}").unlink()
        except: pass
        shm = shared_memory.SharedMemory(name=f"psm_cam_{i}", create=True, size=SHM_SIZE)
        shm_handlers.append(shm)

    processes = []
    for i in range(4):
        p = mp.Process(target=worker_process, args=(i, VIDEOS[i], f"psm_cam_{i}", global_config))
        p.daemon = True
        p.start()
        processes.append(p)

    try:
        print(">>> RoadOS Pro System Started.")
        app.run(host='0.0.0.0', port=5000, threaded=True, use_reloader=False)
    finally:
        for shm in shm_handlers: 
            try: shm.unlink()
            except: pass
