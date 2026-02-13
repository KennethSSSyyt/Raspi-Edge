package main

import (
	"encoding/json"
	"fmt"
	"image"
	"sync"
	"time"

	"github.com/gin-gonic/gin"
	zmq "github.com/pebbe/zmq4"
	"github.com/shirou/gopsutil/v3/cpu"
	"gocv.io/x/gocv"
)

// === 配置 ===
const (
	PC_IP     = "192.168.137.1" // ⚠️ 修改为你 PC 的 IP
	PC_PORT   = "5555"
	FRAME_W   = 640
	FRAME_H   = 360
	SEND_W    = 320
	SEND_H    = 180
	CAM_COUNT = 4
)

var (
	// 图片缓存
	jpgCache [CAM_COUNT][]byte
	// 原始 Mat 缓存 (用于网络线程缩放)
	matCache [CAM_COUNT]gocv.Mat
	// 数据缓存
	dataCache [CAM_COUNT]map[string]interface{}
	// 锁
	mutex sync.RWMutex
)

// === 视频读取协程 (Video Loop) - 极速 ===
func videoLoop(id int, videoPath string) {
	camID := fmt.Sprintf("CAM-%02d", id+1)
	fmt.Printf("🎥 Video Loop %s started\n", camID)

	var cap *gocv.VideoCapture
	var err error
	for i := 0; i < 5; i++ {
		cap, err = gocv.VideoCaptureFile(videoPath)
		if err == nil && cap.IsOpened() { break }
		time.Sleep(1 * time.Second)
	}
	defer cap.Close()

	img := gocv.NewMat()
	resized := gocv.NewMat()
	defer img.Close()
	defer resized.Close()

	for {
		if ok := cap.Read(&img); !ok {
			cap.Set(gocv.VideoCapturePosFrames, 0)
			continue
		}
		if img.Empty() { continue }

		// 1. 缩放用于显示
		gocv.Resize(img, &resized, image.Point{FRAME_W, FRAME_H}, 0, 0, gocv.InterpolationLinear)
		
		// 2. 编码显示流
		bufWeb, err := gocv.IMEncode(".jpg", resized)
		if err == nil {
			safeBytes := make([]byte, bufWeb.Len())
			copy(safeBytes, bufWeb.GetBytes())
			bufWeb.Close()

			mutex.Lock()
			// 更新 JPEG 缓存 (给 Web 看)
			jpgCache[id] = safeBytes
			// 更新 Mat 缓存 (给 Network 用)
			if !matCache[id].Empty() {
				matCache[id].Close()
			}
			matCache[id] = resized.Clone()
			mutex.Unlock()
		}

		time.Sleep(33 * time.Millisecond) // 30 FPS
	}
}

// === 网络发送协程 (Network Loop) - 异步 ===
func networkLoop(id int) {
	camID := fmt.Sprintf("CAM-%02d", id+1)
	fmt.Printf("🌐 Network Loop %s started\n", camID)

	requester, _ := zmq.NewSocket(zmq.REQ)
	defer requester.Close()
	requester.Connect("tcp://" + PC_IP + ":" + PC_PORT)
	requester.SetRcvtimeo(1000 * time.Millisecond)

	small := gocv.NewMat()
	defer small.Close()

	for {
		// 1. 获取最新帧
		mutex.Lock()
		if matCache[id].Empty() {
			mutex.Unlock()
			time.Sleep(50 * time.Millisecond)
			continue
		}
		// 缩放到 320p 发送，极大减少网络压力
		gocv.Resize(matCache[id], &small, image.Point{SEND_W, SEND_H}, 0, 0, gocv.InterpolationLinear)
		mutex.Unlock()

		// 2. 编码发送
		buf, err := gocv.IMEncode(".jpg", small)
		if err == nil {
			requester.Send(camID, zmq.SNDMORE)
			requester.SendBytes(buf.GetBytes(), 0)
			buf.Close()

			// 3. 等待回复 (阻塞这里不会影响视频播放)
			reply, err := requester.Recv(0)
			if err == nil {
				var data map[string]interface{}
				if json.Unmarshal([]byte(reply), &data) == nil {
					mutex.Lock()
					dataCache[id] = data
					mutex.Unlock()
				}
			}
		}
		// 控制 AI 频率 20Hz 左右
		time.Sleep(50 * time.Millisecond)
	}
}

