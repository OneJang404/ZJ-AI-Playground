"""
处理流水线模块
=============
招标文件处理、投标文件处理、交叉审核、主流水线编排
"""
import streamlit as st
import logging
import time
import re
import sys
import os
from io import BytesIO
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.pdf_processor import PDFProcessor
from modules.ocr_engine import OCREngine
from modules.page_filter import PageFilter
from modules.coordinate_checker import CoordinateChecker
from modules.violation_checker import ViolationChecker
from modules.ai_reviewer import AIReviewer
from modules.sig_validator import SigValidator
from modules.renderer import (
    _fmt_time,
    render_invitation_filter_result,
    render_keyword_match_summary,
    render_position_check,
)

logger = logging.getLogger("BidReview")


# ============================================================
@st.cache_resource(show_spinner=False)
def get_ocr_engine(use_gpu: bool = False) -> OCREngine:
    return OCREngine(use_gpu=use_gpu)


# ============================================================
def _draw_red_boxes(img_bytes, boxes, width=3):
    """在图片上画红色矩形框标注关键区域"""
    try:
        img = Image.open(BytesIO(img_bytes))
        draw = ImageDraw.Draw(img)
        for box in boxes:
            if box and len(box) >= 4:
                if isinstance(box[0], (int, float)):
                    x0, y0, x1, y1 = int(box[0]), int(box[1]), int(box[2]), int(box[3])
                    for i in range(width):
                        draw.rectangle([x0 - i, y0 - i, x1 + i, y1 + i], outline="red")
                else:
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

    range_pattern = r'第\s*(\d+)\s*[-–—至到]\s*(\d+)\s*页'
    for m in re.finditer(range_pattern, report_text):
        start, end = int(m.group(1)), int(m.group(2))
        for p in range(start, min(end + 1, start + 6)):
            pages.add(p)

    cleaned = re.sub(range_pattern, '', report_text)

    for m in re.finditer(r'第\s*(\d+)\s*页', cleaned):
        pages.add(int(m.group(1)))

    for m in re.finditer(r'第\s*([\d、，,\s]+)\s*页', cleaned):
        nums = re.split(r'[、，,\s]+', m.group(1))
        for n in nums:
            if n.strip().isdigit():
                pages.add(int(n.strip()))

    return sorted(pages)[:5]


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

    if progress_callback:
        progress_callback(0.03, f"提取文本（{page_count} 页）...")
    text_by_page = pdf_proc.extract_text_per_page()
    if progress_callback:
        progress_callback(0.10, "筛选重点规则页面...")
    keyword_matches = PageFilter.filter_by_text(
        text_by_page, keywords=st.session_state.custom_filter_keywords)
    if progress_callback:
        progress_callback(0.12, f"筛选：{page_count}页→{len(keyword_matches)}页重点")

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

    for i, km in enumerate(key_matches):
        pn = km["page_num"]
        pct = 0.48 + (i + 1) / max(len(key_matches), 1) * 0.22
        if progress_callback and (i % 3 == 0 or i == len(key_matches) - 1):
            progress_callback(pct, f"投标文件重点页：第{pn+1}/{page_count}页")

        pymupdf_text = text_by_page[pn]
        has_enough_text = len(pymupdf_text.strip()) > 30

        try:
            img_bytes = pdf_proc.render_page(pn, dpi=200)

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

    inv_boxes_by_page: dict = {}
    resp_boxes_by_page: dict = {}

    for v in violations:
        src_pn = v["source"].get("page_num")
        src_bbox = v["source"].get("bbox")
        if isinstance(src_pn, (int, float)):
            inv_boxes_by_page.setdefault(int(src_pn), []).append(src_bbox)
        ev_pn = v["evidence"].get("page_display")
        ev_bbox = v["evidence"].get("bbox")
        if isinstance(ev_pn, (int, float)):
            resp_boxes_by_page.setdefault(int(ev_pn), []).append(ev_bbox)

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
                for hw_item in all_handwriting_results:
                    kw = hw_item.get("source_keyword", "")
                    if kw:
                        keyword_results.setdefault(kw, []).append(hw_item)
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
        fb_t = 0
        valid_pages = []
        supplemental_parts = []
        if ai_report and not ai_report.startswith("❌") and resp_pdf_proc is not None:
            mentioned_pages = _parse_page_numbers_from_report(ai_report)
            valid_pages = [
                p for p in mentioned_pages
                if 1 <= p <= resp_pc
            ]
            if valid_pages:
                st.toast(
                    f"🔍 AI报告中提到第{valid_pages}页，正在高精度复核...", icon="🔄")
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
