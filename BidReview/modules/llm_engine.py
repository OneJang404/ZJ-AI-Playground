"""
LLM 驱动审核引擎（新版）
=======================
"大模型为主，传统工具为辅"架构的核心模块。
通过 OpenAI 兼容接口调用 Qwen3-VL 多模态模型（硅基流动托管），
直接分析投标/招标 PDF 文档，替代传统 OCR+规则引擎。

LLM 负责：文档理解、文本/表格/印章识别、章节解析、语义判断
传统工具负责：数字签名验证（pyHanko）、红框截图（PyMuPDF+PIL）、缓存

依赖：requests（HTTP 调用）、PyMuPDF（PDF 渲染为图片）
"""

import os
import re
import json
import time
import base64
import hashlib
import json as _json
import logging
from io import BytesIO
from datetime import datetime
from typing import List, Dict, Optional, Callable, Tuple
from pathlib import Path

import requests
from dotenv import load_dotenv
from PIL import Image, ImageDraw

# ---- 环境变量加载 ----
_project_root = Path(__file__).resolve().parent.parent
load_dotenv(_project_root / ".env")
if not os.getenv("DEEPSEEK_API_KEY") and not os.getenv("SiliconFlow_API_KEY"):
    load_dotenv(_project_root / "api.env")

logger = logging.getLogger("LLMEngine")

# ---- 缓存目录 ----
_CACHE_DIR = _project_root / ".bidreview_cache" / "llm_results"


class ReviewCancelled(Exception):
    """审核被用户取消"""


def _is_cancelled(cancel_event) -> bool:
    """检查取消事件（线程安全）"""
    return cancel_event is not None and cancel_event.is_set()


# ---- 截图辅助函数（模块级） ----

def _pdf_rects_to_pixels(rects, zoom: float) -> list:
    """将 PyMuPDF Rect 列表（PDF点）转换为图片像素坐标"""
    return [[r.x0 * zoom, r.y0 * zoom, r.x1 * zoom, r.y1 * zoom] for r in rects]


def _split_keyword_for_search(keyword: str) -> list:
    """
    将 LLM 返回的关键词拆分为可搜索的片段（按优先级降序）。
    LLM 常返回描述性短语而非 PDF 原文，需要多级降级搜索。
    策略：全词 → 去除虚词的子串 → 逐字符 n-gram（长→短）。
    """
    kw = keyword.strip()
    if not kw:
        return []
    candidates = []

    # 1. 全词优先
    if len(kw) >= 3:
        candidates.append(kw)

    # 2. 中文 n-gram：逐字符滑动窗口（5→4→3→2）
    skip_set = {"的", "了", "是", "在", "与", "及", "和", "或", "不", "未",
                "有", "被", "把", "从", "到", "对", "为", "以", "而", "且",
                "应", "需", "该", "其", "此", "本", "中", "等", "页", "第",
                "应", "已", "未", "无", "可", "均", "仅", "共"}
    chars = [c for c in kw if c not in skip_set and c not in "，。、；：！？\"\"''\s\-|/\\"]
    if len(chars) >= 5:
        for n in (5, 4, 3):
            for i in range(len(chars) - n + 1):
                seg = "".join(chars[i:i+n])
                if seg not in candidates and seg not in skip_set:
                    candidates.append(seg)

    # 3. 去重（保持优先级）
    seen = set()
    result = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            result.append(c)
    return result


def _extract_rule_keywords(rule_text: str) -> list:
    """
    从自定义规则文本中提取搜索关键词。
    优先提取引号内的"证据词"（如 '同意'），再对剩余文本做分词。
    """
    keywords = []
    evidence_terms = []

    # 1. 提取引号内文字作为证据关键词（最高优先级）
    quoted = re.findall(r"['\"「]([^'\"「」]{1,20})['\"」]", rule_text)
    for q in quoted:
        q = q.strip()
        if q and len(q) >= 1:
            evidence_terms.append(q)
            keywords.append(q)

    # 2. 去除引号部分后的剩余文本
    rest = re.sub(r"['\"「][^'\"「」]{1,20}['\"」]", "", rule_text)
    rest = rest.strip()
    if rest:
        keywords.extend(_split_keyword_for_search(rest))

    # 3. 去重保持优先级
    seen = set()
    result = []
    for k in keywords:
        if k and k not in seen:
            seen.add(k)
            result.append(k)

    return result, evidence_terms


def _find_page_for_position(full_text: str, char_pos: int, label: str = "投标文件") -> int:
    """
    根据字符位置定位 PDF 页码。
    通过统计 full_text 中 char_pos 之前的页面标记数量来确定。
    """
    marker = f"【{label} 第"
    count = 0
    pos = 0
    while True:
        idx = full_text.find(marker, pos)
        if idx < 0 or idx >= char_pos:
            break
        count += 1
        pos = idx + 1
    return count if count > 0 else 1


def _find_evidence_pages(evidence_kws: list, context_kws: list, full_text: str,
                         window: int = 500) -> list:
    """
    在全文搜索证据词 + 上下文共现的位置，返回对应的页码列表。
    """
    pages = []
    for ekw in evidence_kws[:4]:
        if not ekw:
            continue
        pos = 0
        while True:
            idx = full_text.find(ekw, pos)
            if idx < 0:
                break
            ctx_start = max(0, idx - window)
            ctx_end = min(len(full_text), idx + len(ekw) + window)
            nearby = full_text[ctx_start:ctx_end]
            for ckw in context_kws[:8]:
                if ckw and ckw != ekw and ckw in nearby:
                    page = _find_page_for_position(full_text, idx)
                    if page not in pages:
                        pages.append(page)
                    break
            pos = idx + len(ekw)
            if len(pages) >= 3:
                break
        if len(pages) >= 3:
            break
    return sorted(pages)


def _build_rule_evidence(evidence_kws: list, context_kws: list, full_text: str,
                         max_matches: int = 3, window: int = 500) -> str:
    """
    在文档全文中搜索证据关键词，并验证上下文关键词是否在附近出现。
    仅当证据词与上下文词在 window 字符内共现时，才认为找到有效证据。

    参数:
        evidence_kws: 证据关键词（引号内文字，如 ['同意']）
        context_kws:  上下文关键词（规则剩余文本的分词，如 ['投标函附录', '备注']）
        full_text:    文档全文
        max_matches:  最多返回几条匹配
        window:       上下文窗口大小（字符数）
    """
    if not full_text:
        return "（全文搜索未找到相关关键词）"

    context_matches = []  # 上下文验证通过的匹配
    no_context_matches = []  # 找到证据词但无上下文

    # ---- 搜索证据关键词，验证上下文 ----
    for ekw in evidence_kws[:4]:
        if not ekw or len(ekw) < 1:
            continue
        pos = 0
        while True:
            idx = full_text.find(ekw, pos)
            if idx < 0:
                break
            # 检查窗口内是否有上下文关键词
            ctx_start = max(0, idx - window)
            ctx_end = min(len(full_text), idx + len(ekw) + window)
            nearby = full_text[ctx_start:ctx_end]

            ctx_found = []
            for ckw in context_kws[:8]:
                if ckw and ckw != ekw and ckw in nearby:
                    ctx_found.append(ckw)
                    if len(ctx_found) >= 2:
                        break

            # 截取上下文片段
            start = max(0, idx - 40)
            end = min(len(full_text), idx + len(ekw) + 40)
            snippet = full_text[start:end].replace("\n", " ")
            snippet = snippet.replace(ekw, f"【{ekw}】")

            if ctx_found:
                context_matches.append((ekw, snippet, ctx_found))
            else:
                no_context_matches.append((ekw, snippet))

            pos = idx + len(ekw)
            if len(context_matches) >= max_matches:
                break
        if len(context_matches) >= max_matches:
            break

    # ---- 组装结果 ----
    if context_matches:
        lines = []
        for ekw, snippet, ctx in context_matches[:max_matches]:
            ctx_str = "、".join(ctx)
            lines.append(f'  ✓ "{ekw}"（上下文: {ctx_str}）→ ...{snippet}...')
        return "全文搜索（上下文验证通过）：\n" + "\n".join(lines)

    if no_context_matches:
        lines = []
        for ekw, snippet in no_context_matches[:2]:
            lines.append(f'  ⚠️ "{ekw}" → ...{snippet}...（但附近未找到相关上下文）')
        ctx_list = "、".join(context_kws[:5]) if context_kws else "无"
        return f"全文搜索（缺少上下文匹配，上下文词: {ctx_list}）：\n" + "\n".join(lines)

    # 无证据关键词时，退化为普通搜索
    for kw in (evidence_kws + context_kws)[:6]:
        idx = full_text.find(kw)
        if idx >= 0:
            start = max(0, idx - 40)
            end = min(len(full_text), idx + len(kw) + 40)
            snippet = full_text[start:end].replace("\n", " ")
            snippet = snippet.replace(kw, f"【{kw}】")
            return f"全文搜索结果：\n  ✓ \"{kw}\" → ...{snippet}..."

    kw_list = "、".join((evidence_kws + context_kws)[:5])
    return f"（全文搜索未找到：{kw_list}）"


