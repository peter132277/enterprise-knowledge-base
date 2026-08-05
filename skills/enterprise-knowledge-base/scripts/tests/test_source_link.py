import importlib.util
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SCRIPT = Path(__file__).resolve().parents[1] / "source_link.py"
SPEC = importlib.util.spec_from_file_location("source_link", SCRIPT)
SOURCE_LINK = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(SOURCE_LINK)


FILE_URL = "https://example.feishu.cn/file/abc123"


class SourceLinkTests(unittest.TestCase):
    def inject(self, markdown: str) -> str:
        return SOURCE_LINK.inject_source_link(
            markdown,
            source_name="原始资料.docx",
            source_url=FILE_URL,
            source_hash="a" * 64,
        )

    def test_injects_into_source_section_and_removes_local_source_lines(self) -> None:
        result = self.inject(
            """正文

## 来源

- 原始文件：旧文件.docx
- 原始位置：`10_来源/旧文件.docx`
- 来源范围：第 1—5 页
- 文件校验：SHA-256 `old`
"""
        )
        self.assertIn("[下载原文件：原始资料.docx]", result)
        self.assertIn(FILE_URL, result)
        self.assertIn("来源范围：第 1—5 页", result)
        self.assertNotIn("原始位置", result)
        self.assertNotIn("旧文件.docx", result)
        self.assertNotIn("文件校验", result)

    def test_supports_traceability_heading_and_preserves_following_section(self) -> None:
        result = self.inject(
            """正文

## 来源与可追溯性

- 提取范围：全文

## 后续章节

结尾
"""
        )
        self.assertIn("## 来源与可追溯性", result)
        self.assertIn("提取范围：全文", result)
        self.assertIn("## 后续章节\n\n结尾", result)

    def test_appends_source_section_when_missing(self) -> None:
        result = self.inject("正文")
        self.assertIn("## 来源", result)
        self.assertTrue(result.endswith(f"- 文件 SHA-256：`{'a' * 64}`\n"))

    def test_replaces_previous_managed_original_subsection_idempotently(self) -> None:
        first = self.inject(
            """正文

## 来源

### 原文件

- [下载原文件：旧.docx](https://example.feishu.cn/file/old)
- 文件 SHA-256：`old`
"""
        )
        second = self.inject(first)
        self.assertEqual(first, second)
        self.assertEqual(second.count("### 原文件"), 1)
        self.assertNotIn("/file/old", second)

    def test_rejects_non_drive_or_external_url(self) -> None:
        for url in (
            "http://example.feishu.cn/file/abc",
            "https://example.feishu.cn/wiki/abc",
            "https://example.com/file/abc",
        ):
            with self.subTest(url=url), self.assertRaises(
                SOURCE_LINK.SourceLinkError
            ):
                SOURCE_LINK.validate_source_url(url)

    def test_compare_ignores_only_source_section(self) -> None:
        expected = """正文

## 来源

- 原始文件：旧.docx

## 结尾

一致
"""
        remote = """# 标题

正文

## 来源

### 原文件

- [下载原文件：新.docx](https://example.feishu.cn/file/new)

## 结尾

一致
"""
        comparison = SOURCE_LINK.compare_ignoring_source(expected, remote)
        self.assertTrue(comparison["match"])
        changed = remote.replace("一致", "远端改动")
        self.assertFalse(
            SOURCE_LINK.compare_ignoring_source(expected, changed)["match"]
        )

    def test_verify_requires_url_and_filename_inside_source_section(self) -> None:
        linked = self.inject("正文")
        self.assertTrue(
            SOURCE_LINK.verify_source_link(
                linked,
                source_name="原始资料.docx",
                source_url=FILE_URL,
            )["verified"]
        )
        outside = f"[原始资料.docx]({FILE_URL})\n\n## 来源\n\n没有链接"
        self.assertFalse(
            SOURCE_LINK.verify_source_link(
                outside,
                source_name="原始资料.docx",
                source_url=FILE_URL,
            )["verified"]
        )
        self.assertTrue(
            SOURCE_LINK.verify_source_link(
                linked.replace("\n", "\r\n"),
                source_name="原始资料.docx",
                source_url=FILE_URL,
            )["verified"]
        )

    def test_compare_accepts_feishu_table_separator_normalization(self) -> None:
        expected = """# 标题

| 产品 | 价格 |
|---|---:|
| 课程 | 2,980元 |
"""
        remote = """# 标题

| 产品 | 价格 |
|-|-|
| 课程 | 2,980元 |
"""
        self.assertTrue(
            SOURCE_LINK.compare_ignoring_source(expected, remote)["match"]
        )
        changed = remote.replace("2,980元", "19,800元")
        self.assertFalse(
            SOURCE_LINK.compare_ignoring_source(expected, changed)["match"]
        )

    def test_compare_accepts_feishu_task_spacing_and_self_link_roundtrip(self) -> None:
        expected = """# 标题

- [ ] 第一项
- [ ] 第二项

- 妙记：<https://tenant.feishu.cn/minutes/example>
"""
        remote = """# 标题

- [ ] 第一项

- [ ] 第二项

- 妙记：[https://tenant.feishu.cn/minutes/example](https://tenant.feishu.cn/minutes/example)
"""
        self.assertTrue(
            SOURCE_LINK.compare_ignoring_source(expected, remote)["match"]
        )
        changed = remote.replace("第二项", "远端改动")
        self.assertFalse(
            SOURCE_LINK.compare_ignoring_source(expected, changed)["match"]
        )

    def test_semantic_compare_classifies_format_only_changes(self) -> None:
        expected = """# 标题

**定位**客户需求。

1. 第一步
2. 第二步
"""
        remote = """# 标题

定位客户需求。

1) 第一步
2) 第二步
"""
        comparison = SOURCE_LINK.compare_ignoring_source(expected, remote)
        self.assertTrue(comparison["match"])
        self.assertFalse(comparison["strict_match"])
        self.assertTrue(comparison["semantic_match"])
        self.assertEqual(comparison["classification"], "format-only")

    def test_semantic_compare_preserves_meaningful_content_and_code(self) -> None:
        expected = """# 标题

客户需求。

```python
print("alpha")
```
"""
        content_changed = expected.replace("客户需求", "渠道需求")
        code_changed = expected.replace('print("alpha")', 'print("beta")')
        self.assertEqual(
            SOURCE_LINK.compare_ignoring_source(expected, content_changed)[
                "classification"
            ],
            "semantic-content-difference",
        )
        self.assertFalse(
            SOURCE_LINK.compare_ignoring_source(expected, code_changed)["match"]
        )

    def test_semantic_compare_fails_closed_for_raw_html(self) -> None:
        expected = "# 标题\n\n正文"
        remote = "# 标题\n\n<div>正文</div>"
        comparison = SOURCE_LINK.compare_ignoring_source(expected, remote)
        self.assertFalse(comparison["match"])
        self.assertFalse(comparison["semantic_supported"])
        self.assertEqual(comparison["classification"], "unsupported-structure")

    def test_semantic_compare_does_not_flatten_table_structure(self) -> None:
        expected = """# 标题

| 产品 | 价格 |
|---|---|
| 课程 | 2980 |
"""
        reordered = """# 标题

| 价格 | 产品 |
|---|---|
| 2980 | 课程 |
"""
        self.assertFalse(
            SOURCE_LINK.compare_ignoring_source(expected, reordered)["match"]
        )


if __name__ == "__main__":
    unittest.main()
