"""
坐标计算模块
=============
功能：计算文本检测框的 IoU（交并比）、空间关系判断、生成位置校验清单
"""

import logging
from typing import List, Dict, Tuple

logger = logging.getLogger(__name__)


class CoordinateChecker:
    """
    坐标校验器
    -----------
    提供边界框几何计算、签名/印章位置合规性校验等静态方法。

    所有方法均为纯计算，不依赖外部状态，可在任意位置直接调用。
    """

    # ================================================================
    # 内部工具方法
    # ================================================================

    @staticmethod
    def _to_rect(bbox: List) -> Tuple[float, float, float, float]:
        """
        将四点坐标转换为轴对齐矩形 (x1, y1, x2, y2)

        参数:
            bbox: 四点坐标 [[x1,y1],[x2,y2],[x3,y3],[x4,y4]]

        返回:
            (x_min, y_min, x_max, y_max)
        """
        xs = [p[0] for p in bbox]
        ys = [p[1] for p in bbox]
        return (min(xs), min(ys), max(xs), max(ys))

    @staticmethod
    def _rect_area(rect: Tuple[float, float, float, float]) -> float:
        """计算矩形面积"""
        w = rect[2] - rect[0]
        h = rect[3] - rect[1]
        return max(0.0, w * h)

    # ================================================================
    # 公开方法
    # ================================================================

    @staticmethod
    def calculate_iou(bbox_a: List, bbox_b: List) -> float:
        """
        计算两个边界框的 IoU（Intersection over Union，交并比）

        IoU = 交集面积 / 并集面积，范围 [0, 1]
        IoU > 0.5 通常认为两个区域高度重叠

        参数:
            bbox_a: 四点坐标列表 [[x1,y1],[x2,y2],[x3,y3],[x4,y4]]
            bbox_b: 四点坐标列表 [[x1,y1],[x2,y2],[x3,y3],[x4,y4]]

        返回:
            float: IoU 值，范围 0.0 ~ 1.0
        """
        rect_a = CoordinateChecker._to_rect(bbox_a)
        rect_b = CoordinateChecker._to_rect(bbox_b)

        # 计算交集区域边界
        inter_left = max(rect_a[0], rect_b[0])
        inter_top = max(rect_a[1], rect_b[1])
        inter_right = min(rect_a[2], rect_b[2])
        inter_bottom = min(rect_a[3], rect_b[3])

        # 无交集
        if inter_right <= inter_left or inter_bottom <= inter_top:
            return 0.0

        # 计算面积
        inter_area = (inter_right - inter_left) * (inter_bottom - inter_top)
        area_a = CoordinateChecker._rect_area(rect_a)
        area_b = CoordinateChecker._rect_area(rect_b)
        union_area = area_a + area_b - inter_area

        if union_area <= 0:
            return 0.0

        return inter_area / union_area

    @staticmethod
    def check_positions(
        keyword_results: Dict[str, List[Dict]]
    ) -> List[Dict]:
        """
        对关键字检测结果进行位置合规性校验，生成校验清单

        校验逻辑：
        - 检查每个关键字是否被检测到
        - 记录检测到的文本内容和置信度
        - 标记未检测到的关键字，给出补充建议
        - 对签名类关键字和印章类关键字分类标记

        参数:
            keyword_results: 关键字匹配结果 {关键字: [OCR结果列表]}

        返回:
            List[Dict]: 位置校验清单，每项结构为：
                {
                    "keyword":    str,   # 关键字
                    "found":      bool,  # 是否检测到
                    "text":       str,   # 检测到的文本
                    "confidence": float, # 置信度
                    "bbox":       list,  # 边界框坐标
                    "position":   str,   # 坐标范围描述
                    "type":       str,   # 类型标签（签名/印章）
                    "status":     str,   # 状态图标+文字
                    "suggestion": str    # 整改建议（如有）
                }
        """
        # 印章类关键字（通常需要红色的物理印章，而非印刷文字）
        seal_keywords = {"公章"}

        checklist: List[Dict] = []

        for keyword, matches in keyword_results.items():
            # 判断当前关键字属于签名类还是印章类
            item_type = "印章" if keyword in seal_keywords else "签名区域"

            if matches:
                # ---- 已检测到关键字 ----
                for idx, match in enumerate(matches, start=1):
                    text = match.get("text", "")
                    confidence = match.get("confidence", 0.0)
                    bbox = match.get("bbox", [])

                    # 生成位置描述文本
                    if bbox:
                        rect = CoordinateChecker._to_rect(bbox)
                        position_desc = (
                            f"坐标范围：({rect[0]:.0f}, {rect[1]:.0f})"
                            f" → ({rect[2]:.0f}, {rect[3]:.0f})"
                            f"，宽高：{rect[2]-rect[0]:.0f}×{rect[3]-rect[1]:.0f}px"
                        )
                    else:
                        position_desc = "无坐标信息"

                    page_display = match.get("page_display", "?")
                    checklist.append({
                        "keyword": keyword,
                        "found": True,
                        "index": idx,
                        "text": text,
                        "confidence": round(confidence, 4),
                        "bbox": bbox,
                        "position": position_desc,
                        "type": item_type,
                        "status": "✅ 已检测",
                        "suggestion": "",
                        "page_display": page_display,
                    })
            else:
                # ---- 未检测到关键字 ----
                checklist.append({
                    "keyword": keyword,
                    "found": False,
                    "index": 0,
                    "text": "",
                    "confidence": 0.0,
                    "bbox": [],
                    "position": "—",
                    "type": item_type,
                    "status": "❌ 未检测到",
                    "suggestion": f"建议在文档中补充或明确【{keyword}】相关内容"
                })

        # 统计
        found_count = sum(1 for item in checklist if item["found"])
        logger.info(
            f"位置校验完成：共 {len(checklist)} 项，"
            f"已检测 {found_count} 项，"
            f"未检测 {len(checklist) - found_count} 项"
        )

        return checklist
