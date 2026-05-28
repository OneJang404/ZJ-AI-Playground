"""
招标文件页面筛选模块
==================
功能：对招标文件OCR结果逐页匹配关键字，筛选出重点规则页面
      非重点页面直接跳过，不参与后续审核，解决页数多、耗时长的问题
"""

import logging
from typing import List, Dict

logger = logging.getLogger(__name__)

# 招标文件筛选关键字库
# 页面OCR文本中包含任一关键字即判定为重点页
FILTER_KEYWORDS = [
    "投标人资格",
    "资质要求",
    "签字",
    "盖章",
    "法定代表人",
    "授权代表",
    "日期",
    "格式要求",
    "响应文件",
    "报价",
    "工期",
    "服务要求",
    "废标条款",
    "承诺",
    "须知",
    "合同条款",
]


class PageFilter:
    """
    招标文件智能页面筛选器
    ---------------------
    对招标文件所有页面的OCR结果进行关键字匹配，
    仅保留包含规则/要求的重点页面，减少后续审核数据量。

    全部为静态方法，无需实例化。
    """

    @staticmethod
    def check_keywords_in_text(text: str, keywords: List[str] = None) -> List[str]:
        """
        检查文本中包含哪些关键字

        参数:
            text:     待检查的文本
            keywords: 关键字列表，默认使用 FILTER_KEYWORDS

        返回:
            List[str]: 命中的关键字列表
        """
        if keywords is None:
            keywords = FILTER_KEYWORDS
        return [kw for kw in keywords if kw in text]

    @staticmethod
    def filter_pages(
        ocr_results_by_page: List[List[Dict]],
        keywords: List[str] = None,
    ) -> List[Dict]:
        """
        逐页筛选：拼接每页OCR文本 → 匹配关键字 → 保留命中页

        参数:
            ocr_results_by_page: 每页的OCR结果列表
                                ocr_results_by_page[i] = 第i页的 List[Dict]
            keywords:            筛选关键字，默认使用 FILTER_KEYWORDS

        返回:
            List[Dict]: 重点页面信息列表，每项结构：
                {
                    "page_num":         int,     # 0-based 页码
                    "page_display":     int,     # 1-based 显示页码
                    "matched_keywords": [str],   # 命中的关键字
                    "ocr_text":         str,     # 本页拼接后的全部文本
                    "ocr_results":      [Dict],  # 本页原始OCR结果
                }
        """
        if keywords is None:
            keywords = FILTER_KEYWORDS

        filtered_pages = []

        for page_num, page_results in enumerate(ocr_results_by_page):
            # 拼接本页所有识别文本
            page_text = " ".join(
                item.get("text", "") for item in page_results
            )

            if not page_text.strip():
                continue

            # 匹配关键字
            matched = PageFilter.check_keywords_in_text(page_text, keywords)

            if matched:
                filtered_pages.append({
                    "page_num": page_num,
                    "page_display": page_num + 1,
                    "matched_keywords": matched,
                    "ocr_text": page_text,
                    "ocr_results": page_results,
                })

        # 输出筛选统计
        total = len(ocr_results_by_page)
        kept = len(filtered_pages)
        logger.info(
            f"招标文件页面筛选完成：{total} 页 → 保留 {kept} 页 "
            f"（过滤 {total - kept} 页，保留率 {kept / max(total, 1) * 100:.1f}%）"
        )

        return filtered_pages

    @staticmethod
    def filter_by_text(
        texts_by_page: List[str],
        keywords: List[str] = None,
    ) -> List[Dict]:
        """
        快速文本筛选：对每页已提取的文本匹配关键字（无需OCR）

        参数:
            texts_by_page: 每页文本列表，texts_by_page[i] = 第i页文本
            keywords:      筛选关键字，默认使用 FILTER_KEYWORDS

        返回:
            List[Dict]: 重点页面信息列表（不含 OCR 结果，需后续补充）
                {"page_num": int, "page_display": int, "matched_keywords": [str], "page_text": str}
        """
        if keywords is None:
            keywords = FILTER_KEYWORDS

        filtered = []
        for page_num, text in enumerate(texts_by_page):
            if not text or not text.strip():
                continue
            matched = PageFilter.check_keywords_in_text(text, keywords)
            if matched:
                filtered.append({
                    "page_num": page_num,
                    "page_display": page_num + 1,
                    "matched_keywords": matched,
                    "page_text": text,
                })

        total = len(texts_by_page)
        kept = len(filtered)
        logger.info(
            f"文本筛选完成：{total} 页 → 保留 {kept} 页 "
            f"（过滤 {total - kept} 页，保留率 {kept / max(total, 1) * 100:.1f}%）"
        )
        return filtered

    @staticmethod
    def get_filter_stats(
        filtered_pages: List[Dict],
        total_pages: int,
    ) -> Dict:
        """
        生成筛选统计摘要

        参数:
            filtered_pages: filter_pages() 的返回结果
            total_pages:    招标文件总页数

        返回:
            Dict: 统计摘要
        """
        keyword_counter = {}
        for fp in filtered_pages:
            for kw in fp["matched_keywords"]:
                keyword_counter[kw] = keyword_counter.get(kw, 0) + 1

        return {
            "total_pages": total_pages,
            "key_pages": len(filtered_pages),
            "filtered_out": total_pages - len(filtered_pages),
            "retention_rate": len(filtered_pages) / max(total_pages, 1),
            "keywords_distribution": keyword_counter,
        }
