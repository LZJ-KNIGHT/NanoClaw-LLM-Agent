"""NanoClaw 会话管理器。

负责把每轮对话以 JSONL 形式持久化到磁盘，支持按 session_key 读写、
清空与枚举会话。所有数据文件都落在 ``workspace/`` 目录下。

选 JSONL 而非数据库：追加写无需读全量、写入 O(1)；进程崩溃最多丢最后一行，
不会毁掉整份历史；纯文本、零依赖，可直接打开人工调试。
"""

import json
import os
from datetime import datetime
from typing import Any


class SessionManager:
    """会话持久化管理器。"""

    def __init__(self, sessions_dir: str = "workspace/sessions") -> None:
        """构造管理器，并确保会话目录存在。

        参数:
            sessions_dir: 会话文件存放目录，默认 workspace/sessions。
        """
        self.sessions_dir = sessions_dir
        os.makedirs(self.sessions_dir, exist_ok=True)  # exist_ok 保证建目录幂等

    def _get_session_path(self, session_key: str) -> str:
        """把 session_key 映射为 JSONL 文件路径。

        ``:`` 在文件名里不便处理，统一替换为 ``_``，例如
        ``cli:direct`` → ``cli_direct.jsonl``。
        """
        filename = session_key.replace(":", "_") + ".jsonl"
        return os.path.join(self.sessions_dir, filename)

    def save_message(self, session_key: str, message: dict) -> None:
        """追加一条消息到会话，自动附带 ISO 时间戳。

        参数:
            session_key: 会话标识（如 cli:direct）。
            message: 单条消息，通常是 {"role": ..., "content": ...}。

        先拷贝再补 timestamp，避免污染调用方传入的原 dict。
        """
        record = dict(message)
        record["timestamp"] = datetime.now().isoformat()  # 仅本地记录，读取时会摘掉

        path = self._get_session_path(session_key)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def rewrite(self, session_key: str, history: list[dict]) -> None:
        """用给定消息列表整体重写会话文件，覆盖原有全部内容。

        与 save_message 的追加语义不同，这里用于压缩历史后把结果持久化，
        避免旧消息在磁盘上无限累积、导致每次启动都超 token 预算。
        """
        path = self._get_session_path(session_key)
        with open(path, "w", encoding="utf-8") as f:
            for message in history:
                record = dict(message)
                record["timestamp"] = datetime.now().isoformat()
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def get_history(self, session_key: str) -> list[dict]:
        """读取会话历史，去掉 timestamp 字段（OpenAI API 不认识它）。

        参数:
            session_key: 会话标识。

        返回:
            消息列表；文件不存在、行为空或单行损坏时都不会抛异常。
        """
        path = self._get_session_path(session_key)
        if not os.path.isfile(path):
            return []  # 首次对话还没有文件

        history: list[dict] = []
        with open(path, encoding="utf-8") as f:
            for line in f:  # 逐行流式读，契合 JSONL，无需一次载入内存
                line = line.strip()
                if not line:
                    continue  # 跳过空行（如文件末尾的换行）
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue  # 单行损坏就跳过，保证整体仍可读
                record.pop("timestamp", None)
                history.append(record)
        return history

    def clear(self, session_key: str) -> None:
        """删除指定会话的持久化文件（不存在则忽略）。"""
        path = self._get_session_path(session_key)
        if os.path.isfile(path):  # 先判存在再删，避免 FileNotFoundError
            os.remove(path)

    def list_sessions(self) -> list[str]:
        """枚举所有已持久化的 session_key（去掉 ``.jsonl`` 后缀）。"""
        if not os.path.isdir(self.sessions_dir):
            return []
        sessions = []
        for filename in os.listdir(self.sessions_dir):
            if filename.endswith(".jsonl"):  # 只认会话文件
                sessions.append(filename[:-6])  # 去掉 ".jsonl"（6 个字符）
        return sorted(sessions)
