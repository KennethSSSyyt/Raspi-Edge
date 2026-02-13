package main

import (
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
	PC_CONTROL_URL = "192.168.137.1:8888" // 🔴 记得确认这里是你 PC 的 IP
	QUEUE_SIZE     = 10000
	WORKER_NUM     = 4

	// Algorithm Thresholds
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
}

type ControlCommand struct {
	Action string `json:"action"`
	Reason string `json:"reason"`
}

// Global Stats (Atomic - Cumulative)
var (
	opsTotal       uint64
	opsDropped     uint64
	opsFiltered    uint64
	opsValid       uint64
	opsEmergency   uint64
	opsControlSent uint64
	congestionFlag int32
)
var opsNoise uint64 // 统计噪点包
// Log File Handles
var eventLogFile *os.File
var perfLogFile *os.File
var fileMutex sync.Mutex

// ================= Main Program =================
func main() {
	runtime.GOMAXPROCS(runtime.NumCPU())

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

	fmt.Printf("✅ RSU Core Started | Monitor: http://[Pi-IP]:8080\n")
	fmt.Printf("📊 Logging Performance Data to: rsu_perf.csv\n")

	taskQueue := make(chan []byte, QUEUE_SIZE)

	for i := 0; i < WORKER_NUM; i++ {
		go worker(i, taskQueue)
	}

	go feedbackLoop()
	go startMonitor()
	
	// Start Performance Logger
	go performanceLogger(taskQueue)

	// High-Concurrency Ingest Loop
	buf := make([]byte, 4096)
	for {
		n, _, err := conn.ReadFromUDP(buf)
		if err != nil {
			continue
		}

		atomic.AddUint64(&opsTotal, 1)

		data := make([]byte, n)
		copy(data, buf[:n])

		// Adaptive Load Shedding Logic
		currentLoad := float64(len(taskQueue)) / float64(QUEUE_SIZE)
		if currentLoad > LOAD_THRESHOLD_CRIT {
			atomic.AddUint64(&opsDropped, 1)
			continue
		}
		if currentLoad > LOAD_THRESHOLD_WARN && rand.Float32() > 0.5 {
			atomic.AddUint64(&opsDropped, 1)
			continue
		}

		select {
		case taskQueue <- data:
		default:
			atomic.AddUint64(&opsDropped, 1)
		}
	}
}

// ================= Performance Monitor Module (Fixed) =================

// Helper to read raw CPU ticks
func getCPUSample() (idle, total float64) {
	data, err := ioutil.ReadFile("/proc/stat")
	if err != nil {
		return 0, 0
	}
	lines := strings.Split(string(data), "\n")
	fields := strings.Fields(lines[0])

	user, _ := strconv.ParseFloat(fields[1], 64)
	nice, _ := strconv.ParseFloat(fields[2], 64)
	system, _ := strconv.ParseFloat(fields[3], 64)
	idleVal, _ := strconv.ParseFloat(fields[4], 64)
	// iowait, irq, softirq... ignored for simplicity but part of total
	
	totalVal := user + nice + system + idleVal
	return idleVal, totalVal
}

