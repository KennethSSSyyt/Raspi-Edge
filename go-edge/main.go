package main

import (
	"encoding/csv"
	"encoding/json"
	"fmt"
	"image"
	"os"
	"runtime"
	"strconv"
	"sync"
	"time"

	"github.com/gin-gonic/gin"
	zmq "github.com/pebbe/zmq4"
	"github.com/shirou/gopsutil/v3/cpu"
	"gocv.io/x/gocv"
)

// === 关键配置 ===
var (
	PC_IP              = "192.168.137.1" // 请务必确认 PC 的 IP
	PC_PORT            = "5555"
	CPU_HIGH_THRESHOLD = 80.0
	WORKER_COUNT       = 4
	CAM_COUNT          = 4
)

var (
	currentCPULoad       float64
	pendingFrames        [4][]byte
	offloadFramesCounter [4]int64
	mutex                sync.RWMutex
)

func benchmarkLoop() {
	filePath := fmt.Sprintf("metrics_threads_%d.csv", WORKER_COUNT)
	f, _ := os.Create(filePath)
	defer f.Close()
	writer := csv.NewWriter(f)
	writer.Write([]string{"Time", "CPU_Usage", "Degrade_Active", "Memory_MB", "Offload_Count"})
	time.Sleep(5 * time.Second)

	for {
		mutex.RLock()
		load := currentCPULoad
		active := load > CPU_HIGH_THRESHOLD
		var total int64
		for _, v := range offloadFramesCounter { total += v }
		mutex.RUnlock()

		var m runtime.MemStats
		runtime.ReadMemStats(&m)

		writer.Write([]string{
			time.Now().Format("15:04:05"),
			fmt.Sprintf("%.2f", load),
			fmt.Sprintf("%v", active),
			fmt.Sprintf("%d", m.Alloc/1024/1024),
			fmt.Sprintf("%d", total),
		})
		writer.Flush()
		time.Sleep(1 * time.Second)
	}
}

func videoLoop(id int, path string) {
	cap, _ := gocv.VideoCaptureFile(path)
	defer cap.Close()
	img := gocv.NewMat(); defer img.Close()
	proc := gocv.NewMat(); defer proc.Close()
	canny := gocv.NewMat(); defer canny.Close()

	for {
		if ok := cap.Read(&img); !ok {
			cap.Set(gocv.VideoCapturePosFrames, 0); continue
		}
		mutex.RLock()
		load := currentCPULoad
		mutex.RUnlock()

		// 模拟计算负载
		gocv.GaussianBlur(img, &proc, image.Pt(5, 5), 0, 0, gocv.BorderDefault)
		if load < CPU_HIGH_THRESHOLD {
			gocv.Canny(proc, &canny, 50, 150)
			canny.CopyTo(&proc)
		}

		gocv.Resize(proc, &proc, image.Point{X: 640, Y: 360}, 0, 0, gocv.InterpolationLinear)
		buf, _ := gocv.IMEncode(".jpg", proc)
		
		mutex.Lock()
		pendingFrames[id] = append([]byte(nil), buf.GetBytes()...)
		mutex.Unlock()
		buf.Close()
		time.Sleep(20 * time.Millisecond)
	}
}

func networkWorker(workerID int, taskChan chan int) {
	context, _ := zmq.NewContext()
	dealer, _ := context.NewSocket(zmq.DEALER)
	// 设置唯一 Identity 有助于 ROUTER 路由
	identity := fmt.Sprintf("W%d-%d", WORKER_COUNT, workerID)
	dealer.SetIdentity(identity)
	dealer.SetRcvtimeo(1000 * time.Millisecond)
	dealer.Connect(fmt.Sprintf("tcp://%s:%s", PC_IP, PC_PORT))
	defer dealer.Close()

	for camID := range taskChan {
		mutex.Lock()
		data := pendingFrames[camID]
		pendingFrames[camID] = nil
		mutex.Unlock()

		if len(data) > 0 {
			// DEALER 发送 2 帧: [CAM-ID, 图像数据]
			dealer.Send(fmt.Sprintf("CAM-%d", camID), zmq.SNDMORE)
			dealer.SendBytes(data, 0)

			// 接收 PC 回复 (只有 1 帧内容)
			reply, err := dealer.Recv(0)
			if err == nil {
				var res map[string]interface{}
				if json.Unmarshal([]byte(reply), &res) == nil {
					if res["status"] == "ok" {
						mutex.Lock()
						offloadFramesCounter[camID]++
						mutex.Unlock()
					}
				}
			}
		}
	}
}

func main() {
	if val, ok := os.LookupEnv("WORKER_COUNT"); ok {
		WORKER_COUNT, _ = strconv.Atoi(val)
	}
	runtime.GOMAXPROCS(runtime.NumCPU())

	taskChan := make(chan int, 100)
	for i := 0; i < WORKER_COUNT; i++ {
		go networkWorker(i, taskChan)
	}

	go func() {
		for {
			for i := 0; i < CAM_COUNT; i++ { taskChan <- i }
			time.Sleep(10 * time.Millisecond)
		}
	}()

	go benchmarkLoop()
	go func() {
		for {
			p, _ := cpu.Percent(time.Second, false)
			if len(p) > 0 {
				mutex.Lock()
				currentCPULoad = p[0]
				mutex.Unlock()
			}
		}
	}()

	videoFiles := []string{
		"/home/pi/raspi-edge-ai/python-infer/videos/video1.mp4",
		"/home/pi/raspi-edge-ai/python-infer/videos/video2.mp4",
		"/home/pi/raspi-edge-ai/python-infer/videos/video3.mp4",
		"/home/pi/raspi-edge-ai/python-infer/videos/video4.mp4",
	}
	for i, p := range videoFiles { go videoLoop(i, p) }

	r := gin.New()
	gin.SetMode(gin.ReleaseMode)
	fmt.Printf("🚀 边缘节点启动 | 线程数: %d | 目标PC: %s\n", WORKER_COUNT, PC_IP)
	r.Run(":5000")
}
