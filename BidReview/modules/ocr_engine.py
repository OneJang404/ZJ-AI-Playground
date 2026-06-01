"""
OCR 识别模块
=============
功能：封装 PaddleOCR 引擎、文字识别、关键字正则匹配
兼容：PaddleOCR 2.x 和 3.x 双版本 API
依赖：paddleocr, numpy, Pillow
"""

import os
import numpy as np
from PIL import Image
import io
import logging
from typing import List, Dict, Optional

# ---- 修复 PaddlePaddle 3.x CPU oneDNN 兼容性问题 ----
# 必须在 paddleocr 首次 import 之前设置，否则 OCR 识别会报错：
# "ConvertPirAttribute2RuntimeAttribute not support [pir::ArrayAttribute<pir::DoubleAttribute>]"
# 使用直接赋值而非 setdefault，确保一定生效
os.environ["FLAGS_use_onednn"] = "0"

# ---- 红色印章检测：HSV 色彩空间中红色的阈值 ----
# PIL HSV 的 H 范围为 0-255（对应 0°-360°），实际红色在 0°(H≈0) 和 360°(H≈255)
RED_SEAL_LOWER_1 = np.array([0, 50, 50])
RED_SEAL_UPPER_1 = np.array([14, 255, 255])     # ≈10° 红色低端
RED_SEAL_LOWER_2 = np.array([241, 50, 50])       # ≈340° 红色高端（环绕）
RED_SEAL_UPPER_2 = np.array([255, 255, 255])     # ≈360°
# 判定印章的最小红色像素占比
RED_SEAL_MIN_RATIO = 0.005  # 页面0.5%以上为红色即判定有印章

logger = logging.getLogger(__name__)

# ============================================================
# 标书审核需要匹配的固定关键字列表
# 可根据实际业务需求增删
# ============================================================
BID_KEYWORDS = [
    "法定代表人签字",   # 法人签署位置
    "法定代表人盖章",   # 法人签章位置
    "授权代表",         # 授权代表签字区域
    "负责人签字",       # 项目负责人签署
    "公章",             # 公司公章
    "签字",             # 通用签字区域
    "盖章",             # 通用盖章区域
    "日期",             # 日期填写区域
    "金额",             # 金额填写区域
    "报价",             # 报价填写区域
    "工期",             # 工期填写区域
    "承诺",             # 承诺函区域
    "投标有效期",       # 投标有效期
]

