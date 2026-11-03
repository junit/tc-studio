# -*- coding: utf-8 -*-
"""
企业私有知识库与运维手册管理（轻量级 RAG）：
支持文档切片（MD/TXT/DOCX/PDF/JSON）、纯 Python BM25 倒排检索、知识召回与 Prompt 动态注入
"""

import os
import re
import json
import math
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from .file_parser import format_file_size

MAX_RAG_TEXT_CHARS = 2_000_000
MAX_RAG_CHUNKS = 5_000
MAX_RAG_PDF_PAGES = 500


def tokenize(text: str) -> List[str]:
    """
    中英文混合分词：
    - 英文、数字及下划线标识符切分
    - 中文字符单字 (unigram) + 二元连续字 (bi-gram)，提供极高召回率且无需庞大外部 ML 分词依赖
    """
    if not text:
        return []
    clean = text.lower()
    tokens = []

    # 1. 英文、数字与代码标识符
    words = re.findall(r"[a-z0-9_\-\./:]+", clean)
    tokens.extend(words)

    # 2. 中文单字与二元相邻字
    cn_chars = re.findall(r"[\u4e00-\u9fa5]", clean)
    for i, ch in enumerate(cn_chars):
        tokens.append(ch)
        if i < len(cn_chars) - 1:
            tokens.append(ch + cn_chars[i + 1])

    return tokens


