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
import pickle
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


# ============================================================
# JSON 校验工具 — 确保 LLM 输出格式正确
# ============================================================

# 合法的严重度取值
VALID_SEVERITIES = {"高", "中", "低"}

# 合法的违规类别
VALID_CATEGORIES = {"内容缺失", "格式不符", "签章问题", "条款不响应",
                    "填写错误", "自定义规则违规"}

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

    @staticmethod
    def _compute_cache_key(inv_bytes: bytes, resp_bytes: bytes, custom_rules: str) -> str:
        """计算审核结果的缓存键（SHA-256）"""
        h = hashlib.sha256()
        h.update(inv_bytes)
        h.update(b"||INV_RESP_SEPARATOR||")
        h.update(resp_bytes)
        h.update(custom_rules.encode("utf-8"))
        return h.hexdigest()

    def _get_cached_result(self, cache_key: str) -> Optional[dict]:
        """检查缓存，命中则返回完整审核结果"""
        cache_file = _CACHE_DIR / f"{cache_key}.pkl"
        if not cache_file.exists():
            return None
        try:
            with open(cache_file, "rb") as f:
                data = pickle.load(f)
            age = time.time() - data.get("_cache_timestamp", 0)
            logger.info(f"💾 缓存命中：{cache_key[:12]}...（{age/3600:.1f}小时前）")
            return data
        except Exception as e:
            logger.warning(f"缓存读取失败：{e}")
            return None

    def _save_cached_result(self, cache_key: str, result: dict):
        """保存审核结果到缓存"""
        cache_file = _CACHE_DIR / f"{cache_key}.pkl"
        try:
            result["_cache_timestamp"] = time.time()
            result["_cached_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(cache_file, "wb") as f:
                pickle.dump(result, f)
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
                return cached

        if _is_cancelled(cancel_event):
            return self._error_response("审核已被用户取消")

        if progress_callback:
            progress_callback(0.05, "准备审核请求...")

        # ---- 阶段1：构建 Prompt + 渲染 PDF 图片 ----
        if progress_callback:
            progress_callback(0.25, "组装AI审核提示词...")

        system_prompt = self._build_system_prompt()
        user_prompt = self._build_user_prompt(inv_text, resp_text, custom_rules)

        if _is_cancelled(cancel_event):
            return self._error_response("审核已被用户取消")

        if progress_callback:
            progress_callback(0.30, "渲染文档页面并调用视觉大模型（预计2-5分钟）...")

        raw_response = self._call_vision_api(
            system_prompt, user_prompt,
            invitation_pdf_bytes, response_pdf_bytes,
            progress_callback, cancel_event,
        )

        if raw_response is None:
            return self._error_response("LLM API 调用失败，请检查网络和 API 配置")

        # ---- 阶段3：解析 + 校验 + 修复 ----
        if progress_callback:
            progress_callback(0.75, "解析并校验AI审核结果...")

        extraction_raw = self._parse_section(raw_response, "EXTRACTION")
        violations_raw = self._parse_section(raw_response, "VIOLATIONS")
        ai_report = self._extract_report(raw_response)

        # JSON 解析
        extraction = self._parse_json(extraction_raw, default={})
        violations_data = self._parse_json(violations_raw, default=[])

        # 多层校验
        extraction, ext_warnings = _validate_extraction(extraction)
        violations, vio_warnings = _validate_violations(violations_data)
        all_warnings = ext_warnings + vio_warnings

        # 从 extraction 派生额外的违规/合规项
        violations, compliant_items = self._derive_items_from_extraction(
            violations, extraction
        )

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
            "你是一位拥有15年经验的资深投标顾问，精通中国招投标法规（《招标投标法》《政府采购法》）。\n\n"
            "## 核心能力\n"
            "1. 精确理解招标文件中每一条资质、格式、签章、报价要求\n"
            "2. 深度解析投标文件结构、内容、合规性\n"
            "3. 识别所有印章（公章/法人章/合同章/财务章）的文字内容和位置\n"
            "4. 比对法定代表人声明与印章文字是否一致\n"
            "5. 解析完整章节结构，区分正文/商务/技术部分\n"
            "6. 提取：投标报价、公司名称、信用代码、法定代表人、工期\n\n"
            "## ⚠️ JSON 格式铁律（违反将导致审核失败）\n"
            "- EXTRACTION 和 VIOLATIONS 必须输出**合法的 JSON**\n"
            "- JSON 中不得包含注释（// 或 /* */）\n"
            "- 字符串用双引号，不得用单引号\n"
            "- 数值不加引号，布尔值用 true/false（小写）\n"
            "- 不得在 JSON 外附加解释文字\n"
            "- 如果某字段在文档中找不到，填 null（不是 \"无\" 或 \"未找到\"）\n\n"
            "## 🎯 空间定位（evidence_bbox / evidence_keyword）\n"
            "- evidence_keyword: 【必填，最重要】违规位置附近的**原文短句**（8-30字），从PDF中**逐字抄录**\n"
            "  ❌ 错误：「目录页未显示页码」— 这是你的判断，PDF里没有这句话 → 搜不到\n"
            "  ✅ 正确：「第六章 施工组织设计」— 这是PDF里实际存在的文字 → 能搜到\n"
            "  选词原则：选该页面上只出现一次的独特文本，系统会自动拆词搜索（长→短）\n"
            "- evidence_bbox: 违规区域大致坐标 [x1,y1,x2,y2]，0~1000。不确定就填 null\n"
            "  仅纯视觉元素（印章、签名、涂改）且无法摘录原文时，才依赖 bbox\n\n"
            "## 输出结构（严格按此顺序）\n\n"
            "---EXTRACTION---\n"
            "{\n"
            '  "bid_price": "投标报价金额（含币种单位，如\\"3,580,000.00元\\"），找不到填null",\n'
            '  "company_name": "投标公司全称",\n'
            '  "credit_code": "统一社会信用代码（18位）",\n'
            '  "legal_representative": "法定代表人姓名",\n'
            '  "contact_person": "授权代表/联系人（或null）",\n'
            '  "bid_validity": "投标有效期（如\\"90天\\"，或null）",\n'
            '  "construction_period": "工期/服务期（或null）",\n'
            '  "seals": [\n'
            '    {"page": 1, "type": "公章", "text": "XX有限公司", "position": "右下角"}\n'
            '  ],\n'
            '  "legal_rep_check": {\n'
            '    "declared_name": "声明法定代表人",\n'
            '    "seal_name": "印章姓名",\n'
            '    "match": true,\n'
            '    "detail": "比对详情"\n'
            '  },\n'
            '  "chapter_structure": [\n'
            '    {"level": 1, "title": "一、投标函", "page": 1}\n'
            '  ],\n'
            '  "document_sections": {\n'
            '    "body_pages": "1-5",\n'
            '    "business_pages": "6-15",\n'
            '    "technical_pages": "16-30"\n'
            '  },\n'
            '  "self_check": {\n'
            '    "bid_price_verified": true,\n'
            '    "credit_code_verified": true,\n'
            '    "legal_rep_verified": true,\n'
            '    "notes": "对关键字段的二次确认说明"\n'
            '  }\n'
            '}\n'
            "---VIOLATIONS---\n"
            "[\n"
            '  {"severity":"高","category":"签章问题","problem_summary":"缺少公章","requirement_detail":"招标文件第3条要求投标函须加盖公章","violation_reason":"投标函末页签章处空白，未检测到公章印记","fix_suggestion":"在投标函指定位置加盖公司公章","evidence_page":1,"evidence_bbox":[150,620,850,750],"evidence_keyword":null},\n'
            '  {"severity":"中","category":"条款不响应","problem_summary":"质保期不满足招标要求","requirement_detail":"招标文件要求质保期不少于36个月","violation_reason":"投标文件技术偏离表中质保期填写为12个月","fix_suggestion":"将质保期修改为36个月或提交偏差说明","evidence_page":3,"evidence_bbox":null,"evidence_keyword":"质保期12个月"}\n'
            "]\n"
            "---REPORT---\n"
            "（Markdown格式审核报告）\n"
            "---END---\n\n"
            "## 审核原则\n"
            "- 严格区分实质问题（签章缺失/资质不符/报价错误）与格式瑕疵\n"
            "- 高🔴：签章缺失、资质不符、报价错误、不响应废标条款\n"
            "- 中🟡：格式偏差、表述不规范、缺少非关键附件\n"
            "- 低🟢：排版瑕疵、用词不当\n"
            "- 报告末尾给出结论：「✅ 建议通过」「⚠️ 修改后通过」「❌ 不建议通过」\n"
            "- 【重要】self_check 字段请逐项确认：bid_price/credit_code/legal_rep 确已从文档中准确提取"
        )

    def _build_user_prompt(self, inv_text: str, resp_text: str, custom_rules: str) -> str:
        INV_MAX = 30000
        RESP_MAX = 50000

        inv_trunc = inv_text[:INV_MAX]
        if len(inv_text) > INV_MAX:
            inv_trunc += f"\n\n[招标文件全文共{len(inv_text)}字符，以上为前{INV_MAX}字符]"

        resp_trunc = resp_text[:RESP_MAX]
        if len(resp_text) > RESP_MAX:
            resp_trunc += f"\n\n[投标文件全文共{len(resp_text)}字符，以上为前{RESP_MAX}字符]"

        rules_block = ""
        if custom_rules.strip():
            rules_block = (
                "\n\n---\n\n## ⚠️ 自定义审核规则（逐条检查，违规标注 [自定义规则]）\n\n"
                f"{custom_rules.strip()}\n"
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
        )

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
                    except Exception:
                        # 单张图片处理失败不影响其他图片
                        continue
            except Exception:
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
    ) -> Optional[str]:
        """
        渲染 PDF 关键页为 JPEG → 以 image_url 上传到 Qwen3-VL。
        这是唯一有效的 API 调用路径（PDF 直传不被 SiliconFlow 支持）。
        """
        import fitz

        logger.info("渲染PDF关键页为JPEG图片...")
        image_parts = []
        total_img_kb = 0

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

        # ---- API 调用（流式获取 TTFB） ----
        for attempt in range(min(self.max_retries, 2)):
            if _is_cancelled(cancel_event):
                logger.info("用户取消审核")
                return None
            try:
                if progress_callback:
                    progress_callback(
                        0.40 + 0.30 * (attempt / min(self.max_retries, 2)),
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
        pattern = rf'---{section}---\s*([\s\S]*?)\s*---(?:VIOLATIONS|REPORT|EXTRACTION|END)---'
        m = re.search(pattern, raw_text)
        if m:
            return m.group(1).strip()
        # 宽松匹配
        parts = raw_text.split(f"---{section}---")
        if len(parts) > 1:
            rest = parts[1]
            for end_marker in ["---VIOLATIONS---", "---REPORT---", "---EXTRACTION---", "---END---"]:
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

        logger.warning(f"JSON解析失败，原文前200字符：{text[:200]}")
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

    @staticmethod
    def _draw_red_boxes(img_bytes: bytes, boxes: list, width: int = 3) -> bytes:
        """图片上画红框"""
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
        except Exception:
            return img_bytes


# ============================================================
# 模块级便捷函数
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
