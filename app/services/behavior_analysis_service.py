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
    """加载 API 配置。

    优先使用统一 API 配置（api_config_service，密钥来自 .env）；
    若统一来源未提供某项，回退到 auto_anno_config.json（向后兼容）。
    """
    from app.services import api_config_service

    # 先读 JSON 文件作为兜底默认值
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    file_cfg = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            file_cfg = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        file_cfg = {}

    creds = api_config_service.get_vision_creds()
    interval = api_config_service.get_vision_request_interval()

    return {
        "api_key": creds.get("api_key") or file_cfg.get("api_key", ""),
        "base_url": creds.get("base_url") or file_cfg.get("base_url", "https://openapi-ai.cmaiot.cn/v1"),
        "model": creds.get("model") or file_cfg.get("model", "Qwen3-VL-8B-Instruct"),
        "request_interval_sec": interval if interval is not None else file_cfg.get("request_interval_sec", 1),
    }


def save_config(config: dict, config_path: str | None = None) -> None:
    """保存 API 配置。

    已迁移到统一配置（/api-config/ 页面，密钥写 .env）。
    此函数保留以兼容旧调用：
    - api_key 委托给 api_config_service 写入 .env（不再明文存 JSON）
    - base_url/model/request_interval_sec 落 auto_anno_config.json 作非敏感兜底
    """
    from app.services import api_config_service

    api_key = config.get("api_key")
    if api_key:
        # 委托统一服务把 key 写入 .env，并同步 base_url/model（多模态审查组）
        api_config_service.save_config({
            'vision_api_key': api_key,
            'vision_base_url': config.get('base_url', ''),
            'vision_model': config.get('model', ''),
            'vision_request_interval_sec': config.get('request_interval_sec'),
        })

    # 非敏感项落 JSON 兜底（不含 key）
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    safe = {
        k: v for k, v in config.items()
        if k != "api_key" and v is not None
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(safe, f, ensure_ascii=False, indent=2)


def get_api_client(config: dict | None = None):
    """获取 OpenAI 兼容的 API client"""
    from openai import OpenAI

    cfg = config or load_config()
    api_key = cfg.get("api_key") or os.environ.get("OPENAI_API_KEY", "")
    base_url = cfg.get("base_url", "https://openapi-ai.cmaiot.cn/v1")

    if not api_key:
        raise ValueError("未配置 API Key，请在 /api-config/ 页面统一配置 OpenAI 兼容组")

    return OpenAI(api_key=api_key, base_url=base_url)


def _encode_image(image_path: str) -> tuple[str, str]:
    """将图片转为 base64 编码，返回 (mime_type, base64_string)"""
    with open(image_path, "rb") as f:
        data = base64.b64encode(f.read()).decode("utf-8")
    ext = os.path.splitext(image_path)[1].lstrip(".").lower()
    mime = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}" if ext else "image/png"
    return mime, data


def build_prompt(valid_event_types: list[str], event_descriptions: dict = None) -> str:
    """根据允许的事件类型构建多模态分析 prompt（要求返回 JSON 含 label+confidence）。

    每个类型同时列出 key（模型要返回的 label）+ 中文名 + 描述。描述优先级：
    用户动态注入的 event_descriptions > DB description > 仅中文名。
    prompt 始终通用——按 valid_event_types 动态列出，不硬编码任一事件类型。
    """
    from app.event_types import get_type_descriptions, get_type_names

    type_descriptions = get_type_descriptions()
    type_names = get_type_names()
    event_descriptions = event_descriptions or {}

    lines = [
        "Analyze the image and identify if any of the following events are present. "
        'Return ONLY a JSON object of the form: '
        '{"labels":[{"label":"<event_type>","confidence":<0.0-1.0>}, ...]}. '
        "No explanations, no markdown fences.",
        "",
    ]
    for i, etype in enumerate(valid_event_types, 1):
        name = type_names.get(etype, etype)
        desc = event_descriptions.get(etype) or type_descriptions.get(etype) or ""
        if desc:
            lines.append(f"{i}. {etype}（{name}）: {desc}")
        else:
            lines.append(f"{i}. {etype}（{name}）")

    lines.extend([
        "",
        f"Valid labels: {', '.join(valid_event_types)}, normal.",
        "Include 'normal' with a confidence only if NONE of the above apply; "
        "never combine 'normal' with other labels.",
        "confidence is your certainty (0.0-1.0) that the label applies to the image.",
    ])
    return "\n".join(lines)


