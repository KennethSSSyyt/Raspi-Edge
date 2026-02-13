import time
import json
import redis
from flask import Flask, Response, jsonify, render_template_string

app = Flask(__name__)

# 连接 Redis (数据中心)
# decode_responses=True 用于读文本数据 (JSON)
r_data = redis.Redis(host='rsu-redis', port=6379, decode_responses=True)
# decode_responses=False 用于读二进制数据 (图片)
r_img = redis.Redis(host='rsu-redis', port=6379, decode_responses=False)

# === 修复版 HTML (强制一屏，无滚动条) ===
HTML_PAGE = """
<!DOCTYPE html>
<html>
<head>
    <title>RoadOS Ultimate Monitor</title>
    <style>
        /* 1. 全局重置：去除所有默认边距，隐藏滚动条 */
        html, body { 
            margin: 0; padding: 0; 
            width: 100%; height: 100%; 
            overflow: hidden; 
            background-color: #000; 
            font-family: 'Consolas', 'Courier New', monospace;
        }

        /* 2. 网格布局：精确的 50% 宽高 */
        .grid { 
            display: grid; 
            grid-template-columns: 1fr 1fr; 
            grid-template-rows: 1fr 1fr; 
            width: 100vw; 
            height: 100vh; 
            gap: 0; /* 无缝隙 */
        }

        /* 3. 摄像头容器：相对定位，用于放置覆盖层 */
        .cam-box { 
            position: relative; 
            width: 100%; 
            height: 100%; 
            border: 1px solid #333; 
            box-sizing: border-box; /* 关键：让边框包含在宽高内 */
            overflow: hidden;
        }

        /* 4. 视频流：保持比例或铺满 */
        .video-stream { 
            width: 100%; 
            height: 100%; 
            object-fit: fill; /* 强制铺满，如果你想要不变形请改用 contain */
            display: block;
        }

        /* === 以下是你喜欢的 UI 覆盖层样式 === */
        .overlay { position: absolute; top: 0; left: 0; width: 100%; height: 100%; pointer-events: none; }
        
        /* 半透明区域 */
        .zone-blue { position: absolute; top: 50%; width: 100%; height: 10%; background: rgba(0, 100, 255, 0.15); }
        .zone-pink { position: absolute; top: 60%; width: 100%; height: 10%; background: rgba(255, 0, 100, 0.15); }
        
        /* 检测线 */
        .line { position: absolute; top: 60%; width: 100%; height: 2px; background: cyan; box-shadow: 0 0 5px cyan; transition: 0.2s; }
        .line.active { background: white; box-shadow: 0 0 15px white; height: 3px; }

        /* 车辆框 (JS生成) */
        .bbox { position: absolute; border: 2px solid lime; transition: all 0.1s linear; }
        .bbox-label { position: absolute; top: -18px; left: -2px; background: lime; color: black; font-size: 11px; padding: 1px 3px; font-weight: bold; }

        /* 仪表盘 (右上角悬浮) */
        .dashboard { 
            position: absolute; top: 10px; right: 10px; 
            background: rgba(0, 0, 0, 0.6); 
            border-left: 3px solid cyan;
            padding: 8px; width: 150px;
            pointer-events: auto;
        }
        .dash-row { display: flex; justify-content: space-between; margin-bottom: 4px; font-size: 12px; color: #ddd; }
        .val { font-weight: bold; color: cyan; }
        .val-danger { color: #ff0055; }

        /* 大车牌显示 (右下角) */
        .plate-box {
            position: absolute; bottom: 40px; right: 10px;
            background: rgba(255, 255, 255, 0.9); 
            border: 2px solid #0033aa; color: #0033aa;
            font-size: 20px; font-weight: bold; padding: 2px 8px;
            display: none; border-radius: 4px;
        }
        
        /* 底部日志 */
        .logs { position: absolute; bottom: 5px; left: 5px; font-size: 10px; color: #666; }
    </style>
</head>
<body>
    <div class="grid">
        <div class="cam-box" id="box-0">
            <img class="video-stream" src="/feed/0">
            <div class="overlay" id="ov-0">
                <div class="zone-blue"></div><div class="zone-pink"></div><div class="line"></div>
                <div class="dashboard">
                    <div class="dash-row" style="color:cyan; font-weight:bold;">CAM-01 [HIGH]</div>
                    <div class="dash-row"><span>Status:</span> <span class="val" id="st-0">--</span></div>
                    <div class="dash-row"><span>Count:</span> <span class="val" id="cnt-0">0</span></div>
                    <div class="dash-row"><span>Speed:</span> <span class="val" id="spd-0">0</span></div>
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
                    <div class="dash-row" style="color:cyan; font-weight:bold;">CAM-02 [HIGH]</div>
                    <div class="dash-row"><span>Status:</span> <span class="val" id="st-1">--</span></div>
                    <div class="dash-row"><span>Count:</span> <span class="val" id="cnt-1">0</span></div>
                    <div class="dash-row"><span>Speed:</span> <span class="val" id="spd-1">0</span></div>
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
                    <div class="dash-row" style="color:orange; font-weight:bold;">CAM-03 [LOW]</div>
                    <div class="dash-row"><span>Status:</span> <span class="val" id="st-2">--</span></div>
                    <div class="dash-row"><span>Count:</span> <span class="val" id="cnt-2">0</span></div>
                    <div class="dash-row"><span>Speed:</span> <span class="val" id="spd-2">0</span></div>
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
                    <div class="dash-row" style="color:orange; font-weight:bold;">CAM-04 [LOW]</div>
                    <div class="dash-row"><span>Status:</span> <span class="val" id="st-3">--</span></div>
                    <div class="dash-row"><span>Count:</span> <span class="val" id="cnt-3">0</span></div>
                    <div class="dash-row"><span>Speed:</span> <span class="val" id="spd-3">0</span></div>
                </div>
                <div class="plate-box" id="lp-3"></div>
                <div class="logs" id="log-3">System Ready...</div>
            </div>
        </div>
    </div>

    <script>
        const COLORS = ["#00ffff", "#ff00ff", "#00ff00", "#ffff00", "#ff8800"];

        function update() {
            fetch('/api/data').then(r => r.json()).then(allData => {
                for(let i=0; i<4; i++) {
                    // 注意：Redis key 是 "cam_0_data"，这里需要构造一下
                    const tracks = allData['tracks_'+i] || [];
                    const stats = allData['stats_'+i] || {};
                    
                    const container = document.getElementById('ov-'+i);
                    
                    // 1. 清除旧框
                    container.querySelectorAll('.bbox').forEach(b => b.remove());
                    
                    // 2. 更新仪表盘
                    document.getElementById('cnt-'+i).innerText = stats.count || 0;
                    
                    const statusElem = document.getElementById('st-'+i);
                    statusElem.innerText = stats.status || "IDLE";
                    if(stats.status === "BUSY") statusElem.className = "val val-danger";
                    else statusElem.className = "val";

                    // 3. 画新框
                    tracks.forEach(t => {
                        // 数据格式 [x1, y1, x2, y2, id]
                        const [x1, y1, x2, y2, id] = t;
                        const W = 640, H = 360; // 对应后端分辨率
                        
                        const div = document.createElement('div');
                        div.className = 'bbox';
                        div.style.left = (x1/W*100) + '%';
                        div.style.top = (y1/H*100) + '%';
                        div.style.width = ((x2-x1)/W*100) + '%';
                        div.style.height = ((y2-y1)/H*100) + '%';
                        
                        const color = COLORS[id % COLORS.length];
                        div.style.borderColor = color;
                        
                        // ID 标签
                        const label = document.createElement('div');
                        label.className = 'bbox-label';
                        label.innerText = 'ID:' + id;
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
    # 一次性读取所有摄像头的数据，减少网络请求
    data = {}
    for i in range(4):
        # 读取 Tracks
        raw_tracks = r_data.get(f"cam_{i}_data")
        data[f"tracks_{i}"] = json.loads(raw_tracks) if raw_tracks else []
        
        # 读取 Stats (如果 AI 引擎写了这个 key)
        raw_stats = r_data.get(f"cam_{i}_stats")
        data[f"stats_{i}"] = json.loads(raw_stats) if raw_stats else {}
        
    return jsonify(data)

def generate(index):
    while True:
        # 从 Redis 读取二进制图片
        img_bytes = r_img.get(f"cam_{index}_img")
        if img_bytes:
            yield (b'--frame\r\n' b'Content-Type: image/jpeg\r\n\r\n' + img_bytes + b'\r\n')
        else:
            time.sleep(0.1) # 没图时等待，防空转
        time.sleep(0.04)

@app.route('/feed/<int:idx>')
def feed(idx):
    return Response(generate(idx), mimetype='multipart/x-mixed-replace; boundary=frame')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, threaded=True)
