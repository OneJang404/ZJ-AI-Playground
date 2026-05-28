"""
数字签名验证模块
===============
使用 pyHanko 验证 PDF 数字签名，展示签名详情。
"""

import logging
from typing import List, Dict, Optional
from io import BytesIO

logger = logging.getLogger(__name__)


class SigValidator:
    """PDF 数字签名验证器"""

    @staticmethod
    def check_signatures(pdf_bytes: bytes) -> Dict:
        """
        验证 PDF 文件中的数字签名

        参数:
            pdf_bytes: PDF 文件字节流

        返回:
            dict: {
                "has_signatures": bool,
                "signature_count": int,
                "signatures": [{
                    "field_name": str,
                    "signer": str,
                    "signing_time": str,
                    "valid": bool,
                    "integrity_ok": bool,
                    "coverage": str,
                    "issue": str,
                }],
                "error": str or None,
            }
        """
        result: Dict = {
            "has_signatures": False,
            "signature_count": 0,
            "signatures": [],
            "error": None,
        }

        try:
            from pyhanko.pdf_utils.reader import PdfFileReader
            from pyhanko.sign.validation import validate_pdf_signature
            from pyhanko.sign.validation.status import PdfSignatureStatus

            reader = PdfFileReader(BytesIO(pdf_bytes))

            sigs = reader.embedded_signatures
            if not sigs:
                result["error"] = None  # 无签名不是错误
                return result

            result["has_signatures"] = True
            result["signature_count"] = len(sigs)

            for sig in sigs:
                sig_info: Dict = {
                    "field_name": "",
                    "signer": "",
                    "signing_time": "",
                    "valid": True,
                    "integrity_ok": True,
                    "coverage": "",
                    "issue": "",
                }

                try:
                    sig_info["field_name"] = sig.field_name or "（未命名）"

                    # 提取签名者证书信息
                    if sig.signer_cert is not None:
                        cert = sig.signer_cert
                        try:
                            from pyhanko_certvalidator import CertificateValidator
                        except ImportError:
                            pass
                        subject = cert.subject
                        # 从证书主题中提取 CN (Common Name)
                        sig_info["signer"] = SigValidator._extract_cn(subject)

                    # 签名时间
                    if sig.self_reported_timestamp is not None:
                        sig_info["signing_time"] = str(sig.self_reported_timestamp)

                    # 验证签名
                    try:
                        status: PdfSignatureStatus = validate_pdf_signature(sig)
                        sig_info["valid"] = status.valid
                        sig_info["integrity_ok"] = status.intact
                        if status.summary():
                            sig_info["issue"] = status.summary()
                    except Exception as val_err:
                        sig_info["valid"] = False
                        sig_info["issue"] = f"验证异常：{str(val_err)[:200]}"

                    # 签名覆盖范围
                    coverage = sig.summarise_integrity_info()
                    if coverage:
                        sig_info["coverage"] = str(coverage)[:200]

                except Exception as sig_err:
                    sig_info["valid"] = False
                    sig_info["issue"] = f"解析异常：{str(sig_err)[:200]}"

                result["signatures"].append(sig_info)

        except ImportError:
            result["error"] = "pyHanko 库未安装，无法验证数字签名"
            logger.warning(result["error"])
        except Exception as e:
            result["error"] = f"签名验证失败：{str(e)[:300]}"
            logger.warning(f"数字签名验证异常：{e}")

        return result

    @staticmethod
    def _extract_cn(subject) -> str:
        """从证书主题中提取 Common Name"""
        try:
            # subject 可能是 dict 或有 .native 属性
            if hasattr(subject, 'native'):
                native = subject.native
                if isinstance(native, dict) and 'common_name' in native:
                    return str(native['common_name'])
            # 尝试 dict 直接访问
            if isinstance(subject, dict):
                return subject.get('common_name', str(subject))
            # 回退：字符串表示
            s = str(subject)
            # 尝试匹配 CN=
            import re
            m = re.search(r'CN=([^,]+)', s)
            if m:
                return m.group(1).strip()
            return s[:100]
        except Exception:
            return str(subject)[:100]
