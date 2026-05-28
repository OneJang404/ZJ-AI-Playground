"""
================================================================================
 投标文件智能审核网页工具（交叉审核模式）
================================================================================
 基于 Streamlit + PaddleOCR + DeepSeek + PyMuPDF，支持「招标文件 + 投标文件」双向对标审核。

 核心功能：
   1. 双文件上传（招标文件 / 投标文件）
   2. 招标文件智能页面筛选（文本预筛 + 仅OCR重点页）
   3. 投标文件混合文本提取（PyMuPDF为主 + OCR补充图片页 + 红色印章检测）
   4. 交叉对标审核 + 违规项图文左右对照展示

 启动方式：
    streamlit run app.py --server.address=0.0.0.0 --server.port=8501
================================================================================
"""

import streamlit as st
import logging
import time
import re
import sys
import os
from io import BytesIO
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from modules.pdf_processor import PDFProcessor
from modules.ocr_engine import OCREngine
from modules.page_filter import PageFilter
from modules.coordinate_checker import CoordinateChecker
from modules.violation_checker import ViolationChecker
from modules.ai_reviewer import AIReviewer
from modules.sig_validator import SigValidator

# ============================================================
st.set_page_config(
    page_title="投标文件智能审核工具",
    page_icon="📄",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
[data-testid="stSidebar"] > div:first-child {
    position: sticky; top: 0; z-index: 10;
}
/* 缓存下拉框宽度 */
div[data-testid="stSelectbox"]:has(select) {
    min-width: 200px !important;
}
</style>
""", unsafe_allow_html=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("BidReview")

# ---- 会话状态 ----
for _key, _default in [
    ("processing_done", False), ("review_results", None),
    ("ai_report", None), ("use_gpu", False),
    ("feedback_info", {}),
]:
    if _key not in st.session_state:
        st.session_state[_key] = _default

# 自定义关键字（从默认列表初始化）
if "custom_filter_keywords" not in st.session_state:
    from modules.page_filter import FILTER_KEYWORDS as _def_fk
    st.session_state.custom_filter_keywords = list(_def_fk)
if "custom_bid_keywords" not in st.session_state:
    from modules.ocr_engine import BID_KEYWORDS as _def_bk
    st.session_state.custom_bid_keywords = list(_def_bk)


# ============================================================
@st.cache_resource(show_spinner=False)
def get_ocr_engine(use_gpu: bool = False) -> OCREngine:
    return OCREngine(use_gpu=use_gpu)


# ============================================================
def _fmt_time(seconds: float) -> str:
    """格式化秒数为 X分Y秒"""
    m, s = divmod(int(seconds), 60)
    return f"{m}分{s}秒" if m > 0 else f"{s}秒"


def _draw_red_boxes(img_bytes, boxes, width=3):
    """在图片上画红色矩形框标注关键区域"""
    try:
        img = Image.open(BytesIO(img_bytes))
        draw = ImageDraw.Draw(img)
        for box in boxes:
            if box and len(box) >= 4:
                if isinstance(box[0], (int, float)):
                    # 格式：[x0, y0, x1, y1]
                    x0, y0, x1, y1 = int(box[0]), int(box[1]), int(box[2]), int(box[3])
                    for i in range(width):
                        draw.rectangle([x0 - i, y0 - i, x1 + i, y1 + i], outline="red")
                else:
                    # 四点格式：[[x1,y1],[x2,y2],[x3,y3],[x4,y4]]
                    pts = [(int(p[0]), int(p[1])) for p in box]
                    draw.line(pts + [pts[0]], fill="red", width=width)
        buf = BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return img_bytes


def _parse_page_numbers_from_report(report_text: str) -> list:
    """
    从AI审核报告中提取被引用的页码（用于高精度OCR复核）

    匹配模式：第X页、第X-Y页、第X、Y、Z页、投标文件第X页
    返回去重排序后的1-based页码列表
    """
    pages = set()

    # 范围模式：第X-Y页、第X至Y页
    range_pattern = r'第\s*(\d+)\s*[-–—至到]\s*(\d+)\s*页'
    for m in re.finditer(range_pattern, report_text):
        start, end = int(m.group(1)), int(m.group(2))
        for p in range(start, min(end + 1, start + 6)):
            pages.add(p)

    cleaned = re.sub(range_pattern, '', report_text)

    # 简单模式：第X页
    for m in re.finditer(r'第\s*(\d+)\s*页', cleaned):
        pages.add(int(m.group(1)))

    # 列举模式：第X、Y、Z页
    for m in re.finditer(r'第\s*([\d、，,\s]+)\s*页', cleaned):
        nums = re.split(r'[、，,\s]+', m.group(1))
        for n in nums:
            if n.strip().isdigit():
                pages.add(int(n.strip()))

    return sorted(pages)[:5]


# ============================================================
# 侧边栏
# ============================================================
def render_sidebar():
    with st.sidebar:
        st.markdown("## 投标文件审核工具")
        st.caption("交叉审核 | PyMuPDF + PaddleOCR + DeepSeek")

        st.markdown("---")
        use_gpu = st.checkbox("启用 GPU 加速", value=st.session_state.use_gpu,
                              help="需要 CUDA 环境。本地开发请取消。")
        st.session_state.use_gpu = use_gpu

        st.markdown("---")
        with st.expander("⚙️ 关键字配置", expanded=False):
            st.caption("招标文件筛选关键字（命中任一即保留该页）")
            new_fk = st.text_input("添加筛选关键字", key="new_fk", placeholder="输入后按回车")
            if new_fk and new_fk not in st.session_state.custom_filter_keywords:
                st.session_state.custom_filter_keywords.append(new_fk)
                st.rerun()
            fk_cols = st.columns(2)
            for i, kw in enumerate(st.session_state.custom_filter_keywords):
                with fk_cols[i % 2]:
                    c1, c2 = st.columns([4, 1])
                    c1.caption(kw)
                    if c2.button("×", key=f"del_fk_{i}"):
                        st.session_state.custom_filter_keywords.pop(i)
                        st.rerun()

            st.caption("审核关键字（用于投标文件关键字匹配）")
            new_bk = st.text_input("添加审核关键字", key="new_bk", placeholder="输入后按回车")
            if new_bk and new_bk not in st.session_state.custom_bid_keywords:
                st.session_state.custom_bid_keywords.append(new_bk)
                st.rerun()
            bk_cols = st.columns(2)
            for i, kw in enumerate(st.session_state.custom_bid_keywords):
                with bk_cols[i % 2]:
                    c1, c2 = st.columns([4, 1])
                    c1.caption(kw)
                    if c2.button("×", key=f"del_bk_{i}"):
                        st.session_state.custom_bid_keywords.pop(i)
                        st.rerun()

            if st.button("🔄 恢复默认关键字", use_container_width=True):
                from modules.page_filter import FILTER_KEYWORDS as _fk
                from modules.ocr_engine import BID_KEYWORDS as _bk
                st.session_state.custom_filter_keywords = list(_fk)
                st.session_state.custom_bid_keywords = list(_bk)
                st.rerun()

        st.markdown("---")
        with st.expander("💾 缓存管理", expanded=False):
            from modules.cache_manager import CacheManager
            cm = CacheManager()
            cached_files = cm.list_cached_files()
            if cached_files:
                st.caption(f"已缓存 {len(cached_files)} 个招标文件")
                for cf in cached_files[:10]:
                    c1, c2 = st.columns([9, 1])
                    with c1:
                        st.caption(
                            f"{cf['name'][:28]} "
                            f"（{cf['page_count']}p, {cf['cached_at']}）"
                        )
                    with c2:
                        if st.button("🗑", key=f"delcache_{cf['hash'][:12]}",
                                     help="删除该缓存文件"):
                            cm.clear_cache_by_hash(cf["hash"])
                            st.rerun()
                if len(cached_files) > 10:
                    st.caption(f"（共{len(cached_files)}个，仅显示最近10个）")
                if st.button("🗑️ 清除所有缓存", use_container_width=True):
                    count = cm.clear_all_cache()
                    st.success(f"已清除 {count} 个缓存文件")
                    st.rerun()
            else:
                st.caption("暂无缓存文件")

        st.markdown("---")
        with st.expander("🔧 开发人员选项", expanded=False):
            fb = st.session_state.get("feedback_info", {})
            if fb:
                st.caption(f"AI复核：{'✅ 已更新' if fb.get('applied') else 'ℹ️ 确认无误'}")
                st.caption(f"复核页面：{fb.get('pages', [])}")
                st.caption(f"复核耗时：{fb.get('time', '?')}")
            else:
                st.caption("本次未执行AI复核")
            try:
                from paddleocr import PaddleOCR
                st.caption("PaddleOCR: 可用")
            except Exception:
                st.caption("PaddleOCR: 未加载")

        st.markdown("---")
        st.caption("首次运行需下载 OCR 模型（约200MB）")
    return use_gpu


# ============================================================
def render_dual_upload_section():
    """双文件上传区 + 缓存下拉 + 独立预上传"""
    from modules.cache_manager import CacheManager
    from io import BytesIO

    cm = CacheManager()
    cached_files = cm.list_cached_files()

    # ---- 独立预上传招标文件 ----
    with st.expander("📘 提前处理招标文件（可选）", expanded=False):
        st.caption("单独上传招标文件进行预处理并缓存，稍后可快速开始审核。")

        # 显示上次预处理结果（跨 rerun 保留）
        if st.session_state.get("_preprocess_done"):
            st.success(st.session_state.get("_preprocess_msg", ""))
            render_invitation_filter_result(
                st.session_state.get("_preprocess_filtered", []),
                st.session_state.get("_preprocess_stats", {}))
            st.session_state._preprocess_done = False

        pre_inv = st.file_uploader("上传招标文件进行预处理", type=["pdf"],
                                    key="pre_inv_uploader", label_visibility="collapsed")
        if pre_inv:
            st.info(f"**{pre_inv.name}**  |  {pre_inv.size / 1024 / 1024:.1f} MB")
            if st.button("🔍 预处理招标文件", type="secondary", key="preprocess_btn"):
                ocr_engine = get_ocr_engine(use_gpu=st.session_state.use_gpu)
                inv_bytes = pre_inv.read()

                # 哈希去重：已缓存则直接展示，避免重复 OCR
                cached = cm.load_invitation_cache(inv_bytes)
                if cached:
                    st.success(
                        f"💾 该文件已缓存（{cached['cached_at']}），"
                        f"{cached['page_count']}页→{len(cached['filtered_pages'])}页重点，无需重复处理。")
                    render_invitation_filter_result(cached["filtered_pages"], cached["filter_stats"])
                else:
                    inv_io = BytesIO(inv_bytes)
                    inv_io.name = pre_inv.name

                    # 进度条 + 剩余时间（第一页耗时 × 剩余页数）+ 百分比
                    progress_bar = st.progress(0, "正在处理招标文件...")
                    status_text = st.empty()
                    eta_c1, eta_c2 = st.columns(2)
                    eta_left = eta_c1.empty()
                    eta_right = eta_c2.empty()
                    _ocr_t0 = {"v": None}   # 第一个 OCR 页开始时间
                    _ocr_total = {"v": 0}   # OCR 总页数

                    def _pre_cb(frac, msg=""):
                        pct = int(frac * 100)
                        progress_bar.progress(min(pct, 99), msg or "正在处理招标文件...")
                        if msg:
                            status_text.info(msg)

                        # 从消息解析 OCR 进度：OCR重点页 1/10（第X页）
                        m = re.search(r"OCR重点页\s*(\d+)/(\d+)", msg)
                        if m:
                            cur = int(m.group(1))
                            total = int(m.group(2))
                            now = time.time()
                            if _ocr_t0["v"] is None:
                                _ocr_t0["v"] = now
                                _ocr_total["v"] = total
                                eta_str = "⏱ 计算中..."
                            elif cur > 1:
                                per_page = (now - _ocr_t0["v"]) / (cur - 1)
                                remaining = total - cur + 1
                                eta_str = f"⏱ 剩余 {_fmt_time(per_page * remaining)}"
                            else:
                                eta_str = "⏱ 计算中..."
                        elif _ocr_t0["v"] is not None:
                            # OCR 已完成，后面没有页了
                            eta_str = "⏱ 即将完成..."
                        else:
                            eta_str = "⏱ 准备中..."

                        eta_left.caption(eta_str)
                        eta_right.markdown(
                            f'<p style="text-align:right;color:rgba(49,51,63,0.6);'
                            f'font-size:0.875rem;margin:0;">{pct}%</p>',
                            unsafe_allow_html=True)

                    filtered_pages, inv_pc, inv_pdf_proc, filter_stats = process_invitation(
                        inv_io, ocr_engine, progress_callback=_pre_cb)
                    cm.save_invitation_cache(
                        inv_bytes, pre_inv.name, len(inv_bytes),
                        filtered_pages, filter_stats, inv_pc)
                    inv_pdf_proc.cleanup()
                    progress_bar.progress(100, "✅ 预处理完成！")

                    # 跨 rerun 保留结果，同时刷新缓存列表和下拉选项
                    st.session_state._preprocess_done = True
                    st.session_state._preprocess_msg = (
                        f"✅ 预处理完成！{inv_pc}页→{len(filtered_pages)}页重点，已缓存。"
                        f"请上传投标文件开始审核。")
                    st.session_state._preprocess_filtered = filtered_pages
                    st.session_state._preprocess_stats = filter_stats
                    st.rerun()

    st.header("📤 第一步：上传文件")

    # 初始化
    if "cache_dropdown" not in st.session_state:
        st.session_state.cache_dropdown = "无"

    # 上传新文件 → 清空下拉框（必须在 selectbox 渲染前执行）
    if st.session_state.get("_clr_dd"):
        st.session_state.cache_dropdown = "无"
        st.session_state._clr_dd = False

    cl, cr = st.columns(2)
    with cl:
        title_col, dd_col = st.columns([1, 1])
        with title_col:
            st.subheader("📘 招标文件")
        with dd_col:
            cache_map = {}
            if cached_files:
                for c in cached_files[:10]:
                    label = f"{c['name'][:30]}（{c['page_count']}p）"
                    cache_map[label] = c
            selected = st.selectbox(
                "从提交记录中选择",
                ["无"] + list(cache_map.keys()),
                key="cache_dropdown",
            )

        # 文件上传器（始终渲染，保证左右对齐）
        inv_file = st.file_uploader("上传招标文件（PDF）", type=["pdf"],
                                     key="inv_uploader", label_visibility="collapsed")

        # 判断有效 inv_file：上传 > 缓存 > 空
        if inv_file is not None:
            # 新上传 → 清空下拉选中（用标记模式避免 widget 实例化后修改）
            if selected != "无":
                st.session_state._clr_dd = True
                st.rerun()
            st.info(f"**{inv_file.name}**  |  {inv_file.size / 1024 / 1024:.1f} MB")
        elif selected != "无" and selected in cache_map:
            c = cache_map[selected]
            raw_bytes = cm.get_original_bytes(c["hash"])
            if raw_bytes:
                inv_file = BytesIO(raw_bytes)
                inv_file.name = c["name"]
                st.info(f"💾 **{c['name']}**  |  {len(raw_bytes) / 1024 / 1024:.1f} MB（从记录加载）")
            else:
                st.warning("缓存读取失败，请重新上传")
                st.caption("请上传招标文件 PDF")
        else:
            st.caption("请上传招标文件 PDF")

    with cr:
        st.subheader("📄 投标文件")
        resp_file = st.file_uploader("上传投标文件（PDF）", type=["pdf"],
                                      key="resp_uploader", label_visibility="collapsed")
        if resp_file:
            st.info(f"**{resp_file.name}**  |  {resp_file.size / 1024 / 1024:.1f} MB")
        else:
            st.caption("请上传投标文件 PDF")

    return inv_file, resp_file


# ============================================================
# 招标文件：文本预筛 + OCR重点页 + 实时进度
# ============================================================
def process_invitation(inv_file, ocr_engine, progress_callback=None):
    pdf_proc = PDFProcessor()
    if progress_callback:
        progress_callback(0.02, "加载招标文件...")
    file_bytes = inv_file.read()
    page_count = pdf_proc.load(file_bytes)
    if page_count == 0:
        raise ValueError("招标文件 PDF 为空")

    # 步骤1：PyMuPDF 快速提取文本 → 筛选
    if progress_callback:
        progress_callback(0.03, f"提取文本（{page_count} 页）...")
    text_by_page = pdf_proc.extract_text_per_page()
    if progress_callback:
        progress_callback(0.10, "筛选重点规则页面...")
    keyword_matches = PageFilter.filter_by_text(
        text_by_page, keywords=st.session_state.custom_filter_keywords)
    if progress_callback:
        progress_callback(0.12, f"筛选：{page_count}页→{len(keyword_matches)}页重点")

    # 步骤2：仅OCR重点页（200 DPI）
    filtered_pages = []
    ocr_fail = 0
    for i, km in enumerate(keyword_matches):
        pn = km["page_num"]
        pct = 0.12 + (i + 1) / max(len(keyword_matches), 1) * 0.26
        if progress_callback:
            progress_callback(pct, f"OCR重点页 {i+1}/{len(keyword_matches)}（第{pn+1}页）")
        try:
            img_bytes = pdf_proc.render_page(pn, dpi=150)
            ocr_results = ocr_engine.recognize(img_bytes) if ocr_engine is not None else []
            ocr_text = " ".join(r.get("text", "") for r in ocr_results)
            filtered_pages.append({
                "page_num": pn, "page_display": pn + 1,
                "matched_keywords": km["matched_keywords"],
                "ocr_text": ocr_text or km["page_text"],
                "ocr_results": ocr_results,
            })
        except Exception as e:
            logger.warning(f"招标文件第{pn+1}页OCR失败：{e}")
            ocr_fail += 1
            filtered_pages.append({
                "page_num": pn, "page_display": pn + 1,
                "matched_keywords": km["matched_keywords"],
                "ocr_text": km["page_text"], "ocr_results": [],
            })

    filter_stats = PageFilter.get_filter_stats(filtered_pages, page_count)
    logger.info(f"招标文件完成：{page_count}页→保留{len(filtered_pages)}页")
    return filtered_pages, page_count, pdf_proc, filter_stats


# ============================================================
# 投标文件：文本预筛 + OCR补充图片页 + 印章检测（仅重点页）
# ============================================================
def process_response(resp_file, ocr_engine, progress_callback=None):
    pdf_proc = PDFProcessor()
    if progress_callback:
        progress_callback(0.42, "加载投标文件...")
    file_bytes = resp_file.read()
    page_count = pdf_proc.load(file_bytes)
    if page_count == 0:
        raise ValueError("投标文件 PDF 为空")

    if progress_callback:
        progress_callback(0.44, f"{page_count} 页，提取文本...")

    # 步骤1：PyMuPDF 快速提取全量文本 → 关键字筛选重点页
    text_by_page = pdf_proc.extract_text_per_page()
    if progress_callback:
        progress_callback(0.46, "筛选投标文件重点页...")
    key_matches = PageFilter.filter_by_text(
        text_by_page, keywords=st.session_state.custom_bid_keywords)
    key_page_nums = {m["page_num"] for m in key_matches}
    if progress_callback:
        progress_callback(0.48, f"筛选：{page_count}页→{len(key_page_nums)}页重点")

    all_ocr_results = []
    seal_pages = []
    ocr_used = 0

    # 非重点页：仅用 PyMuPDF 文本（免渲染、免OCR、免印章检测）
    skipped_pages = 0
    for pn in range(page_count):
        if pn in key_page_nums:
            continue
        skipped_pages += 1
        for line in text_by_page[pn].strip().split("\n"):
            line = line.strip()
            if line:
                all_ocr_results.append({
                    "text": line, "bbox": [[0, 0, 0, 0]],
                    "confidence": 0.99, "page_display": pn + 1,
                })

    # 重点页：逐页渲染 + OCR补充 + 印章检测
    for i, km in enumerate(key_matches):
        pn = km["page_num"]
        pct = 0.48 + (i + 1) / max(len(key_matches), 1) * 0.22
        if progress_callback and (i % 3 == 0 or i == len(key_matches) - 1):
            progress_callback(pct, f"投标文件重点页：第{pn+1}/{page_count}页")

        pymupdf_text = text_by_page[pn]
        has_enough_text = len(pymupdf_text.strip()) > 30

        try:
            img_bytes = pdf_proc.render_page(pn, dpi=200)

            # 红色印章检测
            seal_info = OCREngine.detect_red_seal(img_bytes)
            if seal_info.get("has_seal"):
                seal_pages.append(pn + 1)

            if has_enough_text:
                for line in pymupdf_text.strip().split("\n"):
                    line = line.strip()
                    if line:
                        all_ocr_results.append({
                            "text": line, "bbox": [[0, 0, 0, 0]],
                            "confidence": 0.99, "page_display": pn + 1,
                        })
            else:
                ocr_used += 1
                try:
                    ocr_results = ocr_engine.recognize(img_bytes)
                    for item in ocr_results:
                        item["page_display"] = pn + 1
                    all_ocr_results.extend(ocr_results)
                except Exception as e:
                    logger.warning(f"第{pn+1}页OCR失败：{e}")

            if seal_info.get("has_seal"):
                all_ocr_results.append({
                    "text": "公章（红色印章已检测）",
                    "bbox": [[0, 0, 0, 0]],
                    "confidence": 0.95,
                    "page_display": pn + 1,
                    "is_seal_detected": True,
                })
                all_ocr_results.append({
                    "text": "盖章（红色印章已检测）",
                    "bbox": [[0, 0, 0, 0]],
                    "confidence": 0.95,
                    "page_display": pn + 1,
                    "is_seal_detected": True,
                })

        except Exception as e:
            logger.warning(f"投标文件第{pn+1}页处理失败：{e}")

    full_text = pdf_proc.extract_text()

    logger.info(
        f"投标文件完成：{page_count}页（{len(key_page_nums)}页重点，跳过{skipped_pages}页），"
        f"{len(all_ocr_results)}条文本"
        f"（PyMuPDF为主，OCR补充{ocr_used}个图片页，检测到{len(seal_pages)}个印章页）"
    )
    return all_ocr_results, full_text, page_count, pdf_proc, seal_pages


# ============================================================
def process_cross_review(filtered_pages, resp_all_ocr, resp_full_text,
                         inv_pdf_proc, resp_pdf_proc, progress_callback=None):
    if progress_callback:
        progress_callback(0.74, "关键字匹配...")
    keyword_results = OCREngine.match_keywords(
        resp_all_ocr, keywords=st.session_state.custom_bid_keywords)

    if progress_callback:
        progress_callback(0.76, "位置校验...")
    checker = CoordinateChecker()
    position_checklist = checker.check_positions(keyword_results)

    if progress_callback:
        progress_callback(0.78, "违规检测...")
    vc = ViolationChecker()
    vc.check_signatures_and_seals(filtered_pages, position_checklist)
    vc.check_content_compliance(filtered_pages, resp_full_text, resp_all_ocr)

    violations = vc.get_all_violations()
    compliant_items = vc.get_compliant_items()

    if progress_callback:
        progress_callback(0.80, "截图采集...")
    # 截图（全页+红色矩形框标注关键区域）
    # 收集每页需要标注的bbox列表
    inv_boxes_by_page: dict = {}  # {page_num: [bboxes]}
    resp_boxes_by_page: dict = {}  # {page_display: [bboxes]}

    for v in violations:
        src_pn = v["source"].get("page_num")
        src_bbox = v["source"].get("bbox")
        if isinstance(src_pn, (int, float)):
            inv_boxes_by_page.setdefault(int(src_pn), []).append(src_bbox)
        ev_pn = v["evidence"].get("page_display")
        ev_bbox = v["evidence"].get("bbox")
        if isinstance(ev_pn, (int, float)):
            resp_boxes_by_page.setdefault(int(ev_pn), []).append(ev_bbox)

    # 渲染招标文件页面 + 红线标注
    for pn, boxes in inv_boxes_by_page.items():
        valid_boxes = [b for b in boxes if b]
        try:
            raw = inv_pdf_proc.render_page(pn, dpi=200)
            annotated = _draw_red_boxes(raw, valid_boxes) if valid_boxes else raw
            for v in violations:
                if v["source"].get("page_num") == pn:
                    v["source"]["screenshot_bytes"] = annotated
        except Exception:
            for v in violations:
                if v["source"].get("page_num") == pn:
                    v["source"]["screenshot_bytes"] = None

    # 渲染投标文件页面 + 红线标注
    for pd, boxes in resp_boxes_by_page.items():
        valid_boxes = [b for b in boxes if b]
        try:
            raw = resp_pdf_proc.render_page(pd - 1, dpi=200)
            annotated = _draw_red_boxes(raw, valid_boxes) if valid_boxes else raw
            for v in violations:
                if v["evidence"].get("page_display") == pd:
                    v["evidence"]["screenshot_bytes"] = annotated
        except Exception:
            for v in violations:
                if v["evidence"].get("page_display") == pd:
                    v["evidence"]["screenshot_bytes"] = None

    summary = vc.build_summary()
    return violations, compliant_items, summary, keyword_results, position_checklist


# ============================================================
# 主流水线
# ============================================================
def process_dual_bid(invitation_file, response_file, use_gpu):
    pipeline_start = time.time()
    ocr_engine = get_ocr_engine(use_gpu=use_gpu)
    inv_pdf_proc = resp_pdf_proc = None

    progress_bar = st.progress(0, "🚀 准备开始...")
    status_text = st.empty()
    eta_c1, eta_c2 = st.columns(2)
    eta_left = eta_c1.empty()
    eta_right = eta_c2.empty()

    def _update_eta(frac):
        elapsed = time.time() - pipeline_start
        if frac > 0.005:
            remaining = (elapsed / frac) - elapsed
            eta_left.caption(f"⏱ 剩余 {_fmt_time(max(0, remaining))}")
        else:
            eta_left.caption("⏱ 计算中...")
        eta_right.markdown(
            f'<p style="text-align:right;color:rgba(49,51,63,0.6);'
            f'font-size:0.875rem;margin:0;">{int(frac * 100)}%</p>',
            unsafe_allow_html=True)

    def _cb(_base, _span, label_prefix):
        def cb(frac, msg=""):
            pct = int(frac * 100)
            progress_bar.progress(min(pct, 99), f"{label_prefix} {msg}" if msg else label_prefix)
            if msg:
                status_text.info(msg)
            _update_eta(frac)
        return cb

    try:
        # ---- 阶段1：招标文件（0-40%） ----
        invitation_file.seek(0)
        inv_bytes = invitation_file.read()

        from modules.cache_manager import CacheManager
        cache_mgr = CacheManager()
        cached = cache_mgr.load_invitation_cache(inv_bytes)
        cached_hit = False

        if cached:
            filtered_pages = cached["filtered_pages"]
            filter_stats = cached["filter_stats"]
            inv_pc = cached["page_count"]
            inv_pdf_proc = PDFProcessor()
            inv_pdf_proc.load(inv_bytes)
            progress_bar.progress(40, "✅ 招标文件已从缓存加载")
            _update_eta(0.40)
            status_text.success(f"✅ 招标文件：{inv_pc}页→{len(filtered_pages)}页重点（💾 已加载缓存）")
            st.toast(f"💾 招标文件已从缓存加载（缓存时间：{cached.get('cached_at', '?')}）", icon="💾")
            cached_hit = True
        else:
            invitation_file.seek(0)
            st.toast("🆕 首次处理招标文件，正在进行OCR...", icon="🔄")
            filtered_pages, inv_pc, inv_pdf_proc, filter_stats = process_invitation(
                invitation_file, ocr_engine, _cb(0.00, 0.40, "📘"))
            status_text.success(f"✅ 招标文件：{inv_pc}页→{len(filtered_pages)}页重点")
            # 保存缓存
            cache_mgr.save_invitation_cache(
                inv_bytes, invitation_file.name, len(inv_bytes),
                filtered_pages, filter_stats, inv_pc)
        render_invitation_filter_result(filtered_pages, filter_stats)

        # ---- 阶段2：投标文件（40-72%） ----
        status_text.info("📄 投标文件：文本提取+印章检测...")
        response_file.seek(0)
        resp_all_ocr, resp_full_text, resp_pc, resp_pdf_proc, seal_pages = process_response(
            response_file, ocr_engine, _cb(0.40, 0.32, "📄"))
        status_text.success(
            f"✅ 投标文件：{resp_pc}页，{len(resp_all_ocr)}条文本"
            + (f"，检测到{len(seal_pages)}页有红色印章" if seal_pages else ""))

        # ---- 数字签名验证 ----
        response_file.seek(0)
        sig_result = SigValidator.check_signatures(response_file.read())

        # ---- 阶段3：交叉审核（72-82%） ----
        status_text.info("🔍 交叉审核：违规检测+截图...")
        violations, compliant_items, review_summary, keyword_results, position_checklist = (
            process_cross_review(filtered_pages, resp_all_ocr, resp_full_text,
                                 inv_pdf_proc, resp_pdf_proc,
                                 _cb(0.72, 0.10, "🔍")))
        review_summary["invitation_total_pages"] = inv_pc
        review_summary["invitation_key_pages"] = len(filtered_pages)
        review_summary["response_total_pages"] = resp_pc
        review_summary["seal_pages_detected"] = len(seal_pages) if seal_pages else 0
        status_text.success(f"✅ 交叉审核：{len(violations)}违规 / {len(compliant_items)}合规")

        # ---- 手写体OCR：对签名类关键字进行区域裁切+高精度识别 ----
        signature_kws = [item["keyword"] for item in position_checklist
                         if item.get("type") == "签名区域" and not item["found"]]
        if signature_kws and resp_pdf_proc is not None:
            all_handwriting_results = []
            for pn in range(resp_pc):
                areas = resp_pdf_proc.detect_signature_areas(pn, signature_kws)
                for area in areas:
                    try:
                        hw_results = ocr_engine.recognize(area["cropped_img"])
                        for hr in hw_results:
                            hr["page_display"] = area["page_display"]
                            hr["source_keyword"] = area["keyword"]
                            hr["is_handwriting_ocr"] = True
                        all_handwriting_results.extend(hw_results)
                    except Exception as e:
                        logger.warning(f"手写OCR第{pn+1}页失败：{e}")
            if all_handwriting_results:
                logger.info(f"手写体OCR完成：{len(all_handwriting_results)}条结果")
                # 合并到 keyword_results
                for hw_item in all_handwriting_results:
                    kw = hw_item.get("source_keyword", "")
                    if kw:
                        keyword_results.setdefault(kw, []).append(hw_item)
                # 重新生成 position_checklist
                checker2 = CoordinateChecker()
                position_checklist = checker2.check_positions(keyword_results)

        # ---- 展示折叠结果 ----
        render_keyword_match_summary(keyword_results)
        render_position_check(position_checklist)

        # ---- 阶段4：AI审核（82-100%） ----
        progress_bar.progress(84, "🤖 组装审核数据...")
        _update_eta(0.84)
        status_text.info("🤖 正在调用 DeepSeek AI 进行交叉审核...")

        inv_key_text = "\n".join(
            f"【招标第{fp['page_display']}页】命中：{', '.join(fp['matched_keywords'])}\n{fp['ocr_text']}\n"
            for fp in filtered_pages
        )
        ocr_summary = (
            f"投标文件 {resp_pc} 页，识别 {len(resp_all_ocr)} 条文本。\n"
            + (f"红色印章检测：{len(seal_pages)} 页有印章。\n" if seal_pages else "")
            + "关键字匹配：\n"
        )
        for kw, matches in keyword_results.items():
            ocr_summary += f"  - {kw}：{'✅ ' + str(len(matches)) + '处' if matches else '❌ 未检测到'}\n"

        cl_lines = []
        for item in position_checklist:
            if item["found"]:
                cl_lines.append(f"✅ {item['keyword']} [{item['type']}]：置信度{item['confidence']:.0%} | {item['position']}")
            else:
                cl_lines.append(f"❌ {item['keyword']} [{item['type']}]：{item['status']}")
        checklist_text = "\n".join(cl_lines)

        vl_lines = []
        for v in violations:
            vl_lines.append(
                f"- [{v['severity']}风险] {v['category']} | {v['problem_summary']}\n"
                f"  要求：{v['requirement_detail']}\n  建议：{v['fix_suggestion']}"
            )
        violations_summary = "\n".join(vl_lines) if vl_lines else "预检未发现明显违规项"

        progress_bar.progress(88, "🤖 正在调用 DeepSeek AI...")
        _update_eta(0.88)
        ai_reviewer = AIReviewer()
        with st.spinner("🤖 AI 深度对标分析中（约30-60秒）..."):
            t0 = time.time()
            ai_report = ai_reviewer.review(
                invitation_text=inv_key_text, response_full_text=resp_full_text,
                ocr_summary=ocr_summary, violations_summary=violations_summary,
                position_checklist=checklist_text,
            )
            ai_t = time.time() - t0

        # ---- 阶段4.5：AI复核反馈循环 ----
        feedback_applied = False
        if ai_report and not ai_report.startswith("❌") and resp_pdf_proc is not None:
            mentioned_pages = _parse_page_numbers_from_report(ai_report)
            valid_pages = [
                p for p in mentioned_pages
                if 1 <= p <= resp_pc
            ]
            if valid_pages:
                st.toast(
                    f"🔍 AI报告中提到第{valid_pages}页，正在高精度复核...", icon="🔄")
                supplemental_parts = []
                for pn in valid_pages:
                    page_idx = pn - 1
                    try:
                        reocr_img = resp_pdf_proc.render_page(page_idx, dpi=300)
                        reocr_results = ocr_engine.recognize(reocr_img)
                        reocr_text = "\n".join(
                            r.get("text", "") for r in reocr_results
                        )
                        supplemental_parts.append(
                            f"=== 投标文件第{pn}页（高精度OCR，300 DPI）===\n{reocr_text}\n"
                        )
                    except Exception as e:
                        logger.warning(f"复核OCR第{pn}页失败：{e}")

                if supplemental_parts:
                    supplemental = "\n".join(supplemental_parts)
                    with st.spinner("🔄 AI复核中（基于高精度补充OCR）..."):
                        t_fb = time.time()
                        updated = ai_reviewer.review_with_feedback(
                            ai_report, supplemental)
                        if updated != ai_report:
                            ai_report = updated
                            feedback_applied = True
                        fb_t = time.time() - t_fb
                        if feedback_applied:
                            st.toast(
                                f"✅ AI复核完成，报告已更新（耗时{_fmt_time(fb_t)}）",
                                icon="✅")
                        else:
                            st.toast("ℹ️ AI复核确认原结论无误", icon="ℹ️")

        # 保存反馈信息到session_state（供开发人员选项显示）
        st.session_state.feedback_info = {
            "applied": feedback_applied,
            "pages": valid_pages,
            "time": _fmt_time(fb_t) if feedback_applied or (valid_pages and supplemental_parts) else "N/A",
        }

        total_t = time.time() - pipeline_start
        review_summary["processing_time_seconds"] = total_t

        _update_eta(1.0)
        progress_bar.progress(100, f"✅ 审核完成！总耗时 {_fmt_time(total_t)}")
        status_text.success(f"✅ 全部完成！总耗时 {_fmt_time(total_t)}（AI分析 {_fmt_time(ai_t)}）")

        st.session_state.processing_done = True
        st.session_state.review_results = {
            "violations": violations, "compliant_items": compliant_items,
            "summary": review_summary, "filter_stats": filter_stats,
            "invitation_pages": filtered_pages,
            "sig_result": sig_result,
        }
        st.session_state.ai_report = ai_report
        st.session_state.ai_feedback_applied = feedback_applied

    except ValueError as e:
        st.error(str(e))
    except MemoryError:
        st.error("❌ 内存不足！请拆分PDF或关闭其他程序后重试。")
    except RuntimeError as e:
        st.error(str(e))
    except Exception as e:
        st.error(f"❌ 未预期错误：\n\n```\n{str(e)}\n```")
        logger.exception("未预期异常")
    finally:
        if inv_pdf_proc:
            inv_pdf_proc.cleanup()
        if resp_pdf_proc:
            resp_pdf_proc.cleanup()
        logger.info("临时文件已清理")


# ============================================================
# 渲染函数
# ============================================================

def render_invitation_filter_result(filtered_pages, filter_stats):
    with st.expander("📘 招标文件筛选结果", expanded=False):
        ca, cb, cc, cd = st.columns(4)
        ca.metric("总页数", filter_stats["total_pages"])
        cb.metric("重点页", filter_stats["key_pages"])
        cc.metric("过滤掉", filter_stats["filtered_out"])
        cd.metric("保留率", f"{filter_stats['retention_rate']:.1%}")
        kw_dist = filter_stats.get("keywords_distribution", {})
        if kw_dist:
            st.markdown("**关键字命中：**" + "　".join(
                f"`{k}`×{v}" for k, v in sorted(kw_dist.items(), key=lambda x: -x[1])[:12]
            ))
        if filtered_pages:
            st.markdown("**重点页列表：**")
            lines = []
            for fp in filtered_pages:
                lines.append(f"- 第**{fp['page_display']}**页 → {', '.join(fp['matched_keywords'][:4])}")
            st.markdown("\n".join(lines[:30]))
            if len(lines) > 30:
                st.caption(f"（共{len(lines)}页，仅显示前30页）")


def render_keyword_match_summary(keyword_results):
    """紧凑的关键字匹配摘要"""
    with st.expander("🔑 关键字匹配摘要", expanded=False):
        found_kw = {k: v for k, v in keyword_results.items() if v}
        missing_kw = {k: v for k, v in keyword_results.items() if not v}
        c1, c2 = st.columns(2)
        with c1:
            st.markdown(f"**✅ 已检测（{len(found_kw)}项）**")
            for kw, matches in found_kw.items():
                st.markdown(f"- {kw}：{len(matches)}处")
        with c2:
            st.markdown(f"**❌ 未检测（{len(missing_kw)}项）**")
            for kw in missing_kw:
                st.markdown(f"- {kw}")


def render_position_check(checklist):
    with st.expander("📍 签章位置校验", expanded=False):
        found_n = sum(1 for i in checklist if i["found"])
        c1, c2, c3 = st.columns(3)
        c1.metric("检查项", len(checklist))
        c2.metric("✅ 已检测", found_n)
        c3.metric("❌ 未检测", len(checklist) - found_n)
        for item in checklist:
            icon = "✅" if item["found"] else "❌"
            if item["found"]:
                pg = item.get("page_display", "?")
                st.markdown(f"{icon} **{item['keyword']}** `[{item['type']}]` — `{item['text']}`（{item['confidence']:.0%}，第{pg}页）")
            else:
                st.markdown(f"{icon} **{item['keyword']}** `[{item['type']}]` — {item['status']}")
                if item.get("suggestion"):
                    st.caption(f"　💡 {item['suggestion']}")


def render_signature_info(sig_result: dict):
    """数字签名验证结果"""
    with st.expander("🔏 数字签名验证", expanded=False):
        if sig_result.get("error"):
            st.warning(sig_result["error"])
            return

        if not sig_result.get("has_signatures"):
            st.info("ℹ️ 未检测到数字签名（该投标文件可能未进行数字签署）")
            return

        st.success(f"检测到 {sig_result['signature_count']} 个数字签名")
        for i, sig in enumerate(sig_result.get("signatures", [])):
            st.markdown(f"**签名 {i + 1}**")
            c1, c2 = st.columns(2)
            with c1:
                st.caption(f"签名域：{sig.get('field_name', '?')}")
                st.caption(f"签署者：{sig.get('signer', '?')}")
                st.caption(f"签署时间：{sig.get('signing_time', '?')}")
            with c2:
                valid = sig.get("valid", False)
                intact = sig.get("integrity_ok", False)
                if valid and intact:
                    st.success("✅ 有效")
                elif intact:
                    st.warning("⚠️ 完整性OK，但证书链验证未通过")
                else:
                    st.error("❌ 签名无效/文档已修改")
                if sig.get("coverage"):
                    st.caption(f"覆盖范围：{sig['coverage']}")
                if sig.get("issue"):
                    st.caption(f"备注：{sig['issue']}")
            st.markdown("---")


def render_review_summary(summary):
    st.markdown("---")
    st.header("📊 审核汇总")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("⚠️ 违规", summary.get("total_violations", 0))
    c2.metric("✅ 合规", summary.get("total_compliant", 0))
    c3.metric("📊 合规率", f"{summary.get('compliance_rate', 0):.1%}")
    c4.metric("📘 招标重点页", summary.get("invitation_key_pages", "?"))
    c5.metric("📄 投标页数", summary.get("response_total_pages", "?"))

    c1, c2, c3 = st.columns(3)
    with c1:
        sev = summary.get("violations_by_severity", {})
        st.markdown(f"🔴高:{sev.get('高',0)} 🟡中:{sev.get('中',0)} 🟢低:{sev.get('低',0)}")
    with c2:
        cat = summary.get("violations_by_category", {})
        st.markdown(" | ".join(f"{k}:{v}" for k, v in cat.items()) if cat else "无")
    with c3:
        st.caption(f"⏱ 耗时：{_fmt_time(summary.get('processing_time_seconds', 0))}")


def render_violation_card(violation, idx):
    """违规卡片：可展开查看截图"""
    vid = violation.get("violation_id", f"V-{idx}")
    severity = violation.get("severity", "低")
    category = violation.get("category", "未分类")
    colors = {"高": "#dc3545", "中": "#ffc107", "低": "#17a2b8"}

    with st.container():
        st.markdown(
            f"### {vid}　"
            f'<span style="background:{colors.get(severity,"#888")};color:white;padding:2px 8px;'
            f'border-radius:4px;font-size:13px;">{severity}风险</span>　'
            f'<span style="background:#6c757d;color:white;padding:2px 8px;'
            f'border-radius:4px;font-size:13px;">{category}</span>',
            unsafe_allow_html=True
        )

        # 文字说明（始终显示）
        st.markdown(f"**问题：** {violation.get('problem_summary', '')}")
        st.markdown(f"**要求：** {violation.get('requirement_detail', '')}")
        st.markdown(f"**原因：** {violation.get('violation_reason', '')}")
        st.markdown(f"**建议：** {violation.get('fix_suggestion', '')}")

        # 截图区域：可展开查看（点击放大/缩回）
        src_img = violation.get("source", {}).get("screenshot_bytes")
        ev_img = violation.get("evidence", {}).get("screenshot_bytes")
        src_pg = violation.get("source", {}).get("page_display", "?")
        ev_pg = violation.get("evidence", {}).get("page_display", "?")

        if src_img or ev_img:
            with st.expander("📷 点击查看截图对照（点击图片放大，支持滚轮缩放拖拽）", expanded=False):
                from modules.image_viewer import render_interactive_image
                cL, cR = st.columns(2)
                with cL:
                    src_label = f"**📘 招标文件（第{src_pg}页）**" if isinstance(src_pg, (int, float)) else "**📘 招标文件**"
                    st.markdown(src_label)
                    if src_img:
                        render_interactive_image(src_img, key=f"src_{vid}")
                    else:
                        st.caption("无截图")
                with cR:
                    ev_label = f"**📄 投标文件（第{ev_pg}页）**" if isinstance(ev_pg, (int, float)) else "**📄 投标文件**"
                    st.markdown(ev_label)
                    if ev_img:
                        render_interactive_image(ev_img, key=f"ev_{vid}")
                    else:
                        st.caption("无截图")
        st.markdown("---")


def render_violations_section(violations, compliant_items):
    st.markdown("---")
    st.header("🔍 审核详细结果")

    # 按严重度分组
    high_risk = [v for v in violations if v.get("severity") == "高"]
    mid_risk = [v for v in violations if v.get("severity") == "中"]
    low_risk = [v for v in violations if v.get("severity") == "低"]

    tv, tc = st.tabs([f"❌ 违规项（{len(violations)}）", f"✅ 合规项（{len(compliant_items)}）"])
    with tv:
        if not violations:
            st.success("🎉 未发现违规项！")
        else:
            # 高风险 — 默认展开
            with st.expander(f"🔴 高风险（{len(high_risk)}项）", expanded=True):
                if high_risk:
                    for idx, v in enumerate(high_risk):
                        render_violation_card(v, idx)
                else:
                    st.caption("无高风险项")

            # 中风险 — 默认折叠
            with st.expander(f"🟡 中风险（{len(mid_risk)}项）", expanded=False):
                if mid_risk:
                    offset = len(high_risk)
                    for idx, v in enumerate(mid_risk):
                        render_violation_card(v, offset + idx)
                else:
                    st.caption("无中风险项")

            # 低风险 — 默认折叠
            with st.expander(f"🟢 低风险（{len(low_risk)}项）", expanded=False):
                if low_risk:
                    offset = len(high_risk) + len(mid_risk)
                    for idx, v in enumerate(low_risk):
                        render_violation_card(v, offset + idx)
                else:
                    st.caption("无低风险项")

    with tc:
        if not compliant_items:
            st.info("暂无合规项记录")
        else:
            for idx, c in enumerate(compliant_items[:30]):
                st.markdown(f"✅ **{c.get('problem_summary', '合规')}** — 招标第{c.get('source',{}).get('page_display','?')}页")
                if c.get("requirement_detail"):
                    st.caption(f"　{c['requirement_detail']}")
            if len(compliant_items) > 30:
                st.caption(f"（仅显示前30条，共{len(compliant_items)}条）")


def render_ai_report(report: str):
    st.markdown("---")
    with st.expander("🤖 AI 智能审核报告", expanded=True):
        if st.session_state.get("ai_feedback_applied"):
            st.info("🔄 已结合高精度补充OCR完成AI复核，报告已更新")
        if report.startswith("❌"):
            st.error(report)
        else:
            st.success("✅ 审核完成")
            st.markdown(report)
            st.download_button("📥 下载审核报告（Markdown）", data=report,
                               file_name="投标交叉审核报告.md", mime="text/markdown")


# ============================================================
def main():
    st.title("📄 投标文件智能审核工具（交叉审核模式）")
    st.markdown(
        "> PyMuPDF文本提取 + PaddleOCR补充识别 + 红色印章检测 + DeepSeek交叉审核 "
        "— 上传招标+投标文件，自动对标审核，**违规图文对照展示**。"
    )
    st.markdown("---")
    use_gpu = render_sidebar()
    inv_file, resp_file = render_dual_upload_section()

    both_ready = inv_file is not None and resp_file is not None
    if not both_ready:
        missing = []
        if inv_file is None:
            missing.append("招标文件")
        if resp_file is None:
            missing.append("投标文件")
        st.markdown("---")
        if inv_file is None and resp_file is None:
            st.info("👆 请上传「招标文件」和「投标文件」，然后点击「开始交叉审核」。")
        else:
            st.warning(f"⚠️ 还缺少：**{'、'.join(missing)}**")
        return

    st.markdown("---")
    st.header("🚀 第二步：开始交叉审核")
    _, cbtn, _ = st.columns([1, 2, 1])
    with cbtn:
        if st.button("🚀 开始交叉审核", type="primary", use_container_width=True):
            process_dual_bid(inv_file, resp_file, use_gpu)

    if st.session_state.processing_done and st.session_state.review_results:
        r = st.session_state.review_results
        render_review_summary(r["summary"])
        render_signature_info(r.get("sig_result", {}))
        if st.session_state.ai_report:
            render_ai_report(st.session_state.ai_report)
        render_violations_section(r["violations"], r["compliant_items"])


if __name__ == "__main__":
    main()
