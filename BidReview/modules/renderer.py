"""
UI 渲染模块
==========
所有 Streamlit 页面渲染函数 + 时间格式化工具
"""
import streamlit as st
from modules.image_viewer import render_interactive_image


def _fmt_time(seconds: float) -> str:
    """格式化秒数为 X分Y秒"""
    m, s = divmod(int(seconds), 60)
    return f"{m}分{s}秒" if m > 0 else f"{s}秒"


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
# 以下为 LLM 新版引擎专用的渲染函数
# ============================================================

def render_extraction_info(extraction: dict):
    """渲染 LLM 提取的结构化信息"""
    if not extraction:
        return
    with st.expander("📋 结构化信息提取", expanded=True):
        c1, c2, c3 = st.columns(3)
        with c1:
            st.metric("投标报价", extraction.get("bid_price") or "未识别")
            st.metric("统一社会信用代码", extraction.get("credit_code") or "未识别")
            st.metric("投标有效期", extraction.get("bid_validity") or "未识别")
        with c2:
            st.metric("公司名称", extraction.get("company_name") or "未识别")
            st.metric("法定代表人", extraction.get("legal_representative") or "未识别")
            st.metric("工期/服务期", extraction.get("construction_period") or "未识别")
        with c3:
            st.metric("授权代表", extraction.get("contact_person") or "未识别")


def render_seal_analysis(extraction: dict):
    """渲染印章识别分析结果"""
    seals = extraction.get("seals", [])
    if not seals:
        return
    with st.expander("🔴 印章识别结果", expanded=False):
        st.caption(f"共检测到 {len(seals)} 个印章")
        cols = st.columns(min(len(seals), 3))
        for i, seal in enumerate(seals):
            with cols[i % 3]:
                seal_type = seal.get("type", "未知")
                icon_map = {"公章": "🏛", "法人章": "👤", "合同章": "📝", "财务章": "💰"}
                icon = icon_map.get(seal_type, "🔴")
                st.markdown(
                    f"{icon} **{seal_type}**\n\n"
                    f"文字：`{seal.get('text', '?')}`\n\n"
                    f"位置：{seal.get('position', '?')}\n\n"
                    f"页码：第{seal.get('page', '?')}页"
                )


def render_legal_rep_check(extraction: dict):
    """渲染法定代表人签章比对结果"""
    legal_check = extraction.get("legal_rep_check", {})
    if not legal_check:
        return
    with st.expander("👤 法定代表人签章比对", expanded=False):
        declared = legal_check.get("declared_name", "?")
        seal_name = legal_check.get("seal_name", "?")
        match = legal_check.get("match", False)

        c1, c2 = st.columns(2)
        with c1:
            st.markdown(f"**声明法定代表人**\n\n# {declared}")
        with c2:
            st.markdown(f"**印章文字**\n\n# {seal_name}")

        if match:
            st.success(f"✅ 法定代表人姓名与印章一致：{legal_check.get('detail', '')}")
        else:
            st.error(f"❌ 法定代表人姓名与印章不一致：{legal_check.get('detail', '')}")


def render_chapter_structure(extraction: dict):
    """渲染文档章节结构树"""
    chapters = extraction.get("chapter_structure", [])
    if not chapters:
        return
    with st.expander("📑 投标文件章节结构", expanded=False):
        doc_sec = extraction.get("document_sections", {})
        if doc_sec:
            sec_cols = st.columns(3)
            with sec_cols[0]:
                st.info(f"📄 正文：{doc_sec.get('body_pages', '?')}")
            with sec_cols[1]:
                st.info(f"💼 商务文件：{doc_sec.get('business_pages', '?')}")
            with sec_cols[2]:
                st.info(f"🔧 技术文件：{doc_sec.get('technical_pages', '?')}")

        tree_lines = []
        for ch in chapters:
            prefix = "  " * (ch.get("level", 1) - 1)
            icon = {1: "📘", 2: "📎", 3: "•"}.get(ch.get("level", 1), "•")
            tree_lines.append(
                f"{prefix}{icon} **{ch.get('title', '?')}** "
                f"— 第{ch.get('page', '?')}页"
            )
        st.markdown("\n".join(tree_lines))


