"""
AI 审核模块
============
功能：调用 DeepSeek API（OpenAI 兼容接口）进行投标文件智能审核
特性：超时重试、异常友好提示、结构化审核报告
"""

import os
import time
import logging
from typing import Optional

import requests
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# ============================================================
# 加载环境变量（优先 .env，其次 api.env）
# 使用模块所在目录定位项目根目录，解决 Streamlit CWD 变化问题
# ============================================================
from pathlib import Path
_project_root = Path(__file__).resolve().parent.parent  # modules/ → BidReview/

load_dotenv(_project_root / ".env")
if not os.getenv("DEEPSEEK_API_KEY"):
    load_dotenv(_project_root / "api.env")


class AIReviewer:
    """
    DeepSeek AI 审核器
    ------------------
    负责组装审核提示词并调用 DeepSeek Chat API 生成审核报告。

    配置来源：项目根目录的 .env 文件
    必需环境变量：
        DEEPSEEK_API_KEY  - API 密钥
    可选环境变量：
        DEEPSEEK_API_URL  - API 地址（默认：https://api.deepseek.com/v1/chat/completions）
        DEEPSEEK_MODEL    - 模型名称（默认：deepseek-chat）
        DEEPSEEK_TIMEOUT  - 请求超时秒数（默认：120）
        DEEPSEEK_MAX_RETRIES - 最大重试次数（默认：3）
    """

    def __init__(self):
        """从环境变量初始化审核器配置"""
        # API 密钥
        self.api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()

        # API 地址：自动规范化处理
        api_url = os.getenv(
            "DEEPSEEK_API_URL",
            "https://api.deepseek.com/v1/chat/completions"
        ).strip().rstrip("/")

        # 如果 URL 不以 /chat/completions 结尾，自动追加（兼容用户只填了 base URL 的情况）
        if not api_url.endswith("/chat/completions"):
            api_url += "/chat/completions"

        self.api_url = api_url

        # 可选配置项
        self.model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip()
        self.timeout = int(os.getenv("DEEPSEEK_TIMEOUT", "120"))
        self.max_retries = int(os.getenv("DEEPSEEK_MAX_RETRIES", "3"))

        logger.info(f"AI审核器初始化：model={self.model}, timeout={self.timeout}s")

    # ================================================================
    # 配置校验
    # ================================================================

    def _check_config(self) -> Optional[str]:
        """
        校验配置是否完整，返回错误信息或 None

        返回:
            None 表示配置正确，str 表示错误描述
        """
        if not self.api_key:
            return (
                "DeepSeek API 密钥未配置。\n\n"
                "📌 配置步骤：\n"
                "1. 复制 .env.example 为 .env\n"
                "2. 编辑 .env 文件，填入你的 DEEPSEEK_API_KEY\n"
                "3. 获取密钥：https://platform.deepseek.com/api_keys\n"
                "4. 重启应用"
            )
        if not self.api_key.startswith("sk-"):
            return (
                "API 密钥格式可能不正确（应以 'sk-' 开头），"
                "请检查 .env 文件中的 DEEPSEEK_API_KEY。"
            )
        return None

    # ================================================================
    # 核心审核方法
    # ================================================================

    def review(
        self,
        invitation_text: str,
        response_full_text: str,
        ocr_summary: str,
        violations_summary: str,
        position_checklist: str,
    ) -> str:
        """
        调用 DeepSeek 进行招标/投标文件交叉智能审核

        参数:
            invitation_text:    招标文件重点页文本
            response_full_text: 投标文件全文
            ocr_summary:        OCR 识别结果摘要
            violations_summary: 预检违规项汇总文本
            position_checklist: 签名/盖章位置校验清单文本

        返回:
            str: Markdown 格式的审核报告
                 若失败则返回以 ❌ 开头的友好错误提示
        """
        # 1. 校验配置
        config_error = self._check_config()
        if config_error:
            return f"❌ {config_error}"

        # 2. 组装提示词
        prompt = self._build_prompt(
            invitation_text, response_full_text, ocr_summary,
            violations_summary, position_checklist
        )

        # 3. 带重试机制的 API 调用
        last_error = ""

        for attempt in range(self.max_retries):
            try:
                logger.info(f"调用 DeepSeek API（第 {attempt + 1}/{self.max_retries} 次）...")

                headers = {
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                }

                payload = {
                    "model": self.model,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "你是一位资深投标顾问，拥有10年以上大型项目投标文件审核经验。"
                                "请严格、细致、专业地审核每一份投标文件，确保不遗漏任何问题。"
                                "回复必须使用中文，按合规项、风险项、整改建议三部分组织。"
                                "使用 Markdown 格式，关键问题用粗体标注。"
                            )
                        },
                        {
                            "role": "user",
                            "content": prompt
                        }
                    ],
                    "temperature": 0.3,         # 低温度保证输出一致性
                    "max_tokens": 4096,
                    "stream": False,
                }

                response = requests.post(
                    self.api_url,
                    headers=headers,
                    json=payload,
                    timeout=self.timeout
                )

                # ---- 处理不同状态码 ----
                if response.status_code == 200:
                    result = response.json()
                    content = result["choices"][0]["message"]["content"]
                    logger.info("DeepSeek API 调用成功")
                    return content

                elif response.status_code == 401:
                    return (
                        "❌ API 密钥无效（401 Unauthorized）。\n\n"
                        "请检查 .env 文件中的 DEEPSEEK_API_KEY 是否正确。\n"
                        "获取有效密钥：https://platform.deepseek.com/api_keys"
                    )

                elif response.status_code == 429:
                    # 限流：等待后重试
                    if attempt < self.max_retries - 1:
                        wait_sec = (attempt + 1) * 10
                        logger.warning(f"API 限流（429），等待 {wait_sec} 秒后重试...")
                        time.sleep(wait_sec)
                        continue
                    return "❌ API 请求过于频繁（429），请等待几分钟后重试。"

                elif response.status_code >= 500:
                    # 服务端错误：可重试
                    last_error = f"服务器错误（{response.status_code}）"
                    if attempt < self.max_retries - 1:
                        time.sleep(3)
                        continue

                else:
                    last_error = f"API 返回错误状态码 {response.status_code}：{response.text[:300]}"
                    break  # 客户端错误不重试

            except requests.exceptions.Timeout:
                last_error = f"请求超时（超过 {self.timeout} 秒）"
                logger.warning(f"第 {attempt + 1} 次请求超时")

            except requests.exceptions.ConnectionError as e:
                last_error = f"无法连接到 API 服务器：{str(e)[:200]}"
                break  # 连接错误不需要重试

            except requests.exceptions.RequestException as e:
                last_error = f"网络请求异常：{str(e)[:300]}"
                break

            except Exception as e:
                last_error = f"未知错误：{str(e)[:300]}"
                logger.exception("API 调用发生未预期异常")
                break

        # 4. 所有重试均失败
        return (
            f"❌ AI 审核请求失败：{last_error}\n\n"
            "---\n"
            "### 🔧 排查建议\n"
            "1. 检查网络连接是否正常\n"
            "2. 确认 API 密钥有效且有余额\n"
            "3. 检查 DEEPSEEK_API_URL 是否正确\n"
            "4. 稍后重试（服务器可能临时繁忙）"
        )

    # ================================================================
    # 复核反馈（二次审核）
    # ================================================================

    def review_with_feedback(
        self,
        original_report: str,
        supplemental_context: str,
    ) -> str:
        """
        基于补充OCR上下文进行二次复核，更新审核结论

        参数:
            original_report:      首次AI审核的完整报告
            supplemental_context: 高精度OCR补充识别文本

        返回:
            str: 更新后的Markdown审核报告，失败时返回原报告
        """
        config_error = self._check_config()
        if config_error:
            logger.warning(f"复核跳过：{config_error}")
            return original_report

        prompt = (
            "你之前对一份投标文件进行了交叉审核，以下是你的原始审核报告：\n\n"
            f"{original_report}\n\n"
            "---\n\n"
            "以下是对报告中提到页面的高精度OCR补充识别结果，"
            "请结合这些信息，更新或确认你的审核结论：\n\n"
            f"{supplemental_context}\n\n"
            "---\n\n"
            "请输出更新后的完整审核报告（Markdown格式）。\n"
            "如果补充信息没有实质性改变，请确认原结论并在报告末尾注明「✅ AI复核确认无误」。\n"
            "如果补充信息改变了某些判断，请在修改处标注「[已根据补充OCR更新]」。"
        )

        last_error = ""
        for attempt in range(self.max_retries):
            try:
                logger.info(f"复核API调用（第{attempt+1}/{self.max_retries}次）...")
                headers = {
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                }
                payload = {
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": "你是资深投标顾问。请基于补充OCR信息更新审核报告。"},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.3,
                    "max_tokens": 4096,
                    "stream": False,
                }
                response = requests.post(
                    self.api_url, headers=headers, json=payload, timeout=self.timeout
                )
                if response.status_code == 200:
                    result = response.json()
                    content = result["choices"][0]["message"]["content"]
                    logger.info("AI复核完成")
                    return content
                elif response.status_code == 429 and attempt < self.max_retries - 1:
                    time.sleep((attempt + 1) * 10)
                    continue
                else:
                    last_error = f"状态码 {response.status_code}"
            except requests.exceptions.Timeout:
                last_error = "复核超时"
            except Exception as e:
                last_error = str(e)[:200]

        logger.warning(f"AI复核失败：{last_error}，保留原报告")
        return original_report

    # ================================================================
    # 提示词构建
    # ================================================================

    def _build_prompt(
        self,
        invitation_text: str,
        response_full_text: str,
        ocr_summary: str,
        violations_summary: str,
        position_checklist: str,
    ) -> str:
        """
        组装交叉审核模式的完整提示词

        参数:
            invitation_text:    招标文件重点页文本
            response_full_text: 投标文件全文
            ocr_summary:        OCR 识别摘要
            violations_summary: 预检违规项汇总
            position_checklist: 位置校验清单

        返回:
            str: 组装好的提示词
        """
        # 截断过长文本，为两个文件各分配约一半的上下文空间
        MAX_EACH = 10000
        if len(invitation_text) > MAX_EACH:
            invitation_text = (
                invitation_text[:MAX_EACH]
                + f"\n\n...\n[招标文件重点页原文共 {len(invitation_text)} 字符，已截断前 {MAX_EACH} 字符]"
            )
        if len(response_full_text) > MAX_EACH:
            response_full_text = (
                response_full_text[:MAX_EACH]
                + f"\n\n...\n[投标文件原文共 {len(response_full_text)} 字符，已截断前 {MAX_EACH} 字符]"
            )

        prompt = f"""请对以下招标文件的各项要求与投标文件的实际内容进行交叉对比审核，并生成结构化报告。

---

## 📄 一、招标文件要求摘要（重点规则页面）

{invitation_text}

---

## 📄 二、投标文件全文

{response_full_text}

---

## 🔍 三、OCR 识别与签章位置校验结果

{position_checklist}

---

## ⚠️ 四、预检发现的违规项

{violations_summary}

---

## 📊 五、OCR 识别摘要

{ocr_summary}

---

## 📋 审核输出要求

**重要：报告总字数控制在500字以内，只列出关键发现，不要冗长描述。**

请按以下三部分输出（Markdown格式，简明扼要）：

### ✅ 一、合规项（仅列关键项，每条一行）
### ⚠️ 二、风险项（仅列高风险和实质性问题，标注🔴高/🟡中/🟢低，每条不超过2行）
### 📝 三、整改建议与总结（按优先级排列，给出"建议通过"/"修改后通过"/"不建议通过"）
"""
        return prompt
