"""
交叉审核违规检测模块
==================
功能：对比招标文件要求与投标文件实际内容，检测不合规项
      覆盖签章位置校验、关键字合规检查、违规分类与严重度评定
"""

import logging
from typing import List, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# 违规分类
CATEGORY_MISSING = "内容缺失"       # 招标要求的内容在投标文件中完全找不到
CATEGORY_FORMAT = "格式不符"        # 内容存在但格式不符合要求
CATEGORY_SEAL_POS = "签章位置错误"  # 签字/盖章位置不规范
CATEGORY_NO_RESP = "条款不响应"     # 对招标条款未做出明确响应
CATEGORY_ERROR = "填写错误"         # 内容存在但填写有误

# 严重度
SEVERITY_HIGH = "高"
SEVERITY_MID = "中"
SEVERITY_LOW = "低"

# 签章相关关键字（用于识别招标文件中的签章要求）
SEAL_SIGN_KEYWORDS = [
    "法定代表人签字", "法定代表人盖章", "授权代表", "负责人签字",
    "公章", "签字", "盖章",
]

# 内容合规关键字分组（招标要求 → 投标文件中应出现的对应内容）
COMPLIANCE_GROUPS = {
    "资格资质": ["投标人资格", "资质", "资格要求", "营业执照", "资质证书", "许可证"],
    "报价金额": ["报价", "金额", "价格", "大写", "小写", "总价", "单价"],
    "工期进度": ["工期", "进度", "交付", "完工", "日历天", "工作日"],
    "承诺保证": ["承诺", "保证", "担保", "履约", "质量保证"],
    "格式日期": ["日期", "格式", "签字", "盖章", "装订", "密封"],
    "授权委托": ["授权", "委托", "法定代表人", "授权代表", "代理人"],
}