# ============================================================
# 招标文件页面筛选关键字（用于智能过滤重点规则页面）
# ============================================================
INVITATION_FILTER_KEYWORDS = [
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


class OCREngine:
    """
    PaddleOCR 识别引擎
    -------------------
    封装 PaddleOCR 模型的加载与调用，提供统一的文字识别接口。

    自动检测 PaddleOCR 版本并使用对应 API：
    - 3.x：使用 predict() 方法（推荐）
    - 2.x：使用 ocr() 方法（兼容旧版）

    特性：
    - 支持中英文混合识别
    - 自动方向检测与纠正
    - 返回文本、四点坐标、置信度

    使用示例：
        engine = OCREngine()
        results = engine.recognize(img_bytes)
        keyword_map = OCREngine.match_keywords(results)
    """

    def __init__(self, use_gpu: bool = False):
        """
        初始化引擎（模型延迟加载，首次调用 recognize() 时才真正初始化）

        参数:
            use_gpu: 是否启用 GPU 推理
                     - False: CPU 模式（本地开发，兼容性最好）
                     - True:  GPU 模式（飞桨 AI Studio V100/A100 环境）
        """
        self.use_gpu = use_gpu
        self._ocr = None           # PaddleOCR 实例（延迟创建）
        self._api_version = None   # 2 或 3，自动检测
        self._initialized = False

    def _lazy_init(self):
        """
        延迟初始化 PaddleOCR，自动检测 API 版本
        仅在首次使用时加载模型，避免 import 阶段耗时
        """
        if self._initialized:
            return

        logger.info("⏳ 正在加载 PaddleOCR 模型（首次运行需下载模型文件）...")
        try:
            from paddleocr import PaddleOCR

            # 尝试以 3.x 方式初始化（新版 API）
            # 3.x 支持 device 参数代替 use_gpu
            device = "gpu" if self.use_gpu else "cpu"
            try:
                self._ocr = PaddleOCR(
                    lang="ch",                        # 中文识别
                    use_textline_orientation=True,    # 文字方向分类（3.x 替代 use_angle_cls）
                    device=device,
                )
                self._api_version = 3
                logger.info("✅ 检测到 PaddleOCR 3.x，使用新版 API")
            except TypeError:
                # 3.x 初始化失败，回退到 2.x 方式
                logger.info("PaddleOCR 3.x 初始化失败，尝试 2.x 方式...")
                self._ocr = PaddleOCR(
                    use_angle_cls=True,    # 文字方向分类（2.x 参数）
                    lang="ch",             # 中文识别
                    use_gpu=self.use_gpu,
                    show_log=False,        # 关闭调试日志
                )
                self._api_version = 2
                logger.info("✅ 使用 PaddleOCR 2.x 兼容模式")

            self._initialized = True
            logger.info(f"PaddleOCR 模型加载完成（API v{self._api_version}）")

        except ImportError:
            raise RuntimeError(
                "❌ PaddleOCR 未正确安装。\n"
                "请依次运行以下命令：\n"
                "  pip install paddlepaddle>=3.0.0\n"
                "  pip install paddleocr>=3.0.0\n\n"
                "如遇问题，请参考：\n"
                "  https://github.com/PaddlePaddle/PaddleOCR"
            )
        except Exception as e:
            raise RuntimeError(
                f"❌ PaddleOCR 初始化失败：{str(e)}\n\n"
                "常见原因及解决方案：\n"
                "1. 首次运行需下载模型文件（约 200MB），请确保网络畅通\n"
                "2. 内存不足：建议至少 4GB 可用内存\n"
                "3. 显卡问题：可尝试 use_gpu=False 切换为 CPU 模式\n"
                "4. Python 版本不兼容：建议使用 Python 3.10 ~ 3.12"
            )

    def recognize(self, img_bytes: bytes) -> List[Dict]:
        """
        识别图片中的全部文字

        参数:
            img_bytes: PNG 图片字节流（由 PDFProcessor.render_page() 生成）

        返回:
            List[Dict]: 识别结果列表，每项结构为：
                {
                    "text":       str,   # 识别的文字内容
                    "bbox":       list,  # 四点像素坐标 [[x1,y1],[x2,y2],[x3,y3],[x4,y4]]
                    "confidence": float  # 识别置信度，范围 0.0 ~ 1.0
                }

        异常:
            RuntimeError: OCR 识别过程出错时抛出
        """
        self._lazy_init()

        try:
            # ---- 步骤1：字节流 → PIL Image → numpy 数组 ----
            pil_image = Image.open(io.BytesIO(img_bytes))

            # 统一转为 RGB（处理 RGBA / 灰度图 / 调色板等格式）
            if pil_image.mode != "RGB":
                pil_image = pil_image.convert("RGB")

            # PIL (RGB) → numpy (RGB) → numpy (BGR)
            img_array = np.array(pil_image)            # shape: (H, W, 3), RGB
            img_bgr = img_array[:, :, ::-1].copy()     # RGB → BGR

            # ---- 步骤2：根据 API 版本调用不同方法 ----
            if self._api_version == 3:
                return self._recognize_v3(img_bgr)
            else:
                return self._recognize_v2(img_bgr)

        except Exception as e:
            logger.error(f"OCR 识别异常：{str(e)}")
            raise RuntimeError(
                f"❌ OCR 识别失败：{str(e)}\n"
                "可能原因：图片数据损坏、内存不足、PaddleOCR 模型异常"
            )

    def _recognize_v3(self, img_bgr: np.ndarray) -> List[Dict]:
        """
        PaddleOCR 3.x API：
        - 调用 predict() 方法
        - 返回格式：result[0].res = {dt_polys, rec_texts, rec_scores, ...}
        """
        raw_result = self._ocr.predict(img_bgr)

        # predict() 返回一个可迭代对象，取第一个元素（单张图片的结果）
        result_list = list(raw_result)
        if not result_list:
            return []

        res_data = result_list[0].res  # 获取结果字典

        # 提取各字段
        dt_polys = res_data.get("dt_polys", [])        # 检测框四点坐标
        rec_texts = res_data.get("rec_texts", [])       # 识别文本
        rec_scores = res_data.get("rec_scores", [])     # 置信度

        if dt_polys is None or rec_texts is None:
            return []

        items: List[Dict] = []
        for poly, text, score in zip(dt_polys, rec_texts, rec_scores):
            # poly 是 shape (4, 2) 的 ndarray → 转为 list
            if hasattr(poly, "tolist"):
                bbox = [[int(p[0]), int(p[1])] for p in poly.tolist()]
            else:
                bbox = [[int(p[0]), int(p[1])] for p in poly]

            items.append({
                "text": str(text),
                "bbox": bbox,
                "confidence": float(score)
            })

        return items

    def _recognize_v2(self, img_bgr: np.ndarray) -> List[Dict]:
        """
        PaddleOCR 2.x API（兼容旧版）：
        - 调用 ocr() 方法
        - 返回格式：[[[bbox, (text, conf)], ...]]
        """
        raw_result = self._ocr.ocr(img_bgr, cls=True)

        # raw_result 结构：[[[bbox, (text, conf)], ...]]，每页一个元素
        if not raw_result or not raw_result[0]:
            return []

        items: List[Dict] = []
        for line in raw_result[0]:
            bbox_raw = line[0]        # [[x1,y1], [x2,y2], [x3,y3], [x4,y4]]
            text_info = line[1]       # ("文本内容", 置信度)

            # 坐标转为整数
            bbox = [[int(p[0]), int(p[1])] for p in bbox_raw]

            items.append({
                "text": str(text_info[0]),
                "bbox": bbox,
                "confidence": float(text_info[1])
            })

        return items

    # ---- 印章检测（图像处理） ----

    @staticmethod
    def detect_red_seal(img_bytes: bytes) -> Dict:
        """
        检测图片中是否存在红色印章/公章（基于 HSV 色彩空间红色像素占比）

        参数:
            img_bytes: PNG 图片字节流

        返回:
            Dict: {"has_seal": bool, "red_ratio": float, "red_regions": int}
        """
        try:
            pil_img = Image.open(io.BytesIO(img_bytes))
            if pil_img.mode != "RGB":
                pil_img = pil_img.convert("RGB")
            img_np = np.array(pil_img)

            # RGB → HSV
            hsv = np.array(pil_img.convert("HSV"))

            # 红色在 HSV 中分布于两个区间（0°和180°附近）
            mask1 = np.all(
                (hsv[:, :, 0] >= RED_SEAL_LOWER_1[0]) & (hsv[:, :, 0] <= RED_SEAL_UPPER_1[0]) &
                (hsv[:, :, 1] >= RED_SEAL_LOWER_1[1]) & (hsv[:, :, 1] <= RED_SEAL_UPPER_1[1]) &
                (hsv[:, :, 2] >= RED_SEAL_LOWER_1[2]) & (hsv[:, :, 2] <= RED_SEAL_UPPER_1[2]),
                axis=0
            )
            mask2 = np.all(
                (hsv[:, :, 0] >= RED_SEAL_LOWER_2[0]) & (hsv[:, :, 0] <= RED_SEAL_UPPER_2[0]) &
                (hsv[:, :, 1] >= RED_SEAL_LOWER_2[1]) & (hsv[:, :, 1] <= RED_SEAL_UPPER_2[1]) &
                (hsv[:, :, 2] >= RED_SEAL_LOWER_2[2]) & (hsv[:, :, 2] <= RED_SEAL_UPPER_2[2]),
                axis=0
            )

            red_pixels = np.sum(mask1) + np.sum(mask2)
            total_pixels = img_np.shape[0] * img_np.shape[1]
            red_ratio = red_pixels / max(total_pixels, 1)

            has_seal = red_ratio >= RED_SEAL_MIN_RATIO

            return {
                "has_seal": has_seal,
                "red_ratio": round(red_ratio, 4),
                "red_regions": 1 if has_seal else 0,
            }
        except Exception as e:
            logger.warning(f"印章检测异常：{e}")
            return {"has_seal": False, "red_ratio": 0.0, "red_regions": 0}

    # ---- 静态工具方法 ----

    @staticmethod
    def crop_region(img_bytes: bytes, bbox: List, padding: int = 30) -> bytes:
        """
        裁切图片中的指定区域（用于手写签名区域的高精度OCR）

        参数:
            img_bytes: 完整页面PNG字节流
            bbox:      四点坐标 [[x1,y1],...] 或矩形 [x0,y0,x1,y1]
            padding:   向外扩展像素数

        返回:
            bytes: 裁切后的PNG图片字节流
        """
        from PIL import Image
        pil = Image.open(io.BytesIO(img_bytes))
        w, h = pil.size

        # 统一转为矩形坐标
        if isinstance(bbox[0], (int, float)):
            x0, y0, x1, y1 = int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])
        else:
            xs = [p[0] for p in bbox]
            ys = [p[1] for p in bbox]
            x0, y0, x1, y1 = int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))

        # 扩展边距并裁剪到图片边界内
        x0 = max(0, x0 - padding)
        y0 = max(0, y0 - padding)
        x1 = min(w, x1 + padding)
        y1 = min(h, y1 + padding)

        if x1 <= x0 or y1 <= y0:
            return img_bytes

        cropped = pil.crop((x0, y0, x1, y1))
        buf = io.BytesIO()
        cropped.save(buf, format="PNG")
        return buf.getvalue()

    def recognize_region(
        self, img_bytes: bytes, bbox: List, padding: int = 30
    ) -> List[Dict]:
        """
        裁切并高精度OCR识别指定区域（用于手写签名等精细识别）

        参数:
            img_bytes: 完整页面PNG字节流
            bbox:      目标区域的四点坐标或矩形
            padding:   向外扩展像素数

        返回:
            List[Dict]: OCR识别结果
        """
        cropped = OCREngine.crop_region(img_bytes, bbox, padding)
        return self.recognize(cropped)

    @staticmethod
    def check_keywords_in_text(text: str, keywords: List[str] = None) -> List[str]:
        """
        检查文本中包含哪些关键字（纯子串匹配，用于页面筛选等快速判断场景）

        参数:
            text:     待检查的文本
            keywords: 关键字列表，默认使用 BID_KEYWORDS

        返回:
            List[str]: 命中的关键字列表
        """
        if keywords is None:
            keywords = BID_KEYWORDS
        return [kw for kw in keywords if kw in text]

    @staticmethod
    def match_keywords(
        ocr_results: List[Dict],
        keywords: Optional[List[str]] = None
    ) -> Dict[str, List[Dict]]:
        """
        在 OCR 识别结果中匹配标书固定关键字

        参数:
            ocr_results: OCR 识别结果列表
            keywords:    要匹配的关键字列表，默认使用 BID_KEYWORDS

        返回:
            Dict[str, List[Dict]]:
                key   = 关键字
                value = 匹配到的 OCR 结果列表（可能为空列表）
        """
        if keywords is None:
            keywords = BID_KEYWORDS

        # 初始化结果字典，确保每个关键字都有对应的列表
        found: Dict[str, List[Dict]] = {kw: [] for kw in keywords}

        for item in ocr_results:
            text = item.get("text", "")
            for kw in keywords:
                if kw in text:
                    found[kw].append(item)

        # 输出匹配统计
        for kw, matches in found.items():
            if matches:
                logger.info(f"  关键字【{kw}】：匹配到 {len(matches)} 处")
            else:
                logger.warning(f"  关键字【{kw}】：未匹配到")

        return found
