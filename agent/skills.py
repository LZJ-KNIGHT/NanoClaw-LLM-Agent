"""NanoClaw 技能加载器。

技能（Skill）是 ``skills/`` 目录下的一个子目录，内含一份 ``SKILL.md``，顶部是
YAML frontmatter（name、description 等元数据），正文是指南内容。

采用渐进式披露：System Prompt 只放「技能名 + 一句话简介」这层轻量索引，模型要用时
再用 read_file 打开 SKILL.md。build_skills_summary() 的输出注入 System Prompt，
load_skill() 供模型按需读全文。
"""

import os
from typing import Any

import yaml


class SkillsLoader:
    """加载并解析 skills 目录下的技能定义。"""

    def __init__(self, skills_dir: str = "skills") -> None:
        # 技能根目录，约定其下每个子目录含一份 SKILL.md
        self.skills_dir = skills_dir

    def _parse_frontmatter(self, content: str) -> tuple[dict, str]:
        """解析 SKILL.md 顶部的 YAML frontmatter（文件以 ``---`` 开头、``---`` 结束）。

        Returns:
            ``(metadata, body)``；无 frontmatter 时返回 ``({}, 原文)``。
        """
        # 归一化换行，兼容 Windows CRLF（否则 "---\n" 匹配不上 "\r\n"）
        content = content.replace("\r\n", "\n").replace("\r", "\n")

        if not content.startswith("---\n"):
            return {}, content

        # 从下标 3 开始找第二个 "---" 作为结束标记
        end = content.find("\n---", 3)
        if end == -1:
            return {}, content

        # content[4:end] 掐掉开头的 "---\n"，取出中间的 YAML 文本
        yaml_text = content[4:end]
        # end+1 跳过 "\n"，body 从结束的 "---" 之后开始
        body = content[end + 1:]
        if body.startswith("---"):
            body = body[3:]
        body = body.lstrip("\n")

        # 用 safe_load 而非 load：禁止构造任意 Python 对象，防止技能文件里的
        # 恶意 YAML 触发代码执行
        try:
            metadata = yaml.safe_load(yaml_text)
        except yaml.YAMLError:
            metadata = None
        # 解析结果不是 dict（如空文档/纯标量）时统一兜底成空字典
        if not isinstance(metadata, dict):
            metadata = {}

        return metadata, body

    def list_skills(self) -> list[dict]:
        """扫描所有技能，返回 ``[{name, description, path}]``；name 缺省时回退为目录名。"""
        skills: list[dict] = []
        if not os.path.isdir(self.skills_dir):
            return skills

        # sorted 保证每次扫描顺序稳定，生成的摘要不会随机抖动
        for entry in sorted(os.listdir(self.skills_dir)):
            skill_file = os.path.join(self.skills_dir, entry, "SKILL.md")
            if not os.path.isfile(skill_file):
                continue
            try:
                with open(skill_file, encoding="utf-8") as f:
                    content = f.read()
            except OSError:
                # 单个文件读失败不影响其它技能
                continue

            metadata, _ = self._parse_frontmatter(content)
            skills.append({
                # 没有 name 字段时用目录名作为技能名
                "name": metadata.get("name", entry),
                "description": metadata.get("description", ""),
                "path": entry,
            })
        return skills

    def build_skills_summary(self) -> str:
        """生成技能目录摘要（「技能名 + 路径 + 一句话简介」，渐进式披露的第一层），
        无技能时返回空字符串。
        """
        skills = self.list_skills()
        if not skills:
            return ""

        lines = [
            f"- {s['name']} ({s['path']}/SKILL.md)：{s['description']}"
            for s in skills
        ]
        # 提示模型「需要时先读取 SKILL.md」，把按需读取的行为告诉它
        header = (
            "你有以下技能可用。当你需要使用某项技能时，\n"
            "请先用 read_file 工具读取对应的 SKILL.md 文件获取详细指南。\n\n"
            "可用技能：\n"
        )
        return header + "\n".join(lines)

    def load_skill(self, name: str) -> str | None:
        """返回指定技能的完整正文（去掉 frontmatter），找不到返回 ``None``。

        与 list_skills 的「只看元数据」不同，这里返回正文，属于渐进式披露的第二层。
        """
        skill_file = os.path.join(self.skills_dir, name, "SKILL.md")
        if not os.path.isfile(skill_file):
            return None
        try:
            with open(skill_file, encoding="utf-8") as f:
                content = f.read()
        except OSError:
            return None
        # 正文才是指南内容，frontmatter 元数据不返回
        _, body = self._parse_frontmatter(content)
        return body
