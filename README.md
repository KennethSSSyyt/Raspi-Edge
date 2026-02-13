# Raspi-Edge: 树莓派边缘计算 RSU 系统

一个完整的基于树莓派 5 的边缘计算系统，用于路侧单元（RSU）应用，集成了 Go 微服务、Python AI 推理引擎、实时监控和交通仿真功能。

## 📋 目录

- [功能特性](#功能特性)
- [系统架构](#系统架构)
- [项目结构](#项目结构)
- [快速开始](#快速开始)
- [服务说明](#服务说明)
- [配置说明](#配置说明)
- [监控与可视化](#监控与可视化)
- [开发指南](#开发指南)
- [许可证](#许可证)

## ✨ 功能特性

- **边缘计算核心**：基于 Go 的高性能边缘服务，支持多路视频流处理
- **AI 推理引擎**：集成 YOLO 目标检测模型，支持实时推理
- **计算卸载**：智能 CPU 负载监控，自动将计算任务卸载到云端
- **实时监控**：Grafana + InfluxDB 监控系统，实时查看系统状态
- **Web 界面**：Flask Web 服务器，提供实时视频流和状态监控
- **交通仿真**：SUMO 交通仿真集成，支持性能基准测试
- **容器化部署**：Docker Compose 一键部署，支持多服务编排

## 🏗️ 系统架构

```
┌─────────────────────────────────────────────────────────┐
│                    Raspberry Pi 5                        │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  │
│  │  Go Edge     │  │  Python      │  │  Web Server  │  │
│  │  Service     │  │  Inference   │  │              │  │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘  │
│         │                 │                  │          │
│         └─────────────────┼──────────────────┘          │
│                           │                             │
│                    ┌──────▼──────┐                      │
│                    │   InfluxDB  │                      │
│                    └──────┬──────┘                      │
│                           │                             │
│                    ┌──────▼──────┐                      │
│                    │   Grafana   │                      │
│                    └─────────────┘                      │
└───────────────────────────┬─────────────────────────────┘
                            │
                    ┌───────▼────────┐
                    │  Cloud Server  │
                    │  (PC/Server)   │
                    └────────────────┘
```

## 📁 项目结构

```
raspi-edge/
├── go-edge/                 # Go 边缘服务
│   ├── main.go             # 主服务入口
│   ├── main_v1.go           # 版本 1 实现
│   ├── main_rus.go          # RUS 实现
│   ├── rsu_core_v1.go       # RSU 核心逻辑 v1
│   ├── rsu_core_v2.go       # RSU 核心逻辑 v2
│   ├── go_server_main.go    # Go 服务器主程序
│   ├── Dockerfile           # Go 服务容器化
│   └── videos/              # 测试视频文件
│
├── python-infer/            # Python AI 推理引擎
│   ├── pi_eye_final_v7.py   # 边缘端主程序
│   ├── cloud_server_v2.py   # 云端服务器 v2
│   ├── cloud_server.py      # 云端服务器
│   ├── inference_ultra_fast.py  # 超快速推理
│   ├── inference_pto.py     # PTO 推理
│   ├── optimized_main.py    # 优化主程序
│   ├── run_ablation_study.py # 消融实验
│   ├── pc_cloud_lpr_service.py # 云端车牌识别服务
│   ├── pi_edge_client.py    # 边缘客户端
│   └── *.pt, *.onnx         # AI 模型文件
│
├── ai_engine/               # AI 引擎服务
│   ├── main_ai.py          # AI 引擎主程序
│   ├── main_ai.go          # Go 版本 AI 引擎
│   └── Dockerfile          # AI 引擎容器化
│
├── web_server/             # Web 服务器
│   ├── main_web.py         # Flask Web 服务
│   └── Dockerfile          # Web 服务容器化
│
├── SUMO/                   # SUMO 交通仿真
│   ├── auto_benchmark_controller.py  # 自动基准测试控制器
│   ├── twoWay6lanes.py     # 双向六车道仿真
│   ├── utils.py            # 工具函数
│   ├── *.net.xml           # 路网文件
│   ├── *.rou.xml           # 路由文件
│   └── *.sumocfg           # SUMO 配置文件
│
├── grafana-data/           # Grafana 数据目录
│   ├── csv/                # CSV 数据
│   ├── png/                # 图片数据
│   ├── pdf/                # PDF 报告
│   └── plugins/            # Grafana 插件
│
├── influx_data/            # InfluxDB 数据目录
│   ├── data/               # 时序数据
│   └── meta/               # 元数据
│
├── docker-compose.yml      # Docker Compose 配置
└── README.md              # 项目说明文档
```

## 🚀 快速开始

### 前置要求

- **硬件**：Raspberry Pi
- **操作系统**：Raspberry Pi(ubuntu24)
- **软件依赖**：
  - Docker & Docker Compose
  - Go 1.21+ (如需本地编译)
  - Python 3.8+ (如需本地运行)
  - OpenCV (GoCV)

### 安装步骤

1. **配置环境变量**

编辑 `go-edge/main.go` 和 `python-infer-xxx.py`，设置正确的 IP 地址：

```go
// go-edge/main.go
var PC_IP =   // 修改为你的 PC/服务器 IP
```

```python
# python-infer/pi_eye_final_v7.py
CLOUD_IP =    # 修改为你的 PC/服务器 IP
```

3. **准备视频文件**

将测试视频文件放置到 `python-infer/videos/` 目录：
- `videoxxx.mp4`
- ……

1. **使用 Docker Compose 启动**

2. **访问服务**

- **Web 界面**：http://localhost:5000
- **Grafana 监控**：http://localhost:3000 (默认账号: admin/admin)
- **InfluxDB**：http://localhost:8086

### 本地开发模式

#### 运行 Go 边缘服务

#### 运行 Python 推理引擎

#### 运行 Web 服务器

## 🔧 服务说明

### 1. Go Edge Service (`go-edge/`)

**功能**：
- 多路视频流捕获和处理
- CPU 负载监控和自适应计算卸载
- ZMQ 通信协议，与云端服务器交互
- 实时性能指标记录

**关键特性**：
- 支持并发视频流
- 可配置工作线程数（WORKER_COUNT）
- CPU 高负载时自动降级处理
- CSV 格式的性能指标导出

**配置参数**：
```go
PC_IP                                 // 云端服务器 IP
PC_PORT                               // ZMQ 端口
CPU_HIGH_THRESHOLD                    // CPU 高负载阈值
WORKER_COUNT                          // 工作线程数
CAM_COUNT                             // 摄像头数量
```

### 2. Python Inference Engine (`python-infer/`)

**功能**：
- YOLO 目标检测模型推理
- 多进程/多线程推理优化
- 图像预处理和后处理
- 与 Go 服务的数据交互

**主要脚本**：
- `pi_eye_final_v7.py`：边缘端主程序，处理视频流并发送到云端
- `cloud_server_v2.py`：云端推理服务器，接收图像并返回检测结果
- `inference_ultra_fast.py`：超快速推理实现
- `run_ablation_study.py`：消融实验脚本

### 3. AI Engine (`ai_engine/`)

**功能**：
- 独立的 AI 推理服务
- 支持 YOLO 模型加载和推理
- Redis 数据存储和读取

### 4. Web Server (`web_server/`)

**功能**：
- Flask Web 服务器
- 实时视频流显示（4 路摄像头）
- 系统状态监控界面
- Redis 数据读取和展示

### 5. SUMO Traffic Simulation (`SUMO/`)

**功能**：
- 交通流仿真
- 性能基准测试
- 自动控制器生成
- 数据可视化

---

**注意**：本项目为研究用途，部署到生产环境前请进行充分测试。
