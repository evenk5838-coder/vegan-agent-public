from __future__ import annotations

import asyncio
import base64
import io
import secrets
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta
from typing import Annotated, Literal

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import Depends, FastAPI, File, Form, Query, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, text as sql_text
from sqlalchemy.exc import IntegrityError

from . import core, dates, drafts, knowledge, providers, recipes as recipe_filters, storage_rules, transcription, worker
from .services import (actions as actions_service, chat_reads, draft_service, food_advice,
                       food_service, recipe_query, recipe_save, restaurant_service)
from .services import barcodes
from .services import storage_guidance
from .agent import TURN_BUDGET_SECONDS
from .core import AppError, Job, Record, Session, User, chat_lock, digest, now, record_dict, settings, uid
from .rules import evaluate
from .map_proxy_protocol import ProtocolError as MapProxyProtocolError, encode_jsonp, normalize_proxy_request
from .schemas import (ActionProposal, ChatAttachment, ChatMessageInput,
                      ChatSessionCreate, Credentials, DraftConfirm, DraftCreate, DraftItemUpdate,
                      FoodInput, IngredientInput, Preferences, RecipeInput, ReevaluateInput,
                      RecipeSearch)

hasher = PasswordHasher()
SESSION_COOKIE = 'shushi_session'


@asynccontextmanager
async def lifespan(app):
    core.init_db()
    # schema由Alembic管理，避免每次启动偷偷修改数据库结构。
    worker.recover_and_clean()
    task = asyncio.create_task(worker.loop())
    yield
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


app = FastAPI(title='蔬食', version='0.1.0', lifespan=lifespan)


@app.exception_handler(AppError)
async def app_error(request, e):
    return JSONResponse(status_code=e.status, content={'error': {'code': e.code, 'message': e.message}})


@app.exception_handler(RequestValidationError)
async def validation_error(request, e):
    # 不回显原始input，避免包含密码/上传内容。
    fields = ', '.join('.'.join(str(p) for p in x['loc'][1:]) for x in e.errors()[:4])
    return JSONResponse(status_code=422, content={'error': {'code': 'VALIDATION_ERROR', 'message': f'请检查输入内容：{fields}'}})


@app.middleware('http')
async def boundary(request, call_next):
    request_id = uid()
    if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
        origin = request.headers.get('origin')
        if origin and origin.rstrip('/') != settings.app_origin.rstrip('/'):
            return JSONResponse(status_code=403, content={'error': {'code': 'ORIGIN_DENIED', 'message': '请从蔬食页面提交操作。'}})
    try:
        response = await call_next(request)
    except Exception:
        response = JSONResponse(status_code=500, content={'error': {'code': 'INTERNAL_ERROR', 'message': '操作未完成，请稍后重试。'}})
    response.headers['X-Request-Id'] = request_id
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


def current_user(request: Request):
    token = request.cookies.get(SESSION_COOKIE, '')
    if not token:
        raise AppError('AUTH_REQUIRED', '请先登录。', 401)
    with core.DB() as db:
        session = db.get(Session, digest(token))
        if not session or session.expires <= now():
            raise AppError('AUTH_REQUIRED', '登录已失效，请重新登录。', 401)
        user = db.get(User, session.owner)
        if not user:
            raise AppError('AUTH_REQUIRED', '请重新登录。', 401)
        return user


Authed = Annotated[User, Depends(current_user)]


def owned(db, identifier, owner, kind=None):
    record = db.get(Record, identifier)
    if not record or record.owner != owner or (kind and record.kind != kind) or (record.expires and record.expires <= now()):
        raise AppError('NOT_FOUND', '找不到这条记录。', 404)
    return record


def auth_limit(request, username):
    ip = request.client.host if request.client else 'local'
    identity = digest(ip)
    with core.DB() as db:
        # 记录内不保存IP和密码。限速覆盖注册与登录，避免无限尝试。
        recent = db.scalars(select(Record).where(Record.owner == identity, Record.kind == 'auth_attempt', Record.created >= now()-timedelta(minutes=15))).all()
        if len(recent) >= 20:
            raise AppError('RATE_LIMITED', '尝试次数较多，请稍后再试。', 429)
        db.add(Record(owner=identity, kind='auth_attempt', payload={}, expires=now()+timedelta(minutes=15)))
        db.commit()


def create_session(response, db, user):
    token = secrets.token_urlsafe(32)
    db.add(Session(token_hash=digest(token), owner=user.id, expires=now()+timedelta(days=14)))
    db.commit()
    response.set_cookie(SESSION_COOKIE, token, httponly=True, secure=settings.cookie_secure, samesite='lax', max_age=14*86400, path='/')
    return {'username': user.username, 'preferences': user.preferences}


@app.get('/api/v1/health')
def health():
    try:
        with core.DB() as db:
            db.execute(sql_text('SELECT 1'))
    except Exception:
        raise AppError('DATABASE_UNAVAILABLE', '服务暂时不可用，请稍后重试。', 503) from None
    environment = settings.app_environment
    return {'status': 'ok', 'database': 'ok',
            'environment': environment if environment in {'local-development', 'production', 'test'} else 'configured'}


@app.post('/api/v1/auth/register', status_code=201)
def register(data: Credentials, request: Request, response: Response):
    auth_limit(request, data.username)
    with core.DB() as db:
        user = User(username=data.username.lower(), password_hash=hasher.hash(data.password))
        db.add(user)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            raise AppError('ACCOUNT_EXISTS', '此账号名不可用，请换一个。', 409) from None
        return create_session(response, db, user)