def _parse_label_confidence(result: str, valid_labels: set) -> list[dict]:
    """解析模型输出为 [{"label": str, "confidence": float}]。

    优先按 JSON {"labels":[{"label","confidence"}]} 解析；失败回退逗号分隔标签
    （confidence=1.0，向后兼容旧调用方）。confidence 夹到 [0,1]。
    """
    text = (result or "").strip()
    parsed = None
    try:
        parsed = json.loads(text)
    except Exception:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                parsed = json.loads(text[start:end + 1])
            except Exception:
                parsed = None

    items = []
    if isinstance(parsed, dict) and isinstance(parsed.get("labels"), list):
        for entry in parsed["labels"]:
            if isinstance(entry, dict):
                lab = str(entry.get("label", "")).strip().lower()
                try:
                    conf = float(entry.get("confidence", 1.0))
                except (TypeError, ValueError):
                    conf = 1.0
            elif isinstance(entry, str):
                lab = entry.strip().lower()
                conf = 1.0
            else:
                continue
            if lab in valid_labels:
                items.append({"label": lab, "confidence": max(0.0, min(1.0, conf))})
        if items:
            return items

    # 回退：逗号分隔标签（confidence=1.0，向后兼容旧调用方/旧模型输出）
    for part in (result or "").replace("，", ",").replace("、", ",").split(","):
        lab = part.strip().lower()
        if lab in valid_labels:
            items.append({"label": lab, "confidence": 1.0})
    return items


def analyze_frame(
    client,
    model_name: str,
    image_path: str,
    valid_event_types: list[str],
    event_descriptions: dict = None,
) -> list[dict]:
    """对单帧图片进行多模态行为分析

    Args:
        client: OpenAI 兼容 client
        model_name: 模型名称
        image_path: 图片路径
        valid_event_types: 允许的事件类型列表
        event_descriptions: 可选，{事件类型: 描述}，动态注入 prompt 指导标注（优先于 DB 描述）

    Returns:
        检测到的事件标签列表，每项 {"label": str, "confidence": float}（已过滤、去重），
        保底返回 [{"label": "normal", "confidence": 1.0}]。模型返回非法 JSON 时容错
        回退：按逗号分隔解析标签，confidence 置 1.0（向后兼容旧调用方）。
    """
    mime, b64 = _encode_image(image_path)
    image_url = f"data:{mime};base64,{b64}"

    prompt_text = build_prompt(valid_event_types, event_descriptions)
    valid_labels = set(valid_event_types + ["normal"])

    messages = [
        {"role": "system", "content": "You are an image analysis expert. Identify events precisely. Respond with JSON only."},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt_text},
                {"type": "image_url", "image_url": {"url": image_url}},
            ],
        },
    ]

    completion = client.chat.completions.create(model=model_name, messages=messages)
    result = completion.choices[0].message.content or ""

    items = _parse_label_confidence(result, valid_labels)

    # 去重并保持顺序
    seen = set()
    deduped = []
    for it in items:
        lab = it["label"]
        if lab in seen:
            continue
        seen.add(lab)
        deduped.append(it)

    # 去掉 normal 如果还有其他标签
    if len(deduped) > 1:
        deduped = [it for it in deduped if it["label"] != "normal"]

    return deduped if deduped else [{"label": "normal", "confidence": 1.0}]