class ViolationChecker:
    """
    交叉审核违规检测器
    -----------------
    将招标文件中的规则要求与投标文件的实际内容逐条比对，
    检测签章缺失、内容遗漏、格式错误等问题，输出结构化违规清单。

    使用示例：
        checker = ViolationChecker()
        sig_violations = checker.check_signatures_and_seals(inv_pages, resp_checklist)
        content_violations = checker.check_content_compliance(inv_pages, resp_text, resp_ocr)
        summary = checker.build_summary()
    """

    def __init__(self):
        self._violations: List[Dict] = []
        self._compliant_items: List[Dict] = []
        self._counter = 0  # 违规编号自增

    def _next_id(self) -> str:
        self._counter += 1
        return f"V-{self._counter:03d}"

    # ================================================================
    # 签章/签字位置检测
    # ================================================================

    def check_signatures_and_seals(
        self,
        inv_pages: List[Dict],
        resp_position_checklist: List[Dict],
    ) -> List[Dict]:
        """
        核对招标文件中的签章要求 vs 投标文件中实际签章/签字检测结果

        参数:
            inv_pages:             招标文件重点页列表（PageFilter.filter_pages 输出）
            resp_position_checklist: 投标文件位置校验清单（CoordinateChecker.check_positions 输出）

        返回:
            List[Dict]: 签章相关违规项列表
        """
        violations = []

        # 从招标文件重点页中提取签章要求
        seal_requirements = []
        for inv_page in inv_pages:
            page_text = inv_page.get("ocr_text", "")
            page_ocr = inv_page.get("ocr_results", [])
            for kw in SEAL_SIGN_KEYWORDS:
                if kw in page_text:
                    # 在OCR结果中查找关键字的bbox用于截图裁剪
                    kw_bbox = None
                    for ocr_item in page_ocr:
                        if kw in ocr_item.get("text", ""):
                            kw_bbox = ocr_item.get("bbox")
                            break
                    seal_requirements.append({
                        "keyword": kw,
                        "page_display": inv_page.get("page_display", 0),
                        "page_num": inv_page.get("page_num", 0),
                        "context": self._extract_context(page_text, kw),
                        "bbox": kw_bbox,
                    })

        if not seal_requirements:
            logger.info("招标文件中未发现签章相关要求")
            return violations

        # 逐条核对
        for req in seal_requirements:
            kw = req["keyword"]

            # 在位置校验清单中查找对应项
            checklist_item = None
            for item in resp_position_checklist:
                if item.get("keyword") == kw or kw in item.get("keyword", ""):
                    checklist_item = item
                    break

            if checklist_item is None:
                # 清单中没有对应项，跳过
                continue

            if checklist_item.get("found"):
                # 签章已检测到 → 合规
                self._compliant_items.append({
                    "violation_id": self._next_id(),
                    "category": "签章合规",
                    "severity": SEVERITY_LOW,
                    "source": {
                        "page_display": req["page_display"],
                        "page_num": req["page_num"],
                        "keyword": kw,
                        "requirement_text": req["context"],
                        "screenshot_bytes": None,
                        "bbox": req.get("bbox"),
                    },
                    "evidence": {
                        "page_display": checklist_item.get("page_display", "?"),
                        "found_text": checklist_item.get("text", ""),
                        "screenshot_bytes": None,
                        "bbox": checklist_item.get("bbox"),
                    },
                    "problem_summary": f"「{kw}」已按要求签署",
                    "requirement_detail": req["context"],
                    "violation_reason": "",
                    "fix_suggestion": "",
                    "compliant": True,
                })
            else:
                # 签章缺失 → 违规
                severity = SEVERITY_MID
                if kw in ("法定代表人签字", "公章"):
                    severity = SEVERITY_HIGH

                violation = {
                    "violation_id": self._next_id(),
                    "category": CATEGORY_SEAL_POS,
                    "severity": severity,
                    "source": {
                        "page_display": req["page_display"],
                        "page_num": req["page_num"],
                        "keyword": kw,
                        "requirement_text": req["context"],
                        "screenshot_bytes": None,
                        "bbox": req.get("bbox"),
                    },
                    "evidence": {
                        "page_display": checklist_item.get("page_display", "?"),
                        "found_text": checklist_item.get("text", ""),
                        "screenshot_bytes": None,
                        "bbox": None,
                    },
                    "problem_summary": f"缺少「{kw}」或签章位置不规范",
                    "requirement_detail": f"招标文件第{req['page_display']}页要求：{req['context']}",
                    "violation_reason": checklist_item.get("status", "未检测到"),
                    "fix_suggestion": checklist_item.get("suggestion", f"请在指定位置补充「{kw}」"),
                    "compliant": False,
                }
                violations.append(violation)

        logger.info(
            f"签章检查完成：违规 {len(violations)} 项，合规 {len([c for c in self._compliant_items if c['category'] == '签章合规'])} 项"
        )
        self._violations.extend(violations)
        return violations

    # ================================================================
    # 内容合规检测
    # ================================================================

    def check_content_compliance(
        self,
        inv_pages: List[Dict],
        resp_full_text: str,
        resp_ocr_results: List[Dict],
    ) -> List[Dict]:
        """
        核对招标文件中的资格/资质/格式等要求 vs 投标文件中的实际内容

        参数:
            inv_pages:         招标文件重点页列表
            resp_full_text:    投标文件全文（PyMuPDF提取）
            resp_ocr_results:  投标文件全部OCR结果

        返回:
            List[Dict]: 内容相关违规项列表
        """
        violations = []

        # 对所有OCR文本拼接为可搜索字符串
        resp_ocr_text = " ".join(
            item.get("text", "") for item in resp_ocr_results
        )
        combined_resp_text = f"{resp_full_text} {resp_ocr_text}"

        # 从招标文件重点页提取各类要求
        for inv_page in inv_pages:
            page_text = inv_page.get("ocr_text", "")
            page_display = inv_page.get("page_display", 0)

            for group_name, group_keywords in COMPLIANCE_GROUPS.items():
                for gk in group_keywords:
                    if gk in page_text:
                        # 检查投标文件中是否有相应内容
                        context = self._extract_context(page_text, gk)
                        found = self._check_text_presence(gk, combined_resp_text)

                        if not found:
                            severity = (
                                SEVERITY_HIGH if group_name in ("资格资质", "承诺保证")
                                else SEVERITY_MID
                            )
                            # 在OCR结果中查找关键字的bbox用于截图裁剪
                            kw_bbox = None
                            for ocr_item in inv_page.get("ocr_results", []):
                                if gk in ocr_item.get("text", ""):
                                    kw_bbox = ocr_item.get("bbox")
                                    break
                            violation = {
                                "violation_id": self._next_id(),
                                "category": CATEGORY_MISSING,
                                "severity": severity,
                                "source": {
                                    "page_display": page_display,
                                    "page_num": inv_page.get("page_num", 0),
                                    "keyword": gk,
                                    "requirement_text": context,
                                    "screenshot_bytes": None,
                                    "bbox": kw_bbox,
                                },
                                "evidence": {
                                    "page_display": None,
                                    "found_text": "",
                                    "screenshot_bytes": None,
                                    "bbox": None,
                                },
                                "problem_summary": f"招标文件要求的「{gk}」相关内容在投标文件中未找到",
                                "requirement_detail": f"招标文件第{page_display}页：{context}",
                                "violation_reason": f"投标文件中未检索到与「{gk}」相关的内容",
                                "fix_suggestion": f"请补充与「{gk}」相关的{group_name}内容",
                                "compliant": False,
                            }
                            violations.append(violation)
                        else:
                            # 合规：在投标文件中找到了对应内容
                            # 查找证据在投标文件中的位置
                            ev_pd = None
                            ev_bbox = None
                            ev_text = f"已检索到与「{gk}」相关的内容"
                            for item in resp_ocr_results:
                                if gk in item.get("text", ""):
                                    ev_pd = item.get("page_display")
                                    ev_bbox = item.get("bbox")
                                    ev_text = item.get("text", "")
                                    break
                            # 在OCR结果中查找关键字的bbox用于截图裁剪
                            kw_bbox2 = None
                            for ocr_item in inv_page.get("ocr_results", []):
                                if gk in ocr_item.get("text", ""):
                                    kw_bbox2 = ocr_item.get("bbox")
                                    break
                            self._compliant_items.append({
                                "violation_id": self._next_id(),
                                "category": group_name,
                                "severity": SEVERITY_LOW,
                                "source": {
                                    "page_display": page_display,
                                    "page_num": inv_page.get("page_num", 0),
                                    "keyword": gk,
                                    "requirement_text": context,
                                    "screenshot_bytes": None,
                                    "bbox": kw_bbox2,
                                },
                                "evidence": {
                                    "page_display": ev_pd,
                                    "found_text": ev_text,
                                    "screenshot_bytes": None,
                                    "bbox": ev_bbox,
                                },
                                "problem_summary": f"「{gk}」相关内容已在投标文件中找到",
                                "requirement_detail": context,
                                "violation_reason": "",
                                "fix_suggestion": "",
                                "compliant": True,
                            })

                        break  # 每组只取第一个命中的关键字，避免重复

        logger.info(
            f"内容合规检查完成：违规 {len(violations)} 项"
        )
        self._violations.extend(violations)
        return violations

    # ================================================================
    # 实用方法
    # ================================================================

    @staticmethod
    def _extract_context(text: str, keyword: str, window: int = 60) -> str:
        """提取关键字周围上下文文本"""
        idx = text.find(keyword)
        if idx == -1:
            return ""
        start = max(0, idx - window // 2)
        end = min(len(text), idx + len(keyword) + window // 2)
        snippet = text[start:end].replace("\n", " ")
        if start > 0:
            snippet = "…" + snippet
        if end < len(text):
            snippet = snippet + "…"
        return snippet.strip()

    @staticmethod
    def _check_text_presence(keyword: str, full_text: str) -> bool:
        """
        检查关键字是否在全文中有对应内容（简单子串匹配）
        对于复合关键词（如"投标人资格"），也尝试拆分匹配
        """
        if keyword in full_text:
            return True
        # 对2字以上的关键词，尝试拆分为2-gram模糊匹配
        if len(keyword) >= 4:
            # 取前两个字、后两个字分别匹配
            part1 = keyword[:2]
            part2 = keyword[-2:]
            if part1 in full_text or part2 in full_text:
                return True
        return False

    # ================================================================
    # 违规分类与严重度
    # ================================================================

    @staticmethod
    def classify_violation(
        inv_requirement: str,
        resp_found: str,
        category_hint: str = "",
    ) -> Tuple[str, str]:
        """
        根据招标要求和投标实际内容判定违规类别和严重度

        返回:
            Tuple[str, str]: (类别, 严重度)
        """
        if category_hint == CATEGORY_SEAL_POS:
            return (CATEGORY_SEAL_POS, SEVERITY_HIGH)

        if not resp_found or not resp_found.strip():
            return (CATEGORY_MISSING, SEVERITY_HIGH)

        # 规则化判定
        if any(kw in inv_requirement for kw in ("签字", "盖章", "公章")):
            return (CATEGORY_SEAL_POS, SEVERITY_MID)
        if any(kw in inv_requirement for kw in ("格式", "装订", "密封", "排版")):
            return (CATEGORY_FORMAT, SEVERITY_MID)
        if any(kw in inv_requirement for kw in ("日期", "金额", "报价", "大写", "小写")):
            return (CATEGORY_ERROR, SEVERITY_MID)
        if any(kw in inv_requirement for kw in ("响应", "承诺", "声明", "确认")):
            return (CATEGORY_NO_RESP, SEVERITY_HIGH)

        return (CATEGORY_MISSING, SEVERITY_MID)

    # ================================================================
    # 汇总统计
    # ================================================================

    def get_all_violations(self) -> List[Dict]:
        """获取所有违规项"""
        return self._violations

    def get_compliant_items(self) -> List[Dict]:
        """获取所有合规项"""
        return self._compliant_items

    def build_summary(self, processing_time: float = 0) -> Dict:
        """
        生成审核汇总统计

        参数:
            processing_time: 处理耗时（秒）

        返回:
            Dict: 汇总统计
        """
        total = len(self._violations) + len(self._compliant_items)
        compliance_rate = (
            len(self._compliant_items) / max(total, 1)
        )

        severity_count = {SEVERITY_HIGH: 0, SEVERITY_MID: 0, SEVERITY_LOW: 0}
        category_count = {}
        for v in self._violations:
            sev = v.get("severity", SEVERITY_LOW)
            severity_count[sev] = severity_count.get(sev, 0) + 1
            cat = v.get("category", "未知")
            category_count[cat] = category_count.get(cat, 0) + 1

        return {
            "total_violations": len(self._violations),
            "total_compliant": len(self._compliant_items),
            "total_items_checked": total,
            "compliance_rate": compliance_rate,
            "violations_by_severity": severity_count,
            "violations_by_category": category_count,
            "processing_time_seconds": processing_time,
        }
