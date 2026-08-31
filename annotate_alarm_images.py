#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
下载 AIBOX 告警原图 → 按 rect 画红框 + 置信度标注 → 供 HTML 报告内嵌。
选两条高置信度命中 + 已有的 10:32:25 命中 + 漏检视频帧 + 对比图。
"""
import base64
import io
import json
import os
import ssl
import urllib.request
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

BASE = Path(__file__).resolve().parent
OUT_DIR = BASE / "annotated_images"
OUT_DIR.mkdir(exist_ok=True)
AIBOX = os.environ.get("AIBOX_URL", "")
if not AIBOX:
    print("请设置 AIBOX_URL 环境变量（AIBOX 盒子地址）"); raise SystemExit(1)
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

# 选用的命中告警（高置信度，rect 清晰）
HIT_ALARMS = [
    {"time": "11:05:27", "conf": 0.885, "rect": [0.4109375, 0.1722222222222222, 0.63125, 0.7861111111111111],
     "img": "/data/res/20260805/11/70/3571cbb5-f66f-4a1e-b348-058fca47d7e6_1785899127217544437.jpg",
     "video": "zOqs7Oh9oDM_0.avi", "round": 47},
    {"time": "10:32:25", "conf": 0.313, "rect": [0.3453125, 0.1972222222222222, 0.6921875, 0.8111111111111111],
     "img": "/data/res/20260805/10/70/3571cbb5-f66f-4a1e-b348-058fca47d7e6_1785897145159019046.jpg",
     "video": "0Ow4cotKOuw_0.avi", "round": 1},
]


def call(path, body=None, token=None):
    url = AIBOX + path
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=h, method=("POST" if body else "GET"))
    with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
        return json.load(r)


def download_img(path, token):
    req = urllib.request.Request(AIBOX + path, headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
        return r.read()


def get_font(size):
    for fp in [r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\msyh.ttc",
               r"C:\Windows\Fonts\simhei.ttf", r"C:\Windows\Fonts\arial.ttf"]:
        try:
            return ImageFont.truetype(fp, size)
        except Exception:
            continue
    return ImageFont.load_default()


def annotate(alarm_bytes, rect, conf, label, out_path, max_w=800):
    """在告警图上画红框 + 置信度标签，保存并返回 base64。"""
    im = Image.open(io.BytesIO(alarm_bytes)).convert("RGB")
    W, Hh = im.size
    # 缩放
    if W > max_w:
        nh = round(Hh * max_w / W)
        im = im.resize((max_w, nh), Image.LANCZOS)
        W, Hh = im.size
    x1, y1, x2, y2 = rect
    px1, py1, px2, py2 = round(x1 * W), round(y1 * Hh), round(x2 * W), round(y2 * Hh)
    draw = ImageDraw.Draw(im)
    # 红框（粗）
    lw = max(3, round(W / 200))
    draw.rectangle([px1, py1, px2, py2], outline=(220, 38, 38), width=lw)
    # 标签条
    text = f"{label} {conf:.2f}"
    font = get_font(max(16, round(W / 40)))
    tb = draw.textbbox((0, 0), text, font=font)
    tw, th = tb[2] - tb[0], tb[3] - tb[1]
    pad = 6
    # 标签放在框上方；若上方空间不足放下方
    ly = py1 - th - pad * 2
    if ly < 0:
        ly = py2
    draw.rectangle([px1, ly, px1 + tw + pad * 2, ly + th + pad * 2], fill=(220, 38, 38))
    draw.text((px1 + pad, ly + pad), text, fill=(255, 255, 255), font=font)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85, optimize=True)
    out_path.write_bytes(buf.getvalue())
    return base64.b64encode(buf.getvalue()).decode()


def main():
    print("登录 AIBOX...")
    token = call("/gbg/main/login", {"username": os.environ.get("AIBOX_USER", ""), "password": os.environ.get("AIBOX_PASS", "")})["data"]
    annotated = {}
    for a in HIT_ALARMS:
        print(f"下载 + 标注: {a['time']} (conf={a['conf']:.3f}, {a['video']})")
        raw = download_img(a["img"], token)
        out = OUT_DIR / f"hit_{a['time'].replace(':','')}.jpg"
        b64 = annotate(raw, a["rect"], a["conf"], "打架", out)
        annotated[a["time"]] = {"b64": b64, "path": out, **a}
        print(f"  -> {out.name} ({out.stat().st_size//1024} KB)")

    # 把标注图的 base64 写入 json 供报告脚本读
    meta = {k: {**v, "path": str(v["path"])} for k, v in annotated.items()}
    (BASE / "_annotated_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n完成。标注图 {len(annotated)} 张，元数据写入 _annotated_meta.json")
    for k, v in annotated.items():
        print(f"  {k}: {v['path'].name} ({v['path'].stat().st_size//1024}KB) rect={v['rect']}")


if __name__ == "__main__":
    main()
