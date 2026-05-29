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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from modules.pipeline import get_ocr_engine, process_invitation, process_dual_bid
from modules.renderer import (
    _fmt_time,
    render_invitation_filter_result,
    render_review_summary,
    render_signature_info,
    render_ai_report,
    render_violations_section,
)

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

if "custom_filter_keywords" not in st.session_state:
    from modules.page_filter import FILTER_KEYWORDS as _def_fk
    st.session_state.custom_filter_keywords = list(_def_fk)
if "custom_bid_keywords" not in st.session_state:
    from modules.ocr_engine import BID_KEYWORDS as _def_bk
    st.session_state.custom_bid_keywords = list(_def_bk)


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

    cm = CacheManager()
    cached_files = cm.list_cached_files()

    # ---- 独立预上传招标文件 ----
    with st.expander("📘 提前处理招标文件（可选）", expanded=False):
        st.caption("单独上传招标文件进行预处理并缓存，稍后可快速开始审核。")

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

                cached = cm.load_invitation_cache(inv_bytes)
                if cached:
                    st.success(
                        f"💾 该文件已缓存（{cached['cached_at']}），"
                        f"{cached['page_count']}页→{len(cached['filtered_pages'])}页重点，无需重复处理。")
                    render_invitation_filter_result(cached["filtered_pages"], cached["filter_stats"])
                else:
                    inv_io = BytesIO(inv_bytes)
                    inv_io.name = pre_inv.name

                    progress_bar = st.progress(0, "正在处理招标文件...")
                    status_text = st.empty()
                    eta_c1, eta_c2 = st.columns(2)
                    eta_left = eta_c1.empty()
                    eta_right = eta_c2.empty()
                    _ocr_t0 = {"v": None}
                    _ocr_total = {"v": 0}

                    def _pre_cb(frac, msg=""):
                        pct = int(frac * 100)
                        progress_bar.progress(min(pct, 99), msg or "正在处理招标文件...")
                        if msg:
                            status_text.info(msg)

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

                    st.session_state._preprocess_done = True
                    st.session_state._preprocess_msg = (
                        f"✅ 预处理完成！{inv_pc}页→{len(filtered_pages)}页重点，已缓存。"
                        f"请上传投标文件开始审核。")
                    st.session_state._preprocess_filtered = filtered_pages
                    st.session_state._preprocess_stats = filter_stats
                    st.rerun()

    st.header("📤 第一步：上传文件")

    if "cache_dropdown" not in st.session_state:
        st.session_state.cache_dropdown = "无"

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

        inv_file = st.file_uploader("上传招标文件（PDF）", type=["pdf"],
                                     key="inv_uploader", label_visibility="collapsed")

        if inv_file is not None:
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
