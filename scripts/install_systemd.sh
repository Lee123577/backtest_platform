#!/bin/bash
# ============================================================
# Install backtest as a systemd service
# ------------------------------------------------------------
# After this you can do:
#   systemctl start backtest
#   systemctl restart backtest
#   systemctl status backtest
#   journalctl -u backtest -f
# ============================================================
set -e

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_SRC="$ROOT_DIR/scripts/backtest.service"
UNIT_DST="/etc/systemd/system/backtest.service"

if [ "$EUID" -ne 0 ]; then
    echo "[ERR] 请用 root / sudo 运行：sudo bash $0"
    exit 1
fi

if [ ! -f "$UNIT_SRC" ]; then
    echo "[ERR] 找不到 $UNIT_SRC"
    exit 1
fi

# ── 1. 如果旧的 nohup 后台进程 / start.sh 启动的还在跑，先停掉 ──────
if [ -f "$ROOT_DIR/app.pid" ]; then
    echo "[INFO] 检测到 start.sh 留下的 app.pid，先 stop"
    bash "$ROOT_DIR/scripts/stop.sh" || true
fi
PIDS=$(pgrep -f "run\.py\|uvicorn app.main" 2>/dev/null || true)
if [ -n "$PIDS" ]; then
    echo "[INFO] 杀掉残留的 uvicorn 进程: $PIDS"
    kill $PIDS 2>/dev/null || true
    sleep 2
fi

# ── 2. 生成最终 unit 文件（替换 __ROOT__ 占位符）─────────────────────
echo "[INFO] 写入 $UNIT_DST"
sed "s|__ROOT__|$ROOT_DIR|g" "$UNIT_SRC" > "$UNIT_DST"
chmod 644 "$UNIT_DST"

# ── 3. systemd 注册 + 开机自启 ──────────────────────────────────────
systemctl daemon-reload
systemctl enable backtest.service
systemctl restart backtest.service

# ── 4. 等几秒，输出状态确认 ────────────────────────────────────────
sleep 2
echo ""
echo "──────────── status ────────────"
systemctl status backtest.service --no-pager -n 15 || true
echo ""
echo "[OK] 安装完成。常用命令："
echo "  sudo systemctl restart backtest    # 重启"
echo "  sudo systemctl stop backtest       # 停止"
echo "  sudo systemctl status backtest     # 看状态"
echo "  journalctl -u backtest -f          # 实时日志（Ctrl+C 退出）"
echo "  journalctl -u backtest --since '10 min ago'  # 最近 10 分钟"