@app.post('/api/v1/auth/login')
def login(data: Credentials, request: Request, response: Response):
    auth_limit(request, data.username)
    with core.DB() as db:
        user = db.scalar(select(User).where(User.username == data.username.lower()))
        try:
            valid = user and hasher.verify(user.password_hash, data.password)
        except VerificationError:
            valid = False
        if not valid:
            raise AppError('INVALID_CREDENTIALS', '账号或密码不正确。', 401)
        return create_session(response, db, user)


@app.post('/api/v1/auth/logout')
def logout(request: Request, response: Response):
    with core.DB() as db:
        db.execute(delete(Session).where(Session.token_hash == digest(request.cookies.get(SESSION_COOKIE, ''))))
        db.commit()
    response.delete_cookie(SESSION_COOKIE, path='/')
    return {'ok': True}


@app.get('/api/v1/auth/me')
def me(user: Authed):
    return {'username': user.username, 'preferences': user.preferences}


@app.put('/api/v1/preferences')
def preferences(data: Preferences, user: Authed):
    with core.DB() as db:
        target = db.get(User, user.id)
        target.preferences = data.model_dump()
        db.commit()
    return data


@app.get('/api/v1/capabilities')
def capabilities(user: Authed):
    map_js_ready = providers.map_js_ready()
    return {'photo_configured': bool(settings.deepseek_api_key),
            'map_configured': bool(settings.amap_api_key),
            'map_js_ready': map_js_ready,
            'external_calls_enabled': settings.external_calls_enabled, 'push_configured': worker.push_configured(),
            'chat_configured': bool(settings.deepseek_api_key and settings.external_calls_enabled),
            'transcription_configured': transcription.configured(),
            'transcription': transcription.capabilities(),
            'knowledge_status': '初始配料规则待样例复核；完整菜谱/营养/储存条目仍在整理。',
            'storage_rules_ready': False}


@app.post('/api/v1/ingredients/evaluate')
def ingredients(data: IngredientInput, user: Authed):
    from .services import ingredients as ingredient_service
    with core.DB() as db:
        result, item = ingredient_service.evaluate_and_record(
            db, user.id, data.text, complete=data.complete, label_warnings=data.label_warnings,
            avoid_coriander=user.preferences.get('avoid_coriander', False), source='manual')
        db.commit()
        return {'id': item.id, **result}


@app.get('/api/v1/ingredients/history')
def ingredient_history(user: Authed):
    with core.DB() as db:
        return [record_dict(x) for x in db.scalars(select(Record).where(Record.owner == user.id, Record.kind == 'ingredient', Record.expires > now()).order_by(Record.created.desc()).limit(50))]


@app.get('/api/v1/ingredients/barcode/{code}')
def ingredient_barcode(code: str, user: Authed):
    # Validate before reserving a query. No model and no ingredient history write:
    # the user first checks the returned product/ingredients, then evaluates it.
    normalized = barcodes.normalize_code(code)
    providers.reserve_call(user.id, 'barcode')
    return barcodes.lookup(normalized)


@app.delete('/api/v1/ingredients/history/{identifier}')
def delete_history(identifier: str, user: Authed):
    with core.DB() as db:
        item = owned(db, identifier, user.id, 'ingredient')
        for job in db.scalars(select(Job).where(Job.owner == user.id, Job.kind == 'photo')):
            if job.payload.get('history_id') == identifier:
                db.delete(job)
        db.delete(item)
        db.commit()
    return {'ok': True}


@app.post('/api/v1/ingredients/photo', status_code=202)
async def photo(user: Authed, file: UploadFile = File(...), save_original: bool = Form(False)):
    providers.require_external(settings.deepseek_api_key)
    content = await file.read(10*1024*1024+1)
    if len(content) > 10*1024*1024:
        raise AppError('FILE_TOO_LARGE', '图片请控制在10MB以内。', 413)
    try:
        with Image.open(io.BytesIO(content)) as image:
            if image.format not in {'JPEG', 'PNG', 'WEBP'} or image.width*image.height > 30_000_000:
                raise ValueError()
            image.verify()
            extension = {'JPEG': 'jpg', 'PNG': 'png', 'WEBP': 'webp'}[image.format]
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise AppError('INVALID_IMAGE', '请上传有效的 JPG、PNG 或 WebP 图片。', 422) from None
    providers.reserve_call(user.id, 'photo')
    folder = settings.data_dir / 'images'
    folder.mkdir(exist_ok=True, mode=0o700)
    filename = f'{uid()}.{extension}'
    path = folder / filename
    path.write_bytes(content)
    path.chmod(0o600)
    try:
        with core.DB() as db:
            asset = Record(owner=user.id, kind='image', payload={'filename': filename, 'saved': save_original}, expires=now()+timedelta(days=30) if save_original else now()+timedelta(hours=24))
            db.add(asset)
            db.flush()
            # save_original 一并带入任务：worker 会把它写进识别历史，供对话卡片回读真实状态。
            task = Job(owner=user.id, kind='photo',
                       payload={'asset_id': asset.id, 'filename': filename,
                                'save_original': save_original})
            db.add(task)
            db.commit()
            return {'job_id': task.id, 'asset_id': asset.id if save_original else None}
    except Exception:
        path.unlink(missing_ok=True)
        raise


@app.get('/api/v1/jobs/{identifier}')
def get_job(identifier: str, user: Authed):
    with core.DB() as db:
        job = db.get(Job, identifier)
        if not job or job.owner != user.id:
            raise AppError('NOT_FOUND', '找不到这项任务。', 404)
        return {'id': job.id, 'status': job.status, 'result': job.result}


@app.get('/api/v1/images')
def images(user: Authed):
    with core.DB() as db:
        return [{'id': x.id, 'expires': x.expires.isoformat()} for x in db.scalars(select(Record).where(Record.owner == user.id, Record.kind == 'image', Record.expires > now())) if x.payload.get('saved')]


