"""
PDF处理模块
============
功能：上传PDF、解析页面文本、渲染高分辨率图片、管理临时文件
依赖：PyMuPDF (fitz)
"""

import fitz  # PyMuPDF
import tempfile
import os
import shutil
import logging
from typing import List

logger = logging.getLogger(__name__)


class PDFProcessor:
    """
    PDF 文件处理器
    ---------------
    负责加载 PDF 文件、提取文本内容、将页面渲染为 PNG 图片字节流，
    并在处理完成后自动清理临时文件，不占用磁盘空间。

    使用示例：
        pdf_proc = PDFProcessor()
        page_count = pdf_proc.load(file_bytes)
        img_data = pdf_proc.render_page(0, dpi=300)
        full_text = pdf_proc.extract_text()
        pdf_proc.cleanup()
    """

    def __init__(self):
        """初始化处理器，创建临时工作目录"""
        self.temp_dir = tempfile.mkdtemp(prefix="bid_review_")
        self._doc = None          # fitz.Document 对象
        self._page_count = 0      # PDF 总页数
        logger.info(f"创建临时目录：{self.temp_dir}")

    # ---- 属性 ----

    @property
    def page_count(self) -> int:
        """返回 PDF 总页数（只读）"""
        return self._page_count

    # ---- 核心方法 ----

    def load(self, file_bytes: bytes) -> int:
        """
        从字节流加载 PDF 文件

        参数:
            file_bytes: PDF 文件的完整字节数据（来自 Streamlit 上传组件）

        返回:
            int: PDF 文件总页数

        异常:
            ValueError: 当文件损坏或不是有效 PDF 格式时抛出
        """
        try:
            self._doc = fitz.open(stream=file_bytes, filetype="pdf")
        except Exception as e:
            raise ValueError(
                f"❌ PDF 文件加载失败，请确认文件格式正确且未损坏。\n"
                f"技术细节：{str(e)}"
            )

        self._page_count = len(self._doc)
        # 检查文档是否加密（加密文档无法正常读取）
        if self._doc.is_encrypted:
            raise ValueError(
                "❌ PDF 文件已加密，请先解除密码保护后再上传。"
            )

        logger.info(f"PDF 加载成功，共 {self._page_count} 页")
        return self._page_count

    def render_page(self, page_num: int, dpi: int = 300) -> bytes:
        """
        将指定页面渲染为 PNG 图片字节流

        参数:
            page_num: 页码（从 0 开始计数）
            dpi:     输出分辨率，默认 300 DPI（满足 OCR 精度要求）

        返回:
            bytes: PNG 格式的图片字节数据（可直接传给 PaddleOCR）

        异常:
            RuntimeError: 未先调用 load() 时抛出
            IndexError:   页码超出范围时抛出
        """
        if self._doc is None:
            raise RuntimeError("请先调用 load() 方法加载 PDF 文件")

        if page_num < 0 or page_num >= self._page_count:
            raise IndexError(
                f"页码 {page_num} 超出有效范围 [0, {self._page_count - 1}]"
            )

        page = self._doc[page_num]

        # PDF 默认分辨率为 72 DPI，通过缩放矩阵提升到目标 DPI
        zoom = dpi / 72.0
        matrix = fitz.Matrix(zoom, zoom)

        # 渲染页面为像素图
        pix = page.get_pixmap(matrix=matrix, colorspace="rgb")

        # 输出为 PNG 字节流（无损压缩，不落盘）
        return pix.tobytes("png")

    def get_page_text(self, page_num: int) -> str:
        """
        提取单页文本（PyMuPDF，速度快，<0.1秒/页）

        参数:
            page_num: 页码（从 0 开始）

        返回:
            str: 本页文本内容
        """
        if self._doc is None:
            raise RuntimeError("请先调用 load() 方法加载 PDF 文件")
        return self._doc[page_num].get_text()

    def search_keyword_positions(
        self, page_num: int, keywords: list, render_dpi: int = 200
    ) -> dict:
        """
        使用 PyMuPDF search_for 精确定位关键字在页面上的位置（速度极快，<1ms/页）

        参数:
            page_num:   页码（0-based）
            keywords:   要搜索的关键字列表
            render_dpi: 渲染DPI（用于坐标转换）

        返回:
            dict: {keyword: [[x0,y0,x1,y1], ...]} 图片坐标系下的矩形列表
                  对文本型PDF精准，对扫描件/图片型PDF返回空dict
        """
        if self._doc is None:
            raise RuntimeError("请先调用 load() 方法加载 PDF 文件")

        page = self._doc[page_num]
        scale = render_dpi / 72.0  # PDF坐标→像素坐标
        result = {}

        for kw in keywords:
            rects = page.search_for(kw)
            if rects:
                result[kw] = [
                    [
                        int(r.x0 * scale),
                        int(r.y0 * scale),
                        int(r.x1 * scale),
                        int(r.y1 * scale),
                    ]
                    for r in rects
                ]

        return result

    def extract_text_per_page(self) -> List[str]:
        """
        提取每页文本，返回列表（用于快速筛选）

        返回:
            List[str]: 每页文本，索引对应页码
        """
        if self._doc is None:
            raise RuntimeError("请先调用 load() 方法加载 PDF 文件")
        texts = []
        for i in range(self._page_count):
            texts.append(self._doc[i].get_text())
        return texts

    def extract_text(self) -> str:
        """
        提取整份 PDF 的文本内容

        返回:
            str: 合并后的全文，每页以分隔线标记

        异常:
            RuntimeError: 未先调用 load() 时抛出
        """
        if self._doc is None:
            raise RuntimeError("请先调用 load() 方法加载 PDF 文件")

        all_pages_text: List[str] = []

        for i in range(self._page_count):
            page_text = self._doc[i].get_text()
            if page_text.strip():
                # 每页添加页号标记，方便后续定位
                all_pages_text.append(f"【第 {i + 1} 页】\n{page_text.strip()}")

        full_text = "\n\n".join(all_pages_text)

        if not full_text:
            logger.warning("PDF 中未提取到文本内容（可能是扫描件/图片型PDF）")

        return full_text

    def detect_signature_areas(
        self,
        page_num: int,
        signature_keywords: list = None,
        render_dpi: int = 300,
    ) -> list:
        """
        使用 PyMuPDF search_for 精确定位签名区域，并返回裁切后的高分辨率图片

        参数:
            page_num:           页码（0-based）
            signature_keywords: 签名类关键字列表，默认使用常见签名关键字
            render_dpi:         渲染DPI

        返回:
            list[dict]: [{"keyword": str, "bbox": [x0,y0,x1,y1], "cropped_img": bytes}, ...]
        """
        if signature_keywords is None:
            signature_keywords = [
                "法定代表人签字", "法定代表人盖章", "授权代表",
                "负责人签字", "签字", "签名", "签署",
            ]

        if self._doc is None:
            raise RuntimeError("请先调用 load() 方法加载 PDF 文件")

        page = self._doc[page_num]
        scale = render_dpi / 72.0
        results = []

        for kw in signature_keywords:
            rects = page.search_for(kw)
            if not rects:
                continue

            # 渲染页面高分辨率图片
            zoom = render_dpi / 72.0
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace="rgb")
            page_img = pix.tobytes("png")

            for r in rects:
                bbox = [
                    int(r.x0 * scale),
                    int(r.y0 * scale),
                    int(r.x1 * scale),
                    int(r.y1 * scale),
                ]
                # 扩展区域以捕获周围的签名/手写内容（下方扩展更多，签名通常在文字下方）
                from PIL import Image
                from io import BytesIO
                pil = Image.open(BytesIO(page_img))
                w, h = pil.size
                pad_x, pad_y_top, pad_y_bottom = 40, 10, 120
                x0 = max(0, bbox[0] - pad_x)
                y0 = max(0, bbox[1] - pad_y_top)
                x1 = min(w, bbox[2] + pad_x)
                y1 = min(h, bbox[3] + pad_y_bottom)
                cropped = pil.crop((x0, y0, x1, y1))
                buf = BytesIO()
                cropped.save(buf, format="PNG")
                results.append({
                    "keyword": kw,
                    "bbox": bbox,
                    "page_num": page_num,
                    "page_display": page_num + 1,
                    "cropped_img": buf.getvalue(),
                })

        return results

    def cleanup(self):
        """
        清理所有资源
        - 关闭 PDF 文档句柄
        - 删除临时目录及其中所有文件，不占用磁盘
        """
        if self._doc is not None:
            self._doc.close()
            self._doc = None

        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)
            logger.info(f"已清理临时目录：{self.temp_dir}")

    def __del__(self):
        """析构时自动清理，防止资源泄漏"""
        self.cleanup()
