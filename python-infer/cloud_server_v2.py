import zmq
import cv2
import numpy as np
import json
import time
from ultralytics import YOLO

def start_pc_service():
    context = zmq.Context()
    socket = context.socket(zmq.ROUTER)
    # LINGER 设置为 0 确保关闭时立即释放端口
    socket.setsockopt(zmq.LINGER, 0)
    socket.bind("tcp://*:5555")
    
    print("正在加载 YOLOv8 模型...")
    model = YOLO("yolov8n.pt") 
    print("✅ PC ROUTER 服务已就绪，正在监听端口 5555...")

    while True:
        try:
            # ROUTER 接收到的格式: [WorkerID, CAM_ID, ImageBytes] (共3帧)
            frames = socket.recv_multipart()
            
            if len(frames) < 3:
                print(f"⚠️ 收到异常帧数: {len(frames)}")
                continue
            
            worker_id = frames[0]
            cam_id = frames[1].decode()
            img_bytes = frames[2]
            
            # 模拟处理
            nparr = np.frombuffer(img_bytes, np.uint8)
            frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            
            if frame is not None:
                # 推理
                results = model.predict(frame, imgsz=320, verbose=False)
                response = {"status": "ok", "cam": cam_id, "count": len(results[0].boxes)}
            else:
                response = {"status": "error"}

            # ROUTER 回复格式: [WorkerID, JsonPayload] (共2帧)
            # ZMQ 会根据 WorkerID 自动路由到正确的树莓派线程
            socket.send_multipart([worker_id, json.dumps(response).encode()])
            
        except Exception as e:
            print(f"❌ 运行错误: {e}")
            time.sleep(0.1)

if __name__ == "__main__":
    start_pc_service()