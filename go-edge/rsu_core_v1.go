package main

import (
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io/ioutil"
	"log"
	"math"
	"math/rand"
	"net"
	"net/http"
	"os"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// ================= Configuration =================
const (
	LISTEN_PORT    = ":9999"
	PC_CONTROL_URL = "192.168.1.5:8888" // 🔴 请确认这是你 PC 的 IP
	QUEUE_SIZE     = 20000              // 队列深度
	WORKER_NUM     = 8                  // Worker 数量
	
	// 🔥 暴力负载：每包进行 20万次浮点运算 + 哈希
	COMPUTE_INTENSITY = 200000 

	LOAD_THRESHOLD_WARN = 0.6
	LOAD_THRESHOLD_CRIT = 0.9
	POI_X, POI_Y        = 0.0, 0.0
	ROI_RADIUS          = 1000.0
)

// ================= Data Structures =================
type V2XMessage struct {
	ID   string  `json:"id"`
	Ts   float64 `json:"ts"`
	Type string  `json:"type"`
	X    float64 `json:"x"`
	Y    float64 `json:"y"`
	Spd  float64 `json:"spd"`
	Env  string  `json:"env"`
	Desc string  `json:"desc"`
}

type ControlCommand struct {
	Action string `json:"action"`
	Reason string `json:"reason"`
}

// Global Stats (Atomic)
var (
	opsTotal       uint64
	opsBytes       uint64
	opsDecoded     uint64
	opsProcessed   uint64 // 相当于 Valid
	opsDropped     uint64
	opsEmergency   uint64
	opsNoise       uint64
	opsControlSent uint64
	congestionFlag int32
)

// Logs
var eventLogFile *os.File
var perfLogFile *os.File
var fileMutex sync.Mutex
var lastDropLogTime int64 

// ================= Main Program =================
func main() {
	runtime.GOMAXPROCS(runtime.NumCPU())
	rand.Seed(time.Now().UnixNano())

	initLogs()
	defer eventLogFile.Close()
	defer perfLogFile.Close()

	addr, _ := net.ResolveUDPAddr("udp", LISTEN_PORT)
	conn, err := net.ListenUDP("udp", addr)
	if err != nil {
		fmt.Printf("❌ Startup Failed: %v\n", err)
		return
	}
	defer conn.Close()

	fmt.Printf("✅ RSU Core Started | Intensity: %d | Workers: %d\n", COMPUTE_INTENSITY, WORKER_NUM)

	taskQueue := make(chan []byte, QUEUE_SIZE)

	// 启动 Worker
	for i := 0; i < WORKER_NUM; i++ {
		go worker(i, taskQueue)
	}

	// 🟢 启动所有辅助协程 (之前报错就是因为缺了这些函数的定义)
	go feedbackLoop()
	go startMonitor()
	go performanceLogger(taskQueue)

	buf := make([]byte, 65535)
	for {
		n, _, err := conn.ReadFromUDP(buf)
		if err != nil {
			continue
		}

		atomic.AddUint64(&opsTotal, 1)
		atomic.AddUint64(&opsBytes, uint64(n))

		data := make([]byte, n)
		copy(data, buf[:n])

		// === 自适应流控 ===
		currentLoad := float64(len(taskQueue)) / float64(QUEUE_SIZE)
		dropReason := ""

		if currentLoad > LOAD_THRESHOLD_CRIT {
			if rand.Float32() > 0.1 { 
				dropReason = "Queue_Critical"
			}
		}
		if dropReason == "" && currentLoad > LOAD_THRESHOLD_WARN {
			if rand.Float32() > 0.5 { 
				dropReason = "RED_Algo"
			}
		}

		if dropReason != "" {
			atomic.AddUint64(&opsDropped, 1)
			logDropReason(dropReason)
			continue
		}

		select {
		case taskQueue <- data:
		default:
			atomic.AddUint64(&opsDropped, 1)
			logDropReason("Buffer_Full")
		}
	}
}

// ================= Worker Logic =================
func worker(id int, tasks <-chan []byte) {
	for raw := range tasks {
		var msg V2XMessage
		
		if err := json.Unmarshal(raw, &msg); err != nil {
			continue
		}
		atomic.AddUint64(&opsDecoded, 1)

		// CPU Stress
		_ = sha256.Sum256(raw)
		val := 1.0
		for i := 0; i < COMPUTE_INTENSITY; i++ {
			val = math.Sqrt(val + float64(i)*msg.Spd)
		}
		if val < -1 { fmt.Println("x") }

		if msg.Type == "NOISE" {
			atomic.AddUint64(&opsNoise, 1)
			continue
		}

		dist := math.Sqrt(math.Pow(msg.X-POI_X, 2) + math.Pow(msg.Y-POI_Y, 2))
		if dist > ROI_RADIUS {
			continue
		}

		atomic.AddUint64(&opsProcessed, 1)

		if msg.Type == "EMERGENCY" || msg.Type == "ACCIDENT" || msg.Type == "WARNING" {
			if msg.Type == "ACCIDENT" || msg.Type == "EMERGENCY" {
				atomic.AddUint64(&opsEmergency, 1)
			}
			recordEvent(msg.Type, msg.ID, msg.Spd, msg.Desc)
		}

		limit := 5.0
		if msg.Env == "NIGHT" { limit = 15.0 }
		if msg.Spd < limit {
			atomic.StoreInt32(&congestionFlag, 1)
		}
	}
}

// ================= Performance Monitor =================
func getCPUSample() (idle, total float64) {
	data, err := ioutil.ReadFile("/proc/stat")
	if err != nil { return 0, 0 }
	lines := strings.Split(string(data), "\n")
	fields := strings.Fields(lines[0])
	user, _ := strconv.ParseFloat(fields[1], 64)
	nice, _ := strconv.ParseFloat(fields[2], 64)
	system, _ := strconv.ParseFloat(fields[3], 64)
	idleVal, _ := strconv.ParseFloat(fields[4], 64)
	return idleVal, user + nice + system + idleVal
}

func performanceLogger(queue chan []byte) {
	var prevTotal, prevBytes, prevDecoded, prevProcessed, prevDropped, prevControl, prevEmerg uint64
	prevIdle, prevTotalCPU := getCPUSample()
	
	ticker := time.NewTicker(1 * time.Second)
	
	for range ticker.C {
		currTotal := atomic.LoadUint64(&opsTotal)
		currBytes := atomic.LoadUint64(&opsBytes)
		currDecoded := atomic.LoadUint64(&opsDecoded)
		currProcessed := atomic.LoadUint64(&opsProcessed)
		currDropped := atomic.LoadUint64(&opsDropped)
		currControl := atomic.LoadUint64(&opsControlSent)
		currEmerg := atomic.LoadUint64(&opsEmergency)
		
		currIdle, currTotalCPU := getCPUSample()
		deltaIdle := currIdle - prevIdle
		deltaTotal := currTotalCPU - prevTotalCPU
		cpuUsage := 0.0
		if deltaTotal > 0 { cpuUsage = (1.0 - (deltaIdle / deltaTotal)) * 100.0 }

		rIn := currTotal - prevTotal
		rBytes := currBytes - prevBytes
		rDec := currDecoded - prevDecoded
		rProc := currProcessed - prevProcessed
		rDrop := currDropped - prevDropped
		rCtrl := currControl - prevControl
		rEmerg := currEmerg - prevEmerg
		
		mbps := float64(rBytes) * 8.0 / 1000000.0

		// Console Output
		fmt.Printf("\r🚀 CPU:%.1f%% | In:%d | Mbps:%.2f | Proc:%d | Drop:%d | Q:%d | Ctrl:%d  ", 
			cpuUsage, rIn, mbps, rProc, rDrop, len(queue), rCtrl)
		
		timestamp := time.Now().Format("15:04:05")
		logLine := fmt.Sprintf("%s,%.2f,%d,%.2f,%d,%d,%d,%d,%d,%d\n", 
			timestamp, cpuUsage, rIn, mbps, rDec, rProc, rDrop, len(queue), rEmerg, rCtrl)
		
		fileMutex.Lock()
		perfLogFile.WriteString(logLine)
		fileMutex.Unlock()
		
		prevTotal, prevBytes, prevDecoded, prevProcessed, prevDropped, prevControl, prevEmerg = currTotal, currBytes, currDecoded, currProcessed, currDropped, currControl, currEmerg
		prevIdle, prevTotalCPU = currIdle, currTotalCPU
	}
}

// ================= Helpers (之前缺失的部分) =================

func initLogs() {
	var err error
	eventLogFile, err = os.OpenFile("rsu_events.csv", os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0666)
	if err != nil { log.Fatal(err) }
	
	// 🟢 权限修复: 让 pi 用户可以读写
	os.Chown("rsu_events.csv", 1000, 1000)

	fileMutex.Lock()
	if stat, _ := eventLogFile.Stat(); stat.Size() == 0 {
		eventLogFile.WriteString("Time,Event_Type,Vehicle_ID,Speed,Details\n")
	}
	fileMutex.Unlock()

	perfLogFile, err = os.OpenFile("rsu_perf.csv", os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0666)
	if err != nil { log.Fatal(err) }
	
	// 🟢 权限修复
	os.Chown("rsu_perf.csv", 1000, 1000)

	perfLogFile.WriteString("Time,CPU_Usage,In_Rate,Bandwidth_Mbps,Decode_Rate,Process_Rate,Drop_Rate,Queue_Len,Emerg_Rate,Control_Rate\n")
}

func logDropReason(reason string) {
	now := time.Now().Unix()
	if atomic.LoadInt64(&lastDropLogTime) != now {
		atomic.StoreInt64(&lastDropLogTime, now)
		recordEvent("PACKET_DROP", "SYSTEM", 0, reason)
	}
}

func recordEvent(evtType, vehID string, speed float64, details string) {
	go func() {
		fileMutex.Lock()
		defer fileMutex.Unlock()
		timestamp := time.Now().Format("15:04:05.000")
		line := fmt.Sprintf("%s,%s,%s,%.2f,%s\n", timestamp, evtType, vehID, speed, details)
		eventLogFile.WriteString(line)
	}()
}

func feedbackLoop() {
	pcAddr, _ := net.ResolveUDPAddr("udp", PC_CONTROL_URL)
	conn, _ := net.DialUDP("udp", nil, pcAddr)
	for {
		time.Sleep(2 * time.Second)
		if atomic.SwapInt32(&congestionFlag, 0) == 1 {
			cmd := ControlCommand{Action: "OPTIMIZE_TRAFFIC", Reason: "Congestion"}
			bytes, _ := json.Marshal(cmd)
			conn.Write(bytes)
			atomic.AddUint64(&opsControlSent, 1)
			recordEvent("CONTROL_SENT", "SYSTEM", 0, "Action:Green_Wave")
		}
	}
}

func startMonitor() {
	http.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprintf(w, "RSU Running")
	})
	go http.ListenAndServe(":8080", nil)
}