func performanceLogger(queue chan []byte) {
	var prevTotal, prevValid, prevDropped, prevControl, prevEmerg uint64
	var prevIdle, prevTotalCPU float64
	
	// Initial Sample
	prevIdle, prevTotalCPU = getCPUSample()

	ticker := time.NewTicker(1 * time.Second)
	for range ticker.C {
		// 1. Get Current Snapshot
		currTotal := atomic.LoadUint64(&opsTotal)
		currValid := atomic.LoadUint64(&opsValid)
		currDropped := atomic.LoadUint64(&opsDropped)
		currControl := atomic.LoadUint64(&opsControlSent)
		currEmerg := atomic.LoadUint64(&opsEmergency)

		// 2. Calculate CPU Usage (Delta)
		currIdle, currTotalCPU := getCPUSample()
		
		deltaIdle := currIdle - prevIdle
		deltaTotal := currTotalCPU - prevTotalCPU
		cpuUsage := 0.0
		if deltaTotal > 0 {
			cpuUsage = (1.0 - (deltaIdle / deltaTotal)) * 100.0
		}

		// 3. Calculate Throughput Rates (Delta / sec)
		rateTotal := currTotal - prevTotal
		rateValid := currValid - prevValid
		rateDropped := currDropped - prevDropped
		rateControl := currControl - prevControl // Fixed: Now using prevControl
		rateEmerg := currEmerg - prevEmerg       // Fixed: Now using prevEmerg
		
		// 4. Write to CSV
		timestamp := time.Now().Format("15:04:05")
		queueLen := len(queue)
		
		// Format: Time, CPU%, RX_Rate, Process_Rate, Drop_Rate, Queue_Len, Emerg_Rate, Control_Rate
		logLine := fmt.Sprintf("%s,%.2f,%d,%d,%d,%d,%d,%d\n", 
			timestamp, cpuUsage, rateTotal, rateValid, rateDropped, queueLen, rateEmerg, rateControl)
		
		fileMutex.Lock()
		perfLogFile.WriteString(logLine)
		fileMutex.Unlock()

		// 5. Console Output
		fmt.Printf("\r🚀 CPU: %.1f%% | In: %d/s | Drop: %d/s | Q: %d | Emerg: %d/s | Ctrl: %d/s   ", 
			cpuUsage, rateTotal, rateDropped, queueLen, rateEmerg, rateControl)

		// Update State for next tick
		prevTotal, prevValid, prevDropped, prevControl, prevEmerg = currTotal, currValid, currDropped, currControl, currEmerg
		prevIdle, prevTotalCPU = currIdle, currTotalCPU
	}
}

// ================= Logging Init =================
func initLogs() {
	var err error
	// 1. Event Log (Detailed)
	eventLogFile, err = os.OpenFile("rsu_events.csv", os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0666)
	if err != nil { log.Fatal(err) }
	fileMutex.Lock()
	if stat, _ := eventLogFile.Stat(); stat.Size() == 0 {
		eventLogFile.WriteString("Time,Event_Type,Vehicle_ID,Speed,Action_Taken\n")
	}
	fileMutex.Unlock()

	// 2. Performance Log (Time Series for Origin)
	perfLogFile, err = os.OpenFile("rsu_perf.csv", os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0666)
	if err != nil { log.Fatal(err) }
	// Fixed Header: Added Emergency_Rate and Control_Rate
	perfLogFile.WriteString("Time,CPU_Usage,RX_Rate,Processed_Rate,Dropped_Rate,Queue_Len,Emerg_Rate,Control_Rate\n")
}

func recordEvent(evtType, vehID string, speed float64, action string) {
	go func() {
		fileMutex.Lock()
		defer fileMutex.Unlock()
		timestamp := time.Now().Format("15:04:05.000")
		line := fmt.Sprintf("%s,%s,%s,%.2f,%s\n", timestamp, evtType, vehID, speed, action)
		eventLogFile.WriteString(line)
	}()
}