@app.get('/api/v1/images/{identifier}')
def get_image(identifier: str, user: Authed):
    with core.DB() as db:
        asset = owned(db, identifier, user.id, 'image')
        if not asset.payload.get('saved'):
            raise AppError('NOT_FOUND', '原图未保存。', 404)
        path = settings.data_dir / 'images' / asset.payload['filename']
        if not path.is_file():
            raise AppError('NOT_FOUND', '原图已清理。', 404)
        return FileResponse(path, headers={'Cache-Control': 'no-store'})


@app.delete('/api/v1/images/{identifier}')
def delete_image(identifier: str, user: Authed):
    with core.DB() as db:
        asset = owned(db, identifier, user.id, 'image')
        busy = db.scalars(select(Job).where(Job.owner == user.id, Job.kind == 'photo', Job.status.in_(['pending', 'running']))).all()
        if any(j.payload.get('asset_id') == identifier for j in busy):
            raise AppError('ASSET_BUSY', '图片正在处理中，请稍后再删除。', 409)
        (settings.data_dir / 'images' / asset.payload['filename']).unlink(missing_ok=True)
        db.delete(asset)
        db.commit()
    return {'ok': True}


@app.get('/api/v1/map/config')
def map_config(user: Authed):
    """页内地图配置：只下发公开 JS 标识与同源受限代理地址。"""
    js_key = settings.amap_js_key
    service_host = providers.js_service_host()
    ready = providers.map_js_ready()
    notice = ('页内地图需另配高德 Web 端(JS API)的公开 Key、配套安全密钥与代理地址；'
              '服务端 Web 服务 Key 与 securityJsCode 不下发网页。'
              if not ready else
              '地图本地前提已就绪，但真实加载、域名白名单与 HTTPS 仍需单独验证。')
    return {'enabled': bool(js_key and service_host), 'js_ready': ready,
            'verified': bool(ready and settings.amap_js_verified),
            'js_key': js_key if ready else '',
            'service_host': service_host if ready else '',
            'coordinate_system': 'GCJ-02',
            'notice': notice}


@app.get('/api/v1/map/proxy/{path:path}')
def map_proxy(path: str, request: Request, user: Authed):
    """同源受限代理：白名单路径 + 服务端注入 JS Key/jscode，不做任意 URL 转发。

    同时接受官方 `_AMapService` 前缀（与 map/config 下发的 service_host 一致）。
    """
    try:
        normalized, params, callback = normalize_proxy_request(path, request.query_params)
    except MapProxyProtocolError as error:
        raise AppError(error.code, error.message, error.status) from None
    if not providers.map_js_ready():
        raise AppError('MAP_NOT_CONFIGURED', '地图尚未配置，请使用餐厅列表。', 503)
    providers.reserve_call(user.id, 'map_js')
    # JS SDK 代理使用 JS 平台 Key 与配套安全密钥；Web 服务 Key 只用于后端餐厅查询。
    params['key'] = settings.amap_js_key
    params['jscode'] = settings.amap_js_security_code
    data = providers.request_json('GET', f'https://restapi.amap.com/{normalized}', params=params)
    if callback:
        return Response(encode_jsonp(data, callback), media_type='application/javascript',
                        headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})
    return data


@app.get('/api/v1/restaurants')
def restaurants(user: Authed,
                city: str = Query(default='北京', min_length=1, max_length=40),
                location: str | None = Query(default=None, pattern=r'^-?\d{1,3}(\.\d+)?,\s*-?\d{1,2}(\.\d+)?$'),
                keywords: str | None = Query(default=None, max_length=80),
                radius_m: int | None = Query(default=None, ge=250, le=50000),
                page: int | None = Query(default=None), page_size: int | None = Query(default=None)):
    """区域/周边餐厅查询（单页）。分页与查询口径由共用服务统一校验。"""
    return restaurant_service.search(user.id, city=city, location=location,
                                     page=page, page_size=page_size, keywords=keywords, radius_m=radius_m)


def recipe_payload(data: RecipeInput, user):
    if not data.confirmed:
        raise AppError('CONFIRMATION_REQUIRED', '请确认后再保存到个人菜谱。')
    if data.source_url and not providers.public_https(data.source_url):
        raise AppError('INVALID_SOURCE', '来源链接须为公开HTTPS网页。')
    origin = data.origin if data.origin in ('user_original', 'adapted', 'external') else 'user_original'
    return {**data.model_dump(exclude={'confirmed'}), 'origin': origin,
            'review_status': '用户录入，营养说明未审核',
            'evaluation': evaluate(data.ingredients, True, [], user.preferences.get('avoid_coriander', False))}


@app.get('/api/v1/recipes')
def recipes(user: Authed, meal: str = '', tag: str = '', purpose: str = '', diet: str = '',
            cuisine: str = '', q: str = '', page: int = 1, page_size: int = 20):
    """本人菜谱 + 已发布公共菜谱；支持关键词、分页与真实总量。"""
    filters = recipe_filters.normalize_filters(meal, tag, purpose, diet, cuisine)
    result = recipe_query.query_recipes(user.id, filters, user.preferences, q or None)
    items, public_rows = result['personal'], result['public']
    for row in public_rows:
        row['evaluation'] = row.get('evaluation') or {}
    merged = items + public_rows
    page = max(1, min(int(page or 1), 100000))
    page_size = max(1, min(int(page_size or 20), 50))
    start = (page - 1) * page_size
    window = merged[start:start + page_size]
    return {'items': window,
            'personal_count': len(items), 'public_count': len(public_rows),
            'total': len(merged), 'page': page, 'page_size': page_size,
            'has_more': start + len(window) < len(merged),
            'next_page': page + 1 if start + len(window) < len(merged) else None,
            'filters': filters, 'query': q or None,
            'meal_entries': list(recipe_filters.MEALS),
            'tag_entries': list(recipe_filters.TAGS),
            'legacy_notice': '旧记录的 main 保留为「正餐（待补分类）」，不会自动改为午餐或晚餐。',
            'nutrition_reference': {'text': '一般蔬食搭配应包含多样食物；豆类、坚果等可作为搭配的一部分。具体菜谱营养特点需要对应食材及资料支持。', 'source': 'https://www.nhs.uk/live-well/eat-well/how-to-eat-a-balanced-diet/the-vegan-diet/'},
            'public_library_status': '公开审核菜谱条目尚在整理；当前可录入并筛选自己的菜谱。'}


