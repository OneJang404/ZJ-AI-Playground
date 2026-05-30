"""
PDF 文本提取查看器 — 开发工具
用 PyMuPDF 查看 PDF 内部文本、词条、图片块等，辅助调试和规则编写。
"""
import streamlit as st
import fitz

st.set_page_config(page_title="PDF 查看器", page_icon="🔍", layout="wide")

st.title("PDF 文本提取查看器")
st.caption("直接展示 PyMuPDF 能提取到的全部文本内容与结构信息")

# ---- 文件来源：可从主页缓存选，也可独立上传 ----
col1, col2 = st.columns([3, 1])
with col1:
    pdf_file = st.file_uploader("上传 PDF 文件", type=["pdf"], key="pdf_inspector_upload")
with col2:
    if pdf_file:
        st.caption(f"{pdf_file.name}\n{pdf_file.size / 1024 / 1024:.1f} MB")

if not pdf_file:
    st.info("上传一个 PDF 文件即可查看 PyMuPDF 提取结果。")
    st.stop()

# ---- 打开 PDF ----
doc = fitz.open(stream=pdf_file.read(), filetype="pdf")

# ---- 概览 ----
meta_col1, meta_col2, meta_col3, meta_col4 = st.columns(4)
with meta_col1:
    st.metric("总页数", doc.page_count)
with meta_col2:
    total_imgs = sum(len(page.get_images()) for page in doc)
    st.metric("内嵌图片", total_imgs)
with meta_col3:
    total_img_blocks = sum(
        sum(1 for b in page.get_text("dict")["blocks"] if b["type"] == 1)
        for page in doc
    )
    st.metric("图片块 (type=1)", total_img_blocks)
with meta_col4:
    st.metric("文件格式", doc.metadata.get("format", "PDF"))

# ---- 页选择 ----
page_num = st.number_input(
    "跳转页码", min_value=1, max_value=doc.page_count, value=1, step=1
)
page = doc[page_num - 1]

# ---- 文本内容 ----
st.subheader(f"第 {page_num} 页文本内容")
text = page.get_text()
if text.strip():
    st.text_area("get_text() 输出", text, height=400, key=f"text_p{page_num}", label_visibility="collapsed")
else:
    st.warning("该页 get_text() 返回空（可能是扫描件/纯图片 PDF）")

# ---- 结构详情 ----
st.subheader("结构分析")
c1, c2, c3 = st.columns(3)

words = page.get_text("words")
with c1:
    st.metric("词条数 (words)", len(words))
    if words:
        with st.expander("查看前 10 个词条"):
            for w in words[:10]:
                # w = [x0, y0, x1, y1, text, block_no, line_no, word_no]
                st.caption(f"({w[0]:.0f},{w[1]:.0f})-({w[2]:.0f},{w[3]:.0f}) `{w[4]}`")

blocks = page.get_text("dict")["blocks"]
text_blocks = [b for b in blocks if b["type"] == 0]
img_blocks = [b for b in blocks if b["type"] == 1]
with c2:
    st.metric("文本块", len(text_blocks))
    st.metric("图片块", len(img_blocks))
    if img_blocks:
        with st.expander("图片块坐标"):
            for i, b in enumerate(img_blocks):
                st.caption(f"#{i} ({b['bbox'][0]:.0f},{b['bbox'][1]:.0f})-({b['bbox'][2]:.0f},{b['bbox'][3]:.0f})")

images = page.get_images(full=True)
with c3:
    st.metric("内嵌图片", len(images))
    if images:
        with st.expander("图片详情"):
            for i, img in enumerate(images[:10]):
                st.caption(f"#{i} {img[1]}x{img[2]} ({img[0]}B)")

# ---- 表格检测 ----
st.subheader("表格检测")
tabs = page.find_tables()
if tabs and tabs.tables:
    st.success(f"检测到 {len(tabs.tables)} 个表格")
    for i, table in enumerate(tabs.tables):
        with st.expander(f"表格 #{i+1} ({len(table.row_count)}行 × {len(table.col_count)}列)"):
            st.dataframe([[cell.to_dict().get("text", "") for cell in row] for row in table.rows])
else:
    st.caption("find_tables() 未检测到表格（文字型 PDF 的表格通常不是标准 PDF table 结构）")

doc.close()
