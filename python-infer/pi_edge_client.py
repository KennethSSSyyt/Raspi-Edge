import cv2
import time
import json
import socket
import imagezmq
import psutil
import threading
import numpy as np
import os
from flask import Flask, Response, jsonify, render_template_string

# ⚠️ 修改为你的 PC IP
CLOUD_IP = "192.168.137.1" 

BASE_DIR = "/home/pi/raspi-edge-ai/python-infer" 
VIDEO_DIR = os.path.join(BASE_DIR, "videos")
VIDEOS = [
    os.path.join(VIDEO_DIR, "video1.mp4"),
    os.path.join(VIDEO_DIR, "video2.mp4"),
    os.path.join(VIDEO_DIR, "video3.mp4"),
    os.path.join(VIDEO_DIR, "video4.mp4")
]

FRAME_W, FRAME_H = 640, 360
VIDEO_READ_SKIP = 1 # 保证流畅度

# 全局数据缓存
global_frames = {}
global_data = {}

# 初始化缓存，防止前端读取空数据报错
for i in range(4): 
    global_frames[i] = np.zeros((FRAME_H, FRAME_W, 3), dtype=np.uint8)
    global_data[str(i)] = { # 注意这里 key 是字符串 "0", "1"... 方便 JS 读取
        "tracks": [],
        "metrics": {"idx":0, "status":"WAIT", "avg_spd":0, "plate":"--", "logs":[]},
        "env": {"time":"--", "weather":"--"},
        "pc_cpu": 0
    }

def cloud_client_thread(index, video_path):
    cam_id = f"C{index+1}"
    print(f"🔌 {cam_id} connecting to {CLOUD_IP}...") # 打印连接尝试
    sender = imagezmq.ImageSender(connect_to=f'tcp://{CLOUD_IP}:5555', REQ_REP=True)
    sender.zmq_socket.setsockopt(imagezmq.zmq.RCVTIMEO, 800) 
    
    cap = cv2.VideoCapture(video_path)
    frame_cnt = 0
    
    while True:
        # 物理加速
        for _ in range(VIDEO_READ_SKIP): cap.read()
        ret, frame = cap.read()
        
        if not ret:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            continue
            
        frame = cv2.resize(frame, (FRAME_W, FRAME_H))
        frame_cnt += 1
        
        # 1. 存入视频缓存 (纯视频)
        global_frames[index] = frame
        
        # 2. 发送给 PC (每3帧发一次)
        if frame_cnt % 3 == 0:
            ret, jpg_buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 50])
            try:
                reply = sender.send_jpg(cam_id, jpg_buffer)
                # 更新全局数据
                data = json.loads(reply.decode('utf-8'))
                global_data[str(index)] = data # 使用字符串索引
                if frame_cnt % 30 == 0: 
                    print(f"✅ {cam_id} Linked! PC-CPU: {data.get('pc_cpu')}%")
            except Exception as e: 
                # === 这里会告诉你为什么连不上 ===
                print(f"❌ {cam_id} Link Error: {e}")        
        time.sleep(0.02)

app = Flask(__name__)