@app.post('/api/v1/recipes', status_code=201)
def create_recipe(data: RecipeInput, user: Authed, request: Request, response: Response):
    """Confirmed personal create, optionally keyed by Idempotency-Key.

    First write: 201; same key/content replay: 200 with replayed=true.
    Different content: 409 IDEMPOTENCY_CONFLICT; deleted result: 410 RECIPE_DELETED.
    No header preserves the original create behavior. Response includes version/ETag.
    """
    payload = recipe_payload(data, user)
    with core.DB() as db:
        r, created = recipe_save.create_personal(db, user.id, payload, request.headers.get('Idempotency-Key'))
        db.commit()
        result = recipe_save.view(r)
    if not created:
        response.status_code = 200
        result['replayed'] = True
    response.headers['ETag'] = f'"{result["version"]}"'
    return result


@app.put('/api/v1/recipes/{identifier}')
def update_recipe(identifier: str, data: RecipeInput, user: Authed, request: Request, response: Response):
    """Update owned personal recipe in place; public entries cannot be changed.

    If-Match accepts the last returned version (bare or ETag-quoted). A stale token
    returns 409 RECIPE_CHANGED without writing. Missing token keeps legacy behavior.
    """
    payload = recipe_payload(data, user)
    expected = recipe_save.expected_version(request.headers.get('If-Match'))
    with core.DB() as db:
        record = recipe_save.update_personal(db, user.id, identifier, payload, expected)
        db.commit()
        result = recipe_save.view(record)
    response.headers['ETag'] = f'"{result["version"]}"'
    return result


@app.delete('/api/v1/recipes/{identifier}')
def delete_recipe(identifier: str, user: Authed):
    with core.DB() as db:
        recipe_save.remove_personal(db, user.id, identifier)
        db.commit()
    return {'ok': True}


@app.post('/api/v1/recipes/search')
def search_recipe(data: RecipeSearch, user: Authed):
    # 库内匹配与筛选同时生效；库内无结果才走联网补位（候选），候选不冒充完整教程。
    filters = recipe_filters.normalize_filters(data.meal, data.tag, data.purpose, data.diet, data.cuisine)
    result = recipe_query.query_recipes(user.id, filters, user.preferences, data.query)
    personal, public = result['personal'], result['public']
    local = result['items']
    page = max(1, min(int(getattr(data, 'page', 1) or 1), 100000))
    page_size = max(1, min(int(getattr(data, 'page_size', 20) or 20), 50))
    start = (page - 1) * page_size
    window = local[start:start + page_size]
    if local:
        return {'local': True, 'items': window, 'filters': filters,
                'total': len(local), 'page': page, 'page_size': page_size,
                'has_more': start + len(window) < len(local),
                'personal_count': len(personal), 'public_count': len(public),
                'public_notice': ('公共条目为来源摘要时不能当作完整教程；保存仍需你自己核对内容。'
                                  if any(not p['tutorial_complete'] for p in public) else None)}
    providers.reserve_call(user.id, 'recipe_search')
    candidates = providers.search_web(f'{data.query} Vegan 食谱')
    result_rows = []
    with core.DB() as db:
        for c in candidates:
            record = Record(owner=user.id, kind='candidate', payload={**c, 'status': '待查看原文并复核配料'}, expires=now()+timedelta(days=7))
            db.add(record)
            db.flush()
            result_rows.append(record_dict(record))
        db.commit()
    return {'local': False, 'items': result_rows, 'filters': filters, 'total': len(result_rows),
            'page': 1, 'page_size': page_size, 'has_more': False,
            'notice': '这些是待核对的候选来源，不是已整理的完整教程；确认后才保存到个人菜谱。'}


@app.post('/api/v1/recipes/candidates/{identifier}/save', status_code=201)
def save_candidate(identifier: str, data: RecipeInput, response: Response, user: Authed):
    """把联网候选保存到个人菜谱：需用户确认内容；同一候选重复确认不产生重复记录。

    首次保存返回 201；重放（同候选已保存）返回 200 与同一记录 id。
    """
    payload = recipe_payload(data, user)
    with core.DB() as db:
        candidate = owned(db, identifier, user.id, 'candidate')
        item, created = recipe_save.save_from_candidate(db, user.id, payload, candidate)
        db.commit()
        result = recipe_save.view(item)
    if not created:
        response.status_code = 200
        result['replayed'] = True
    return result


class AssessInput(BaseModel):
    name: str = Field(default='', max_length=100)
    food_class: Literal['fresh', 'cooked', 'packaged', 'unknown'] = 'unknown'
    storage: Literal['ambient', 'refrigerated', 'frozen', 'unknown'] | None = None
    condition: Literal['raw', 'cut', 'cooked', 'opened', 'unopened', 'unknown'] | None = None
    opened: bool | None = None
    temperature_c: float | None = Field(default=None, ge=-80, le=70)
    location: Literal['indoor', 'outdoor', 'unknown'] | None = None
    date_type: Literal['purchase', 'production', 'prepared', 'opened', 'unknown'] | None = None
    started_at: str | None = None
    package_note: str | None = Field(default=None, max_length=1000)
    expires_at: str | None = None
    deadline_source: Literal['unknown', 'package', 'rule', 'user'] = 'unknown'


