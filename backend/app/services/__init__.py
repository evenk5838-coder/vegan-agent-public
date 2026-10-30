"""共用业务服务层。

约定（架构说明 v0.3 第 8 节）：
- HTTP 路由与 Agent 工具适配器都调用这里的函数，不各写一份业务逻辑；
- 服务不导入 main.py、不调用本机 HTTP 端点；
- 依赖方向：路由/工具 → services → 数据访问与供应商适配。
"""
from . import actions  # noqa: F401
from . import chat_reads  # noqa: F401
from . import food_advice  # noqa: F401
from . import food_drafts as draft_service  # noqa: F401
from . import foods as food_service  # noqa: F401
from . import recipe_query  # noqa: F401
from . import recipe_save  # noqa: F401
from . import restaurants as restaurant_service  # noqa: F401
