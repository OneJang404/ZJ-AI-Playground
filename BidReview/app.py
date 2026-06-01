"""
================================================================================
 投标文件智能审核工具
================================================================================
 基于 Streamlit + Qwen3-VL 多模态大模型 + PyMuPDF，
 支持招标/投标文件自动对标审核、自定义规则、文档理解、印章识别。

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
import threading
from io import BytesIO
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ---- 抑制后台线程访问 session_state 时的 ScriptRunContext 警告 ----
logging.getLogger("streamlit.runtime.scriptrunner").setLevel(logging.ERROR)
# 防止 HTTP 调试日志泄露 API 密钥
logging.getLogger("urllib3").setLevel(logging.WARNING)

from modules.renderer import (
    _fmt_time,
    render_review_summary,
    render_signature_info,
    render_extraction_info,
    render_seal_analysis,
    render_legal_rep_check,
    render_chapter_structure,
    render_llm_violations_section,
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
    ("feedback_info", {}),
    ("custom_rules", ""),       # 用户自定义的自然语言审核规则
    # 异步审核状态管理
    ("_review_status", "idle"), # "idle" | "running" | "done" | "cancelled"
    ("_review_progress", {"frac": 0.0, "msg": ""}),
    ("_review_result_raw", None),
    ("_review_start_time", 0.0),
]:
    if _key not in st.session_state:
        st.session_state[_key] = _default

# ---- 耗时历史记录（用于精准预估剩余时间） ----
import json as _json
_TIMING_FILE = Path(__file__).resolve().parent / ".bidreview_cache" / "timing_history.json"

def _load_timing_history() -> list:
    """加载历史耗时记录 [{"size_mb": float, "time_s": float, "model": str}, ...]"""
    try:
        if _TIMING_FILE.exists():
            data = _json.loads(_TIMING_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []

def _save_timing_record(size_mb: float, time_s: float, model: str):
    """保存一条耗时记录（最多保留 30 条）"""
    records = _load_timing_history()
    from datetime import datetime as _dt
    records.append({
        "size_mb": round(size_mb, 2),
        "time_s": round(time_s, 1),
        "model": model,
        "date": _dt.now().strftime("%m-%d %H:%M"),
    })
    _TIMING_FILE.parent.mkdir(parents=True, exist_ok=True)
    _TIMING_FILE.write_text(_json.dumps(records[-30:], ensure_ascii=False), encoding="utf-8")

def _predict_from_history(total_size_mb: float) -> float | None:
    """
    从历史记录中预估总耗时。
    匹配 ±40% 文件大小范围内的记录取平均，无匹配则取最近 5 条平均。
    无历史记录返回 None。
    """
    records = _load_timing_history()
    if not records:
        return None
    lo = total_size_mb * 0.6
    hi = total_size_mb * 1.4
    similar = [r for r in records if lo <= r["size_mb"] <= hi]
    if similar:
        return sum(r["time_s"] for r in similar) / len(similar)
    recent = records[-5:]
    return sum(r["time_s"] for r in recent) / len(recent)


def render_llm_sidebar():
    """AI审核模式侧边栏：自定义规则 + 缓存管理"""
    bypass_cache = False
    with st.sidebar:
        st.markdown("## 📄 投标文件审核工具")
        st.caption("AI审核模式  |  Qwen3-VL 多模态")

        st.markdown("---")
        # 自定义审核规则（缓存 + 精炼 + 逐条管理）
        with st.expander("📝 自定义审核规则", expanded=True):
            st.html("""
            <style>
            [data-testid="stTextInput"] input { height: unset !important; padding: 8px 12px !important; }
            [data-testid="stFileUploaderDropzoneInstructions"] { display: none !important; }
            </style>
            """)
            from modules.cache_manager import CacheManager
            import uuid as _uuid
            _cm = CacheManager()

            # 首次加载从缓存恢复
            if "_rules_list" not in st.session_state:
                cached = _cm.load_rules()
                st.session_state._rules_list = cached if cached else []
                _lines = [r.get("refined") or r.get("raw", "") for r in st.session_state._rules_list]
                st.session_state.custom_rules = "\n".join(_lines)

            # ---- 添加新规则 ----
            c_add1, c_add2 = st.columns([5, 1], vertical_alignment="center")
            with c_add1:
                new_rule = st.text_input(
                    "添加规则", key="rule_template",
                    placeholder="例：所有报价保留两位小数",
                    label_visibility="collapsed",
                )
            with c_add2:
                refine_all = st.button("✨ 精炼", use_container_width=True,
                                       help="让AI把规则提炼为简洁格式",
                                       disabled=len(st.session_state._rules_list) == 0)

            if new_rule:
                raw = new_rule.strip()
                existing_raws = {r["raw"] for r in st.session_state._rules_list}
                if raw not in existing_raws:
                    rule = {"id": _uuid.uuid4().hex[:8], "raw": raw,
                            "refined": "", "created_at": time.strftime("%m-%d %H:%M")}
                    st.session_state._rules_list.append(rule)
                    _cm.save_rules(st.session_state._rules_list)
                    st.rerun()

            # ---- LLM 精炼 ----
            if refine_all:
                with st.spinner("AI 精炼规则中..."):
                    from modules.llm_engine import LLMEngine
                    engine = LLMEngine()
                    raws = [r["raw"] for r in st.session_state._rules_list]
                    refined = engine.refine_rules(raws)
                    for i, item in enumerate(refined):
                        if i < len(st.session_state._rules_list):
                            st.session_state._rules_list[i]["refined"] = item["refined"]
                    _cm.save_rules(st.session_state._rules_list)
                st.rerun()

            # ---- 规则展示 ----
            rules = st.session_state._rules_list
            if rules:
                _lines = [r.get("refined") or r.get("raw", "") for r in rules]
                st.session_state.custom_rules = "\n".join(_lines)
                st.caption(f"当前 {len(rules)} 条规则")
                for i, rule in enumerate(rules):
                    text = rule.get("refined") or rule.get("raw", "")
                    is_refined = bool(rule.get("refined"))
                    c1, c2 = st.columns([20, 1], vertical_alignment="center")
                    with c1:
                        color = "#10b981" if is_refined else "#6b7280"
                        indicator = "◆" if is_refined else "◇"
                        st.markdown(
                            f'<div style="padding:3px 8px;margin:1px 0;'
                            f'border-left:3px solid {color};border-radius:0 6px 6px 0;'
                            f'font-size:13px;line-height:2.2;background:#f8fafc;">'
                            f'<span style="color:{color};margin-right:6px;">{indicator}</span>{text}'
                            f'</div>',
                            unsafe_allow_html=True,
                        )
                    with c2:
                        if st.button("✕", key=f"delrule_{rule['id']}", help="删除此规则"):
                            st.session_state._rules_list.pop(i)
                            _cm.save_rules(st.session_state._rules_list)
                            st.rerun()

                if st.button("🗑️ 清空全部规则", use_container_width=True):
                    st.session_state._rules_list = []
                    st.session_state.custom_rules = ""
                    _cm.clear_rules()
                    st.rerun()
            else:
                st.caption("暂无自定义规则，在上方输入框添加")
                st.session_state.custom_rules = ""

        # 缓存控制（独立于规则编辑区）
        bypass_cache = st.checkbox(
            "🔄 跳过缓存（强制重新审核）",
            value=False,
            help="勾选后将忽略已有缓存，重新调用LLM审核（会产生API费用）",
        )

        st.markdown("---")
        # 缓存管理
        with st.expander("💾 缓存管理", expanded=False):
            from modules.cache_manager import CacheManager
            cm = CacheManager()
            cached_files = cm.list_cached_files()
            if cached_files:
                st.caption(f"已缓存 {len(cached_files)} 个文件")
                for cf in cached_files[:10]:
                    c1, c2 = st.columns([9, 1], vertical_alignment="center")
                    with c1:
                        st.caption(f"{cf['name'][:28]}（{cf['page_count']}p, {cf['cached_at']}）")
                    with c2:
                        if st.button("🗑", key=f"delcache2_{cf['hash'][:12]}", help="删除此缓存"):
                            cm.clear_cache_by_hash(cf["hash"])
                            st.rerun()
                if len(cached_files) > 10:
                    st.caption(f"（共 {len(cached_files)} 个，仅显示最近 10 个）")
                if st.button("🗑️ 清除所有缓存", use_container_width=True):
                    count = cm.clear_all_cache()
                    st.success(f"已清除 {count} 个缓存")
                    st.rerun()
            else:
                st.caption("暂无缓存文件")

    return bypass_cache


def render_llm_dual_upload_section():
    """双文件上传区：缓存下拉 + 文件上传（直接上传优先于下拉选择）"""
    from modules.cache_manager import CacheManager
    cm = CacheManager()
    cached_files = cm.list_cached_files()

    # 初始化 + 处理旧下拉值残留
    if "cache_dropdown2" not in st.session_state:
        st.session_state.cache_dropdown2 = "无"
    if st.session_state.get("_clr_dd2"):
        st.session_state.cache_dropdown2 = "无"
        st.session_state._clr_dd2 = False

    # 提前构建 cache_map，读取当前下拉值
    cache_map = {}
    if cached_files:
        for c in cached_files[:10]:
            cache_map[f"{c['name'][:30]}（{c['page_count']}p）"] = c
    selected = st.session_state.get("cache_dropdown2", "无")

    cl, cr = st.columns(2)
    with cl:
        st.subheader("📘 招标文件")

        # 上传框在上
        inv_file = st.file_uploader(
            "上传招标文件", type=["pdf", "docx", "doc"],
            key="inv_uploader2", label_visibility="collapsed",
        )

        if inv_file is not None:
            if selected != "无":
                st.session_state._clr_dd2 = True
                st.rerun()
            # 从文件头几页提取项目名称
            from modules.docx_extractor import extract_project_name
            file_bytes = inv_file.read()
            inv_file.seek(0)
            project_name = extract_project_name(file_bytes, inv_file.name)
            st.info(f"**{project_name}**")
            st.caption(f"📎 {inv_file.name}  |  {inv_file.size / 1024 / 1024:.1f} MB")
            # 自动缓存上传的文件，下次可在下拉列表中找到
            try:
                cm.save_uploaded_invitation(file_bytes, inv_file.name)
            except Exception:
                pass
        elif selected != "无" and selected in cache_map:
            c = cache_map[selected]
            raw_bytes = cm.get_original_bytes(c["hash"])
            if raw_bytes:
                inv_file = BytesIO(raw_bytes)
                inv_file.name = c["name"]
                from modules.docx_extractor import extract_project_name
                project_name = extract_project_name(raw_bytes, c["name"])
                st.info(f"💾 **{project_name}**")
                st.caption(f"📎 {c['name']}  |  {len(raw_bytes) / 1024 / 1024:.1f} MB（从记录加载）")
            else:
                st.warning("缓存读取失败，请重新上传")
                st.caption("支持 PDF / DOCX / DOC")
        else:
            st.caption("支持 PDF / DOCX / DOC")

        # 下拉选择在下方
        st.selectbox(
            "从提交记录中选择",
            ["无"] + list(cache_map.keys()),
            key="cache_dropdown2", label_visibility="collapsed",
        )

    with cr:
        st.subheader("📄 投标文件")
        resp_file = st.file_uploader(
            "上传投标文件", type=["pdf", "docx", "doc"],
            key="resp_uploader2", label_visibility="collapsed",
        )
        if resp_file:
            st.info(f"**{resp_file.name}**  |  {resp_file.size / 1024 / 1024:.1f} MB")
        else:
            st.caption("支持 PDF / DOCX / DOC")

    return inv_file, resp_file


def main_new():
    """AI 审核模式主函数"""
    st.title("📄 投标文件智能审核工具")
    st.caption("AI审核模式 — 文档理解、印章识别、章节解析、合规判断，全自动完成")
    st.markdown("---")

    # ---- 异步审核状态管理 ----
    rs = st.session_state.get("_review_state", {})
    review_status = rs.get("status", "idle") if rs else "idle"

    if review_status == "running":
        _show_review_progress(rs)
        import time as _time
        _time.sleep(1)
        st.rerun()

    if review_status == "cancelled":
        st.warning("⚠️ 审核已被取消。可重新开始。")
        rs["status"] = "idle"
        rs["result"] = None
        rs["report"] = None
        rs["raw"] = None

    if review_status == "done" and not rs.get("result"):
        raw = rs.get("raw", {}) or {}
        st.error(f"❌ {raw.get('error', '审核失败：未知错误')}")
        rs["status"] = "idle"
        rs["raw"] = None

    if review_status == "done" and rs.get("result"):
        _render_llm_results(rs)
        st.markdown("---")
        _, reset_btn, _ = st.columns([1, 1, 1])
        with reset_btn:
            if st.button("🔄 开始新审核", use_container_width=True):
                rs["status"] = "idle"
                rs["result"] = None
                rs["report"] = None
                rs["raw"] = None
                st.rerun()
        return

    # ---- 空闲状态：上传 + 审核 ----
    bypass_cache = render_llm_sidebar()
    inv_file, resp_file = render_llm_dual_upload_section()

    both_ready = inv_file is not None and resp_file is not None
    if not both_ready:
        st.markdown("---")
        missing = [n for n, f in [("招标文件", inv_file), ("投标文件", resp_file)] if f is None]
        if len(missing) == 2:
            st.info("👆 请上传招标文件和投标文件，支持 PDF / DOCX / DOC 格式")
        else:
            st.warning(f"⚠️ 还缺少：{'、'.join(missing)}")
        return

    st.markdown("---")
    _, cbtn, _ = st.columns([1, 2, 1])
    with cbtn:
        do_review = st.button("🚀 开始 AI 审核", type="primary", use_container_width=True)

    if do_review:
        _start_async_llm_review(inv_file, resp_file, bypass_cache)
        st.rerun()


def _start_async_llm_review(inv_file, resp_file, bypass_cache=False):
    """
    启动异步 LLM 审核（后台线程），立即返回。
    所有跨线程数据通过 _review_state dict 原地修改传递（避免 st.session_state 跨线程赋值失效）。
    """
    inv_name = inv_file.name
    resp_name = resp_file.name
    inv_bytes = inv_file.read()
    resp_bytes = resp_file.read()
    custom_rules = st.session_state.custom_rules

    cancel_event = threading.Event()
    progress = {"frac": 0.0, "msg": "准备中..."}
    pipeline_start = time.time()
    total_size_mb = (len(inv_bytes) + len(resp_bytes)) / (1024 * 1024)

    # ★ 所有跨线程数据通过这个可变 dict 传递（原地修改，不赋值 st.session_state）
    review_state = {
        "status": "running",
        "progress": progress,
        "start_time": pipeline_start,
        "total_size_mb": total_size_mb,
        "cancel_event": cancel_event,
        "result": None,
        "report": None,
        "feedback": {},
        "raw": None,
    }
    st.session_state._review_state = review_state

    def _run_review():
        try:
            from modules.llm_engine import LLMEngine
            engine = LLMEngine()

            def _cb(frac, msg):
                progress["frac"] = frac
                progress["msg"] = msg

            result = engine.review_documents(
                invitation_pdf_bytes=inv_bytes,
                response_pdf_bytes=resp_bytes,
                custom_rules=custom_rules,
                progress_callback=_cb,
                bypass_cache=bypass_cache,
                cancel_event=cancel_event,
            )

            if cancel_event.is_set():
                review_state["status"] = "cancelled"
                return

            if not result.get("ok"):
                review_state["status"] = "done"
                review_state["raw"] = result
                return

            # 数字签名验证
            from modules.llm_engine import verify_pdf_signatures
            sig_result = verify_pdf_signatures(resp_bytes)

            # 违规截图（逐张更新进度 + 支持取消）
            progress["frac"] = 0.90
            progress["msg"] = "生成违规截图..."
            if cancel_event.is_set():
                review_state["status"] = "cancelled"
                return

            violations_with_img = LLMEngine.annotate_violation_screenshots(
                result["violations"], resp_bytes, dpi=150,
                progress_callback=_cb, cancel_event=cancel_event,
            )
            if cancel_event.is_set():
                review_state["status"] = "cancelled"
                return

            total_t = time.time() - pipeline_start
            result["summary"]["processing_time_seconds"] = total_t
            _save_timing_record(total_size_mb, total_t, engine.model)

            # ★ 原地修改 dict，不赋值 st.session_state
            review_state["status"] = "done"
            review_state["result"] = {
                "violations": violations_with_img,
                "compliant_items": result["compliant_items"],
                "summary": result["summary"],
                "extraction": result["extraction"],
                "sig_result": sig_result,
                "from_cache": result.get("from_cache", False),
            }
            review_state["report"] = result["ai_report"]
            review_state["feedback"] = {}
            review_state["raw"] = result

        except (KeyboardInterrupt, SystemExit):
            logger.info("审核线程收到终止信号")
            review_state["status"] = "cancelled"
        except MemoryError:
            review_state["status"] = "done"
            review_state["raw"] = {
                "ok": False, "error": "内存不足！请拆分PDF或关闭其他程序后重试。"
            }
        except Exception as e:
            logger.exception("LLM审核异常")
            review_state["status"] = "done"
            review_state["raw"] = {
                "ok": False, "error": f"LLM 审核异常：{str(e)}"
            }

    thread = threading.Thread(target=_run_review, daemon=True)
    thread.start()
    logger.info(f"异步审核线程已启动：{inv_name} + {resp_name}")


def _show_review_progress(rs: dict):
    """显示审核进度条 + 取消按钮"""

    progress = rs.get("progress", {})
    frac = progress.get("frac", 0.0)
    msg = progress.get("msg", "准备中...")

    st.markdown("---")
    st.header("🤖 AI 审核进行中")

    pct = min(int(frac * 100), 99)
    st.progress(pct, msg)

    elapsed = time.time() - rs.get("start_time", time.time())
    if frac > 0.005:
        remaining = (elapsed / frac) - elapsed
        hist_pred = _predict_from_history(rs.get("total_size_mb", 0))
        if hist_pred is not None:
            hist_remaining = max(0, hist_pred - elapsed)
            remaining = 0.7 * hist_remaining + 0.3 * remaining
        eta_text = f"⏱ 已用 {_fmt_time(elapsed)}  |  预计剩余 {_fmt_time(max(0, remaining))}"
    else:
        hist_pred = _predict_from_history(rs.get("total_size_mb", 0))
        if hist_pred is not None:
            eta_text = f"⏱ 已用 {_fmt_time(elapsed)}  |  基于历史预计 {_fmt_time(hist_pred)}"
        else:
            eta_text = f"⏱ 已用 {_fmt_time(elapsed)}  |  正在连接 AI 服务..."

    c1, c2, c3 = st.columns([1, 2, 1])
    with c2:
        st.caption(eta_text)
        if st.button("⚠️ 取消审核", type="secondary", use_container_width=True):
            cancel_event = rs.get("cancel_event")
            if cancel_event:
                cancel_event.set()
            rs["status"] = "cancelled"
            rs["result"] = None
            rs["report"] = None
            logger.info("用户点击取消审核")
            st.rerun()


def _render_llm_results(rs: dict):
    """渲染 LLM 审核结果"""
    r = rs.get("result", {})
    extraction = r.get("extraction", {})

    if r.get("from_cache"):
        raw = rs.get("raw", {}) or {}
        st.success(
            f"💾 审核结果来自缓存（{raw.get('_cached_at', '?')}），无需重复调用API"
        )
    elapsed = time.time() - rs.get("start_time", time.time())
    if elapsed > 1:
        pred = _predict_from_history(rs.get("total_size_mb", 0))
        if pred:
            delta = elapsed - pred
            if delta > 0:
                st.caption(f"⏱ 实际耗时 {_fmt_time(elapsed)}，比预期慢 {_fmt_time(abs(delta))}（预期 {_fmt_time(pred)}）")
            else:
                st.caption(f"⏱ 实际耗时 {_fmt_time(elapsed)}，比预期快 {_fmt_time(abs(delta))}（预期 {_fmt_time(pred)}）")
        else:
            st.caption(f"⏱ 实际耗时 {_fmt_time(elapsed)}")
    validation_warnings = r.get("validation_warnings", [])
    if validation_warnings:
        with st.expander(
            f"⚠️ 数据校验提示（{len(validation_warnings)}条）", expanded=False
        ):
            for w in validation_warnings:
                st.warning(w)

    # ---- 审核详细结果（最优先展示） ----
    violations = r.get("violations", [])
    compliant_items = r.get("compliant_items", [])
    render_llm_violations_section(violations, compliant_items)

    render_review_summary(r["summary"])
    render_extraction_info(extraction)
    render_seal_analysis(extraction)
    render_legal_rep_check(extraction)
    render_chapter_structure(extraction)

    if r.get("sig_result"):
        render_signature_info(r["sig_result"])

    report = rs.get("report", "")
    if report:
        with st.expander("🤖 LLM 智能审核报告", expanded=False):
            if report.startswith("❌"):
                st.error(report)
            else:
                st.success("✅ LLM 审核完成")
                st.markdown(report)
                st.download_button(
                    "📥 下载审核报告（Markdown）",
                    data=report,
                    file_name="投标LLM审核报告.md",
                    mime="text/markdown",
                )


# ============================================================
# 应用入口
# ============================================================
if __name__ == "__main__":
    # 审核运行中跳过侧边栏渲染，避免轮询闪烁
    rs = st.session_state.get("_review_state", {})
    _is_reviewing = rs.get("status") == "running" if rs else False

    # 审核运行中只渲染最小侧边栏，避免重复 widget key 冲突
    rs = st.session_state.get("_review_state", {})
    if rs.get("status") == "running":
        with st.sidebar:
            st.markdown("## 📄 投标文件审核工具")
            st.caption("AI 审核进行中，完成后自动显示结果")
            st.progress(rs.get("progress", {}).get("frac", 0))

    main_new()