class ActionConfirm(BaseModel):
    confirm_key: Literal['user_confirmed']
    idempotency_key: str | None = Field(default=None, max_length=128)
    acknowledged_fields: list[str] = Field(default_factory=list, max_length=12)


@app.get('/api/v1/actions')
def list_actions(user: Authed):
    """本人待确认操作（刷新/换设备后可恢复）。"""
    with core.DB() as db:
        return actions_service.list_open(db, user.id)


@app.post('/api/v1/actions', status_code=201)
def create_action(data: ActionProposal, user: Authed):
    """创建待确认修改：只记录拟改值，不写正式数据。"""
    with core.DB() as db:
        food_service.load(db, data.food_id, user.id)          # 越权/不存在 → 404
        food = food_service.load(db, data.food_id, user.id)
        pending = actions_service.create(
            db, user.id, kind='food_action',
            target={'food_id': data.food_id, 'name': food.payload.get('name')},
            proposed=data.model_dump(exclude={'food_id'}, exclude_none=True),
            base_version=food.payload.get('version'))
    return pending


@app.get('/api/v1/actions/{identifier}')
def get_action(identifier: str, user: Authed):
    with core.DB() as db:
        return actions_service.view(actions_service.load(db, identifier, user.id))


@app.post('/api/v1/actions/{identifier}/confirm')
def confirm_action(identifier: str, data: ActionConfirm, user: Authed):
    """用户确认后执行；版本冲突 409，过期 409，重复确认返回首次结果。"""
    with core.DB() as db:
        record = actions_service.load(db, identifier, user.id)
        return actions_service.confirm(db, record, user.id, data.confirm_key, data.idempotency_key, data.acknowledged_fields)


@app.delete('/api/v1/actions/{identifier}')
def cancel_action(identifier: str, user: Authed):
    with core.DB() as db:
        return actions_service.cancel(db, actions_service.load(db, identifier, user.id))


@app.post('/api/v1/foods/assess')
def assess_food(data: AssessInput, user: Authed):
    """只读试算：不建记录、不发提醒、不调用外部服务。"""
    payload = data.model_dump()
    payload.pop('name', None)
    payload['name'] = data.name
    return {'suggestion': food_advice.assess(payload), 'advice_version': food_advice.ADVICE_VERSION}


@app.get('/api/v1/foods')
def foods(user: Authed):
    with core.DB() as db:
        return {'items': food_service.read_all(db, user.id),
                'notice': '依据包装或用户日期管理；条件化储存参考条目尚待审核，无依据不生成天数。'}


@app.post('/api/v1/foods/storage-guidance')
def preview_storage_guidance(data: storage_guidance.GuidanceInput, user: Authed):
    """Explicit paid search for preview facts; does not save food or change dates."""
    return storage_guidance.generate(user.id, data.facts())


@app.get('/api/v1/foods/{identifier}/storage-guidance')
def read_storage_guidance(identifier: str, user: Authed):
    """Read an account/condition-matched cache only; never a provider request."""
    return storage_guidance.for_food(user.id, identifier)


@app.post('/api/v1/foods/{identifier}/storage-guidance')
def search_storage_guidance(identifier: str, user: Authed):
    return storage_guidance.for_food(user.id, identifier, search=True)


@app.post('/api/v1/foods', status_code=201)
def create_food(data: FoodInput, user: Authed):
    if data.deadline_source == 'rule':
        # 规则期限只能由重评接口按已发布且适用的规则生成。
        raise AppError('INVALID_RULE_BASIS', '规则期限不能直接提交，请重评后由系统生成。', 422)
    with core.DB() as db:
        record = food_service.create(db, user.id, data.model_dump(mode='json'))
        return food_service.view(record)


@app.put('/api/v1/foods/{identifier}')
def update_food(identifier: str, data: FoodInput, user: Authed):
    with core.DB() as db:
        record = food_service.load(db, identifier, user.id)
        record = food_service.update(db, record, data.model_dump(mode='json'),
                                     data.model_fields_set, data.reconfirmed)
        result = food_service.view(record)
        result.update(getattr(record, '_auto_reevaluated', {'auto_reevaluated': False}))
        return result


@app.get('/api/v1/storage/rules')
def storage_rules_catalog(user: Authed):
    """公开可追溯的规则条目与状态；未匹配条目时系统保持未知。"""
    return storage_rules.describe()


@app.post('/api/v1/foods/{identifier}/reevaluate')
def reevaluate_food(identifier: str, data: ReevaluateInput, user: Authed):
    """按新条件与证据重评期限；路由只做鉴权与响应适配。"""
    with core.DB() as db:
        record = food_service.load(db, identifier, user.id)
        return food_service.reevaluate(db, record, data)


@app.delete('/api/v1/foods/{identifier}')
def delete_food(identifier: str, user: Authed):
    with core.DB() as db:
        food_service.remove(db, food_service.load(db, identifier, user.id))
    return {'ok': True}

def chat_session_owned(db, identifier, owner):
    return owned(db, identifier, owner, 'chat_session')


@app.post('/api/v1/chat/sessions', status_code=201)
def create_chat_session(data: ChatSessionCreate, user: Authed):
    with core.DB() as db:
        session = Record(owner=user.id, kind='chat_session',
                         payload={'title': (data.title or '').strip() or '新对话', 'updated': now().isoformat()})
        db.add(session)
        db.commit()
        return {'id': session.id, 'created': session.created.isoformat()}