class RagManager:
    """轻量级知识库与文档检索引擎 (Pure Python BM25 + Section Chunking)"""

    def __init__(self, data_root: Optional[str] = None):
        if not data_root:
            base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
            data_root = os.path.join(base_dir, "data", "bots")
        self.data_root = data_root
        self._lock = threading.RLock()

    def _get_kb_dir(self, bot_id: str) -> str:
        kb_dir = os.path.join(self.data_root, bot_id, "knowledge")
        os.makedirs(kb_dir, mode=0o700, exist_ok=True)
        os.chmod(kb_dir, 0o700)
        files_dir = os.path.join(kb_dir, "files")
        os.makedirs(files_dir, mode=0o700, exist_ok=True)
        os.chmod(files_dir, 0o700)
        return kb_dir

    def _get_docs_path(self, bot_id: str) -> str:
        return os.path.join(self._get_kb_dir(bot_id), "docs.json")

    def _get_chunks_path(self, bot_id: str) -> str:
        return os.path.join(self._get_kb_dir(bot_id), "chunks.json")

    def _read_json(self, file_path: str, default: Any) -> Any:
        with self._lock:
            if not os.path.exists(file_path):
                return default
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return default

    def _write_json(self, file_path: str, data: Any):
        with self._lock:
            temp_path = f"{file_path}.{os.getpid()}.{threading.get_ident()}.{int(time.time() * 1000)}.tmp"
            try:
                fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.chmod(temp_path, 0o600)
                os.replace(temp_path, file_path)
            except Exception:
                if os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except Exception:
                        pass
                raise

    def list_documents(self, bot_id: str) -> List[Dict[str, Any]]:
        """列出指定 BOT 挂载的所有企业知识库与手册文档"""
        docs_path = self._get_docs_path(bot_id)
        return self._read_json(docs_path, [])

    def get_document(self, bot_id: str, doc_id: str) -> Optional[Dict[str, Any]]:
        """获取单个文档元数据"""
        docs = self.list_documents(bot_id)
        for d in docs:
            if d.get("id") == doc_id:
                return d
        return None

    def get_document_file_path(self, bot_id: str, doc_id: str) -> Optional[str]:
        """获取文档原始文件绝对路径"""
        doc = self.get_document(bot_id, doc_id)
        if not doc:
            return None
        kb_dir = self._get_kb_dir(bot_id)
        stored_name = doc.get("stored_name")
        if stored_name:
            path = os.path.join(kb_dir, "files", stored_name)
            if os.path.exists(path):
                return path
        return None

    def extract_text_from_bytes(self, filename: str, content: bytes) -> str:
        """从各类型文件中提取文本（MD/TXT/DOCX/PDF/JSON/CSV）"""
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

        if ext in ("md", "txt", "log", "yaml", "yml", "ini"):
            for enc in ("utf-8", "gbk", "latin-1"):
                try:
                    return content.decode(enc)
                except UnicodeDecodeError:
                    continue
            return content.decode("utf-8", errors="replace")

        elif ext == "json":
            try:
                parsed = json.loads(content.decode("utf-8", errors="replace"))
                return json.dumps(parsed, ensure_ascii=False, indent=2)
            except Exception:
                return content.decode("utf-8", errors="replace")

        elif ext == "docx":
            import io
            import docx
            doc_file = io.BytesIO(content)
            doc = docx.Document(doc_file)
            paras = [p.text for p in doc.paragraphs if p.text.strip()]
            for table in doc.tables:
                for row in table.rows:
                    row_texts = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                    if row_texts:
                        paras.append(" | ".join(row_texts))
            return "\n\n".join(paras)

        elif ext == "pdf":
            import io
            import pypdf
            pdf_file = io.BytesIO(content)
            reader = pypdf.PdfReader(pdf_file)
            if len(reader.pages) > MAX_RAG_PDF_PAGES:
                raise ValueError(f"PDF 页数超过 {MAX_RAG_PDF_PAGES} 页限制")
            pages = []
            for idx, page in enumerate(reader.pages):
                text = page.extract_text() or ""
                if text.strip():
                    pages.append(f"--- [第 {idx + 1} 页] ---\n{text.strip()}")
            return "\n\n".join(pages)

        elif ext in ("csv", "tsv"):
            for enc in ("utf-8", "gbk"):
                try:
                    return content.decode(enc)
                except UnicodeDecodeError:
                    continue
            return content.decode("utf-8", errors="replace")

        else:
            return content.decode("utf-8", errors="replace")

    def chunk_text(self, text: str, doc_title: str) -> List[Dict[str, Any]]:
        """
        结构化切片：
        1. 优先按 Markdown 标题或段落分块
        2. 控制单个切片大小在 300~700 字符之间，带有上下文重叠
        """
        chunks = []
        if not text.strip():
            return chunks

        # 检查是否包含 Markdown 标题
        lines = text.split("\n")
        current_header = doc_title
        current_chunk_lines = []
        current_chunk_len = 0

        for line in lines:
            header_match = re.match(r"^(#{1,4})\s+(.+)$", line.strip())
            if header_match:
                # 遇到新标题且已有内容，结算上一块
                if current_chunk_lines:
                    chunk_text = "\n".join(current_chunk_lines).strip()
                    if chunk_text:
                        chunks.append({
                            "title": current_header,
                            "content": chunk_text
                        })
                    current_chunk_lines = []
                    current_chunk_len = 0
                current_header = header_match.group(2).strip()

            current_chunk_lines.append(line)
            current_chunk_len += len(line) + 1

            # 超过阈值 (700 字符) 切割一块
            if current_chunk_len >= 650:
                chunk_text = "\n".join(current_chunk_lines).strip()
                if chunk_text:
                    chunks.append({
                        "title": current_header,
                        "content": chunk_text
                    })
                # 保留最后 2 行作为重叠上下文 (sliding overlap)
                overlap = current_chunk_lines[-2:] if len(current_chunk_lines) >= 2 else []
                current_chunk_lines = list(overlap)
                current_chunk_len = sum(len(l) + 1 for l in overlap)

        if current_chunk_lines:
            chunk_text = "\n".join(current_chunk_lines).strip()
            if chunk_text:
                chunks.append({
                    "title": current_header,
                    "content": chunk_text
                })

        return chunks

    def add_document(
        self,
        bot_id: str,
        filename: str,
        content: bytes,
        tags: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        上传并挂载新知识库文件：解析文本 -> 智能切片 -> 计算词频与 Token -> 写入本地索引
        """
        with self._lock:
            kb_dir = self._get_kb_dir(bot_id)
            doc_id = f"doc_{int(time.time() * 1000)}"
            safe_filename = os.path.basename(filename)
            stored_name = f"{doc_id}_{safe_filename}"
            stored_path = os.path.join(kb_dir, "files", stored_name)

            # 1. 提取文本并切片 before persisting anything, so malformed or
            # oversized documents cannot leave half-built index state behind.
            text = self.extract_text_from_bytes(safe_filename, content)
            if len(text) > MAX_RAG_TEXT_CHARS:
                raise ValueError(f"知识库文本超过 {MAX_RAG_TEXT_CHARS:,} 字符限制")
            raw_chunks = self.chunk_text(text, safe_filename)
            if len(raw_chunks) > MAX_RAG_CHUNKS:
                raise ValueError(f"知识库切片超过 {MAX_RAG_CHUNKS:,} 块限制")

            # 2. 保存物理文件
            fd = os.open(stored_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(content)
            os.chmod(stored_path, 0o600)

            chunks_data = []
            for idx, rc in enumerate(raw_chunks):
                chunk_id = f"{doc_id}_c{idx}"
                c_text = rc["content"]
                tokens = tokenize(f"{rc['title']} {c_text}")
                chunks_data.append({
                    "chunk_id": chunk_id,
                    "doc_id": doc_id,
                    "doc_name": safe_filename,
                    "index": idx,
                    "title": rc["title"],
                    "content": c_text,
                    "tokens": tokens,
                    "token_count": len(tokens)
                })

            # 3. 构造文档元数据
            ext = safe_filename.rsplit(".", 1)[-1].lower() if "." in safe_filename else "txt"
            doc_meta = {
                "id": doc_id,
                "name": safe_filename,
                "stored_name": stored_name,
                "file_type": ext,
                "size": len(content),
                "size_formatted": format_file_size(len(content)),
                "chunks_count": len(chunks_data),
                "enabled": True,
                "tags": tags or ["运维手册", "业务规范"],
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                "preview": text[:200].strip()
            }

            # 4. 追加保存文档列表与 Chunks
            docs = self.list_documents(bot_id)
            docs.insert(0, doc_meta)
            self._write_json(self._get_docs_path(bot_id), docs)

            all_chunks = self._read_json(self._get_chunks_path(bot_id), [])
            all_chunks.extend(chunks_data)
            self._write_json(self._get_chunks_path(bot_id), all_chunks)

            return doc_meta

    def toggle_document(self, bot_id: str, doc_id: str, enabled: Optional[bool] = None) -> bool:
        """启用或禁用某篇文档参与 RAG 语义检索"""
        with self._lock:
            docs = self.list_documents(bot_id)
            found = False
            for d in docs:
                if d.get("id") == doc_id:
                    if enabled is None:
                        d["enabled"] = not d.get("enabled", True)
                    else:
                        d["enabled"] = bool(enabled)
                    found = True
                    break
            if found:
                self._write_json(self._get_docs_path(bot_id), docs)
            return found

    def delete_document(self, bot_id: str, doc_id: str) -> bool:
        """删除挂载的文档及其索引切片"""
        with self._lock:
            docs = self.list_documents(bot_id)
            target = None
            rem_docs = []
            for d in docs:
                if d.get("id") == doc_id:
                    target = d
                else:
                    rem_docs.append(d)

            if not target:
                return False

            # 1. 删除磁盘文件
            kb_dir = self._get_kb_dir(bot_id)
            stored_name = target.get("stored_name")
            if stored_name:
                fpath = os.path.join(kb_dir, "files", stored_name)
                if os.path.exists(fpath):
                    try:
                        os.remove(fpath)
                    except Exception:
                        pass

            # 2. 从 docs.json 移除
            self._write_json(self._get_docs_path(bot_id), rem_docs)

            # 3. 从 chunks.json 移除对应 chunks
            all_chunks = self._read_json(self._get_chunks_path(bot_id), [])
            rem_chunks = [c for c in all_chunks if c.get("doc_id") != doc_id]
            self._write_json(self._get_chunks_path(bot_id), rem_chunks)

            return True

    def search(
        self,
        bot_id: str,
        query: str,
        top_k: int = 4,
        min_score: float = 0.05
    ) -> List[Dict[str, Any]]:
        """
        基于 BM25 算法的高性能检索：
        - 仅对已启用的文档切片进行评分
        - 支持中英文分词与标题加权
        - 计算精确的相关度分数
        """
        clean_query = (query or "").strip()
        if not clean_query:
            return []

        # 1. 获取有效文档映射
        docs = self.list_documents(bot_id)
        enabled_doc_ids = {d["id"] for d in docs if d.get("enabled", True)}
        if not enabled_doc_ids:
            return []

        all_chunks = self._read_json(self._get_chunks_path(bot_id), [])
        active_chunks = [c for c in all_chunks if c.get("doc_id") in enabled_doc_ids]
        if not active_chunks:
            return []

        query_tokens = tokenize(clean_query)
        if not query_tokens:
            return []

        # 2. 统计语料库全局参数
        N = len(active_chunks)
        total_len = sum(len(c.get("tokens", [])) for c in active_chunks)
        avgdl = total_len / N if N > 0 else 1.0

        # 文档频率 df(q): 包含 token q 的切片数量
        df: Dict[str, int] = {}
        for c in active_chunks:
            chunk_tokens_set = set(c.get("tokens", []))
            for qt in set(query_tokens):
                if qt in chunk_tokens_set:
                    df[qt] = df.get(qt, 0) + 1

        # 3. 计算 BM25 得分
        k1 = 1.5
        b = 0.75
        scored_chunks = []

        for c in active_chunks:
            c_tokens = c.get("tokens", [])
            doc_len = len(c_tokens)
            score = 0.0

            # 词频统计 tf
            token_freq: Dict[str, int] = {}
            for t in c_tokens:
                token_freq[t] = token_freq.get(t, 0) + 1

            for qt in query_tokens:
                n_qt = df.get(qt, 0)
                if n_qt == 0:
                    continue
                # 经典 BM25 IDF
                idf = math.log(1.0 + (N - n_qt + 0.5) / (n_qt + 0.5))
                tf = token_freq.get(qt, 0)
                if tf > 0:
                    term_score = idf * (tf * (k1 + 1.0)) / (tf + k1 * (1.0 - b + b * (doc_len / avgdl)))
                    score += term_score

            # 标题包含加权：若标题直接包含关键词，给予额外权重
            title = c.get("title", "")
            for qt in query_tokens:
                if len(qt) >= 2 and qt in title.lower():
                    score += 0.8

            if score >= min_score:
                scored_chunks.append({
                    "chunk_id": c.get("chunk_id"),
                    "doc_id": c.get("doc_id"),
                    "doc_name": c.get("doc_name"),
                    "title": c.get("title"),
                    "content": c.get("content"),
                    "score": round(score, 3)
                })

        # 按得分从高到低排序截取 top_k
        scored_chunks.sort(key=lambda x: x["score"], reverse=True)
        return scored_chunks[:top_k]

    def format_chunks_for_prompt(self, chunks: List[Dict[str, Any]]) -> str:
        """将检索到的切片格式化为规范的 Prompt 注入段落"""
        if not chunks:
            return ""
        lines = [
            "## 📚 企业私有知识库与运维手册实时召回参考 (Mounted Runbooks & KB)",
            "系统已从挂载的私有业务文档中自动检索到以下最相关的内部规范或排障依据，请优先采纳并结合分析：",
            ""
        ]
        for idx, chk in enumerate(chunks, 1):
            doc_name = chk.get("doc_name", "知识文档")
            title = chk.get("title", "相关章节")
            content = chk.get("content", "").strip()
            score = chk.get("score", 0)
            lines.append(f"### [参考 {idx}] 《{doc_name}》- {title} (相关度: {score})")
            lines.append(f"```text\n{content}\n```\n")

        return "\n".join(lines)


rag_manager = RagManager()
