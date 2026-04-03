#!/usr/bin/env python3
"""启动Flask应用"""
from app import create_app

app = create_app()

if __name__ == '__main__':
    print("""
╔══════════════════════════════════════════════════════════════╗
║                    视频水印Benchmark平台                         ║
╠══════════════════════════════════════════════════════════════╣
║  访问地址: http://localhost:8080                               ║
║  首页:     http://localhost:8080/                              ║
║  视频管理: http://localhost:8080/videos/                       ║
║  告警图片: http://localhost:8080/alerts/                       ║
╚══════════════════════════════════════════════════════════════╝
    """)
    app.run(debug=True, host='0.0.0.0', port=8080)
