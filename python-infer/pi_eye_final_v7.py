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
VIDEO_READ_SKIP = 1 # 保证流畅

global_frames = {}
global_data = {}

for i in range(4): 
    global_frames[i] = np.zeros((FRAME_H, FRAME_W, 3), dtype=np.uint8)
    global_data[str(i)] = {}

def cloud_client_thread(index, video_path):
    cam_id = f"C{index+1}"
    sender = imagezmq.ImageSender(connect_to=f'tcp://{CLOUD_IP}:5555', REQ_REP=True)
    sender.zmq_socket.setsockopt(imagezmq.zmq.RCVTIMEO, 1000) 
    
    cap = cv2.VideoCapture(video_path)
    frame_cnt = 0
    
    while True:
        for _ in range(VIDEO_READ_SKIP): cap.read()
        ret, frame = cap.read()
        if not ret:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            continue
            
        frame = cv2.resize(frame, (FRAME_W, FRAME_H))
        frame_cnt += 1
        global_frames[index] = frame
        
        if frame_cnt % 3 == 0:
            ret, jpg_buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 50])
            try:
                reply = sender.send_jpg(cam_id, jpg_buffer)
                data = json.loads(reply.decode('utf-8'))
                global_data[str(index)] = data
            except: pass
        time.sleep(0.02)

app = Flask(__name__)

