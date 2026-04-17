#!/usr/bin/env python3
"""
告警图片验证脚本
用法:
  单张验证: python scripts/verify_alert.py report/402_1774925112_103.png
  批量验证: python scripts/verify_alert.py --batch
  使用模拟OCR测试: python scripts/verify_alert.py report/402_1774925112_103.png --mock-ocr '{"video_id": "046", "timestamp_seconds": 90}'
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# 确保同级目录可导入，以便直接调用 ocr_easy（复用 Reader）
if str(Path(__file__).parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).parent))

try:
    import ocr_easy
    HAS_OCR_EASY = True
except Exception:
    HAS_OCR_EASY = False

_OCR_READER = None


def _get_ocr_reader():
    global _OCR_READER
    if _OCR_READER is None and HAS_OCR_EASY:
        _OCR_READER = ocr_easy.get_reader()
    return _OCR_READER


def run_ocr_direct(image_path):
    """直接调用 ocr_easy（复用 Reader），返回结构与 ocr_easy.main 一致"""
    reader = _get_ocr_reader()
    ocr_text = ocr_easy.preprocess_and_ocr(str(image_path), reader=reader)
    parsed = ocr_easy.parse_watermark_text(ocr_text)
    return {
        "image": str(image_path),
        **parsed
    }


def extract_alert_type_id(image_path):
    """从文件名提取告警类型ID"""
    filename = Path(image_path).name
    # 匹配末尾的 _数字.png 格式
    match = re.search(r'_(\d+)\.png$', filename)
    if match:
        return match.group(1)
    return None


def parse_alert_config(config_path):
    """解析告警配置文件（纯文本格式，每行 "id name"）"""
    config = {}
    config_path = Path(config_path)
    if not config_path.exists():
        return config
    with open(config_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split(maxsplit=1)
            if len(parts) == 2:
                alert_id, alert_type = parts
                config[alert_id] = alert_type
    return config


def get_alert_type(alert_type_id, config):
    """获取告警类型"""
    return config.get(alert_type_id)


def load_ground_truth(video_id):
    """加载ground truth文件"""
    if not video_id:
        return None
    gt_path = Path(f"ground_truth/{video_id}.json")
    if not gt_path.exists():
        return None
    try:
        with open(gt_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def verify_event(ground_truth, alert_type, timestamp_seconds, tolerance=5):
    """
    验证事件是否匹配
    返回: (verdict, reason, matched_event)
    verdict: "correct" | "incorrect" | "unknown"
    """
    if timestamp_seconds is None:
        return "unknown", "No valid timestamp from OCR", None

    if ground_truth is None:
        return "unknown", "Ground truth not available", None

    events = ground_truth.get("events", [])
    if not events:
        return "incorrect", "No events in ground truth", None

    # 检查范围 [timestamp - tolerance, timestamp + tolerance]
    check_start = timestamp_seconds - tolerance
    check_end = timestamp_seconds + tolerance

    for event in events:
        if event.get("type") != alert_type:
            continue
        event_start = event.get("start", 0)
        event_end = event.get("end", float('inf'))

        # 检查两个区间是否有重叠
        # 事件区间 [event_start, event_end]
        # 检查区间 [check_start, check_end]
        if not (check_end < event_start or check_start > event_end):
            return "correct", f"Found matching event at {event_start}-{event_end}s", event

    return "incorrect", f"No matching {alert_type} event found within ±{tolerance}s", None


def run_ocr_subprocess(image_path):
    """通过子进程运行ocr_easy.py"""
    script_path = Path(__file__).parent / "ocr_easy.py"
    try:
        result = subprocess.run(
            [sys.executable, str(script_path), str(image_path)],
            capture_output=True,
            text=True,
            timeout=60
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
        else:
            return {"error": result.stderr or "OCR failed"}
    except Exception as e:
        return {"error": str(e)}


def verify_single_image(image_path, config, tolerance=5, mock_ocr=None):
    """验证单张图片"""
    image_path = str(image_path)
    result = {
        "image": image_path,
        "alert_type_id": None,
        "alert_type": None,
        "ocr_result": None,
        "ground_truth_file": None,
        "verdict": "unknown",
        "reason": None,
        "matched_event": None
    }

    # 1. 提取告警类型ID
    alert_type_id = extract_alert_type_id(image_path)
    result["alert_type_id"] = alert_type_id
    if not alert_type_id:
        result["reason"] = "Could not extract alert type ID from filename"
        return result

    # 2. 获取告警类型
    alert_type = get_alert_type(alert_type_id, config)
    result["alert_type"] = alert_type
    if not alert_type:
        result["reason"] = f"Unknown alert type ID: {alert_type_id}"
        return result

    # 3. OCR识别（或使用模拟数据）
    if mock_ocr:
        ocr_result = {
            "image": image_path,
            "raw_ocr_text": "",
            "video_id": mock_ocr.get("video_id"),
            "timestamp": mock_ocr.get("timestamp"),
            "timestamp_seconds": mock_ocr.get("timestamp_seconds"),
            "success": mock_ocr.get("video_id") is not None or mock_ocr.get("timestamp_seconds") is not None
        }
        result["ocr_result"] = ocr_result
    else:
        if HAS_OCR_EASY:
            ocr_result = run_ocr_direct(image_path)
        else:
            ocr_result = run_ocr_subprocess(image_path)
        if "error" in ocr_result:
            result["reason"] = f"OCR failed: {ocr_result['error']}"
            return result
        result["ocr_result"] = ocr_result

    # 4. 加载ground truth
    video_id = ocr_result.get("video_id")
    ground_truth = load_ground_truth(video_id)
    if video_id:
        result["ground_truth_file"] = f"ground_truth/{video_id}.json"

    # 5. 验证事件
    timestamp_seconds = ocr_result.get("timestamp_seconds")
    verdict, reason, matched_event = verify_event(
        ground_truth, alert_type, timestamp_seconds, tolerance
    )
    result["verdict"] = verdict
    result["reason"] = reason
    result["matched_event"] = matched_event

    return result


def find_alert_images(report_dir="report"):
    """查找report目录下所有告警图片"""
    report_dir = Path(report_dir)
    if not report_dir.exists():
        return []
    images = []
    for img_path in report_dir.glob("*.png"):
        # 排除 watermark_ 开头的文件
        if not img_path.name.startswith("watermark_"):
            images.append(str(img_path))
    return sorted(images)


def batch_verify(report_dir="report", config=None, tolerance=5, mock_ocr=None):
    """批量验证"""
    if config is None:
        config = parse_alert_config("report/config.json")
    images = find_alert_images(report_dir)
    results = []
    for img_path in images:
        result = verify_single_image(img_path, config, tolerance, mock_ocr)
        results.append(result)
    return results


def print_human_readable(result):
    """打印人类可读的结果"""
    verdict_colors = {
        "correct": "\033[92m✓ CORRECT\033[0m",
        "incorrect": "\033[91m✗ INCORRECT\033[0m",
        "unknown": "\033[93m? UNKNOWN\033[0m"
    }
    color = verdict_colors.get(result["verdict"], result["verdict"])

    print(f"\n{color}")
    print(f"  Image: {result['image']}")
    print(f"  Alert: {result['alert_type_id']} → {result['alert_type']}")
    if result['ocr_result']:
        ocr = result['ocr_result']
        print(f"  OCR: video_id={ocr.get('video_id')}, timestamp={ocr.get('timestamp')} ({ocr.get('timestamp_seconds')}s)")
    print(f"  Reason: {result['reason']}")
    if result['matched_event']:
        evt = result['matched_event']
        print(f"  Matched: {evt['type']} [{evt['start']}-{evt['end']}s]")


def print_summary(results):
    """打印统计摘要"""
    total = len(results)
    correct = sum(1 for r in results if r["verdict"] == "correct")
    incorrect = sum(1 for r in results if r["verdict"] == "incorrect")
    unknown = sum(1 for r in results if r["verdict"] == "unknown")

    print("\n" + "=" * 50)
    print(f"Summary: Total={total}, Correct={correct}, Incorrect={incorrect}, Unknown={unknown}")
    print("=" * 50)


def main():
    parser = argparse.ArgumentParser(description='告警图片验证')
    parser.add_argument('image', nargs='?', help='单张图片路径')
    parser.add_argument('--batch', action='store_true', help='批量验证report目录下所有图片')
    parser.add_argument('--config', default='report/config.json', help='配置文件路径 (默认: report/config.json)')
    parser.add_argument('--output', help='输出JSON文件路径 (批量模式默认: report/verification_results.json)')
    parser.add_argument('--tolerance', type=int, default=5, help='时间容错秒数 (默认: 5)')
    parser.add_argument('--quiet', action='store_true', help='只输出JSON，不输出人类可读信息')
    parser.add_argument('--mock-ocr', help='模拟OCR结果JSON，用于测试 (例如: \'{"video_id": "046", "timestamp_seconds": 90}\')')

    args = parser.parse_args()

    config = parse_alert_config(args.config)

    mock_ocr_data = None
    if args.mock_ocr:
        try:
            mock_ocr_data = json.loads(args.mock_ocr)
        except json.JSONDecodeError:
            print(f"Error: --mock-ocr 参数不是有效的JSON: {args.mock_ocr}", file=sys.stderr)
            sys.exit(1)

    if args.batch:
        # 批量模式
        results = batch_verify("report", config, args.tolerance, mock_ocr_data)

        if not args.quiet:
            for result in results:
                print_human_readable(result)
            print_summary(results)

        # 保存到文件
        output_path = args.output or "report/verification_results.json"
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(results, f, ensure_ascii=False, indent=2)

        if not args.quiet:
            print(f"\nResults saved to: {output_path}")

        # 输出JSON到stdout
        if args.quiet:
            print(json.dumps(results, ensure_ascii=False, indent=2))

    elif args.image:
        # 单张图片模式
        result = verify_single_image(args.image, config, args.tolerance, mock_ocr_data)

        if not args.quiet:
            print_human_readable(result)

        # 输出JSON
        print(json.dumps(result, ensure_ascii=False, indent=2))

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
