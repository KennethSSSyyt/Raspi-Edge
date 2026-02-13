import cv2
import json
import redis
import time
import os
import numpy as np
import multiprocessing as mp
from ultralytics import YOLO

# === 配置 ===
VIDEO_DIR = "/app/videos"
VIDEOS = [
    os.path.join(VIDEO_DIR, "video1.mp4"),
    os.path.join(VIDEO_DIR, "video2.mp4"),
    os.path.join(VIDEO_DIR, "video3.mp4"),
    os.path.join(VIDEO_DIR, "video4.mp4")
]

FRAME_W, FRAME_H = 640, 360
SKIP_FRAMES = 3 

r = redis.Redis(host='rsu-redis', port=6379, decode_responses=False) # 注意: decode_responses=False 用于存二进制图片

def worker(index, video_path):
    print(f"🚀 Worker {index} started. Video: {video_path}")
    
    model = YOLO("yolov8n.pt")
    cap = cv2.VideoCapture(video_path)
    frame_cnt = 0
    
    while True:
        ret, frame = cap.read()
        if not ret:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            continue
            
        frame = cv2.resize(frame, (FRAME_W, FRAME_H))
        frame_cnt += 1
        
        # === AI 推理 ===
        if frame_cnt % SKIP_FRAMES == 0:
            results = model.track(frame, persist=True, verbose=False, classes=[2,3,5,7], tracker="bytetrack.yaml")
            
            tracks = []
            if results[0].boxes.id is not None:
                boxes = results[0].boxes.xyxy.cpu().numpy().astype(int)
                ids = results[0].boxes.id.cpu().numpy().astype(int)
                for box, obj_id in zip(boxes, ids):
                    tracks.append([int(b) for b in box] + [int(obj_id)])
            
            # 存数据 (JSON)
            r.set(f"cam_{index}_data", json.dumps(tracks))

        # === 存图片 (JPEG) ===
        # 存入 Redis，有效期 1 秒，防止内存溢出
        ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
        r.setex(f"cam_{index}_img", 1, buffer.tobytes())
        
        time.sleep(0.02)

def main():
    while not os.path.exists(VIDEOS[0]):
        print("Waiting for videos...")
        time.sleep(2)

    processes = []
    for i in range(4):
        p = mp.Process(target=worker, args=(i, VIDEOS[i]))
        p.start()
        processes.append(p)
        
    for p in processes: p.join()

if __name__ == "__main__":
    main()