def _verify_custom_rule_violations(
    violations: list, resp_full_text: str, custom_rules_text: str
) -> Tuple[list, list, list]:
    """
    后验证（方案B）：对 LLM 返回的自定义规则违规，回到文档全文中搜索证据。
    若找到正面证据证明文档已满足规则要求 → 从违规列表剔除，转为合规项。

    返回: (cleaned_violations, false_positives, warnings)
    """
    if not resp_full_text or not custom_rules_text.strip():
        return violations, [], []

    # 预解析所有自定义规则的 (证据词, 上下文词)
    all_rules_info = []  # [(evidence_kws, context_kws), ...]
    for rule_line in custom_rules_text.strip().split("\n"):
        rule_line = rule_line.strip()
        if rule_line:
            all_kws, ev = _extract_rule_keywords(rule_line)
            ctx = [k for k in all_kws if k not in ev]
            all_rules_info.append((ev, ctx))

    cleaned = []
    false_positives = []
    warnings = []

    for v in violations:
        cat = str(v.get("category", ""))
        summary = str(v.get("problem_summary", ""))
        reason = str(v.get("violation_reason", ""))
        req_detail = str(v.get("requirement_detail", ""))

        # 扩大检测：旧标记 + 新 category + 自定义规则关键词泛匹配
        is_custom = (
            cat == "自定义规则违规"
            or "[自定义规则]" in summary
            or "[自定义规则]" in reason
            or "自定义规则" in summary
            or "自定义规则" in req_detail
        )

        if not is_custom:
            cleaned.append(v)
            continue

        # 从多个来源提取证据关键词
        combined = f"{summary} {reason} {req_detail}"
        evidence_kws = re.findall(r"['\"「]([^'\"「」]{1,20})['\"」]", combined)
        context_kws = []
        if not evidence_kws:
            # 从所有规则中收集证据词和上下文词
            for ev, ctx in all_rules_info:
                evidence_kws.extend(ev)
                context_kws.extend(ctx)
        else:
            # 从所有规则中收集上下文词
            for ev, ctx in all_rules_info:
                context_kws.extend(ctx)

        if not evidence_kws:
            cleaned.append(v)
            continue

        # 上下文感知搜索：证据词必须在上下文词附近才算有效
        found_with_context = False
        found_kw = ""
        WINDOW = 500
        for ekw in evidence_kws:
            if not ekw:
                continue
            pos = 0
            while True:
                idx = resp_full_text.find(ekw, pos)
                if idx < 0:
                    break
                # 检查窗口内是否有上下文关键词
                ctx_start = max(0, idx - WINDOW)
                ctx_end = min(len(resp_full_text), idx + len(ekw) + WINDOW)
                nearby = resp_full_text[ctx_start:ctx_end]
                for ckw in context_kws:
                    if ckw and ckw != ekw and ckw in nearby:
                        found_with_context = True
                        found_kw = f"{ekw}（上下文: {ckw}）"
                        break
                if found_with_context:
                    break
                pos = idx + len(ekw)
            if found_with_context:
                break

        if found_with_context:
            warnings.append(
                f"自定义规则误报已排除：文档中找到「{found_kw}」→ "
                f"原判：{summary[:60]}"
            )
            v["compliant"] = True
            v["severity"] = "低"
            v["_false_positive_removed"] = True
            false_positives.append(v)
        else:
            cleaned.append(v)

    return cleaned, false_positives, warnings


# ============================================================
# JSON 校验工具 — 确保 LLM 输出格式正确
# ============================================================

# 合法的严重度取值
VALID_SEVERITIES = {"高", "中", "低"}

# 合法的违规类别
VALID_CATEGORIES = {"内容缺失", "格式不符", "签章问题", "条款不响应",
                    "填写错误", "自定义规则违规", "未分类"}

# extraction 中必须存在的字段及其类型
REQUIRED_EXTRACTION_FIELDS = {
    "bid_price": str,
    "company_name": str,
    "credit_code": str,
    "legal_representative": str,
}

# extraction 中可选字段
OPTIONAL_EXTRACTION_FIELDS = [
    "contact_person", "bid_validity", "construction_period",
    "seals", "legal_rep_check", "chapter_structure", "document_sections",
    "self_check",
]

# 信用代码正则：18位（数字+字母，字母大写）
CREDIT_CODE_PATTERN = re.compile(r'^[0-9A-HJ-NPQRTUWXY]{2}\d{6}[0-9A-HJ-NPQRTUWXY]{10}$')


def _validate_extraction(extraction: dict) -> Tuple[dict, List[str]]:
    """
    校验并修复 LLM 返回的结构化提取数据

    返回:
        (cleaned: dict, warnings: List[str])
    """
    warnings = []
    cleaned = {}

    if not isinstance(extraction, dict):
        return {}, ["extraction 不是有效的 JSON 对象"]

    # 1. 必须字段检查
    for field, expected_type in REQUIRED_EXTRACTION_FIELDS.items():
        value = extraction.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            warnings.append(f"必须字段「{field}」为空或缺失")
            cleaned[field] = None
        elif not isinstance(value, expected_type):
            warnings.append(f"字段「{field}」类型错误：期望{expected_type.__name__}，实际{type(value).__name__}")
            cleaned[field] = str(value) if value else None
        else:
            cleaned[field] = value.strip() if isinstance(value, str) else value

    # 2. 信用代码格式校验
    cc = cleaned.get("credit_code")
    if cc and not CREDIT_CODE_PATTERN.match(cc.replace(" ", "")):
        cleaned["credit_code"] = cc.replace(" ", "")
        warnings.append(f"信用代码「{cc}」格式可疑，请人工核实")

    # 3. 报价合理性校验（应包含数字）
    bp = cleaned.get("bid_price")
    if bp and not re.search(r'\d', str(bp)):
        warnings.append(f"报价「{bp}」中未检测到数字，可能提取有误")

    # 4. 可选字段（保留原值）
    for field in OPTIONAL_EXTRACTION_FIELDS:
        if field in extraction and extraction[field] is not None:
            cleaned[field] = extraction[field]

    # 5. seals 字段校验
    seals = cleaned.get("seals", [])
    if isinstance(seals, list):
        valid_seals = []
        for s in seals:
            if isinstance(s, dict) and s.get("type"):
                valid_seals.append(s)
        cleaned["seals"] = valid_seals
    else:
        cleaned["seals"] = []

    # 6. legal_rep_check 校验
    lrc = cleaned.get("legal_rep_check", {})
    if isinstance(lrc, dict):
        # "match" 字段应该是布尔值
        if "match" in lrc and not isinstance(lrc["match"], bool):
            lrc["match"] = str(lrc["match"]).lower() in ("true", "yes", "是", "一致")
        cleaned["legal_rep_check"] = lrc

    # 7. self_check 自检信息保留
    sc = cleaned.get("self_check", {})
    if isinstance(sc, dict):
        logger.info(f"LLM自检结果：{json.dumps(sc, ensure_ascii=False)[:300]}")

    return cleaned, warnings


def _validate_violations(violations: list) -> Tuple[list, List[str]]:
    """
    校验并修复违规项列表

    返回:
        (cleaned: list, warnings: List[str])
    """
    warnings = []
    cleaned = []

    if not isinstance(violations, list):
        return [], ["violations 不是有效的 JSON 数组"]

    for i, v in enumerate(violations):
        if not isinstance(v, dict):
            warnings.append(f"违规项[{i}]不是对象，已跳过")
            continue

        item = {
            "violation_id": f"LLM-{i+1:03d}",
            "severity": v.get("severity", "低") if v.get("severity") in VALID_SEVERITIES else "低",
            "category": v.get("category", "未分类") if v.get("category") in VALID_CATEGORIES else "未分类",
            "source": {
                "page_display": None,
                "page_num": None,
                "keyword": v.get("category", ""),
                "requirement_text": v.get("requirement_detail", ""),
                "screenshot_bytes": None,
                "bbox": None,
            },
            "evidence": {
                "page_display": v.get("evidence_page"),
                "found_text": str(v.get("evidence_keyword") or ""),
                "screenshot_bytes": None,
                "bbox": v.get("evidence_bbox"),
            },
            "problem_summary": str(v.get("problem_summary", ""))[:100],
            "requirement_detail": str(v.get("requirement_detail", ""))[:500],
            "violation_reason": str(v.get("violation_reason", ""))[:500],
            "fix_suggestion": str(v.get("fix_suggestion", ""))[:500],
            "compliant": False,
        }

        if item["severity"] not in VALID_SEVERITIES:
            warnings.append(f"违规项[{i}]严重度「{v.get('severity')}」无效，已重置为「低」")

        cleaned.append(item)

    return cleaned, warnings


# ============================================================
# LLM 驱动的审核引擎
# ============================================================

# ============================================================
# 模块级便捷函数
# ============================================================