@app.get('/api/v1/chat/sessions')
def chat_sessions(user: Authed):
    with core.DB() as db:
        sessions = [s for s in db.scalars(select(Record).where(Record.owner == user.id, Record.kind == 'chat_session').order_by(Record.created.desc()))]
        messages = db.scalars(select(Record).where(Record.owner == user.id, Record.kind == 'chat_message')).all()
        items = []
        for s in sessions:
            msgs = [m for m in messages if m.payload.get('session_id') == s.id]
            items.append({'id': s.id, 'created': s.created.isoformat(), 'updated': s.payload.get('updated'),
                          'title': s.payload.get('title'), 'message_count': len(msgs),
                          'last_message_at': max((m.created.isoformat() for m in msgs), default=None)})
        return {'items': items}


@app.delete('/api/v1/chat/sessions/{identifier}')
def delete_chat_session(identifier: str, user: Authed):
    with core.DB() as db:
        chat_lock(db, user.id, identifier)
        chat_session_owned(db, identifier, user.id)
        for m in db.scalars(select(Record).where(Record.owner == user.id, Record.kind == 'chat_message')).all():
            if m.payload.get('session_id') == identifier:
                db.delete(m)
        for j in db.scalars(select(Job).where(Job.owner == user.id, Job.kind == 'chat_turn')).all():
            if j.payload.get('session_id') == identifier:
                # 清除任务中残留的回复正文/卡片副本；活动任务一并取消，防迟到回写。
                j.result = {}
                if j.status in ('queued', 'running'):
                    j.status = 'cancelled'
        db.delete(db.get(Record, identifier))
        db.commit()
    return {'ok': True}


@app.post('/api/v1/chat/sessions/{identifier}/messages', status_code=201)
def chat_message(identifier: str, data: ChatMessageInput, response: Response, user: Authed):
    with core.DB() as db:
        chat_lock(db, user.id, identifier)
        chat_session_owned(db, identifier, user.id)
        providers.require_chat()
        attachments = [a.model_dump() for a in data.attachments]
        for att in data.attachments:
            job = db.get(Job, att.job_id)
            if not job or job.owner != user.id or job.kind != 'photo':
                raise AppError('NOT_FOUND', '找不到关联的配料图片任务。', 404)
        if data.idempotency_key:
            for m in db.scalars(select(Record).where(Record.owner == user.id, Record.kind == 'chat_message')).all():
                if m.payload.get('session_id') == identifier and m.payload.get('idempotency_key') == data.idempotency_key and m.payload.get('role') == 'user':
                    if m.payload.get('text') == data.content and m.payload.get('attachments') == attachments:
                        turn = db.get(Job, m.payload.get('turn_id')) if m.payload.get('turn_id') else None
                        response.status_code = 200
                        if turn and turn.owner == user.id and turn.kind == 'chat_turn':
                            return {'turn_id': turn.id, 'status': turn.status}
                        return {'turn_id': None, 'status': 'succeeded'}
                    raise AppError('IDEMPOTENCY_CONFLICT', '同一幂等键对应内容不一致，请更换幂等键。', 409)
        for j in db.scalars(select(Job).where(Job.owner == user.id, Job.kind == 'chat_turn', Job.status.in_(['queued', 'running']))).all():
            if j.payload.get('session_id') == identifier:
                raise AppError('TURN_ACTIVE', '上一条消息仍在处理中，请稍后再试。', 409)
        seq = 0
        for m in db.scalars(select(Record).where(Record.owner == user.id, Record.kind == 'chat_message')).all():
            if m.payload.get('session_id') == identifier:
                seq = max(seq, m.payload.get('seq', 0))
        msg = Record(owner=user.id, kind='chat_message',
                     payload={'session_id': identifier, 'role': 'user', 'content_type': 'text',
                              'text': data.content, 'cards': None, 'seq': seq + 1,
                              'idempotency_key': data.idempotency_key, 'attachments': attachments},
                     expires=now() + timedelta(days=90))
        db.add(msg)
        db.flush()
        turn = Job(owner=user.id, kind='chat_turn', status='queued',
                   payload={'session_id': identifier, 'user_message_id': msg.id, 'attachments': attachments,
                            'idempotency_key': data.idempotency_key,
                            'budget': {'tool_calls_used': 0,
                                       'deadline_wall': (now() + timedelta(seconds=TURN_BUDGET_SECONDS)).isoformat()}},
                   due=now())
        db.add(turn)
        db.flush()
        msg.payload = {**msg.payload, 'turn_id': turn.id}
        session = db.get(Record, identifier)
        session.payload = {**session.payload, 'updated': now().isoformat()}
        db.commit()
        return {'turn_id': turn.id, 'status': 'queued'}


@app.get('/api/v1/chat/sessions/{identifier}/messages')
def chat_messages(identifier: str, user: Authed):
    with core.DB() as db:
        chat_session_owned(db, identifier, user.id)
        rows = [r for r in db.scalars(select(Record).where(Record.owner == user.id, Record.kind == 'chat_message'))]
        rows = [r for r in rows if r.payload.get('session_id') == identifier]
        rows.sort(key=lambda r: (r.payload.get('seq', 0), r.created.isoformat()))
        items = []
        for r in rows:
            turn_id = r.payload.get('turn_id')
            turn_status = None
            if turn_id:
                turn = db.get(Job, turn_id)
                if turn and turn.owner == user.id and turn.kind == 'chat_turn':
                    turn_status = turn.status
            items.append({'id': r.id, 'role': r.payload.get('role'), 'content_type': r.payload.get('content_type'),
                          'text': r.payload.get('text'), 'cards': r.payload.get('cards'),
                          'created': r.created.isoformat(), 'turn_id': turn_id,
                          'needs_input': r.payload.get('needs_input', False),
                          'input_prompt': r.payload.get('input_prompt', ''),
                          # 旧消息没有这两个字段：places 为 []、guard 为 None（前端按“旧结果”处理）
                          'places': r.payload.get('places') or [],
                          'restaurant_guard': r.payload.get('restaurant_guard'),
                          'citations': r.payload.get('citations') or [],
                          'status': turn_status if turn_id else 'sent'})
        return {'items': items}


