#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
easytrader 服务管理器

命令行:
  py easytrader_manager.py              交互菜单
  py easytrader_manager.py start        后台启动
  py easytrader_manager.py stop         停止
  py easytrader_manager.py restart      重启
  py easytrader_manager.py status       状态
  py easytrader_manager.py daemon        守护进程 (崩溃自动重启)
  py easytrader_manager.py logs         查看日志
"""

import os
import sys
import time
import json
import socket
import subprocess

PORT = 1430
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SERVER_SCRIPT = os.path.join(SCRIPT_DIR, "easytrader_server_auth.py")
PYTHON = sys.executable
PID_FILE = os.path.join(SCRIPT_DIR, ".easytrader.pid")
LOG_FILE = os.path.join(SCRIPT_DIR, "easytrader_server.log")

# Windows 进程创建标志
DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
CREATE_NEW_PROCESS_GROUP = getattr(
    subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200
)
IS_WIN = sys.platform == "win32"


# ==================== 工具函数 ====================

def port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def pid_by_port(port):
    try:
        r = subprocess.run(
            ["netstat", "-ano"], capture_output=True, timeout=5
        )
        stdout = r.stdout.decode("gbk", errors="replace")
        for line in stdout.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                parts = line.split()
                if parts:
                    return int(parts[-1])
    except Exception:
        pass
    return None


def pid_from_file():
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, "r") as f:
                return int(f.read().strip())
        except Exception:
            pass
    return None


def health_check():
    try:
        import urllib.request
        resp = urllib.request.urlopen(
            f"http://127.0.0.1:{PORT}/health", timeout=2
        )
        return json.loads(resp.read())
    except Exception:
        return None


def kill_pid(pid):
    if IS_WIN:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True, timeout=5,
        )
    else:
        import signal
        os.kill(pid, signal.SIGTERM)


# ==================== 命令 ====================

def cmd_start():
    if port_in_use(PORT):
        pid = pid_by_port(PORT)
        h = health_check()
        if h and h.get("status") == "ok":
            print(f"[*] 服务已在运行 (PID: {pid})")
            print(f"    面板: http://localhost:{PORT}/")
            return
        print(f"[x] 端口 {PORT} 被其他程序占用 (PID: {pid})")
        return

    print(f"[*] 启动 easytrader 服务 (端口 {PORT})...")

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    log = open(LOG_FILE, "w", encoding="utf-8")
    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP if IS_WIN else 0
    proc = subprocess.Popen(
        [PYTHON, SERVER_SCRIPT],
        stdout=log,
        stderr=subprocess.STDOUT,
        creationflags=flags,
        env=env,
    )
    log.close()

    with open(PID_FILE, "w") as f:
        f.write(str(proc.pid))

    for _ in range(10):
        time.sleep(0.5)
        if port_in_use(PORT):
            print(f"[+] 启动成功")
            print(f"    PID:  {proc.pid}")
            print(f"    面板: http://localhost:{PORT}/")
            print(f"    日志: {LOG_FILE}")
            h = health_check()
            if h:
                print(f"    健康: {h.get('status')}  登录: {h.get('logged_in')}")
            return

    print("[!] 进程已启动但端口未就绪, 查看日志:")
    print(f"    {LOG_FILE}")


def cmd_stop():
    pid = pid_by_port(PORT)
    if not pid:
        pid = pid_from_file()
    if not pid:
        if not port_in_use(PORT):
            print(f"[-] 端口 {PORT} 空闲, 服务未运行")
        else:
            print(f"[!] 端口 {PORT} 被占用但无法定位 PID")
            print("    手动查找: netstat -ano | findstr :1430")
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
        return

    print(f"[*] 停止服务 (PID: {pid})...")
    kill_pid(pid)
    time.sleep(1)

    if not port_in_use(PORT):
        print("[+] 已停止")
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
    else:
        print("[!] 端口仍被占用, 可能需要手动结束")


def cmd_restart():
    print("[*] 重启中...")
    cmd_stop()
    time.sleep(1)
    cmd_start()


def cmd_status():
    if not port_in_use(PORT):
        print(f"[-] 服务未运行 (端口 {PORT} 空闲)")
        return
    pid = pid_by_port(PORT) or pid_from_file()
    print(f"[+] 服务运行中")
    print(f"    端口: {PORT}")
    print(f"    PID:  {pid}")
    h = health_check()
    if h:
        print(f"    状态: {h.get('status')}")
        print(f"    登录: {h.get('logged_in')}")
    else:
        print("    健康: 无法连接")


def cmd_daemon():
    print("=" * 50)
    print("  easytrader 守护进程模式")
    print("  崩溃自动重启  Ctrl+C 退出")
    print("=" * 50)

    if port_in_use(PORT):
        print("[*] 停止已有实例...")
        cmd_stop()
        time.sleep(1)

    count = 0
    while True:
        count += 1
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n[*] 启动 #{count} ({ts})")

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"

        log = open(LOG_FILE, "a", encoding="utf-8")
        log.write(f"\n{'='*50}\n重启 #{count} {ts}\n{'='*50}\n")
        log.flush()
        flags = CREATE_NEW_PROCESS_GROUP if IS_WIN else 0
        proc = subprocess.Popen(
            [PYTHON, SERVER_SCRIPT],
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=flags,
            env=env,
        )
        log.close()

        with open(PID_FILE, "w") as f:
            f.write(str(proc.pid))

        try:
            while proc.poll() is None:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n[*] 收到退出信号, 停止服务...")
            proc.kill()
            proc.wait()
            if os.path.exists(PID_FILE):
                os.remove(PID_FILE)
            print("[+] 守护进程已退出")
            return

        print(f"[!] 服务退出 (code={proc.returncode})")
        print(f"[*] 3 秒后重启 (Ctrl+C 退出)...")
        try:
            time.sleep(3)
        except KeyboardInterrupt:
            print("\n[*] 守护进程已退出")
            if os.path.exists(PID_FILE):
                os.remove(PID_FILE)
            return


def cmd_logs():
    if not os.path.exists(LOG_FILE):
        print("[-] 无日志文件")
        return
    print(f"[*] 日志最后 30 行 ({LOG_FILE}):\n")
    with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    for line in lines[-30:]:
        print(line, end="")
    print()


def menu():
    while True:
        running = port_in_use(PORT)
        state = "[运行中]" if running else "[已停止]"
        print("\n" + "=" * 42)
        print(f"  easytrader 服务管理器  {state}")
        print("=" * 42)
        print("  1. 启动        2. 停止")
        print("  3. 重启        4. 状态")
        print("  5. 守护进程    6. 查看日志")
        print("  0. 退出")
        print("=" * 42)
        try:
            choice = input("选择> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        actions = {
            "1": cmd_start, "2": cmd_stop, "3": cmd_restart,
            "4": cmd_status, "5": cmd_daemon, "6": cmd_logs,
        }
        if choice in actions:
            actions[choice]()
        elif choice == "0":
            break
        else:
            print("无效选择")


USAGE = f"""easytrader 服务管理器

用法:
  py {os.path.basename(__file__)} <命令>

命令:
  start    后台启动
  stop     停止
  restart  重启
  status   状态
  daemon   守护进程 (崩溃自动重启)
  logs     查看日志
  menu     交互菜单 (默认)
"""

if __name__ == "__main__":
    cmds = {
        "start": cmd_start, "stop": cmd_stop, "restart": cmd_restart,
        "status": cmd_status, "daemon": cmd_daemon, "logs": cmd_logs,
        "menu": menu,
    }
    if len(sys.argv) > 1 and sys.argv[1].lower() in cmds:
        cmds[sys.argv[1].lower()]()
    elif len(sys.argv) > 1:
        print(USAGE)
    else:
        menu()