def render_llm_violations_section(violations: list, compliant_items: list):
    """渲染 LLM 引擎的违规项（复用违规卡片样式，增加自定义规则标记）"""
    st.markdown("---")
    st.header("🔍 审核详细结果（LLM 引擎）")

    high_risk = [v for v in violations if v.get("severity") == "高"]
    mid_risk = [v for v in violations if v.get("severity") == "中"]
    low_risk = [v for v in violations if v.get("severity") == "低"]

    tv, tc = st.tabs([f"❌ 违规项（{len(violations)}）", f"✅ 合规项（{len(compliant_items)}）"])
    with tv:
        if not violations:
            st.success("🎉 未发现违规项！")
        else:
            with st.expander(f"🔴 高风险（{len(high_risk)}项）", expanded=True):
                if high_risk:
                    for idx, v in enumerate(high_risk):
                        _render_llm_violation_card(v, idx, "高")
                else:
                    st.caption("无高风险项")

            with st.expander(f"🟡 中风险（{len(mid_risk)}项）", expanded=False):
                if mid_risk:
                    for idx, v in enumerate(mid_risk):
                        _render_llm_violation_card(v, idx, "中")
                else:
                    st.caption("无中风险项")

            with st.expander(f"🟢 低风险（{len(low_risk)}项）", expanded=False):
                if low_risk:
                    for idx, v in enumerate(low_risk):
                        _render_llm_violation_card(v, idx, "低")
                else:
                    st.caption("无低风险项")

    with tc:
        if not compliant_items:
            st.info("暂无合规项记录")
        else:
            for idx, c in enumerate(compliant_items[:30]):
                rule_tag = " [自定义规则]" if "自定义规则" in c.get("category", "") else ""
                st.markdown(f"✅ **{c.get('problem_summary', '合规')}**{rule_tag}")
                if c.get("fix_suggestion"):
                    st.caption(f"　💡 {c['fix_suggestion']}")


def _render_llm_violation_card(violation: dict, idx: int, severity_group: str):
    """渲染单个 LLM 违规卡片"""
    vid = violation.get("violation_id", f"LLM-{idx}")
    severity = violation.get("severity", "低")
    category = violation.get("category", "未分类")
    colors = {"高": "#dc3545", "中": "#ffc107", "低": "#17a2b8"}

    is_custom = "自定义规则" in category
    rule_badge = ' <span style="background:#6f42c1;color:white;padding:1px 6px;border-radius:3px;font-size:11px;">自定义规则</span>' if is_custom else ""

    with st.container():
        st.markdown(
            f"### {vid}　"
            f'<span style="background:{colors.get(severity,"#888")};color:white;padding:2px 8px;'
            f'border-radius:4px;font-size:13px;">{severity}风险</span>　'
            f'<span style="background:#6c757d;color:white;padding:2px 8px;'
            f'border-radius:4px;font-size:13px;">{category}</span>'
            f'{rule_badge}',
            unsafe_allow_html=True
        )

        st.markdown(f"**问题：** {violation.get('problem_summary', '')}")
        st.markdown(f"**要求：** {violation.get('requirement_detail', '')}")
        st.markdown(f"**原因：** {violation.get('violation_reason', '')}")
        st.markdown(f"**建议：** {violation.get('fix_suggestion', '')}")

        # 展示截图（若有）
        ev = violation.get("evidence", {})
        ev_img = ev.get("screenshot_bytes") if isinstance(ev, dict) else None
        ev_pg = ev.get("page_display") if isinstance(ev, dict) else None

        if ev_img:
            with st.expander("📷 查看截图（点击放大）", expanded=False):
                st.markdown(f"**📄 投标文件（第{ev_pg}页）**" if ev_pg else "**📄 投标文件**")
                from modules.image_viewer import render_interactive_image
                render_interactive_image(ev_img, key=f"llm_ev_{vid}")
        st.markdown("---")
