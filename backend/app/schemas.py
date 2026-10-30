from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field, field_validator, model_validator


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r'^[A-Za-z0-9_]+$')
    password: str = Field(min_length=8, max_length=128)


class Preferences(BaseModel):
    diet: Literal['pure', 'vegan', 'lacto_ovo'] = 'vegan'
    avoid_coriander: bool = False


class IngredientInput(BaseModel):
    text: str = Field(min_length=1, max_length=12000)
    complete: bool = False
    label_warnings: list[str] = Field(default_factory=list, max_length=20)

    @field_validator('label_warnings')
    @classmethod
    def short_warnings(cls, v):
        if any(len(x) > 1000 for x in v):
            raise ValueError('单条标签提示过长')
        return v


class Extracted(BaseModel):
    ingredients: list[str] = Field(min_length=1, max_length=300)
    complete: bool
    label_warnings: list[str] = Field(default_factory=list, max_length=20)

    @field_validator('ingredients', 'label_warnings')
    @classmethod
    def short_items(cls, v):
        if any(not x.strip() or len(x) > 1000 for x in v):
            raise ValueError('字段为空或过长')
        return v


class RecipeInput(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    ingredients: str = Field(min_length=1, max_length=6000)
    steps: str = Field(min_length=1, max_length=10000)
    # 旧值 main 保留兼容（旧记录与旧客户端不迁移、不改写）；新录入引导选择具体餐次。
    meal: Literal['breakfast', 'lunch', 'dinner', 'afternoon_tea', 'main'] = 'main'
    tags: list[Literal['dessert']] = Field(default_factory=list, max_length=5)
    purpose: Literal['everyday', 'protein', 'fat'] = 'everyday'
    # foreign仅用于兼容未细分旧记录，不代表西式、不触发数据迁移。
    cuisine: Literal['chinese', 'western', 'other', 'foreign'] = 'chinese'
    nutrition_notes: str = Field(default='', max_length=2000)
    source_url: str = Field(default='', max_length=2000)
    # 原创归属：个人原创可不填外部 URL，但要能记录来源性质
    origin: Literal['user_original', 'adapted', 'external'] = 'user_original'
    yield_amount: float | None = Field(default=None, ge=0, le=100)
    yield_unit: str = Field(default='', max_length=20)
    confirmed: bool


class RecipeSearch(BaseModel):
    query: str = Field(min_length=1, max_length=120)
    page: int = Field(default=1, ge=1, le=100000)
    page_size: int = Field(default=20, ge=1, le=50)
    # 与列表相同的过滤条件，必须实际生效（不能只给列表加筛选）。
    meal: str | None = Field(default=None, max_length=20)
    tag: str | None = Field(default=None, max_length=20)
    purpose: str | None = Field(default=None, max_length=20)
    diet: str | None = Field(default=None, max_length=20)
    cuisine: str | None = Field(default=None, max_length=20)


class FoodInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    # 食品类别与开封状态：与加工状态 condition 并列，可同时表达（旧记录缺省即未知）
    food_class: Literal['fresh', 'cooked', 'packaged', 'unknown'] = 'unknown'
    opened: bool | None = None
    storage: Literal['ambient', 'refrigerated', 'frozen', 'unknown'] = 'unknown'
    location: Literal['indoor', 'outdoor', 'unknown'] = 'unknown'
    temperature_c: float | None = Field(default=None, ge=-80, le=70)
    condition: Literal['raw', 'cut', 'cooked', 'opened', 'unopened', 'unknown'] = 'unknown'
    date_type: Literal['purchase', 'production', 'prepared', 'opened', 'unknown'] = 'unknown'
    started_at: datetime | None = None
    expires_at: datetime | None = None
    # rule 只能由后端重评写入并在后续状态修改中保留，不接受客户端自行声明（见 update_food）。
    deadline_source: Literal['unknown', 'package', 'user', 'rule'] = 'unknown'
    package_note: str = Field(default='', max_length=1000)
    storage_note: str = Field(default='', max_length=500)
    remind_at: datetime | None = None
    status: Literal['active', 'used', 'discarded'] = 'active'
    reconfirmed: bool = False

    @field_validator('name')
    @classmethod
    def food_name(cls, value):
        value = value.strip()
        if not value:
            raise ValueError('请提供食材名称')
        return value

    @field_validator('storage', 'condition', 'date_type', mode='before')
    @classmethod
    def unknown_facts(cls, value):
        return 'unknown' if value is None or isinstance(value, str) and not value.strip() else value

    @field_validator('started_at', 'expires_at', 'remind_at')
    @classmethod
    def timezone_required(cls, v):
        if v is not None and (v.tzinfo is None or v.utcoffset() is None):
            raise ValueError('请提供带时区的日期时刻')
        return v

    @model_validator(mode='after')
    def source_required(self):
        if self.expires_at and self.deadline_source == 'unknown':
            raise ValueError('设置日期须标明包装说明或用户设置')
        if self.deadline_source == 'package' and not self.package_note.strip():
            raise ValueError('请记录适用于当前状态的包装说明')

        if self.remind_at and self.expires_at and self.remind_at > self.expires_at:
            raise ValueError('提醒时刻不能晚于已设置日期')
        return self


# 供应商已定：阿里云百炼（北京）非实时短音频。上限按官方口径（5 分钟 / 音频 10MB）。
class DraftCreate(BaseModel):
    items: list[dict]
    chat_turn_id: str | None = None
    # 相对日期解析上下文：未提供时用服务器当前时间与 Asia/Shanghai。
    reference_time: str | None = None
    timezone: str | None = Field(default=None, max_length=64)


class DraftItemUpdate(BaseModel):
    clear: list[str] = Field(default_factory=list, max_length=10)
    name: str | None = None
    food_class: Literal['fresh', 'cooked', 'packaged', 'unknown'] | None = None
    opened: bool | None = None
    storage: str | None = None
    condition: str | None = None
    date_type: str | None = None
    started_at: str | None = None
    expires_at: str | None = None
    deadline_source: str | None = None
    package_note: str | None = None
    storage_note: str | None = None
    remind_at: str | None = None
    location: str | None = None
    temperature_c: float | None = None


class ActionProposal(BaseModel):
    """正式修改的拟改值：HTTP 端点与模型工具共用同一份校验。"""
    action: Literal['food_status', 'food_condition', 'food_reminder']
    food_id: str = Field(min_length=1, max_length=64)
    status: Literal['active', 'used', 'discarded'] | None = None
    storage: Literal['ambient', 'refrigerated', 'frozen'] | None = None
    condition: Literal['raw', 'cut', 'cooked'] | None = None
    temperature_c: float | None = Field(default=None, ge=-80, le=70)
    opened: bool | None = None
    food_class: Literal['fresh', 'cooked', 'packaged', 'unknown'] | None = None
    remind_at: str | None = None
    date_type: Literal['purchase', 'production', 'prepared', 'opened', 'unknown'] | None = None
    started_at: str | None = None
    storage_note: str | None = Field(default=None, max_length=500)
    package_note: str | None = Field(default=None, max_length=1000)

    @field_validator('remind_at', 'started_at')
    @classmethod
    def remind_timezone(cls, v):
        if not v:
            return v
        try:
            parsed = datetime.fromisoformat(v)
        except ValueError:
            raise ValueError('提醒时间格式不正确，请提供带时区的时间') from None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError('提醒时间需要带时区')
        return v


class DraftConfirm(BaseModel):
    item_ids: list[str] | None = None
    idempotency_key: str | None = Field(default=None, max_length=128)
    # 前端回传它展示给用户的建议摘要；与当前不一致时返回 409 DRAFT_CHANGED
    advice_digest: str | None = Field(default=None, max_length=128)


class ReevaluateEvidence(BaseModel):
    deadline_source: Literal['package', 'rule', 'user', 'unknown'] = 'unknown'
    package_note: str | None = Field(default=None, max_length=1000)
    expires_at: str | None = None
    remind_at: str | None = None
    rule_id: str | None = Field(default=None, max_length=64)


class ReevaluateInput(BaseModel):
    food_class: Literal['fresh', 'cooked', 'packaged', 'unknown'] | None = None
    opened: bool | None = None
    storage: Literal['ambient', 'refrigerated', 'frozen', 'unknown'] | None = None
    condition: Literal['raw', 'cut', 'cooked', 'opened', 'unopened', 'unknown'] | None = None
    temperature_c: float | None = Field(default=None, ge=-80, le=70)
    location: Literal['indoor', 'outdoor', 'unknown'] | None = None
    clear: list[str] = Field(default_factory=list, max_length=10)
    evidence: ReevaluateEvidence = Field(default_factory=ReevaluateEvidence)


TRANSCRIPTION_LIMITS = {'provider': 'aliyun-bailian', 'region': 'cn-beijing',
                        'model': 'qwen-audio-3.0-asr-flash',
                        'mime_types': ['audio/wav', 'audio/mp4', 'audio/webm', 'audio/mpeg', 'audio/ogg'],
                        'max_bytes': 10485760, 'max_base64_bytes': 13981016,
                        'max_seconds': 300, 'min_seconds': 0.2,
                        'status': '按供应商官方上限设定；真实格式与时长仍需实测'}


class ChatSessionCreate(BaseModel):
    title: str = Field(default='', max_length=60)


class ChatAttachment(BaseModel):
    kind: Literal['ingredient_photo']
    job_id: str = Field(min_length=1, max_length=64)


class ChatMessageInput(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    idempotency_key: str | None = Field(default=None, max_length=128)
    attachments: list[ChatAttachment] = Field(default_factory=list, max_length=10)
