# -*- coding: utf-8 -*-
"""Yamtrack Redis 看护进程

职责：启动 redis-server.exe，若它被意外终止则自动重启，并记录重启原因。

为什么需要它：
    Redis 是 Yamtrack 的硬依赖（缓存 + Celery 队列）。它作为一个后台常驻服务，
    如果被外部因素意外终止（误关窗口、系统清理工具、安全软件等），
    表现就是 Django 立刻抛 ConnectionError: Error 10061 connecting to 127.0.0.1:6379。
    源生 Redis 本身不会留下任何"被终止"的记录，很难排查。
    本脚本让 Redis 变成自愈服务：死了立刻拉起来，并把每次重启记录下来。

日志：
    .runtime/logs/redis.log         Redis 自身输出
    .runtime/logs/redis-daemon.log  看护进程记录（启动、重启）
"""

import os
import socket
import subprocess
import sys
import time
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REDIS_DIR = os.path.join(BASE_DIR, ".runtime", "redis")
LOG_DIR = os.path.join(BASE_DIR, ".runtime", "logs")
REDIS_LOG = os.path.join(LOG_DIR, "redis.log")
DAEMON_LOG = os.path.join(LOG_DIR, "redis-daemon.log")

EXE = os.path.join(REDIS_DIR, "redis-server.exe")
CONF = "yamtrack.conf"

HOST = "127.0.0.1"
PORT = 6379

# CREATE_NO_WINDOW：让 Redis 不依附任何控制台窗口。
# 这样即使本看护窗口被误关，Redis 也不会跟着被带走。
CREATE_NO_WINDOW = 0x08000000


def note(msg):
    """写入看护日志（UTF-8，追加）。"""
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with open(DAEMON_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line, flush=True)


def port_in_use():
    """6379 是否已经有人在监听（说明已经有 Redis 在跑）。"""
    s = socket.socket()
    s.settimeout(1.0)
    try:
        s.connect((HOST, PORT))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def main():
    if not os.path.isfile(EXE):
        note("找不到 %s ，无法启动 Redis。" % EXE)
        return 2

    os.makedirs(LOG_DIR, exist_ok=True)
    os.chdir(REDIS_DIR)

    note("Redis 看护进程启动（PID %d）。可执行文件：%s" % (os.getpid(), EXE))

    if port_in_use():
        note(
            "端口 %d 已被占用（已有 Redis 在运行）。本进程转为待命，"
            "等它退出后自动接管，以获得重启保护。" % PORT
        )
        while port_in_use():
            time.sleep(5)
        note("端口已释放，开始接管 Redis。")

    restarts = 0
    while True:
        started = time.time()
        with open(REDIS_LOG, "ab") as out:
            proc = subprocess.Popen(
                [EXE, CONF],
                cwd=REDIS_DIR,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW,
            )
        rc = proc.wait()
        alive = time.time() - started

        # 正常运行一段时间后就不算"连续失败"，退避计数归零
        if alive > 60:
            restarts = 0

        if rc == 0 and alive > 10:
            note("redis-server 正常退出（存活 %.1f 秒），看护进程一并结束。" % alive)
            return 0

        restarts += 1
        delay = min(2 ** min(restarts, 4), 15)  # 2 / 4 / 8 / 15 秒
        note(
            "redis-server 意外退出（退出码 %s，存活 %.1f 秒），%d 秒后自动重启（累计第 %d 次）。"
            % (rc, alive, delay, restarts)
        )
        time.sleep(delay)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception:
        import traceback

        note("看护进程自身异常退出：\n" + traceback.format_exc())
        sys.exit(2)
