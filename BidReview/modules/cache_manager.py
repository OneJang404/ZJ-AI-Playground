"""
缓存管理模块
============
功能：将处理过的招标文件结果持久化到磁盘，支持跨会话复用
      使用文件内容 SHA256 作为缓存键，存储 OCR 结果和筛选信息
"""

import hashlib
import json
import pickle
import logging
import shutil
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class CacheManager:
    """
    招标文件缓存管理器
    ------------------
    将招标文件处理结果（筛选后的重点页、筛选统计）缓存到本地磁盘，
    支持跨会话复用，避免重复 OCR 处理。

    缓存结构：
        .bidreview_cache/
          <sha256_hash>/
              metadata.json       # 文件元信息
              original.pdf        # 原始 PDF 文件（支持缓存恢复）
              filtered_pages.pkl  # 筛选后的重点页数据
              filter_stats.pkl    # 筛选统计

    使用示例：
        mgr = CacheManager()
        cached = mgr.load_invitation_cache(file_bytes)
        if cached:
            filtered_pages = cached["filtered_pages"]
        else:
            # ... 处理招标文件 ...
            mgr.save_invitation_cache(file_bytes, name, size,
                                      filtered_pages, filter_stats, page_count)
    """

    def __init__(self, cache_dir: str = None):
        """
        初始化缓存管理器

        参数:
            cache_dir: 缓存目录路径，默认为 BidReview/.bidreview_cache/
        """
        if cache_dir is None:
            cache_dir = Path(__file__).resolve().parent.parent / ".bidreview_cache"
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ================================================================
    # 文件哈希
    # ================================================================

    @staticmethod
    def _hash_bytes(data: bytes) -> str:
        """计算字节数据的 SHA256 哈希"""
        return hashlib.sha256(data).hexdigest()

    # ================================================================
    # 读取缓存
    # ================================================================

    def load_invitation_cache(self, file_bytes: bytes) -> Optional[Dict]:
        """
        检查是否存在缓存，存在则返回缓存数据

        参数:
            file_bytes: 招标文件完整字节

        返回:
            Dict 或 None:
                {
                    "file_name": str,
                    "file_size": int,
                    "page_count": int,
                    "filtered_pages": List[Dict],
                    "filter_stats": Dict,
                    "cached_at": str,
                }
        """
        file_hash = self._hash_bytes(file_bytes)
        hash_dir = self.cache_dir / file_hash

        if not hash_dir.exists():
            logger.info(f"缓存未命中：{file_hash[:12]}...")
            return None

        meta_path = hash_dir / "metadata.json"
        pages_path = hash_dir / "filtered_pages.pkl"
        stats_path = hash_dir / "filter_stats.pkl"

        if not (meta_path.exists() and pages_path.exists() and stats_path.exists()):
            logger.warning(f"缓存数据不完整：{file_hash[:12]}...")
            # 清理不完整的缓存
            shutil.rmtree(hash_dir, ignore_errors=True)
            return None

        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                metadata = json.load(f)
            with open(pages_path, "rb") as f:
                filtered_pages = pickle.load(f)
            with open(stats_path, "rb") as f:
                filter_stats = pickle.load(f)

            logger.info(
                f"✅ 缓存命中：{metadata.get('file_name', '?')} "
                f"（{file_hash[:12]}...，{metadata.get('page_count', 0)}页，"
                f"缓存于 {metadata.get('cached_at', '?')}）"
            )
            return {
                "file_name": metadata.get("file_name", ""),
                "file_size": metadata.get("file_size", 0),
                "page_count": metadata.get("page_count", 0),
                "filtered_pages": filtered_pages,
                "filter_stats": filter_stats,
                "cached_at": metadata.get("cached_at", ""),
            }
        except Exception as e:
            logger.warning(f"读取缓存失败：{e}")
            return None

    # ================================================================
    # 保存缓存
    # ================================================================

    def save_invitation_cache(
        self,
        file_bytes: bytes,
        file_name: str,
        file_size: int,
        filtered_pages: List[Dict],
        filter_stats: Dict,
        page_count: int,
    ) -> bool:
        """
        保存招标文件处理结果到缓存

        参数:
            file_bytes:     招标文件完整字节
            file_name:      文件名
            file_size:      文件大小（字节）
            filtered_pages: 筛选后的重点页列表
            filter_stats:   筛选统计
            page_count:     总页数

        返回:
            bool: 是否保存成功
        """
        file_hash = self._hash_bytes(file_bytes)
        hash_dir = self.cache_dir / file_hash
        hash_dir.mkdir(parents=True, exist_ok=True)

        try:
            # 元数据（JSON，方便人工查看）
            metadata = {
                "file_name": file_name,
                "file_size": file_size,
                "file_hash": file_hash,
                "page_count": page_count,
                "key_pages": len(filtered_pages),
                "cached_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            with open(hash_dir / "metadata.json", "w", encoding="utf-8") as f:
                json.dump(metadata, f, ensure_ascii=False, indent=2)

            # 原始 PDF 文件（支持缓存恢复，无需重新上传）
            with open(hash_dir / "original.pdf", "wb") as f:
                f.write(file_bytes)

            # 筛选结果（pickle，保留完整 Python 对象）
            with open(hash_dir / "filtered_pages.pkl", "wb") as f:
                pickle.dump(filtered_pages, f)
            with open(hash_dir / "filter_stats.pkl", "wb") as f:
                pickle.dump(filter_stats, f)

            logger.info(
                f"💾 缓存已保存：{file_name} → {file_hash[:12]}..."
            )
            return True
        except Exception as e:
            logger.error(f"保存缓存失败：{e}")
            shutil.rmtree(hash_dir, ignore_errors=True)
            return False

    # ================================================================
    # 缓存管理
    # ================================================================

    def list_cached_files(self) -> List[Dict]:
        """
        列出所有已缓存的文件

        返回:
            List[Dict]: 每个元素包含 name, size, hash, cached_at, page_count, key_pages
        """
        result = []
        if not self.cache_dir.exists():
            return result

        for hash_dir in sorted(self.cache_dir.iterdir()):
            if not hash_dir.is_dir():
                continue
            meta_path = hash_dir / "metadata.json"
            if not meta_path.exists():
                continue
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                result.append({
                    "name": meta.get("file_name", "?"),
                    "size": meta.get("file_size", 0),
                    "hash": meta.get("file_hash", hash_dir.name),
                    "cached_at": meta.get("cached_at", "?"),
                    "page_count": meta.get("page_count", 0),
                    "key_pages": meta.get("key_pages", 0),
                })
            except Exception:
                continue

        # 按缓存时间倒序
        result.sort(key=lambda x: x.get("cached_at", ""), reverse=True)
        return result

    def get_original_bytes(self, file_hash: str) -> Optional[bytes]:
        """从缓存中读取原始 PDF 字节"""
        pdf_path = self.cache_dir / file_hash / "original.pdf"
        if pdf_path.exists():
            return pdf_path.read_bytes()
        return None

    def clear_cache_by_hash(self, file_hash: str) -> bool:
        """清除指定哈希的缓存"""
        hash_dir = self.cache_dir / file_hash
        if hash_dir.exists():
            shutil.rmtree(hash_dir, ignore_errors=True)
            logger.info(f"已清除缓存：{file_hash[:12]}...")
            return True
        return False

    def clear_all_cache(self) -> int:
        """清除所有缓存，返回清除数量"""
        count = 0
        if not self.cache_dir.exists():
            return count
        for item in self.cache_dir.iterdir():
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
                count += 1
        logger.info(f"已清除全部 {count} 个缓存")
        return count

    # ================================================================
    # 自定义规则缓存
    # ================================================================

    def _rules_path(self) -> Path:
        return self.cache_dir / "custom_rules.json"

    def load_rules(self) -> list:
        """加载缓存的规则列表，按创建时间正序"""
        path = self._rules_path()
        if not path.exists():
            return []
        try:
            rules = json.loads(path.read_text("utf-8"))
            return sorted(rules, key=lambda r: r.get("created_at", ""))
        except Exception:
            return []

    def save_rules(self, rules: list) -> bool:
        """保存规则列表到缓存"""
        try:
            self._rules_path().write_text(
                json.dumps(rules, ensure_ascii=False, indent=2), "utf-8"
            )
            return True
        except Exception as e:
            logger.warning(f"保存规则缓存失败：{e}")
            return False

    def clear_rules(self) -> bool:
        """清除规则缓存"""
        path = self._rules_path()
        if path.exists():
            path.unlink()
            return True
        return False