@app.get('/api/v1/chat/sessions/{identifier}/turns/{turn_id}')
def chat_turn(identifier: str, turn_id: str, user: Authed):
    with core.DB() as db:
        chat_session_owned(db, identifier, user.id)
        turn = db.get(Job, turn_id)
        if not turn or turn.owner != user.id or turn.kind != 'chat_turn' or turn.payload.get('session_id') != identifier:
            raise AppError('NOT_FOUND', '找不到这项对话任务。', 404)
        return {'id': turn.id, 'status': turn.status,
                'reply': turn.result.get('reply'),
                'error': turn.result.get('error'),
                # 失败时也返回已完成的只读结果卡片；成功时为空数组（兼容旧结构）。
                'cards': turn.result.get('cards') or []}


@app.post('/api/v1/foods/drafts', status_code=201)
def create_food_draft(data: DraftCreate, user: Authed):
    with core.DB() as db:
        record = draft_service.create(db, user.id, data.items, data.chat_turn_id,
                                      data.reference_time, data.timezone)
        return draft_service.view(record)


@app.get('/api/v1/foods/drafts')
def list_food_drafts(user: Authed):
    with core.DB() as db:
        return draft_service.list_drafts(db, user.id)


@app.get('/api/v1/foods/drafts/{identifier}')
def get_food_draft(identifier: str, user: Authed):
    with core.DB() as db:
        return draft_service.view(draft_service.load(db, identifier, user.id))


@app.put('/api/v1/foods/drafts/{identifier}/items/{item_id}')
def update_food_draft_item(identifier: str, item_id: str, data: DraftItemUpdate, user: Authed):
    """更新草稿项：未传=保留，显式 null 或 clear=清空；路由只做适配。"""
    changes = draft_service.clear_targets(data.model_fields_set, data.model_dump(), data.clear)
    with core.DB() as db:
        core.advisory_lock(db, 'draft', user.id, identifier)
        record = draft_service.load(db, identifier, user.id)
        return draft_service.update_item(db, record, item_id, changes)


@app.post('/api/v1/foods/drafts/{identifier}/confirm')
def confirm_food_draft(identifier: str, data: DraftConfirm, user: Authed):
    """确认入库：草稿锁 + 单事务（食材/提醒/草稿状态/幂等）。"""
    with core.DB() as db:
        core.advisory_lock(db, 'draft', user.id, identifier)
        record = draft_service.load(db, identifier, user.id)
        if data.advice_digest:
            record.payload = {**record.payload, 'advice_digest': data.advice_digest}
        return draft_service.confirm(db, record, data)


@app.delete('/api/v1/foods/drafts/{identifier}')
def delete_food_draft(identifier: str, user: Authed):
    with core.DB() as db:
        core.advisory_lock(db, 'draft', user.id, identifier)
        draft_service.remove(db, draft_service.load(db, identifier, user.id))
    return {'ok': True}

class DateParseInput(BaseModel):
    phrase: str = Field(min_length=1, max_length=40)
    reference_time: str | None = None
    timezone: str | None = Field(default=None, max_length=64)


@app.post('/api/v1/dates/parse')
def parse_relative_date(data: DateParseInput, user: Authed):
    """把“今天/昨天/明天下午3点”等解析为明确时刻，供前端与草稿核对。"""
    return dates.parse(data.phrase, data.reference_time, data.timezone)


@app.get('/api/v1/recipes/candidates')
def my_candidates(user: Authed):
    """本人未保存的联网候选（跨设备恢复）。"""
    return chat_reads.recipe_candidates(user.id)


@app.get('/api/v1/recipes/{identifier}')
def get_recipe(identifier: str, user: Authed, response: Response):
    """Owner-only personal detail. Public knowledge remains read-only on its existing API."""
    with core.DB() as db:
        result = recipe_filters.evaluate_recipe(
            recipe_save.view(recipe_save.load_personal(db, user.id, identifier)), user.preferences)
    response.headers['ETag'] = f'"{result["version"]}"'
    return result


@app.get('/api/v1/transcriptions')
def my_transcriptions(user: Authed):
    """本人最近的转写任务（跨设备恢复；仅未过期的）。"""
    with core.DB() as db:
        rows = db.scalars(select(Job).where(Job.owner == user.id, Job.kind == 'transcription')
                          .order_by(Job.created.desc()).limit(10)).all()
        items = [{'job_id': j.id, 'status': j.status, 'created': j.created.isoformat(),
                  'text': (j.result or {}).get('text'),
                  'simulated': bool((j.result or {}).get('simulated')),
                  'expires_at': (j.result or {}).get('expires_at')} for j in rows]
    return {'items': items, 'count': len(items)}


@app.post('/api/v1/transcriptions', status_code=202)
async def create_transcription(user: Authed, file: UploadFile = File(...),
                               duration_seconds: float | None = Form(default=None)):
    """上传录音→异步转写。成功返回可编辑文本草稿，不自动创建聊天消息。"""
    transcription.require()
    content = await file.read(transcription.MAX_BYTES + 1)
    info = transcription.validate_upload(content, (file.content_type or '').split(';')[0].strip(),
                                        duration_seconds)
    providers.reserve_call(user.id, 'transcription')
    folder = settings.data_dir / 'audio'
    folder.mkdir(exist_ok=True, mode=0o700)
    extension = transcription.PROVIDER_FORMAT[info['detected_mime']]
    filename = f'{uid()}.{extension}'
    path = folder / filename
    path.write_bytes(content)
    path.chmod(0o600)
    try:
        with core.DB() as db:
            asset = Record(owner=user.id, kind='audio', payload={'filename': filename},
                           expires=now()+timedelta(hours=24))
            db.add(asset)
            db.flush()
            task = Job(owner=user.id, kind='transcription',
                       payload={'asset_id': asset.id, 'filename': filename,
                                'mime': info['detected_mime'], 'original': file.filename or '',
                                'duration_seconds': info['duration_seconds'],
                                'bytes': info['bytes']},
                       due=now())
            db.add(task)
            db.commit()
            return {'job_id': task.id, 'status': 'pending', 'expires_in_hours': 24,
                    'detected_mime': info['detected_mime'], 'duration_seconds': info['duration_seconds']}
    except Exception:
        path.unlink(missing_ok=True)
        raise