# === 前端代码：完全复刻并修复数据绑定 ===
HTML_PAGE = """
<!DOCTYPE html>
<html>
<head>
    <title>RoadOS Ultimate</title>
    <style>
        body { margin: 0; background: #000; overflow: hidden; font-family: 'Consolas', sans-serif; }
        .grid { display: grid; grid-template-columns: 50% 50%; height: 100vh; width: 100vw; }
        .cam-box { position: relative; border: 1px solid #222; overflow: hidden; }
        .video-stream { width: 100%; height: 100%; object-fit: fill; opacity: 0.8; }
        
        /* 覆盖层 */
        .overlay { position: absolute; top: 0; left: 0; width: 100%; height: 100%; pointer-events: none; }
        
        /* 检测线与区域 */
        .line { position: absolute; top: 60%; width: 100%; height: 2px; background: cyan; transition: all 0.2s; box-shadow: 0 0 5px cyan;}
        .line.active { background: white; height: 3px; box-shadow: 0 0 10px white; }
        .zone-blue { position: absolute; top: 50%; width: 100%; height: 10%; background: rgba(0, 100, 255, 0.2); }
        .zone-pink { position: absolute; top: 60%; width: 100%; height: 10%; background: rgba(255, 0, 100, 0.2); }
        
        /* 车辆框 */
        .bbox { position: absolute; border: 2px solid lime; transition: all 0.1s linear; }
        .bbox-label { position: absolute; top: -15px; left: 0; background: lime; color: black; font-size: 10px; padding: 0 2px; font-weight: bold; }
        
        /* 仪表盘 (右上角) */
        .dashboard { 
            position: absolute; top: 10px; right: 10px; 
            background: rgba(0, 0, 0, 0.7); 
            border: 1px solid cyan; border-left: 3px solid cyan;
            padding: 5px; width: 160px; pointer-events: auto;
        }
        .dash-row { margin-bottom: 3px; font-size: 12px; color: white; display: flex; justify-content: space-between; }
        .label { color: #aaa; }
        .val { font-weight: bold; color: cyan; }
        .val-danger { color: #ff0055; }
        
        /* 大字车牌 (右下) */
        .plate-box {
            position: absolute; bottom: 40px; right: 10px;
            background: white; border: 2px solid blue;
            color: blue; font-weight: bold; font-size: 18px;
            padding: 2px 8px; display: none;
        }
        
        /* 日志 (左下) */
        .logs { position: absolute; bottom: 5px; left: 5px; font-size: 10px; color: #888; text-shadow: 1px 1px 0 #000; }
    </style>
</head>
<body>
    <div class="grid">
        <div class="cam-box" id="box-0">
            <img class="video-stream" src="/feed/0">
            <div class="overlay" id="ov-0">
                <div class="zone-blue"></div><div class="zone-pink"></div><div class="line"></div>
                
                <div class="dashboard">
                    <div class="dash-row"><span style="color:cyan">CAM-01 [HIGH]</span></div>
                    <div class="dash-row"><span class="label">Env:</span> <span class="val" id="env-0">--</span></div>
                    <div class="dash-row"><span class="label">Pi/PC CPU:</span> <span class="val" id="cpu-0">0/0%</span></div>
                    <div class="dash-row"><span class="label">Status:</span> <span class="val" id="st-0">--</span></div>
                    <div class="dash-row"><span class="label">Count:</span> <span class="val" id="cnt-0">0</span></div>
                    <div class="dash-row"><span class="label">Avg Spd:</span> <span class="val" id="spd-0">0</span></div>
                </div>
                
                <div class="plate-box" id="lp-0"></div>
                <div class="logs" id="log-0">System Ready...</div>
            </div>
        </div>
        
        <div class="cam-box" id="box-1">
            <img class="video-stream" src="/feed/1">
            <div class="overlay" id="ov-1">
                <div class="zone-blue"></div><div class="zone-pink"></div><div class="line"></div>
                <div class="dashboard">
                    <div class="dash-row"><span style="color:cyan">CAM-02 [HIGH]</span></div>
                    <div class="dash-row"><span class="label">Env:</span> <span class="val" id="env-1">--</span></div>
                    <div class="dash-row"><span class="label">Pi/PC CPU:</span> <span class="val" id="cpu-1">0/0%</span></div>
                    <div class="dash-row"><span class="label">Status:</span> <span class="val" id="st-1">--</span></div>
                    <div class="dash-row"><span class="label">Count:</span> <span class="val" id="cnt-1">0</span></div>
                    <div class="dash-row"><span class="label">Avg Spd:</span> <span class="val" id="spd-1">0</span></div>
                </div>
                <div class="plate-box" id="lp-1"></div>
                <div class="logs" id="log-1">System Ready...</div>
            </div>
        </div>

        <div class="cam-box" id="box-2">
            <img class="video-stream" src="/feed/2">
            <div class="overlay" id="ov-2">
                <div class="zone-blue"></div><div class="zone-pink"></div><div class="line"></div>
                <div class="dashboard">
                    <div class="dash-row"><span style="color:orange">CAM-03 [LOW]</span></div>
                    <div class="dash-row"><span class="label">Env:</span> <span class="val" id="env-2">--</span></div>
                    <div class="dash-row"><span class="label">Pi/PC CPU:</span> <span class="val" id="cpu-2">0/0%</span></div>
                    <div class="dash-row"><span class="label">Status:</span> <span class="val" id="st-2">--</span></div>
                    <div class="dash-row"><span class="label">Count:</span> <span class="val" id="cnt-2">0</span></div>
                    <div class="dash-row"><span class="label">Avg Spd:</span> <span class="val" id="spd-2">0</span></div>
                </div>
                <div class="plate-box" id="lp-2"></div>
                <div class="logs" id="log-2">System Ready...</div>
            </div>
        </div>

        <div class="cam-box" id="box-3">
            <img class="video-stream" src="/feed/3">
            <div class="overlay" id="ov-3">
                <div class="zone-blue"></div><div class="zone-pink"></div><div class="line"></div>
                <div class="dashboard">
                    <div class="dash-row"><span style="color:orange">CAM-04 [LOW]</span></div>
                    <div class="dash-row"><span class="label">Env:</span> <span class="val" id="env-3">--</span></div>
                    <div class="dash-row"><span class="label">Pi/PC CPU:</span> <span class="val" id="cpu-3">0/0%</span></div>
                    <div class="dash-row"><span class="label">Status:</span> <span class="val" id="st-3">--</span></div>
                    <div class="dash-row"><span class="label">Count:</span> <span class="val" id="cnt-3">0</span></div>
                    <div class="dash-row"><span class="label">Avg Spd:</span> <span class="val" id="spd-3">0</span></div>
                </div>
                <div class="plate-box" id="lp-3"></div>
                <div class="logs" id="log-3">System Ready...</div>
            </div>
        </div>
    </div>

    <script>
        const COLORS = ["#00ffff", "#ff00ff", "#00ff00", "#ffff00", "#ff8800"];

        function update() {
            fetch('/data').then(r => r.json()).then(allData => {
                // 遍历 0 到 3 号摄像头
                for(let i=0; i<4; i++) {
                    const data = allData[i];
                    if(!data) continue;
                    
                    const container = document.getElementById('ov-'+i);
                    // 1. 清除旧的车辆框
                    const oldBoxes = container.querySelectorAll('.bbox');
                    oldBoxes.forEach(b => b.remove());
                    
                    const metrics = data.metrics || {};
                    const tracks = data.tracks || [];
                    const env = data.env || {};
                    const pc_cpu = data.pc_cpu || 0;
                    const pi_cpu = data.pi_cpu || 0; // 从 Python 注入
                    
                    // 2. 更新文字数据
                    document.getElementById('env-'+i).innerText = (env.weather || "-") + " / " + (env.time || "-");
                    document.getElementById('cpu-'+i).innerText = pi_cpu + "% / " + pc_cpu + "%";
                    
                    const stElem = document.getElementById('st-'+i);
                    stElem.innerText = metrics.status || "WAIT";
                    stElem.className = metrics.status === "JAM" ? "val val-danger" : "val val-ok";
                    
                    document.getElementById('cnt-'+i).innerText = tracks.length;
                    document.getElementById('spd-'+i).innerText = metrics.avg_spd || 0;
                    
                    // 3. 更新日志
                    if(metrics.logs && metrics.logs.length > 0) {
                        document.getElementById('log-'+i).innerText = metrics.logs[metrics.logs.length-1];
                    }
                    
                    // 4. 更新大车牌
                    const lpBox = document.getElementById('lp-'+i);
                    if(metrics.plate && metrics.plate !== '--') {
                        lpBox.innerText = metrics.plate;
                        lpBox.style.display = 'block';
                    }
                    
                    // 5. 更新检测线
                    const line = container.querySelector('.line');
                    if(metrics.triggered) line.classList.add('active');
                    else line.classList.remove('active');

                    // 6. 绘制新框 (HTML Div)
                    tracks.forEach(t => {
                        const [x1, y1, x2, y2, id] = t;
                        const W = 640, H = 360;
                        
                        const div = document.createElement('div');
                        div.className = 'bbox';
                        // 坐标转百分比
                        div.style.left = (x1/W*100) + '%';
                        div.style.top = (y1/H*100) + '%';
                        div.style.width = ((x2-x1)/W*100) + '%';
                        div.style.height = ((y2-y1)/H*100) + '%';
                        
                        const color = COLORS[id % COLORS.length];
                        div.style.borderColor = color;
                        
                        // ID 标签
                        const label = document.createElement('div');
                        label.className = 'bbox-label';
                        label.innerText = id;
                        label.style.backgroundColor = color;
                        div.appendChild(label);
                        
                        container.appendChild(div);
                    });
                }
            }).catch(e => console.log("Data fetch error", e));
        }
        
        // 100ms 刷新一次前端数据，保证流畅
        setInterval(update, 100);
    </script>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML_PAGE)

@app.route('/data')
def get_data():
    # 注入 Pi 自己的 CPU 负载
    pi_load = psutil.cpu_percent()
    for k in global_data:
        global_data[k]['pi_cpu'] = pi_load
    return jsonify(global_data)

def generate(index):
    while True:
        frame = global_frames[index]
        ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 60])
        yield (b'--frame\r\n' b'Content-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')
        time.sleep(0.04)

@app.route('/feed/<int:idx>')
def feed(idx):
    return Response(generate(idx), mimetype='multipart/x-mixed-replace; boundary=frame')

if __name__ == '__main__':
    for i in range(4):
        t = threading.Thread(target=cloud_client_thread, args=(i, VIDEOS[i]))
        t.daemon = True
        t.start()
        
    print("✅ Pi Client V6 Started.")
    app.run(host='0.0.0.0', port=5000, threaded=True, use_reloader=False)