// === HTML 模板 ===
const HTML_PAGE = `
<!DOCTYPE html>
<html>
<head>
<title>RoadOS Async</title>
<style>
    html, body { margin: 0; padding: 0; width: 100%; height: 100%; overflow: hidden; background: #000; font-family: monospace; }
    .grid { display: grid; grid-template-columns: 50% 50%; height: 100vh; width: 100vw; }
    .box { position: relative; border: 1px solid #222; box-sizing: border-box; }
    img { width: 100%; height: 100%; object-fit: fill; display: block; }
    .overlay { position: absolute; top: 0; left: 0; width: 100%; height: 100%; pointer-events: none; }
    
    .dashboard { position: absolute; top: 10px; right: 10px; background: rgba(0,0,0,0.8); border-left: 4px solid cyan; padding: 5px; width: 180px; pointer-events: auto; }
    .row { display: flex; justify-content: space-between; margin-bottom: 2px; font-size: 11px; color: #eee; }
    .val { font-weight: bold; color: cyan; }
    
    .bbox { position: absolute; border: 2px solid; transition: 0.1s linear; }
    .tag { position: absolute; top: -14px; left: 0; background: inherit; color: #000; font-size: 10px; font-weight: bold; padding: 0 2px; }
    
    .zone-blue { position: absolute; top: 50%; width: 100%; height: 10%; background: rgba(0, 100, 255, 0.2); }
    .zone-pink { position: absolute; top: 60%; width: 100%; height: 10%; background: rgba(255, 0, 100, 0.2); }
    .line { position: absolute; top: 60%; width: 100%; height: 2px; background: cyan; box-shadow: 0 0 5px cyan; transition: 0.2s; }
    .line.active { background: white; box-shadow: 0 0 15px white; height: 3px; }
</style>
</head>
<body>
<div class="grid">
    <div class="box" id="b0"><img src="/video/0"><div class="overlay" id="ov-0"></div></div>
    <div class="box" id="b1"><img src="/video/1"><div class="overlay" id="ov-1"></div></div>
    <div class="box" id="b2"><img src="/video/2"><div class="overlay" id="ov-2"></div></div>
    <div class="box" id="b3"><img src="/video/3"><div class="overlay" id="ov-3"></div></div>
</div>
<script>
const COLORS = ["#00ffff", "#ff00ff", "#00ff00", "#ffff00", "#ff8800"];
const dashTemplate = '<div class="zone-blue"></div><div class="zone-pink"></div><div class="line"></div><div class="dashboard"><div class="row" style="color:cyan;font-weight:bold;border-bottom:1px solid #444;">CAM-ID [HIGH]</div><div class="row"><span>Weather:</span><span class="val" id="env">--</span></div><div class="row"><span>Pi/PC CPU:</span><span class="val" id="cpu">--</span></div><div class="row"><span>Status:</span><span class="val" id="st">--</span></div><div class="row"><span>Count:</span><span class="val" id="cnt">0</span></div><div class="row"><span>Speed:</span><span class="val" id="spd">0</span></div><div class="row"><span>LPR:</span><span class="val" style="color:#f0f" id="lp">--</span></div></div>';

for(let i=0; i<4; i++) {
    document.getElementById('ov-'+i).innerHTML = dashTemplate.replace('CAM-ID', 'CAM-0'+(i+1));
}

function update() {
    fetch('/api/data').then(r => r.json()).then(allData => {
        for(let i=0; i<4; i++) {
            const data = allData[i];
            if(!data) continue;
            const div = document.getElementById('ov-'+i);
            
            const m = data.metrics || {};
            const t = data.tracks || [];
            const e = data.env || {};
            
            div.querySelector('#env').innerText = (e.weather||'-');
            div.querySelector('#cpu').innerText = parseInt(data.pi_cpu||0) + "% / " + parseInt(data.pc_cpu||0) + "%";
            div.querySelector('#st').innerText = m.status || 'WAIT';
            div.querySelector('#cnt').innerText = m.count || 0;
            div.querySelector('#spd').innerText = (m.avg_spd||0) + " km/h";
            div.querySelector('#lp').innerText = m.plate || '--';
            
            const line = div.querySelector('.line');
            if(m.triggered) line.classList.add('active'); else line.classList.remove('active');

            div.querySelectorAll('.bbox').forEach(b => b.remove());
            t.forEach(tk => {
                const [x1, y1, x2, y2, id] = tk;
                const W = 320.0; const H = 180.0; 
                const el = document.createElement('div');
                el.className = 'bbox';
                el.style.borderColor = COLORS[id % 5];
                el.style.left = (x1/W*100)+'%'; el.style.top = (y1/H*100)+'%';
                el.style.width = ((x2-x1)/W*100)+'%'; el.style.height = ((y2-y1)/H*100)+'%';
                el.innerHTML = '<div class="tag" style="background:'+COLORS[id % 5]+'">ID:'+id+'</div>';
                div.appendChild(el);
            });
        }
    });
}
setInterval(update, 100);
</script>
</body>
</html>
`

func main() {
	for i := 0; i < CAM_COUNT; i++ {
		dataCache[i] = make(map[string]interface{})
		matCache[i] = gocv.NewMat()
	}

	videoFiles := []string{
		"/app/videos/video1.mp4", "/app/videos/video2.mp4",
		"/app/videos/video3.mp4", "/app/videos/video4.mp4",
	}

	// 启动视频读取
	for i, path := range videoFiles {
		go videoLoop(i, path)
	}
	
	// 启动网络发送
	for i := 0; i < CAM_COUNT; i++ {
		go networkLoop(i)
	}

	r := gin.Default()
	r.GET("/", func(c *gin.Context) { c.Header("Content-Type", "text/html"); c.String(200, HTML_PAGE) })
	
	r.GET("/api/data", func(c *gin.Context) {
		mutex.RLock()
		p, _ := cpu.Percent(0, false)
		piCpu := 0.0
		if len(p) > 0 { piCpu = p[0] }
		
		res := make(map[int]interface{})
		for i := 0; i < CAM_COUNT; i++ {
			d := make(map[string]interface{})
			for k, v := range dataCache[i] { d[k] = v }
			d["pi_cpu"] = piCpu
			res[i] = d
		}
		mutex.RUnlock()
		c.JSON(200, res)
	})

	r.GET("/video/:id", func(c *gin.Context) {
		id := 0
		fmt.Sscanf(c.Param("id"), "%d", &id)
		c.Header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
		for {
			mutex.RLock()
			data := jpgCache[id]
			mutex.RUnlock()

			if len(data) > 0 {
				c.Writer.Write([]byte("--frame\r\nContent-Type: image/jpeg\r\n\r\n"))
				c.Writer.Write(data); c.Writer.Write([]byte("\r\n"))
			} else {
				time.Sleep(50 * time.Millisecond)
			}
			time.Sleep(40 * time.Millisecond)
		}
	})
	r.Run(":5000")
}