@app.get('/api/v1/transcriptions/{identifier}')
def get_transcription(identifier: str, user: Authed):
    """取回转写文本草稿（可编辑）；未发送的草稿最迟 24 小时清理。"""
    with core.DB() as db:
        task = db.get(Job, identifier)
        if not task or task.owner != user.id or task.kind != 'transcription':
            raise AppError('NOT_FOUND', '找不到这次转写任务。', 404)
        return {'id': task.id, 'status': task.status,
                'text': task.result.get('text'),
                'simulated': bool(task.result.get('simulated')),
                'error': task.result.get('error'),
                'expires_at': task.result.get('expires_at')}


@app.get('/api/v1/capabilities/transcription')
def transcription_capabilities(user: Authed):
    return {**transcription.capabilities(), 'configured': transcription.configured(),
            'mode': 'async_job_editable_text',
            'notice': '转写成功只返回可编辑文字，不自动发送消息；未配置时请改用打字。'}


@app.get('/api/v1/knowledge')
def knowledge_list(user: Authed, type: str = '', q: str = ''):
    """公开知识查询：只返回已发布条目；未发布/空库明确说明。"""
    if type and type not in knowledge.TYPES:
        raise AppError('INVALID_FILTER', f'未知知识类型：{type}', 422)
    data = knowledge.public_items(type or None)
    if q:
        needle = q.casefold()
        data['items'] = [i for i in data['items']
                         if needle in (i['title'] or '').casefold() or needle in (i['body'] or '').casefold()]
        if not data['items']:
            data['state'] = 'empty'
    return data


@app.get('/api/v1/knowledge/status')
def knowledge_status(user: Authed):
    """审核链路状态：各类各状态的条目数量；未发布不计入对外内容。"""
    with core.DB() as db:
        rows = [r.payload for r in db.scalars(select(Record).where(Record.owner == 'system', Record.kind == 'knowledge'))]
    counts = {t: {s: 0 for s in knowledge.STATUSES} for t in knowledge.TYPES}
    for row in rows:
        counts.setdefault(row.get('type'), {s: 0 for s in knowledge.STATUSES})
        counts[row.get('type')][row.get('status', 'draft')] += 1
    published = sum(1 for row in rows if row.get('status') == knowledge.PUBLIC_STATUS)
    return {'counts': counts, 'total': len(rows), 'published': published,
            'state': 'empty' if published == 0 else 'published',
            'notice': ('知识库暂无已发布条目；导入内容默认为待审核，不对外展示。'
                       if published == 0 else '只有已发布条目会出现在查询结果中。')}


@app.get('/api/v1/notifications/config')
def notification_config(user: Authed):
    return {'public_key': settings.vapid_public_key if worker.push_configured() else '',
            'notice': 'iPhone需添加到主屏幕并允许通知；实际送达尚待真机验证。'}


@app.post('/api/v1/notifications/subscriptions')
async def subscribe(request: Request, user: Authed):
    if not worker.push_configured():
        raise AppError('PUSH_NOT_CONFIGURED', '主动通知尚未配置，请先使用应用内列表。', 503)
    data = await request.json()
    try:
        keys = data['keys']
        assert worker.valid_push_endpoint(data['endpoint'])
        assert len(base64.urlsafe_b64decode(keys['p256dh']+'==')) == 65
        assert len(base64.urlsafe_b64decode(keys['auth']+'==')) == 16
        assert len(data['endpoint']) < 3000
        clean = {'endpoint': data['endpoint'], 'keys': {'p256dh': keys['p256dh'], 'auth': keys['auth']}}
    except (KeyError, TypeError, ValueError, AssertionError):
        raise AppError('INVALID_SUBSCRIPTION', '通知订阅格式或地址不正确。', 422) from None
    with core.DB() as db:
        core.advisory_lock(db, 'push_subscription', user.id)
        matches = [sub for sub in db.scalars(select(Record).where(
            Record.kind == 'subscription', Record.owner == user.id))
            if sub.payload.get('endpoint') == clean['endpoint']]
        if matches:
            matches[0].payload = clean
            for duplicate in matches[1:]:
                db.delete(duplicate)
        else:
            db.add(Record(owner=user.id, kind='subscription', payload=clean))
        for job in db.scalars(select(Job).where(Job.owner == user.id, Job.kind == 'reminder', Job.status.in_(['waiting_configuration', 'waiting_subscription']))):
            job.status = 'pending'
            job.attempts = 0
            job.result = {}
        db.commit()
    return {'ok': True}


@app.delete('/api/v1/notifications/subscriptions')
def unsubscribe(user: Authed):
    with core.DB() as db:
        core.advisory_lock(db, 'push_subscription', user.id)
        db.execute(delete(Record).where(Record.owner == user.id, Record.kind == 'subscription'))
        db.commit()
    return {'ok': True}


@app.get('/api/v1/notifications/tasks')
def notification_tasks(user: Authed):
    with core.DB() as db:
        return [{'id': j.id, 'status': j.status, 'due': j.due.isoformat(), 'result': j.result} for j in db.scalars(select(Job).where(Job.owner == user.id, Job.kind == 'reminder').order_by(Job.created.desc()).limit(50))]
