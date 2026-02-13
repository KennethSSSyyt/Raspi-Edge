# 超低延迟 RSU Edge AI 完整版本（可直接运行）
# 目录结构说明（建议在项目文件夹中创建以下文件）
# - rsu_main.py  （主入口：创建线程、启动 Web）
# - video_reader.py  （独立线程：持续读取视频）
# - ai_worker.py  （独立线程：YOLOv8 异步推理 + ByteTrack）
# - tracker.py  （预测式 tracker，不卡顿）
# - web_server.py（Flask MJPEG 低延迟 Web 服务）
# - requirements.txt（依赖）
#
# 以下代码为完整单文件版，可直接复制为 rsu_main.py 运行
#=============================================================

import cv2
import time
import threading
import queue
import numpy as np
from flask import Flask, Response
from ultralytics import YOLO

#==================== 超低延迟参数 ====================
FRAME_W, FRAME_H = 640, 360
AI_SIZE = 320
SKIP = 2  # 每 2 帧做一次 AI

#==================== 全局共享队列 ====================
frame_q = queue.Queue(maxsize=2)     # 最新帧（摄像头线程 → AI 线程）
ai_output = {'boxes': [], 'time': 0} # AI 输出（AI 线程 → UI 线程）
lock = threading.Lock()

#==================== 1. 摄像头线程 ====================
def video_reader(path):
    cap = cv2.VideoCapture(path)
    cap.set(cv2.CAP_PROP_FPS, 30)

    while True:
        ret, frame = cap.read()
        if not ret:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            continue

        frame = cv2.resize(frame, (FRAME_W, FRAME_H))

        # 覆盖最新帧
        if frame_q.full():
            try: frame_q.get_nowait()
            except: pass
        frame_q.put(frame)

#==================== 2. AI 推理线程 ====================
def ai_worker():
    model = YOLO("yolov8n.pt")
    frame_count = 0

    while True:
        try:
            frame = frame_q.get(timeout=0.1)
        except:
            continue

        frame_count += 1
        if frame_count % SKIP != 0:
            continue

        # YOLOv8 异步推理
        t0 = time.time()
        res = model.track(frame, persist=True, imgsz=AI_SIZE, verbose=False, tracker="bytetrack.yaml")

        boxes = []
        if res[0].boxes.id is not None:
            arr = res[0].boxes.data.cpu().numpy()
            boxes = [b[:5].tolist() for b in arr]

        # 写入共享结果
        with lock:
            ai_output['boxes'] = boxes
            ai_output['time'] = t0

#==================== 预测式 Tracker（实现不卡顿关键） ====================
class PredictiveTracker:
    def __init__(self):
        self.tracks = {}  # {id: {box, vx, vy, last_time}}

    def update(self, boxes, t_ai):
        for b in boxes:
            x1, y1, x2, y2, obj_id = b
            cx = (x1 + x2) / 2
            cy = (y1 + y2) / 2

            if obj_id not in self.tracks:
                self.tracks[obj_id] = {
                    'box': [x1, y1, x2, y2],
                    'vx': 0,
                    'vy': 0,
                    'last_c': (cx, cy),
                    'last_time': t_ai
                }
            else:
                old = self.tracks[obj_id]
                old_cx, old_cy = old['last_c']
                dt = t_ai - old['last_time']
                if dt > 0:
                    old['vx'] = (cx - old_cx) / dt
                    old['vy'] = (cy - old_cy) / dt

                old['box'] = [x1, y1, x2, y2]
                old['last_c'] = (cx, cy)
                old['last_time'] = t_ai

    def predict(self, t_now):
        out = []
        for obj_id, d in self.tracks.items():
            dt = max(0, t_now - d['last_time'])
            if dt > 0.2: dt = 0
            x1, y1, x2, y2 = d['box']
            x1 += d['vx'] * dt
            y1 += d['vy'] * dt
            x2 += d['vx'] * dt
            y2 += d['vy'] * dt
            out.append([int(x1), int(y1), int(x2), int(y2), obj_id])
        return out

tracker = PredictiveTracker()

#==================== 3. Web 流媒体输出 ====================
app = Flask(__name__)

def generate():
    while True:
        try:
            frame = frame_q.get(timeout=0.03)
        except:
            continue

        # 读取 AI 输出
        with lock:
            boxes = ai_output['boxes']
            t_ai = ai_output['time']

        # 更新 tracker
        tracker.update(boxes, t_ai)
        pred_boxes = tracker.predict(time.time())

        # 绘制框（实时不卡顿）
        for x1, y1, x2, y2, obj_id in pred_boxes:
            cv2.rectangle(frame, (x1,y1), (x2,y2), (0,255,0), 2)
            cv2.putText(frame, str(obj_id), (x1, y1-4), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0,255,0), 2)

        ret, jpeg = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        yield (b'--frame
Content-Type: image/jpeg

' + jpeg.tobytes() + b'
')

@app.route('/')
def video():
    return Response(generate(), mimetype='multipart/x-mixed-replace; boundary=frame')

#==================== 主启动 ====================
if __name__ == '__main__':
    video_path = "/home/pi/video.mp4"

    threading.Thread(target=video_reader, args=(video_path,), daemon=True).start()
    threading.Thread(target=ai_worker, daemon=True).start()

    app.run(host="0.0.0.0", port=5000, threaded=True)
