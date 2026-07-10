"""多模态图像行为分析服务

参考 42-image-tool 的多模态分析逻辑，独立维护在主项目中。
直接对整帧图片进行分析（不做 YOLO 裁剪）。
"""

import os
import base64
import json
from pathlib import Path


DEFAULT_CONFIG_PATH = Path(__file__).parent.parent / "auto_anno_config.json"


def load_config(config_path: str | None = None) -> dict:
    """加载 API 配置，找不到返回空字典"""
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_config(config: dict, config_path: str | None = None) -> None:
    """保存 API 配置"""
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def get_api_client(config: dict | None = None):
    """获取 OpenAI 兼容的 API client"""
    from openai import OpenAI

    cfg = config or load_config()
    api_key = cfg.get("api_key") or os.environ.get("OPENAI_API_KEY", "")
    base_url = cfg.get("base_url", "https://openapi-ai.cmaiot.cn/v1")

    if not api_key:
        raise ValueError("未配置 API Key，请填写 auto_anno_config.json 或设置 OPENAI_API_KEY 环境变量")

    return OpenAI(api_key=api_key, base_url=base_url)


def _encode_image(image_path: str) -> tuple[str, str]:
    """将图片转为 base64 编码，返回 (mime_type, base64_string)"""
    with open(image_path, "rb") as f:
        data = base64.b64encode(f.read()).decode("utf-8")
    ext = os.path.splitext(image_path)[1].lstrip(".").lower()
    mime = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}" if ext else "image/png"
    return mime, data


def build_prompt(valid_event_types: list[str]) -> str:
    """根据允许的事件类型构建多模态分析 prompt"""
    from app.event_types import get_type_descriptions

    type_descriptions = get_type_descriptions()

    lines = [
        "Analyze the image and identify if any of the following events are present. "
        "Return ONLY the applicable English labels, comma-separated. No explanations.",
        "",
    ]
    for i, etype in enumerate(valid_event_types, 1):
        desc = type_descriptions.get(etype, etype)
        lines.append(f"{i}. {etype}: {desc}")

    lines.extend([
        "",
        f"Valid labels: {', '.join(valid_event_types)}, normal.",
        "Return 'normal' only if NONE of the above apply.",
        "Never combine 'normal' with other labels.",
    ])
    return "\n".join(lines)


def analyze_frame(
    client,
    model_name: str,
    image_path: str,
    valid_event_types: list[str],
) -> list[str]:
    """对单帧图片进行多模态行为分析

    Args:
        client: OpenAI 兼容 client
        model_name: 模型名称
        image_path: 图片路径
        valid_event_types: 允许的事件类型列表

    Returns:
        检测到的事件标签列表（已过滤、去重），保底返回 ["normal"]
    """
    mime, b64 = _encode_image(image_path)
    image_url = f"data:{mime};base64,{b64}"

    prompt_text = build_prompt(valid_event_types)
    valid_labels = set(valid_event_types + ["normal"])

    messages = [
        {"role": "system", "content": "You are an image analysis expert. Identify events precisely."},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt_text},
                {"type": "image_url", "image_url": {"url": image_url}},
            ],
        },
    ]

    completion = client.chat.completions.create(model=model_name, messages=messages)
    result = completion.choices[0].message.content or "normal"

    # 解析标签
    labels = []
    for part in result.replace("，", ",").replace("、", ",").split(","):
        label = part.strip().lower()
        if label in valid_labels:
            labels.append(label)

    # 去重并保持顺序
    seen = set()
    labels = [l for l in labels if not (l in seen or seen.add(l))]

    # 去掉 normal 如果还有其他标签
    if len(labels) > 1 and "normal" in labels:
        labels.remove("normal")

    return labels if labels else ["normal"]