def _draw_red_boxes(img_bytes: bytes, boxes: list, width: int = 3) -> bytes:
    """图片上画红框（JPEG 输出，性能优于 PNG）"""
    try:
        img = Image.open(BytesIO(img_bytes))
        draw = ImageDraw.Draw(img)
        for box in boxes:
            if box and len(box) >= 4:
                if isinstance(box[0], (int, float)):
                    x0, y0, x1, y1 = int(box[0]), int(box[1]), int(box[2]), int(box[3])
                    for i in range(width):
                        draw.rectangle([x0-i, y0-i, x1+i, y1+i], outline="red")
                else:
                    pts = [(int(p[0]), int(p[1])) for p in box]
                    draw.line(pts + [pts[0]], fill="red", width=width)
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue()
    except Exception as e:
        logger.warning(f"红框绘制失败，返回原图：{e}")
        return img_bytes


class LLMEngine:
    """
    LLM 驱动审核引擎（新版架构核心）
    -------------------------------
    使用 Qwen3-VL 32B 多模态模型直接分析 PDF，
    包含严格 JSON 输出、多层验证、结果缓存。

    使用示例：
        engine = LLMEngine()
        result = engine.review_documents(
            invitation_pdf_bytes=tender_bytes,
            response_pdf_bytes=bid_bytes,
            custom_rules="所有报价金额必须保留两位小数",
            progress_callback=lambda frac, msg: print(f"{frac:.0%}: {msg}"),
        )
    """

    # 支持的 PDF MIME 类型
    PDF_MIME = "application/pdf"

    def __init__(self):
        """从环境变量初始化审核器配置"""
        # 兼容 DEEPSEEK_API_KEY 和 SiliconFlow_API_KEY 两种写法
        # 跳过明显是占位符的值（sk-xxx...、your-key 等）
        def _is_valid_key(k):
            if not k or not k.strip():
                return False
            k = k.strip()
            # 占位符检测
            if k.startswith("sk-xxx") or k.startswith("sk-XXX"):
                return False
            if k.lower() in ("your-key", "your_api_key", "your-key-here", "placeholder"):
                return False
            return True

        raw_key = os.getenv("SiliconFlow_API_KEY", "") or os.getenv("DEEPSEEK_API_KEY", "")
        self.api_key = raw_key.strip() if _is_valid_key(raw_key) else ""

        # API 端点（默认硅基流动，OpenAI 兼容格式，支持 Qwen3-VL 多模态）
        api_base = (
            os.getenv("SiliconFlow_API_URL", "")
            or os.getenv("DEEPSEEK_API_URL", "")
            or "https://api.siliconflow.cn"
        ).strip().rstrip("/")

        # 确保以 /v1 结尾（OpenAI 兼容格式需要）
        if not api_base.endswith("/v1"):
            api_base += "/v1"
        self.api_url = f"{api_base}/chat/completions"

        # 硅基流动的 VL 视觉模型 ID
        self.model = os.getenv(
            "DEEPSEEK_MODEL",
            "Qwen/Qwen3-VL-32B-Instruct",
        ).strip()
        self.timeout = int(os.getenv("LLM_API_TIMEOUT", "300"))
        self.max_retries = int(os.getenv("DEEPSEEK_MAX_RETRIES", "3"))

        # 确保缓存目录存在
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)

        logger.info(f"LLM引擎：model={self.model}, api_url={self.api_url}")

    def _check_config(self) -> Optional[str]:
        if not self.api_key:
            return "请配置 API 密钥。在 .env 文件中设置 DEEPSEEK_API_KEY。\n获取地址：https://cloud.siliconflow.cn/account/ak"
        return None

    # ================================================================
    # 结果缓存（基于文件+规则的哈希）
    # ================================================================

    def _compute_cache_key(self, inv_bytes: bytes, resp_bytes: bytes, custom_rules: str) -> str:
        """计算审核结果的缓存键（SHA-256），含模型名防止切换模型后命中旧缓存"""
        h = hashlib.sha256()
        h.update(inv_bytes)
        h.update(b"||INV_RESP_SEPARATOR||")
        h.update(resp_bytes)
        h.update(custom_rules.encode("utf-8"))
        h.update(self.model.encode("utf-8"))
        return h.hexdigest()

    def _get_cached_result(self, cache_key: str) -> Optional[dict]:
        """检查缓存，命中则返回完整审核结果"""
        cache_file = _CACHE_DIR / f"{cache_key}.json"
        if not cache_file.exists():
            return None
        try:
            data = _json.loads(cache_file.read_text(encoding="utf-8"))
            age = time.time() - data.get("_cache_timestamp", 0)
            logger.info(f"💾 缓存命中：{cache_key[:12]}...（{age/3600:.1f}小时前）")
            return data
        except Exception as e:
            logger.warning(f"缓存读取失败：{e}")
            return None

    def _save_cached_result(self, cache_key: str, result: dict):
        """保存审核结果到缓存"""
        cache_file = _CACHE_DIR / f"{cache_key}.json"
        try:
            result["_cache_timestamp"] = time.time()
            result["_cached_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cache_file.write_text(_json.dumps(result, ensure_ascii=False), encoding="utf-8")
            logger.info(f"💾 缓存已保存：{cache_key[:12]}...")
        except Exception as e:
            logger.warning(f"缓存保存失败：{e}")

    # ================================================================
    # 主审核方法
    # ================================================================

    def review_documents(
        self,
        invitation_pdf_bytes: bytes,
        response_pdf_bytes: bytes,
        custom_rules: str = "",
        progress_callback: Optional[Callable[[float, str], None]] = None,
        bypass_cache: bool = False,
        cancel_event=None,
    ) -> dict:
        """
        多模态文档审核主入口

        参数:
            invitation_pdf_bytes:  招标文件字节
            response_pdf_bytes:    投标文件字节
            custom_rules:          自定义自然语言审核规则（每行一条）
            progress_callback:     进度回调 (frac 0~1, msg)
            bypass_cache:          是否跳过缓存（默认 False）

        返回:
            {
                "ok": bool, "violations": [...], "compliant_items": [...],
                "summary": {...}, "ai_report": str, "extraction": {...},
                "validation_warnings": [...], "error": str|None,
                "from_cache": bool,
            }
        """
        config_error = self._check_config()
        if config_error:
            return self._error_response(config_error)

        # ---- Word / PDF 文档预处理（统一转为 PDF + 文本） ----
        from modules.docx_extractor import (
            is_docx, is_doc, convert_docx_to_pdf, extract_docx_text,
        )

        inv_raw_bytes = invitation_pdf_bytes   # 原始字节用于缓存 key
        resp_raw_bytes = response_pdf_bytes
        docx_warnings = []

        def _prepare_document(file_bytes, label):
            """处理任意格式（PDF/DOCX/DOC），返回 (text, pages, pdf_bytes)"""
            if is_docx(file_bytes):
                text, pages = extract_docx_text(file_bytes)
                pdf_bytes = convert_docx_to_pdf(file_bytes)
                if pdf_bytes:
                    logger.info(f"{label} DOCX → PDF 转换成功，{len(pdf_bytes)/1024:.0f}KB")
                    return text, pages, pdf_bytes
                else:
                    docx_warnings.append(
                        f"{label}为DOCX格式，但LibreOffice未安装，无法渲染页面图片。"
                        "请安装LibreOffice：https://www.libreoffice.org/download/"
                    )
                    return text, pages, None
            elif is_doc(file_bytes):
                pdf_bytes = convert_docx_to_pdf(file_bytes)
                if pdf_bytes:
                    logger.info(f"{label} DOC → PDF 转换成功，{len(pdf_bytes)/1024:.0f}KB")
                    text, pages = self._extract_text_only(pdf_bytes, label)
                    return text, pages, pdf_bytes
                else:
                    docx_warnings.append(
                        f"{label}为旧版DOC格式，必须安装LibreOffice才能处理。"
                        "请安装：https://www.libreoffice.org/download/"
                    )
                    return f"[{label}为DOC格式，无LibreOffice无法提取文本]", 0, None
            else:
                text, pages = self._extract_text_only(file_bytes, label)
                return text, pages, file_bytes

        inv_text, inv_pages, inv_pdf = _prepare_document(invitation_pdf_bytes, "招标文件")
        if inv_pdf is not None:
            invitation_pdf_bytes = inv_pdf
        resp_text, resp_pages, resp_pdf = _prepare_document(response_pdf_bytes, "投标文件")
        if resp_pdf is not None:
            response_pdf_bytes = resp_pdf

        # ---- 缓存检查（基于原始字节，确保 Word 文件缓存一致） ----
        cache_key = self._compute_cache_key(inv_raw_bytes, resp_raw_bytes, custom_rules)
        if not bypass_cache:
            cached = self._get_cached_result(cache_key)
            if cached:
                if progress_callback:
                    progress_callback(0.88, "💾 缓存命中，跳过API调用...")
                cached["from_cache"] = True
                cached["ok"] = True
                # 向后兼容：旧缓存可能缺少页数，用 PDF 字节补填
                if isinstance(cached.get("summary"), dict):
                    s = cached["summary"]
                    if "invitation_key_pages" not in s or "response_total_pages" not in s:
                        import fitz
                        if "invitation_key_pages" not in s:
                            inv_doc = fitz.open(stream=invitation_pdf_bytes, filetype="pdf")
                            s["invitation_total_pages"] = len(inv_doc)
                            s["invitation_key_pages"] = len(inv_doc)
                            inv_doc.close()
                        if "response_total_pages" not in s:
                            resp_doc = fitz.open(stream=response_pdf_bytes, filetype="pdf")
                            s["response_total_pages"] = len(resp_doc)
                            resp_doc.close()
                # 方案B：即使缓存命中也执行后验证（排除旧缓存中的自定义规则误报）
                if custom_rules.strip():
                    cached_v, cached_fp, _ = _verify_custom_rule_violations(
                        cached.get("violations", []), resp_text, custom_rules
                    )
                    cached["violations"] = cached_v
                    cached.setdefault("compliant_items", [])
                    cached["compliant_items"].extend(cached_fp)
                return cached

        if _is_cancelled(cancel_event):
            return self._error_response("审核已被用户取消")

        if progress_callback:
            progress_callback(0.05, "准备审核请求...")

        # ---- 阶段1：构建 Prompt + 渲染 PDF 图片 ----
        if progress_callback:
            progress_callback(0.25, "组装AI审核提示词...")

        system_prompt = self._build_system_prompt()
        user_prompt, evidence_pages = self._build_user_prompt(inv_text, resp_text, custom_rules)

        if _is_cancelled(cancel_event):
            return self._error_response("审核已被用户取消")

        if progress_callback:
            progress_callback(0.30, "渲染文档页面并调用视觉大模型（预计2-5分钟）...")

        raw_response = self._call_vision_api(
            system_prompt, user_prompt,
            invitation_pdf_bytes, response_pdf_bytes,
            progress_callback, cancel_event,
            extra_pages=evidence_pages,
        )

        if raw_response is None:
            return self._error_response("LLM API 调用失败，请检查网络和 API 配置")

        # ---- 阶段3：解析 + 校验 + 修复 ----
        if progress_callback:
            progress_callback(0.75, "解析并校验AI审核结果...")

        extraction_raw = self._parse_section(raw_response, "EXTRACTION")
        violations_raw = self._parse_section(raw_response, "VIOLATIONS")
        custom_rules_raw = self._parse_section(raw_response, "CUSTOM_RULES_CHECK")
        ai_report = self._extract_report(raw_response)

        # JSON 解析
        extraction = self._parse_json(extraction_raw, default={})
        violations_data = self._parse_json(violations_raw, default=[])

        # 解析自定义规则判定（新增结构化输出）
        cr_violations, cr_compliant, cr_warnings = self._parse_custom_rules_check(
            custom_rules_raw, custom_rules
        )

        # 多层校验
        extraction, ext_warnings = _validate_extraction(extraction)
        violations, vio_warnings = _validate_violations(violations_data)
        violations.extend(cr_violations)
        all_warnings = ext_warnings + vio_warnings + cr_warnings

        # 方案B：后验证自定义规则违规（全文搜索证据 → 排除误报）
        violations, false_positives, post_warnings = _verify_custom_rule_violations(
            violations, resp_text, custom_rules
        )
        all_warnings.extend(post_warnings)

        # 从 extraction 派生额外的违规/合规项
        violations, compliant_items = self._derive_items_from_extraction(
            violations, extraction
        )
        # 将 CUSTOM_RULES_CHECK 的合规项 + 后验证排除的误报 合并到合规项
        compliant_items.extend(cr_compliant)
        compliant_items.extend(false_positives)

        # ---- 阶段4：生成报告 + 缓存 ----
        if progress_callback:
            progress_callback(0.85, "组装审核报告...")

        summary = self._build_summary(violations, compliant_items)
        summary["invitation_total_pages"] = inv_pages
        summary["invitation_key_pages"] = inv_pages
        summary["response_total_pages"] = resp_pages

        all_warnings.extend(docx_warnings)

        result = {
            "ok": True,
            "violations": violations,
            "compliant_items": compliant_items,
            "summary": summary,
            "ai_report": ai_report,
            "extraction": extraction,
            "validation_warnings": all_warnings,
            "error": None,
            "from_cache": False,
        }

        # 保存缓存
        self._save_cached_result(cache_key, result)

        if progress_callback:
            progress_callback(0.95, "审核结果已缓存")

        return result

    def _error_response(self, msg: str) -> dict:
        return {
            "ok": False, "error": msg,
            "violations": [], "compliant_items": [],
            "summary": {}, "ai_report": f"❌ {msg}",
            "extraction": {}, "validation_warnings": [msg],
            "from_cache": False,
        }

    # ================================================================
    # 自定义规则精炼
    # ================================================================

    def refine_rules(self, raw_rules: list) -> list:
        """
        调用 LLM 将自然语言规则精炼为简洁格式
        返回: [{"raw": "...", "refined": "..."}, ...]，失败时refined=raw
        """
        if not raw_rules or not self.api_key:
            return [{"raw": r, "refined": r} for r in raw_rules]

        rules_text = "\n".join(f"{i+1}. {r}" for i, r in enumerate(raw_rules))
        prompt = (
            "将以下投标审核规则精炼为简洁格式。要求：\n"
            "- 每条规则 ≤30 字\n"
            "- 保留核心要求（禁止什么/必须什么）\n"
            "- 去除冗余表述\n"
            "- 输出 JSON 数组，每项一个字符串，与输入顺序一一对应\n\n"
            f"{rules_text}\n\n"
            '输出格式：["规则1", "规则2", ...]'
        )

        try:
            resp = requests.post(
                self.api_url,
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"},
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": "你是文档规则提炼专家。只输出JSON数组。"},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.1,
                    "max_tokens": 1024,
                },
                timeout=30,
            )
            if resp.status_code == 200:
                text = resp.json()["choices"][0]["message"]["content"].strip()
                refined = json.loads(text)
                if isinstance(refined, list) and len(refined) == len(raw_rules):
                    return [{"raw": raw, "refined": r} for raw, r in zip(raw_rules, refined)]
        except Exception as e:
            logger.warning(f"规则精炼失败：{e}")

        return [{"raw": r, "refined": r} for r in raw_rules]

    # ================================================================
    # PDF 文本提取（PyMuPDF — 轻量、不依赖OCR）
    # ================================================================

    def _extract_text_only(self, pdf_bytes: bytes, label: str) -> Tuple[str, int]:
        """纯文本提取，为 LLM 提供结构化文本参考。返回 (文本, 页数)"""
        import fitz
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        page_count = len(doc)
        parts = []
        for i in range(page_count):
            text = doc[i].get_text()
            if text.strip():
                parts.append(f"【{label} 第{i+1}页】\n{text.strip()}")
        doc.close()
        full = "\n\n".join(parts)
        logger.info(f"{label}文本提取：{page_count}页，{len(full)}字符")
        return full, page_count

    # ================================================================
    # Prompt 构建（严格 JSON 输出 + 自检）
    # ================================================================

    def _build_system_prompt(self) -> str:
        return (
            "资深投标顾问。精通招投标法规，能识别印章/签名/表格，比对法人声明与印章一致性。\n\n"
            "## JSON 铁律\n"
            "- 合法 JSON：双引号、无注释、布尔用 true/false、找不到填 null\n"
            "- evidence_keyword 必填：从 PDF 逐字抄录的原文短句（8-30字），选该页独特文本\n"
            "- evidence_bbox：仅纯视觉元素（印章/签名）无法摘录原文时才用\n\n"
            "## 输出（严格按此顺序，不可遗漏）\n\n"
            "---EXTRACTION---\n"
            "{\n"
            '  "bid_price":"报价金额（含币种，找不到null）",\n'
            '  "company_name":"公司全称",\n'
            '  "credit_code":"18位信用代码",\n'
            '  "legal_representative":"法定代表人",\n'
            '  "contact_person":"授权代表（或null）",\n'
            '  "bid_validity":"投标有效期（或null）",\n'
            '  "construction_period":"工期（或null）",\n'
            '  "seals":[{"page":1,"type":"公章","text":"XX有限公司","position":"右下角"}],\n'
            '  "legal_rep_check":{"declared_name":"声明法人","seal_name":"印章姓名","match":true,"detail":"比对详情"},\n'
            '  "chapter_structure":[{"level":1,"title":"一、投标函","page":1}],\n'
            '  "document_sections":{"body_pages":"1-5","business_pages":"6-15","technical_pages":"16-30"},\n'
            '  "self_check":{"bid_price_verified":true,"credit_code_verified":true,"legal_rep_verified":true,"notes":"二次确认"}\n'
            '}\n'
            "---VIOLATIONS---\n"
            "[\n"
            '  {"severity":"高","category":"签章问题","problem_summary":"缺少公章","requirement_detail":"招标文件要求加盖公章","violation_reason":"签章处空白","fix_suggestion":"加盖公章","evidence_page":1,"evidence_bbox":[150,620,850,750],"evidence_keyword":null},\n'
            '  {"severity":"中","category":"条款不响应","problem_summary":"质保期不满足","requirement_detail":"招标要求≥36个月","violation_reason":"投标填写12个月","fix_suggestion":"修改为36个月","evidence_page":3,"evidence_bbox":null,"evidence_keyword":"质保期12个月"}\n'
            "]\n"
            "---CUSTOM_RULES_CHECK---\n"
            "[\n"
            '  {"rule_index":1,"verdict":"compliant","evidence":"投标函附录备注栏填写同意"},\n'
            '  {"rule_index":2,"verdict":"violated","evidence":"质保期12个月，不满足36个月"},\n'
            '  {"rule_index":3,"verdict":"uncertain","evidence":"未找到相关条款"}\n'
            "]\n"
            "verdict: compliant / violated / uncertain\n"
            "---REPORT---\n"
            "（Markdown 报告，末尾给出：✅建议通过 / ⚠️修改后通过 / ❌不建议通过）\n"
            "---END---\n\n"
            "## 审核原则\n"
            "高🔴签章缺失/资质不符/报价错误/废标条款 → 中🟡格式偏差/表述不规范 → 低🟢排版/用词\n"
            "self_check 逐项确认关键字段已准确提取；CUSTOM_RULES_CHECK 逐条输出不可遗漏"
        )

    def _build_user_prompt(self, inv_text: str, resp_text: str, custom_rules: str) -> Tuple[str, list]:
        INV_MAX = 30000
        RESP_MAX = 50000

        inv_trunc = inv_text[:INV_MAX]
        if len(inv_text) > INV_MAX:
            inv_trunc += f"\n\n[招标文件全文共{len(inv_text)}字符，以上为前{INV_MAX}字符]"

        resp_trunc = resp_text[:RESP_MAX]
        if len(resp_text) > RESP_MAX:
            resp_trunc += f"\n\n[投标文件全文共{len(resp_text)}字符，以上为前{RESP_MAX}字符]"

        # ---- 方案A：全文搜索每条自定义规则的证据 ----
        rules_block = ""
        evidence_pages = []
        if custom_rules.strip():
            rules_lines = custom_rules.strip().split("\n")
            rules_parts = []
            for idx, rule_line in enumerate(rules_lines):
                rule_line = rule_line.strip()
                if not rule_line:
                    continue
                keywords, ev_kws = _extract_rule_keywords(rule_line)
                context_kws = [k for k in keywords if k not in ev_kws]
                evidence = _build_rule_evidence(ev_kws, context_kws, resp_text)
                # 定位证据所在页码，追加页面引用
                pages = _find_evidence_pages(ev_kws, context_kws, resp_text)
                page_hint = f"\n📎 证据所在页码：第{'、'.join(str(p) for p in pages)}页" if pages else ""
                rules_parts.append(
                    f"### 规则 {idx + 1}: {rule_line}\n📎 {evidence}{page_hint}"
                )
                for p in pages:
                    if p not in evidence_pages:
                        evidence_pages.append(p)
            rules_block = (
                "\n\n---\n\n"
                "## ⚠️ 自定义审核规则\n\n"
                "逐条检查以下规则。对每条规则基于**提供的证据**给出三选一判定：\n"
                "- compliant：文档明确满足要求\n"
                "- violated：文档明确违反要求\n"
                "- uncertain：提供的材料无法判断\n\n"
                "⚠️ 关键原则：\n"
                "- 若📎全文搜索证据显示要求已被满足，必须判定为 compliant\n"
                "- 禁止在证据不足时猜测为 violated\n"
                "- 判定结果输出到 ---CUSTOM_RULES_CHECK--- JSON 数组中，每条规则一行，不可遗漏\n"
                "- evidence 字段写入支撑判定的**原文证据**（直接从文档中摘录，不可编造）\n"
                "- 📎证据所在页码的PDF页面已随消息上传，请直接查看对应页面图片核实\n\n"
                + "\n\n".join(rules_parts)
            )

        return (
            "请对招标文件与投标文件进行深度交叉审核。PDF文件已随此消息一同上传，请查看PDF中的印章、签名、表格等视觉元素。\n\n"
            "---\n\n"
            "## 📘 招标文件全文（文本参考）\n\n"
            f"{inv_trunc}\n\n"
            "---\n\n"
            "## 📄 投标文件全文（文本参考）\n\n"
            f"{resp_trunc}\n\n"
            "---\n\n"
            "## 📌 重要提醒\n"
            "1. 文本提取可能有遗漏，请以**PDF原件**中的视觉信息为准\n"
            "2. 印章文字内容必须从PDF图片中**直接读取**，不可推测\n"
            "3. 所有金额数字请**逐位核对**，不要四舍五入\n"
            "4. 信用代码必须精确到每一位\n"
            "5. 在 self_check 中确认已对关键字段进行二次核实"
            f"{rules_block}"
        ), evidence_pages

    # ================================================================
    # PDF 智能压缩
    # ================================================================

    def _compress_pdf(self, pdf_bytes: bytes, progress_callback=None) -> Optional[bytes]:
        """
        使用 PyMuPDF + PIL 压缩 PDF 中的大图片以减小文件体积。
        对 >300KB 的图片自动降分辨率（max 2000px）和降低 JPEG 质量（55%）。

        参数:
            pdf_bytes: 原始 PDF 字节流

        返回:
            压缩后的 PDF 字节流，失败时返回 None
        """
        import fitz

        try:
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        except Exception as e:
            logger.warning(f"压缩前打开PDF失败：{e}")
            return None

        page_count = len(doc)
        compressed_count = 0
        saved_bytes = 0
        original_size = len(pdf_bytes)

        logger.info(f"智能压缩开始：原始{original_size/1024/1024:.1f}MB，{page_count}页")

        for i in range(page_count):
            try:
                page = doc[i]
                images = page.get_images(full=True)
                if not images:
                    continue

                for img_info in images:
                    xref = img_info[0]
                    try:
                        base_image = doc.extract_image(xref)
                        img_bytes = base_image["image"]
                        img_size_kb = len(img_bytes) / 1024

                        # 仅压缩 >300KB 的图片
                        if img_size_kb <= 300:
                            continue

                        # 用 PIL 重新编码
                        pil_img = Image.open(BytesIO(img_bytes))
                        orig_mode = pil_img.mode

                        # RGBA → RGB（JPEG 不支持透明通道）
                        if pil_img.mode in ("RGBA", "P"):
                            pil_img = pil_img.convert("RGB")

                        # 限制最大分辨率 2000px
                        w, h = pil_img.size
                        max_dim = max(w, h)
                        if max_dim > 2000:
                            scale = 2000 / max_dim
                            new_w, new_h = int(w * scale), int(h * scale)
                            pil_img = pil_img.resize((new_w, new_h), Image.LANCZOS)
                            logger.info(f"  第{i+1}页图片缩放：{w}×{h} → {new_w}×{new_h}")

                        # 重新编码为 JPEG
                        buf = BytesIO()
                        pil_img.save(buf, format="JPEG", quality=55, optimize=True)
                        new_bytes = buf.getvalue()

                        # 仅当确实更小时才替换
                        if len(new_bytes) < len(img_bytes):
                            doc.update_stream(xref, new_bytes)
                            compressed_count += 1
                            saved = len(img_bytes) - len(new_bytes)
                            saved_bytes += saved
                            logger.info(
                                f"  第{i+1}页图片压缩：{img_size_kb:.0f}KB "
                                f"→ {len(new_bytes)/1024:.0f}KB "
                                f"（节省{saved/1024:.0f}KB）"
                            )
                    except Exception as e:
                        logger.warning(f"第{i+1}页图片压缩失败：{e}")
                        continue
            except Exception as e:
                logger.warning(f"PDF压缩处理失败：{e}")
                continue

        # 保存并返回
        out = BytesIO()
        doc.save(out, deflate=True, garbage=4)
        doc.close()

        compressed_size = len(out.getvalue())
        if compressed_count > 0:
            ratio = (1 - compressed_size / original_size) * 100
            logger.info(
                f"压缩完成：{original_size/1024/1024:.1f}MB → "
                f"{compressed_size/1024/1024:.1f}MB "
                f"（{ratio:.0f}%，共{compressed_count}张图片）"
            )
        else:
            logger.info("未发现需要压缩的大图片，保持原始文件")

        return out.getvalue()

    # ================================================================
    # API 调用 — 视觉模型图片模式（主路径）
    # ================================================================
    #
    # SiliconFlow Qwen3-VL API 限制（实测确认）：
    #   - 仅支持 image_url（JPEG / PNG），不支持 PDF 直传
    #   - 最大像素面积：12,845,056 px（3584×3584）
    #   - 请求体上限：~50MB（建议控制在 45MB 以内）
    #   - 图片数量无硬限制，受 token 预算约束
    #   - detail 参数：high（全分辨率）/ low（448×448，仅256 token）
    #
    # 本系统策略（32B 模型优化，避免超时）：
    #   - 投标文件：第1页（封面/报价）+ 最后1页（签章页）= 2 页，100 DPI
    #   - 招标文件：第1页（招标要求关键页）= 1 页，90 DPI
    #   - 总计最多 3 张图，控制在 ~300KB 以内，~3M 总像素
    #   - JPEG quality 65，单张 >250KB 二次压缩
    #   - detail: "high"（文档审核需要看清印章和签名）

    # 图片渲染参数（为 32B 模型优化：3 张图 ~3M px，预计 60-90s）
    DPI_KEY_PAGES = 100     # 关键页（首尾/签章页）
    DPI_REF_PAGES = 90      # 参考页
    JPEG_QUALITY = 65       # JPEG 初始质量
    MAX_SINGLE_IMAGE_KB = 250   # 单张超过此值启动二次压缩
    MAX_IMAGE_PX = 12_845_056   # 12.8M px 像素上限（3584×3584）
    MAX_PAYLOAD_ESTIMATE_MB = 45  # 请求体安全上限

    def _call_vision_api(
        self,
        system_prompt: str,
        user_prompt: str,
        inv_pdf_bytes: bytes,
        resp_pdf_bytes: bytes,
        progress_callback=None,
        cancel_event=None,
        extra_pages: list = None,
    ) -> Optional[str]:
        """
        渲染 PDF 关键页为 JPEG → 以 image_url 上传到 Qwen3-VL。
        这是唯一有效的 API 调用路径（PDF 直传不被 SiliconFlow 支持）。
        """
        import fitz

        logger.info("渲染PDF关键页为JPEG图片...")
        image_parts = []
        total_img_kb = 0.0

        def _render_pages(doc, start_page, count, label, dpi, jpg_quality):
            """渲染指定页面范围，返回 image_url 列表"""
            nonlocal total_img_kb
            parts = []
            end = min(start_page + count, len(doc))
            for i in range(start_page, end):
                if _is_cancelled(cancel_event):
                    return parts
                try:
                    page = doc[i]
                    # 计算缩放矩阵，确保不超像素上限
                    page_w = page.rect.width
                    page_h = page.rect.height
                    zoom = dpi / 72.0
                    render_w = int(page_w * zoom)
                    render_h = int(page_h * zoom)
                    render_px = render_w * render_h

                    if render_px > self.MAX_IMAGE_PX:
                        scale = (self.MAX_IMAGE_PX / render_px) ** 0.5
                        zoom *= scale
                        logger.debug(
                            f"  {label}第{i+1}页缩放至{int(render_w*scale)}×"
                            f"{int(render_h*scale)}（{render_px}→{self.MAX_IMAGE_PX}px）"
                        )

                    pix = page.get_pixmap(
                        matrix=fitz.Matrix(zoom, zoom), colorspace="rgb"
                    )
                    jpg_bytes = pix.tobytes("jpeg", jpg_quality)
                    img_kb = len(jpg_bytes) / 1024

                    # 二次压缩
                    if img_kb > self.MAX_SINGLE_IMAGE_KB:
                        pil_img = Image.open(BytesIO(jpg_bytes))
                        w, h = pil_img.size
                        if max(w, h) > 2000:
                            s = 2000 / max(w, h)
                            pil_img = pil_img.resize(
                                (int(w * s), int(h * s)), Image.LANCZOS
                            )
                        buf = BytesIO()
                        pil_img.save(buf, format="JPEG", quality=50, optimize=True)
                        jpg_bytes = buf.getvalue()
                        logger.info(
                            f"  {label}第{i+1}页二次压缩："
                            f"{img_kb:.0f}KB→{len(jpg_bytes)/1024:.0f}KB"
                        )
                        img_kb = len(jpg_bytes) / 1024

                    b64 = base64.b64encode(jpg_bytes).decode("utf-8")
                    total_img_kb += len(jpg_bytes) / 1024
                    parts.append({
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{b64}",
                            "detail": "high",
                        },
                    })
                except Exception as e:
                    logger.warning(f"渲染{label}第{i+1}页失败：{e}")
            return parts

        # ---- 投标文件：第1页（封面/报价）+ 最后1页（签章页） ----
        bid_doc = fitz.open(stream=resp_pdf_bytes, filetype="pdf")
        bid_pages = len(bid_doc)
        image_parts += _render_pages(
            bid_doc, 0, 1, "投标(首页)",
            self.DPI_KEY_PAGES, self.JPEG_QUALITY,
        )
        if bid_pages > 1:
            image_parts += _render_pages(
                bid_doc, bid_pages - 1, 1, "投标(签章页)",
                self.DPI_KEY_PAGES, self.JPEG_QUALITY,
            )
        bid_doc.close()

        # ---- 招标文件：第1页（招标要求通常在开头） ----
        inv_doc = fitz.open(stream=inv_pdf_bytes, filetype="pdf")
        inv_pages = len(inv_doc)
        image_parts += _render_pages(
            inv_doc, 0, 1, "招标",
            self.DPI_REF_PAGES, self.JPEG_QUALITY,
        )
        inv_doc.close()

        # ---- 自定义规则证据页：渲染证据所在的投标文件页面 ----
        if extra_pages:
            bid_doc2 = fitz.open(stream=resp_pdf_bytes, filetype="pdf")
            for pg in extra_pages:
                pg_idx = pg - 1  # 1-indexed → 0-indexed
                if 0 <= pg_idx < len(bid_doc2):
                    image_parts += _render_pages(
                        bid_doc2, pg_idx, 1, f"投标(证据第{pg}页)",
                        self.DPI_KEY_PAGES, self.JPEG_QUALITY,
                    )
            bid_doc2.close()

        if _is_cancelled(cancel_event):
            return None

        if not image_parts:
            logger.error("未能渲染任何页面")
            return None

        total_mb = total_img_kb / 1024
        logger.info(
            f"渲染完成：{len(image_parts)}张JPEG，"
            f"总计{total_img_kb:.0f}KB（{total_mb:.1f}MB）"
        )

        if progress_callback:
            progress_callback(
                0.35,
                f"已渲染{len(image_parts)}张关键页（{total_mb:.1f}MB），调用AI视觉分析..."
            )

        # ---- 组装 payload ----
        user_content = image_parts + [{"type": "text", "text": user_prompt}]
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": 16384,
        }

        # 精确请求体大小 + 文本 token 估算
        payload_json = json.dumps(payload, ensure_ascii=False)
        payload_mb = len(payload_json) / (1024 * 1024)
        text_chars = len(system_prompt) + len(user_prompt)
        est_text_tokens = text_chars // 2  # 中英文混合粗略估算
        logger.info(
            f"📏 请求体={payload_mb:.2f}MB（上限{self.MAX_PAYLOAD_ESTIMATE_MB}MB）| "
            f"图片={len(image_parts)}张/{total_img_kb:.0f}KB | "
            f"文本={text_chars}字符(~{est_text_tokens}token) | "
            f"模型={self.model}"
        )

        if payload_mb > self.MAX_PAYLOAD_ESTIMATE_MB:
            logger.warning(
                f"请求体 {payload_mb:.1f}MB 超过 {self.MAX_PAYLOAD_ESTIMATE_MB}MB 上限，"
                "LLM API 可能返回 413 错误"
            )

        # ---- API 调用（流式获取 TTFB） ----
        for attempt in range(self.max_retries):
            if _is_cancelled(cancel_event):
                logger.info("用户取消审核")
                return None
            try:
                if progress_callback:
                    progress_callback(
                        0.40 + 0.30 * (attempt / max(self.max_retries, 1)),
                        f"AI视觉分析中（{len(image_parts)}张图，第{attempt+1}次，TTFB等待中）..."
                    )

                t_req = time.time()
                resp = requests.post(
                    self.api_url, headers=headers, data=payload_json,
                    timeout=(30, self.timeout),  # (连接超时, 读取超时)
                    stream=True,
                )
                ttfb = time.time() - t_req
                logger.info(f"⏱ TTFB={ttfb:.1f}s | HTTP {resp.status_code}")

                if resp.status_code == 200:
                    # 流式读取响应体
                    t_dl_start = time.time()
                    chunks = []
                    for chunk in resp.iter_content(chunk_size=8192):
                        if _is_cancelled(cancel_event):
                            resp.close()
                            logger.info("用户在下载响应时取消")
                            return None
                        chunks.append(chunk)
                    dl_time = time.time() - t_dl_start
                    total_time = time.time() - t_req

                    raw = b"".join(chunks).decode("utf-8")
                    data = json.loads(raw)
                    content = data["choices"][0]["message"]["content"]
                    usage = data.get("usage", {})
                    logger.info(
                        f"✅ API成功 | 总耗时={total_time:.1f}s (TTFB={ttfb:.1f}s, "
                        f"下载={dl_time:.1f}s) | prompt_token={usage.get('prompt_tokens', '?')} "
                        f"completion_token={usage.get('completion_tokens', '?')} | "
                        f"回复={len(content)}字符"
                    )
                    return content

                # 非 200 响应
                err_body = b"".join(resp.iter_content(chunk_size=8192)).decode("utf-8", errors="replace")[:500]
                logger.warning(
                    f"❌ API失败（{attempt+1}）HTTP {resp.status_code} "
                    f"TTFB={ttfb:.1f}s：{err_body}"
                )
                if resp.status_code in (400, 413):
                    break
                if resp.status_code == 429:
                    time.sleep((attempt + 1) * 15)
                    continue
                time.sleep(3)

            except requests.exceptions.ConnectionError as e:
                elapsed = time.time() - t_req if 't_req' in dir() else 0
                logger.error(f"🔌 连接失败（{attempt+1}）{elapsed:.0f}s：{e}")
                time.sleep(5)
            except requests.exceptions.Timeout:
                elapsed = time.time() - t_req if 't_req' in dir() else 0
                logger.error(
                    f"⏰ API超时（{attempt+1}）已等待{elapsed:.0f}s "
                    f"(超时上限={self.timeout}s, 模型={self.model})"
                )
                if attempt < min(self.max_retries, 2) - 1:
                    logger.info(f"  准备重试（{attempt+2}/{min(self.max_retries, 2)}）...")
                time.sleep(5)
            except requests.exceptions.RequestException as e:
                elapsed = time.time() - t_req if 't_req' in dir() else 0
                logger.error(f"🌐 网络错误（{attempt+1}）{elapsed:.0f}s：{e}")
                time.sleep(3)

        return None

    # ================================================================
    # 结果解析
    # ================================================================

    @staticmethod
    def _parse_section(raw_text: str, section: str) -> str:
        """从 LLM 输出中提取指定 section 的原始文本"""
        markers = "VIOLATIONS|REPORT|EXTRACTION|CUSTOM_RULES_CHECK|END"
        pattern = rf'---{section}---\s*([\s\S]*?)\s*---(?:{markers})---'
        m = re.search(pattern, raw_text)
        if m:
            return m.group(1).strip()
        # 宽松匹配
        parts = raw_text.split(f"---{section}---")
        if len(parts) > 1:
            rest = parts[1]
            for end_marker in [
                "---VIOLATIONS---", "---REPORT---", "---EXTRACTION---",
                "---CUSTOM_RULES_CHECK---", "---END---",
            ]:
                if end_marker in rest:
                    return rest.split(end_marker)[0].strip()
            return rest.strip()
        return ""

    @staticmethod
    def _parse_json(json_str: str, default=None):
        """健壮的 JSON 解析：处理常见 LLM 输出问题"""
        if not json_str or not json_str.strip():
            return default
        text = json_str.strip()

        # 移除 markdown 代码块
        text = re.sub(r'^```(?:json)?\s*\n?', '', text, flags=re.MULTILINE)
        text = re.sub(r'\n?\s*```\s*$', '', text, flags=re.MULTILINE)

        # 尝试直接解析
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 尝试提取括号范围
        if isinstance(default, dict):
            start, end = text.find('{'), text.rfind('}')
            if start >= 0 and end > start:
                try:
                    return json.loads(text[start:end+1])
                except json.JSONDecodeError:
                    pass
        elif isinstance(default, list):
            start, end = text.find('['), text.rfind(']')
            if start >= 0 and end > start:
                try:
                    return json.loads(text[start:end+1])
                except json.JSONDecodeError:
                    pass

        # 尝试修复常见错误：尾部逗号、单引号
        try:
            fixed = re.sub(r',\s*}', '}', text)
            fixed = re.sub(r',\s*]', ']', fixed)
            return json.loads(fixed)
        except json.JSONDecodeError:
            pass

        # ---- 抢救截断的 JSON 数组（max_tokens 不够时常见） ----
        if isinstance(default, list) and text.strip().startswith('['):
            # 策略：逐行匹配完整的 {...} 对象
            objs = []
            depth = 0
            start_idx = -1
            for i, ch in enumerate(text):
                if ch == '{':
                    if depth == 0:
                        start_idx = i
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0 and start_idx >= 0:
                        obj_str = text[start_idx:i+1]
                        try:
                            obj = json.loads(obj_str)
                            objs.append(obj)
                        except json.JSONDecodeError:
                            pass
                        start_idx = -1
            if objs:
                logger.warning(
                    f"JSON数组被截断，已抢救 {len(objs)} 个完整对象 "
                    f"（丢弃 {1 if depth > 0 else 0} 个不完整对象）"
                )
                return objs

        logger.warning(f"JSON解析失败，原文长度{len(text)}字符，前50字符：{text[:50]}")
        return default

    def _extract_report(self, raw_text: str) -> str:
        """提取 Markdown 审核报告"""
        m = re.search(r'---REPORT---\s*([\s\S]*?)\s*---END---', raw_text)
        if m:
            return m.group(1).strip()
        parts = raw_text.split("---REPORT---")
        if len(parts) > 1:
            return parts[-1].replace("---END---", "").strip()
        return raw_text.strip()

    @staticmethod
    def _parse_custom_rules_check(raw_json: str, custom_rules_text: str) -> Tuple[list, list, list]:
        """
        解析 ---CUSTOM_RULES_CHECK--- JSON，将每条判定转为
        violation（violated）或 compliant_item（compliant/uncertain）。

        返回: (violations, compliant_items, warnings)
        """
        data = LLMEngine._parse_json(raw_json, default=None)
        if not isinstance(data, list) or len(data) == 0:
            return [], [], []

        # 构建 rule_index → rule_text 映射
        rule_lines = [r.strip() for r in custom_rules_text.strip().split("\n") if r.strip()]
        rule_map = {}
        for i, line in enumerate(rule_lines):
            rule_map[i + 1] = line  # 1-indexed

        violations = []
        compliant_items = []
        warnings = []

        for item in data:
            if not isinstance(item, dict):
                continue
            idx = item.get("rule_index", -1)
            verdict = str(item.get("verdict", "")).lower().strip()
            evidence = str(item.get("evidence", ""))[:500]
            rule_text = rule_map.get(idx, f"自定义规则#{idx}")

            if verdict == "violated":
                violations.append({
                    "violation_id": f"CR-{idx:02d}",
                    "category": "自定义规则违规",
                    "severity": "中",
                    "problem_summary": f"自定义规则违规：{rule_text[:80]}",
                    "requirement_detail": rule_text,
                    "violation_reason": evidence,
                    "fix_suggestion": f"请根据规则要求修改：{rule_text}",
                    "source": {
                        "page_display": None, "page_num": None,
                        "keyword": rule_text[:30],
                        "requirement_text": rule_text,
                        "screenshot_bytes": None, "bbox": None,
                    },
                    "evidence": {
                        "page_display": None,
                        "found_text": evidence,
                        "screenshot_bytes": None, "bbox": None,
                    },
                    "compliant": False,
                })
            elif verdict == "compliant":
                compliant_items.append({
                    "violation_id": f"CR-{idx:02d}",
                    "category": "自定义规则合规",
                    "severity": "低",
                    "problem_summary": f"✅ 自定义规则合规：{rule_text[:80]}",
                    "requirement_detail": rule_text,
                    "violation_reason": evidence,
                    "fix_suggestion": "",
                    "source": {
                        "page_display": None, "page_num": None,
                        "keyword": rule_text[:30],
                        "requirement_text": rule_text,
                        "screenshot_bytes": None, "bbox": None,
                    },
                    "evidence": {
                        "page_display": None,
                        "found_text": evidence,
                        "screenshot_bytes": None, "bbox": None,
                    },
                    "compliant": True,
                })
            elif verdict == "uncertain":
                compliant_items.append({
                    "violation_id": f"CR-{idx:02d}",
                    "category": "自定义规则存疑",
                    "severity": "低",
                    "problem_summary": f"⚠️ 无法判断：{rule_text[:80]}",
                    "requirement_detail": rule_text,
                    "violation_reason": evidence or "提供的材料无法判断该规则是否满足",
                    "fix_suggestion": "建议人工核实",
                    "source": {
                        "page_display": None, "page_num": None,
                        "keyword": rule_text[:30],
                        "requirement_text": rule_text,
                        "screenshot_bytes": None, "bbox": None,
                    },
                    "evidence": {
                        "page_display": None,
                        "found_text": evidence,
                        "screenshot_bytes": None, "bbox": None,
                    },
                    "compliant": True,
                })
            else:
                warnings.append(f"CUSTOM_RULES_CHECK[{idx}] 无效 verdict: {verdict}")

        return violations, compliant_items, warnings

    def _derive_items_from_extraction(
        self, violations: List[dict], extraction: dict
    ) -> Tuple[List[dict], List[dict]]:
        """
        从 extraction 中派生签名/印章相关的违规/合规项
        """
        compliant_items = []

        # 法定代表人签章比对
        legal = extraction.get("legal_rep_check", {})
        if legal:
            declared = legal.get("declared_name", "?")
            seal_name = legal.get("seal_name", "?")
            if legal.get("match") is False:
                violations.append({
                    "violation_id": "LLM-SEAL-001",
                    "category": "签章问题",
                    "severity": "高",
                    "source": {
                        "page_display": None, "page_num": None,
                        "keyword": "法定代表人签章",
                        "requirement_text": f"法定代表人应为{declared}",
                        "screenshot_bytes": None, "bbox": None,
                    },
                    "evidence": {
                        "page_display": None,
                        "found_text": f"印章：{seal_name}",
                        "screenshot_bytes": None, "bbox": None,
                    },
                    "problem_summary": "法定代表人姓名与印章不一致",
                    "requirement_detail": f"声明法定代表人为「{declared}」，印章为「{seal_name}」",
                    "violation_reason": legal.get("detail", "姓名不匹配"),
                    "fix_suggestion": "请确认法定代表人信息并更换正确印章",
                    "compliant": False,
                })
            elif legal.get("match") is True:
                compliant_items.append({
                    "violation_id": "LLM-SEAL-C001",
                    "category": "签章合规",
                    "severity": "低",
                    "source": {"page_display": None, "page_num": None,
                               "keyword": "法定代表人签章",
                               "requirement_text": "签章与声明一致",
                               "screenshot_bytes": None, "bbox": None},
                    "evidence": {"page_display": None,
                                 "found_text": f"印章：{seal_name}",
                                 "screenshot_bytes": None, "bbox": None},
                    "problem_summary": "法定代表人签章与声明一致",
                    "requirement_detail": "",
                    "violation_reason": "",
                    "fix_suggestion": "",
                    "compliant": True,
                })

        # 印章数量检查（通常投标文件至少应有公章）
        seals = extraction.get("seals", [])
        if isinstance(seals, list) and len(seals) == 0:
            violations.append({
                "violation_id": "LLM-SEAL-002",
                "category": "签章问题",
                "severity": "高",
                "source": {"page_display": None, "page_num": None,
                           "keyword": "公章", "requirement_text": "投标文件应加盖公章",
                           "screenshot_bytes": None, "bbox": None},
                "evidence": {"page_display": None, "found_text": "",
                             "screenshot_bytes": None, "bbox": None},
                "problem_summary": "投标文件中未检测到任何印章",
                "requirement_detail": "根据招投标法规，投标文件须加盖公章",
                "violation_reason": "LLM在文档中未检测到印章",
                "fix_suggestion": "请确认投标文件是否已加盖公章",
                "compliant": False,
            })

        return violations, compliant_items

    def _build_summary(self, violations: List[dict], compliant_items: List[dict]) -> dict:
        total = len(violations) + len(compliant_items)
        compliance_rate = len(compliant_items) / max(total, 1)

        sev = {"高": 0, "中": 0, "低": 0}
        cats = {}
        for v in violations:
            s = v.get("severity", "低")
            sev[s] = sev.get(s, 0) + 1
            c = v.get("category", "未知")
            cats[c] = cats.get(c, 0) + 1

        return {
            "total_violations": len(violations),
            "total_compliant": len(compliant_items),
            "total_items_checked": total,
            "compliance_rate": compliance_rate,
            "violations_by_severity": sev,
            "violations_by_category": cats,
        }

    # ============================================================
    # 违规截图（传统工具辅助）
    # ============================================================

    @staticmethod
    def annotate_violation_screenshots(
        violations: List[dict],
        pdf_bytes: bytes,
        dpi: int = 150,
        progress_callback=None,
        cancel_event=None,
    ) -> List[dict]:
        """为违规项生成红框标注的页面截图（混合定位：关键词搜索优先 + LLM坐标兜底）"""
        try:
            import fitz
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            page_count = len(doc)
            zoom = dpi / 72.0
            total = len([v for v in violations if v.get("evidence", {}).get("page_display")])
            done = 0

            for v in violations:
                if _is_cancelled(cancel_event):
                    doc.close()
                    logger.info("截图生成被用户取消")
                    return violations

                ev = v.get("evidence", {})
                pn = ev.get("page_display")
                if not pn or pn < 1 or pn > page_count:
                    continue
                try:
                    if progress_callback and total > 0:
                        progress_callback(
                            0.90 + 0.05 * (done / total),
                            f"生成违规截图（{done+1}/{total}，第{pn}页）..."
                        )

                    t0 = time.time()
                    page = doc[pn - 1]
                    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace="rgb")
                    t_pix = time.time()
                    img_bytes = pix.tobytes("jpeg", 80)
                    t_jpg = time.time()
                    img_w, img_h = pix.width, pix.height
                    logger.info(
                        f"📸 第{pn}页渲染：{img_w}×{img_h}px | "
                        f"get_pixmap={t_pix-t0:.1f}s tobytes={t_jpg-t_pix:.1f}s "
                        f"({len(img_bytes)/1024:.0f}KB)"
                    )

                    # ---- 混合定位：关键词搜索 → LLM坐标 → 整页红框 ----
                    keyword = str(ev.get("found_text", "")).strip()
                    llm_bbox = ev.get("bbox")
                    boxes_to_draw = []

                    # 策略1：PyMuPDF 文本搜索（逐级降级）
                    if keyword:
                        pdf_rects = page.search_for(keyword)
                        if pdf_rects:
                            boxes_to_draw = _pdf_rects_to_pixels(pdf_rects, zoom)
                            logger.info(
                                f"  🔍 精确命中「{keyword}」→ {len(pdf_rects)} 处（PyMuPDF）"
                            )
                        else:
                            # 降级1：拆词搜索（LLM常返回描述性短语而非原文字）
                            segments = _split_keyword_for_search(keyword)
                            for seg in segments:
                                if len(seg) < 3:
                                    continue
                                pdf_rects = page.search_for(seg)
                                if pdf_rects:
                                    boxes_to_draw = _pdf_rects_to_pixels(pdf_rects, zoom)
                                    logger.info(
                                        f"  🔍 拆词命中「{seg}」→ {len(pdf_rects)} 处 "
                                        f"（原词「{keyword}」未搜到）"
                                    )
                                    break
                            if not boxes_to_draw:
                                logger.info(
                                    f"  ⚠️ 关键词「{keyword}」及拆词均未搜到，"
                                    f"回退到LLM坐标"
                                )

                    # 策略2：LLM 归一化坐标（视觉元素兜底）
                    if not boxes_to_draw and llm_bbox and len(llm_bbox) == 4:
                        # 归一化 0→1000 → 图片像素
                        x1 = llm_bbox[0] / 1000.0 * img_w
                        y1 = llm_bbox[1] / 1000.0 * img_h
                        x2 = llm_bbox[2] / 1000.0 * img_w
                        y2 = llm_bbox[3] / 1000.0 * img_h
                        # 确保坐标顺序正确
                        if x1 > x2:
                            x1, x2 = x2, x1
                        if y1 > y2:
                            y1, y2 = y2, y1
                        boxes_to_draw.append([x1, y1, x2, y2])
                        logger.info(
                            f"  🎯 LLM坐标定位：归一化{llm_bbox} → "
                            f"像素[{x1:.0f},{y1:.0f},{x2:.0f},{y2:.0f}]"
                        )

                    # 策略3：整页红框（最终兜底）
                    if not boxes_to_draw:
                        logger.info(f"  📄 无定位信息，使用整页红框")
                        boxes_to_draw.append([-3, -3, img_w+3, img_h+3])

                    # ---- 画框到图片上 ----
                    pil_img = Image.open(BytesIO(img_bytes))
                    draw = ImageDraw.Draw(pil_img)
                    for bx in boxes_to_draw:
                        # 画3px宽红框（扩边确保可见）
                        for offset in range(3):
                            draw.rectangle(
                                [bx[0]-offset, bx[1]-offset,
                                 bx[2]+offset, bx[3]+offset],
                                outline="red",
                            )
                    buf = BytesIO()
                    pil_img.save(buf, format="JPEG", quality=85)
                    img_bytes = buf.getvalue()
                    t_draw = time.time()
                    logger.info(
                        f"  画框完成：{len(boxes_to_draw)}个框 "
                        f"（{t_draw-t_jpg:.1f}s，输出{len(img_bytes)/1024:.0f}KB）"
                    )

                    ev["screenshot_bytes"] = img_bytes
                    v["evidence"] = ev
                    done += 1
                except Exception as e:
                    logger.warning(f"截图失败（第{pn}页）：{e}")

            doc.close()
            if total > 0:
                logger.info(f"违规截图完成：{done}/{total} 张")
        except Exception as e:
            logger.warning(f"截图生成失败：{e}")

        return violations

# ============================================================
# 模块级便捷函数（由 annotate_violation_screenshots 使用）
# ============================================================

def verify_pdf_signatures(pdf_bytes: bytes) -> dict:
    """验证 PDF 数字签名（委托给 SigValidator）"""
    from modules.sig_validator import SigValidator
    return SigValidator.check_signatures(pdf_bytes)


def highlight_and_screenshot(
    pdf_bytes: bytes,
    page_num: int,
    bbox: list = None,
    dpi: int = 150,
) -> Optional[bytes]:
    """生成带红框标注的单页截图"""
    try:
        import fitz
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        if page_num < 0 or page_num >= len(doc):
            doc.close()
            return None
        page = doc[page_num]
        zoom = dpi / 72.0
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace="rgb")
        img_bytes = pix.tobytes("jpeg", 80)
        doc.close()
        if bbox:
            img_bytes = LLMEngine._draw_red_boxes(img_bytes, [bbox])
        return img_bytes
    except Exception as e:
        logger.warning(f"highlight_and_screenshot 失败：{e}")
        return None
