import sys
import tempfile
from collections.abc import Generator
from pathlib import Path
from typing import Any

from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage
from dify_plugin.file.file import File as DifyFile

# 插件根目录加入 sys.path，便于在任何启动方式下都能导入 ofdrw_cli。
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from ofdrw_cli import OfdrwCliError, convert_to_text  # noqa: E402

EMPTY_TEXT_HINT = (
    "导出结果为空：该 OFD 文档可能整页由图片或矢量路径图元构成，"
    "或使用字形索引定位文字，因此无法提取出纯文本。"
)


class TextExporterTool(Tool):
    """输入 OFD 文件，导出纯文本（OFDRW TextExporter）。"""

    def _invoke(
        self, tool_parameters: dict[str, Any]
    ) -> Generator[ToolInvokeMessage, None, None]:
        ofd_file = self._resolve_file(tool_parameters.get("ofd_file"))
        pages = self._normalize_pages(tool_parameters.get("pages"))
        max_chars = self._safe_int(tool_parameters.get("max_chars"), 0)
        attach_file = bool(tool_parameters.get("attach_file"))

        filename = ofd_file.filename or "input.ofd"
        try:
            blob = ofd_file.blob
        except Exception as e:  # noqa: BLE001 - 统一转换为清晰的用户提示
            raise RuntimeError(f"无法读取上传的 OFD 文件：{e}") from e

        if not blob:
            raise ValueError("上传的 OFD 文件内容为空。")
        if not blob.startswith(b"PK"):
            raise ValueError(
                "输入文件不是有效的 OFD 文档：OFD 本质是 ZIP 包，文件头应为 'PK'。"
            )

        tmp_root = Path(tempfile.mkdtemp(prefix="ofdrw-dify-"))
        try:
            src = tmp_root / "input.ofd"
            dst = tmp_root / "output.txt"
            src.write_bytes(blob)

            try:
                result = convert_to_text(src, dst, pages=pages)
            except OfdrwCliError as e:
                raise RuntimeError(str(e)) from e

            full_text: str = result["text"]
            meta: dict[str, Any] = dict(result["meta"])
        finally:
            for path in (tmp_root / "output.txt", tmp_root / "output.txt.result.json"):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            try:
                tmp_root.rmdir()
            except OSError:
                pass

        text = full_text
        truncated = False
        if max_chars > 0 and len(text) > max_chars:
            text = text[:max_chars]
            truncated = True

        meta.update(
            {
                "file_name": filename,
                "file_size": len(blob),
                "requested_pages": pages or "all",
                "returned_char_count": len(text),
                "truncated": truncated,
            }
        )

        yield self.create_text_message(text if text.strip() else EMPTY_TEXT_HINT)
        yield self.create_json_message(meta)

        if attach_file:
            txt_name = Path(filename).stem + ".txt"
            yield self.create_blob_message(
                full_text.encode("utf-8"),
                meta={"mime_type": "text/plain", "filename": txt_name},
            )

    @staticmethod
    def _resolve_file(value: Any) -> DifyFile:
        if value is None:
            raise ValueError("缺少必填参数 ofd_file，请上传一个 OFD 文件。")
        if isinstance(value, DifyFile):
            return value
        if isinstance(value, dict):
            # 正常情况下 SDK 已把文件参数转换为 File 对象，这里做一层兜底。
            return DifyFile(**value)
        raise ValueError(f"参数 ofd_file 类型不支持：{type(value).__name__}")

    @staticmethod
    def _normalize_pages(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _safe_int(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