// ================= Worker Logic =================
func worker(id int, tasks <-chan []byte) {
	for raw := range tasks {
		var msg V2XMessage
		if err := json.Unmarshal(raw, &msg); err != nil {
			continue
		}

		// 1. 噪点过滤 (Noise Filtering)
		if msg.Type == "NOISE" {
			atomic.AddUint64(&opsNoise, 1)
			continue // 直接丢弃，不进行后续空间计算
		}

		// 2. 空间过滤
		dist := math.Sqrt(math.Pow(msg.X-POI_X, 2) + math.Pow(msg.Y-POI_Y, 2))
		if dist > ROI_RADIUS {
			atomic.AddUint64(&opsFiltered, 1)
			continue
		}

		atomic.AddUint64(&opsValid, 1)

		// 3. 紧急车辆处理 (救护车)
		if msg.Type == "EMERGENCY" {
			atomic.AddUint64(&opsEmergency, 1)
			recordEvent("AMBULANCE_DETECTED", msg.ID, msg.Spd, "Preempt_Traffic_Light")
			// 立即触发绿波，不等待轮询
			atomic.StoreInt32(&congestionFlag, 2) // 2 代表最高优先级
		} else if msg.Type == "ACCIDENT" {
			atomic.AddUint64(&opsEmergency, 1)
			recordEvent("ACCIDENT_DETECTED", msg.ID, msg.Spd, "Alert_Center")
			atomic.StoreInt32(&congestionFlag, 1) // 1 代表普通拥堵
		}

		// 4. 拥堵检测
		if msg.Spd < 5.0 && msg.Type == "BSM" {
			atomic.StoreInt32(&congestionFlag, 1)
		}
	}
}

// 反馈循环修改 (增加限速指令)
func feedbackLoop() {
	pcAddr, _ := net.ResolveUDPAddr("udp", PC_CONTROL_URL)
	conn, _ := net.DialUDP("udp", nil, pcAddr)

	for {
		time.Sleep(1 * time.Second) // 检查频率加快

		flag := atomic.SwapInt32(&congestionFlag, 0) // 读出并重置

		if flag == 2 {
			// 最高优先级：救护车通行 -> 绿波
			cmd := ControlCommand{Action: "OPTIMIZE_TRAFFIC", Reason: "Ambulance_Preemption"}
			bytes, _ := json.Marshal(cmd)
			conn.Write(bytes)
			atomic.AddUint64(&opsControlSent, 1)
			fmt.Println("🚑 [EVP] Ambulance Detected! Sending Green Wave!")

		} else if flag == 1 {
			// 普通拥堵/事故 -> 降低限速 (VSL) + 绿波
			// 先发限速指令
			cmdSpeed := ControlCommand{Action: "SET_SPEED_LIMIT", Reason: "Accident_Safety"}
			bytes1, _ := json.Marshal(cmdSpeed)
			conn.Write(bytes1)
			
			time.Sleep(100 * time.Millisecond) // 稍微间隔

			cmdLight := ControlCommand{Action: "OPTIMIZE_TRAFFIC", Reason: "Congestion_Flush"}
			bytes2, _ := json.Marshal(cmdLight)
			conn.Write(bytes2)

			atomic.AddUint64(&opsControlSent, 2)
			fmt.Println("📤 [Control] Accident! Lowering Speed Limit & Flushing Traffic")
		}
	}
}
// ================= Feedback Loop =================

// ================= HTTP Dashboard =================
func startMonitor() { http.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) { 
		fmt.Fprintf(w, "<h1>RSU Status</h1><p>RX: %d</p><p>Dropped: %d</p>",
			atomic.LoadUint64(&opsTotal), atomic.LoadUint64(&opsDropped))
	})
	go http.ListenAndServe(":8080", nil)
}

// 监控面板修改 (增加 Noise 显示)
func consoleReporter() {
	for {
		time.Sleep(1 * time.Second)
		fmt.Println("\n---------------- RSU Monitor (Ultimate) ----------------")
		fmt.Printf("📥 Total RX       : %d\n", atomic.LoadUint64(&opsTotal))
		fmt.Printf("🗑️  Noise Filtered : %d (Radar Clutter)\n", atomic.LoadUint64(&opsNoise))
		fmt.Printf("✅ Processed      : %d\n", atomic.LoadUint64(&opsValid))
		fmt.Printf("🚑 Emergency      : %d\n", atomic.LoadUint64(&opsEmergency))
		fmt.Printf("🚦 Control Actions: %d\n", atomic.LoadUint64(&opsControlSent))
		fmt.Println("--------------------------------------------------------")
	}
}
