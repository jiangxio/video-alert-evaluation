#!/usr/bin/env bash
# 启动两个评测平台并打开浏览器
#   主平台 视觉算法评测平台  :8080  (run.py, waitress)
#   子平台 图像标注与评测平台 :5000  (od-dataset-manager/app.py)
set -u

# 切到脚本所在目录（项目根）
cd "$(dirname "$(readlink -f "$0")")" || exit 1

# ── 进入 conda 环境（base）────────────────────────────────────────────
CONDA_BASE="/data/jx_data/miniconda3"
if [ -f "$CONDA_BASE/etc/profile.d/conda.sh" ]; then
  # shellcheck disable=SC1091
  source "$CONDA_BASE/etc/profile.d/conda.sh"
  conda activate base || echo "⚠ conda activate 失败，回退到 PATH 中的 python"
else
  echo "⚠ 未找到 conda ($CONDA_BASE)，回退到 PATH 中的 python"
fi

WEB_PORT=8080
OD_PORT=5000
LOG_DIR="/tmp"

# 判断端口是否已被监听
port_in_use() { ss -tln 2>/dev/null | grep -q ":$1\b"; }

# start_if_down <port> <显示名> <启动命令> <工作目录> <tag>
start_if_down() {
  local port=$1 name=$2 cmd=$3 cwd=$4 tag=$5
  if port_in_use "$port"; then
    echo "✔ $name 已在运行 (http://localhost:$port)"
    return 0
  fi
  echo "▶ 启动 $name ..."
  ( cd "$cwd" && nohup $cmd >"$LOG_DIR/${tag}.log" 2>&1 & echo $! >"$LOG_DIR/${tag}.pid" )
  for _ in $(seq 1 30); do
    if port_in_use "$port"; then
      echo "✔ $name 已就绪 (http://localhost:$port)"
      return 0
    fi
    sleep 1
  done
  echo "✗ $name 启动超时，查看日志: $LOG_DIR/${tag}.log"
  return 1
}

start_if_down "$WEB_PORT" "视觉算法评测平台(主)" "python run.py"    "."                 "video_alert_8080"
start_if_down "$OD_PORT"  "图像标注与评测平台(子)" "python app.py" "od-dataset-manager" "od_5000"

# ── 打开浏览器 ──────────────────────────────────────────────────────
echo
echo "🌐 打开浏览器 ..."
if command -v xdg-open >/dev/null 2>&1; then
  xdg-open "http://localhost:$WEB_PORT" >/dev/null 2>&1 &
  sleep 1
  xdg-open "http://localhost:$OD_PORT"  >/dev/null 2>&1 &
else
  echo "⚠ 未找到 xdg-open，请手动访问下方地址"
fi

echo
echo "═══════════════════════════════════════════════════════════"
echo "  主平台  视觉算法评测平台   http://localhost:$WEB_PORT"
echo "  子平台  图像标注与评测平台  http://localhost:$OD_PORT"
echo "═══════════════════════════════════════════════════════════"
echo "  日志:  $LOG_DIR/video_alert_8080.log   $LOG_DIR/od_5000.log"
echo "  停止:  kill \$(cat $LOG_DIR/video_alert_8080.pid $LOG_DIR/od_5000.pid)"