# === 完整的 HTML 模板 (包含标题和仪表盘) ===
HTML_PAGE = """
<!DOCTYPE html>
<html>
<head>
    <title>RoadOS Ultimate</title>
    <style>
        body { margin: 0; background: #000; overflow: hidden; font-family: 'Consolas', sans-serif; }
        
        /* 1. 顶部标题栏 (找回标题) */
        .header {
            height: 40px; background: #111; border-bottom: 2px solid #333;
            display: flex; align-items: center; justify-content: center;
            color: #00f3ff; font-size: 18px; font-weight: bold; letter-spacing: 2px;
        }
        
        /* 2. 网格布局 (减去标题高度) */
        .grid { 
            display: grid; 
            grid-template-columns: 50% 50%; 
            height: calc(100vh - 42px); 
            width: 100vw; 
        }
        .cam-box { position: relative; border: 1px solid #222; overflow: hidden; }
        .video-stream { width: 100%; height: 100%; object-fit: fill; opacity: 0.9; }
        
        .overlay { position: absolute; top: 0; left: 0; width: 100%; height: 100%; pointer-events: none; }
        
        /* 3. 视觉元素 */
        .line { position: absolute; top: 60%; width: 100%; height: 2px; background: cyan; transition: 0.2s; box-shadow: 0 0 5px cyan; }
        .line.active { background: white; box-shadow: 0 0 15px white; height: 3px; }
        .zone-blue { position: absolute; top: 50%; width: 100%; height: 10%; background: rgba(0, 100, 255, 0.2); }
        .zone-pink { position: absolute; top: 60%; width: 100%; height: 10%; background: rgba(255, 0, 100, 0.2); }
        
        /* 4. 仪表盘 (右上角) */
        .dashboard { 
            position: absolute; top: 10px; right: 10px; 
            background: rgba(0, 0, 0, 0.7); 
            border: 1px solid cyan; border-left: 3px solid cyan;
            padding: 5px; width: 180px; pointer-events: auto;
        }
        .dash-row { display: flex; justify-content: space-between; margin-bottom: 3px; font-size: 11px; color: #ddd; }
        .val { font-weight: bold; color: cyan; }
        .label { color: #aaa; }
        
        /* 5. 车辆框 */
        .bbox { position: absolute; border: 2px solid lime; transition: all 0.1s linear; }
        .bbox-label { position: absolute; top: -15px; left: 0; background: lime; color: black; font-size: 10px; padding: 0 2px; font-weight: bold; }
        
        /* 6. 日志 */
        .logs { position: absolute; bottom: 5px; left: 5px; font-size: 10px; color: #888; text-shadow: 1px 1px 0 #000; }
    </style>
</head>
<body>
    <div class="header">
        🚀 RoadOS Cloud-Edge Fusion System (Pi 5 + PC RTX)
    </div>
    <div class="grid">
        <div class="cam-box" id="box-0">
            <img class="video-stream" src="/feed/0">
            <div class="overlay" id="ov-0">
                <div class="zone-blue"></div><div class="zone-pink"></div><div class="line"></div>
                <div class="dashboard">
                    <div class="dash-row" style="color:cyan; font-weight:bold; border-bottom:1px solid #444; padding-bottom:2px;">CAM-01 [HIGH]</div>
                    <div class="dash-row"><span class="label">Weather:</span> <span class="val" id="env-0">--</span></div>
                    <div class="dash-row"><span class="label">CPU (Pi/PC):</span> <span class="val" id="cpu-0">0/0%</span></div>
                    <div class="dash-row"><span class="label">Status:</span> <span class="val" id="st-0">--</span></div>
                    <div class="dash-row"><span class="label">Total Flow:</span> <span class="val" id="cnt-0">0</span></div>
                    <div class="dash-row"><span class="label">Avg Speed:</span> <span class="val" id="spd-0">0</span></div>
                    <div class="dash-row"><span class="label">Last LPR:</span> <span class="val" style="color:#f0f" id="lp-0">--</span></div>
                </div>
                <div class="logs" id="log-0">System Ready...</div>
            </div>
        </div>
        
        <div class="cam-box" id="box-1"><img class="video-stream" src="/feed/1"><div class="overlay" id="ov-1"></div></div>
        <div class="cam-box" id="box-2"><img class="video-stream" src="/feed/2"><div class="overlay" id="ov-2"></div></div>
        <div class="cam-box" id="box-3"><img class="video-stream" src="/feed/3"><div class="overlay" id="ov-3"></div></div>
    </div>

    <script>
        const COLORS = ["#00ffff", "#ff00ff", "#00ff00", "#ffff00", "#ff8800"];

        // 初始化其他格子的HTML结构 (JS自动填充)
        for(let i=1; i<4; i++) {
            document.getElementById('ov-'+i).innerHTML = document.getElementById('ov-0').innerHTML.replace(/0/g, i).replace('CAM-01', 'CAM-0'+(i+1));
        }

        function update() {
            fetch('/api/data').then(r => r.json()).then(allData => {
                for(let i=0; i<4; i++) {
                    const data = allData[i];
                    if(!data) continue;
                    
                    const container = document.getElementById('ov-'+i);
                    const metrics = data.metrics || {};
                    const tracks = data.tracks || [];
                    const env = data.env || {};
                    const pc_cpu = data.pc_cpu || 0;
                    const pi_cpu = data.pi_cpu || 0;
                    
                    // 1. 更新数据文字
                    document.getElementById('env-'+i).innerText = (env.weather || "-");
                    document.getElementById('cpu-'+i).innerText = pi_cpu + "% / " + pc_cpu + "%";
                    document.getElementById('st-'+i).innerText = metrics.status || "WAIT";
                    document.getElementById('cnt-'+i).innerText = metrics.count || 0;
                    document.getElementById('spd-'+i).innerText = (metrics.avg_spd || 0) + " km/h";
                    document.getElementById('lp-'+i).innerText = metrics.plate || "--";
                    
                    // 2. 更新线颜色
                    const line = container.querySelector('.line');
                    if(metrics.triggered) line.classList.add('active');
                    else line.classList.remove('active');
                    
                    // 3. 更新日志
                    if(metrics.logs && metrics.logs.length > 0) {
                        document.getElementById('log-'+i).innerText = metrics.logs[0];
                    }

                    // 4. 绘制框 (先清空)
                    container.querySelectorAll('.bbox').forEach(b => b.remove());
                    tracks.forEach(t => {
                        const [x1, y1, x2, y2, id] = t;
                        const W = 640, H = 360;
                        const div = document.createElement('div');
                        div.className = 'bbox';
                        div.style.left = (x1/W*100) + '%';
                        div.style.top = (y1/H*100) + '%';
                        div.style.width = ((x2-x1)/W*100) + '%';
                        div.style.height = ((y2-y1)/H*100) + '%';
                        const color = COLORS[id % COLORS.length];
                        div.style.borderColor = color;
                        
                        const label = document.createElement('div');
                        label.className = 'bbox-label';
                        label.innerText = id;
                        label.style.backgroundColor = color;
                        div.appendChild(label);
                        container.appendChild(div);
                    });
                }
            }).catch(e => console.log(e));
        }
        setInterval(update, 100);
    </script>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML_PAGE)

@app.route('/api/data')
def get_data():
    payload = {}
    pi_cpu = psutil.cpu_percent()
    for i in range(4):
        data = global_data.get(str(i), {})
        data['pi_cpu'] = pi_cpu
        payload[str(i)] = data
    return jsonify(payload)

def generate(index):
    while True:
        frame = global_frames[index]
        ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
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
    app.run(host='0.0.0.0', port=5000, threaded=True)
