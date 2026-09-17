import sys
from pathlib import Path
from typing import Any

from dify_plugin import ToolProvider
from dify_plugin.errors.tool import ToolProviderCredentialValidationError

# 插件根目录加入 sys.path，便于在任何启动方式下都能导入 ofdrw_cli。
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from ofdrw_cli import check_environment  # noqa: E402


class OfdrwConverterProvider(ToolProvider):
    """OFDRW 文档转换工具提供者。

    本插件不依赖任何第三方凭据，凭据校验阶段只检查插件自带资源是否完整。
    """

    def _validate_credentials(self, credentials: dict[str, Any]) -> None:
        try:
            status = check_environment()
        except Exception as e:  # noqa: BLE001 - 统一转换为 Dify 可识别的错误
            raise ToolProviderCredentialValidationError(str(e)) from e

        if status["missing"]:
            raise ToolProviderCredentialValidationError(
                "插件自带资源缺失，无法工作："
                + ", ".join(status["missing"])
                + "。请重新打包插件，或设置环境变量 OFDRW_JAVA_HOME 指向可用的 JDK/JRE。"
            )
