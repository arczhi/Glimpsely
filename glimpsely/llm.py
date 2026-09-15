"""omlx client: OpenAI-compatible chat completions with vision support."""

import base64
import json
from pathlib import Path

import httpx

from .config import Config

VISION_PROMPT = (
    "你是 Glimpsely 的记忆助手。用户在微信里随手转发的一条消息（可能含截图）。"
    "输出只允许一个 JSON 对象本身：不要解释、不要自查、不要英文、不要 markdown、"
    "不要任何其他文字。第一个字符必须是 { 。\n"
    '{\n'
    '  "kind": "courier|bill|coupon|event|address|person|chat_digest|note|other",\n'
    '  "title": "一句话摘要(<=30字)",\n'
    '  "entities": {"键": "值"},\n'
    '  "deadline": "ISO8601或null(有时效才填:优惠券到期/日程/账单还款日;相对时间以当前时间折算)",\n'
    '  "importance": 1到5的整数,\n'
    '  "user_intent": "用户为什么转这条(<=25字)",\n'
    '  "memory_note": "存入记忆库的一句话(<=40字)"\n'
    '}\n'
    "kind 判断：快递=courier；账单/收款/付款=bill；优惠券/折扣/红包=coupon；"
    "日程/会议/航班/火车=event；地址/导航/门店=address；聊天记录截图=chat_digest；"
    "纯感想或看不懂=note。"
)

REPAIR_PROMPT = (
    "你的上一次输出不是纯JSON。请根据原始任务与你的分析，直接输出一个只含字段 "
    "kind,title,entities,deadline,importance,user_intent,memory_note 的 JSON 对象，"
    "不要任何其他文字。"
)


class LLMError(Exception):
    pass


class OmlxClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def chat(self, prompt: str, image_path: Path | None = None,
             max_tokens: int | None = None, temperature: float | None = None,
             timeout: float | None = None) -> str:
        content: list[dict] = []
        if image_path is not None:
            b64 = base64.b64encode(Path(image_path).read_bytes()).decode()
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}"},
            })
        content.append({"type": "text", "text": prompt})
        payload = {
            "model": self.cfg.omlx_model,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": max_tokens or self.cfg.llm_max_tokens,
            "temperature": self.cfg.llm_temperature if temperature is None else temperature,
            "stream": False,
            # Qwen3.5 thinking mode off: JSON must be first output
            "chat_template_kwargs": {"enable_thinking": False},
        }
        resp = httpx.post(
            f"{self.cfg.omlx_base_url}/v1/chat/completions",
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.cfg.omlx_api_key}",
            },
            timeout=timeout or self.cfg.llm_timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            raise LLMError(f"unexpected response: {data}") from e

    def understand(self, text: str | None, image_path: Path | None) -> str:
        from datetime import datetime
        parts = []
        if text:
            parts.append(f"随附文字：{text}")
        weekday = "一二三四五六日"[datetime.now().weekday()]
        clock = f"当前时间：{datetime.now().isoformat(timespec='minutes')}，星期{weekday}。"
        prompt = clock + VISION_PROMPT
        if parts:
            prompt = "消息正文：" + "；".join(parts) + "\n" + prompt
        return self.chat(prompt, image_path=image_path)

    def repair(self, prior_output: str, text: str | None,
               image_path: Path | None) -> str:
        """Bounded second call: turn prior rambling into pure JSON."""
        content = f"原始分析输出：\n{prior_output[:1500]}\n\n{REPAIR_PROMPT}"
        if image_path is not None:
            return self.chat(content, image_path=image_path,
                             max_tokens=400, temperature=0.0)
        return self.chat(content, max_tokens=400, temperature=0.0)


def _salvage_truncated_json(raw: str) -> dict | None:
    """Close dangling braces/quotes of a truncated LLM JSON and try to parse."""
    start = raw.find("{")
    if start == -1:
        return None
    frag = raw[start:]
    # drop the last incomplete line (likely a half-written value)
    lines = frag.rsplit("\n", 1)
    frag = lines[0] if len(lines) > 1 else frag
    depth = 0
    for ch in frag:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
    if depth < 0:
        depth = 0
    frag = frag.rstrip().rstrip(",")
    fixed = frag + '"}' * 0  # placeholder no-op
    # trim a trailing dangling "key": "value without quote
    if frag.count('"') % 2 == 1:
        frag += '"'
    fixed = frag + "\n" + ('"}' if frag.rstrip().endswith(":") else "") + "}" * depth
    try:
        data = json.loads(fixed)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


SCHEMA_KEYS = {"kind", "title", "entities", "deadline", "importance",
               "user_intent", "memory_note"}


def _iter_balanced_json(raw: str):
    """Yield balanced {...} substrings from raw text, outermost first."""
    start = raw.find("{")
    if start == -1:
        return
    depth = 0
    for i in range(start, len(raw)):
        if raw[i] == "{":
            depth += 1
        elif raw[i] == "}":
            depth -= 1
            if depth == 0:
                yield raw[start:i + 1]
                nxt = raw.find("{", i + 1)
                if nxt == -1:
                    return
                start, depth = nxt, 0


def _is_record_shaped(d: dict) -> bool:
    return isinstance(d, dict) and "kind" in d and (
        len(set(d) & SCHEMA_KEYS) >= 3)


def extract_json(raw: str) -> dict | None:
    """Generic: first balanced JSON object in the text (for CLI/debug)."""
    for frag in _iter_balanced_json(raw.strip()):
        try:
            return json.loads(frag)
        except json.JSONDecodeError:
            continue
    return None


def extract_decision(raw: str) -> dict | None:
    """Find the skill-router decision JSON (must contain "skill")."""
    for frag in _iter_balanced_json(raw.strip()):
        try:
            d = json.loads(frag)
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict) and "skill" in d:
            return d
    return None


def extract_record(raw: str) -> dict | None:
    """Schema-aware: find the record-shaped JSON among all candidates."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    for frag in _iter_balanced_json(raw):
        try:
            d = json.loads(frag)
        except json.JSONDecodeError:
            continue
        if _is_record_shaped(d):
            return d
    for frag in _iter_balanced_json(raw):
        try:
            d = json.loads(frag)
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict) and "kind" in d:
            return d
    return _salvage_truncated_json(raw)
