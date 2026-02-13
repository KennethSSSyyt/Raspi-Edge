import os
import subprocess
import time
import pandas as pd

# 严格对齐路径
GO_DIR = "/home/pi/raspi-edge-ai/go-edge"
EXE_NAME = "edge-system"
TEST_THRESHOLDS = [60, 70, 80, 90]
DURATION = 50

def build():
    print("🛠️ 正在编译...")
    # 强制清理旧文件并重新编译
    if os.path.exists(os.path.join(GO_DIR, EXE_NAME)):
        os.remove(os.path.join(GO_DIR, EXE_NAME))
    res = subprocess.run(["go", "build", "-o", EXE_NAME, "main.go"], cwd=GO_DIR)
    if res.returncode != 0:
        print("❌ 编译失败"); exit(1)
    # 赋予执行权限
    os.chmod(os.path.join(GO_DIR, EXE_NAME), 0o755)

def run_test(t):
    os.system("pkill -9 edge-system") # 清理残留
    print(f"\n[Stage] 测试阈值: {t}%")
    env = os.environ.copy()
    env["CPU_THRESHOLD"] = str(t)
    
    exe_path = os.path.join(GO_DIR, EXE_NAME)
    # 检查文件是否存在
    if not os.path.exists(exe_path):
        print(f"❌ 错误: 找不到文件 {exe_path}"); return

    process = subprocess.Popen([exe_path], env=env, cwd=GO_DIR)
    time.sleep(DURATION)
    process.terminate()
    process.wait()

if __name__ == "__main__":
    build()
    for t in TEST_THRESHOLDS:
        run_test(t)
    print("✅ 所有实验完成。")
