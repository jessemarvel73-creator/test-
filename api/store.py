from aiohttp import web
import asyncio
import csv
import hashlib
import hmac
import io
import secrets
import time
import aiohttp
import json
import html
import re
from urllib.parse import parse_qsl, quote, urlsplit

import config

from database import (
    get_all_games,
    get_categories,
    create_game,
    create_category,
    create_product,
    fetch_all,
    fetch_one,
    get_or_create_user,
    create_pending_order,
    cancel_pending_order,
    get_cart_items,
    add_cart_item,
    update_cart_item,
    remove_cart_item,
    clear_cart,
    create_pending_order_from_cart,
    get_user_orders,
    get_user_order_details,
    hide_user_order,
    get_referral_stats,
    get_referrals,
    add_favorite,
    remove_favorite,
    get_user_favorite_products,
    calculate_promo_discount,
    complete_free_order,
    mark_order_delivered,
    list_promo_codes,
    create_promo_code,
    set_promo_allowed_users,
    get_promo_allowed_users,
    toggle_promo_code,
    get_loyalty_balance,
    get_loyalty_history,
    create_loyalty_redemption,
    cancel_loyalty_redemption,
    create_broadcast,
    get_all_active_telegram_users,
    admin_list_tickets,
    admin_count_tickets,
    admin_delete_ticket,
    admin_get_ticket,
    admin_add_ticket_message,
    admin_update_ticket_status,
    create_ticket,
    get_user_tickets,
    get_ticket_for_user,
    add_ticket_message,
    create_feedback,
    list_feedback,
    list_product_reviews,
    create_product_review,
    vote_review,
    create_notification,
    get_user_notifications,
    get_unread_notification_count,
    mark_notification_read,
    mark_all_notifications_read,
    add_stock_alert,
    remove_stock_alert,
    has_stock_alert,
    cancel_pending_review_payment,
    get_promo_for_purchase,
    get_pool,
    ensure_promo_schema,
    get_referral_settings,
    set_referral_settings,
    begin_order_delivery,
    finish_order_delivery,
) 

ADMIN_SESSION_COOKIE = "cpm_admin_session"
ADMIN_SESSION_MAX_AGE = getattr(config, "ADMIN_SESSION_MAX_AGE_SECONDS", 60 * 60 * 12)
ADMIN_LOGIN_WINDOW_SECONDS = 10 * 60
ADMIN_LOGIN_MAX_FAILURES = getattr(config, "ADMIN_LOGIN_MAX_FAILURES", 5)
ADMIN_LOGIN_LOCK_SECONDS = getattr(config, "ADMIN_LOGIN_LOCK_SECONDS", 15 * 60)

async def _ensure_promo_schema_ready():
    """Run the lightweight Promo schema compatibility migration once per process."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await ensure_promo_schema(conn)

# In-memory limiter is intentionally scoped to this process. Render deployments
# normally run a single instance for this service; failed attempts are still
# throttled before the expensive secret comparison.
_ADMIN_LOGIN_FAILURES: dict[str, list[float]] = {}
_ADMIN_LOGIN_LOCKED_UNTIL: dict[str, float] = {}


def _client_ip(request: web.Request) -> str:
    forwarded = str(request.headers.get("X-Forwarded-For", "")).split(",", 1)[0].strip()
    return forwarded or request.remote or "unknown"


def _admin_login_rate_limited(ip: str) -> bool:
    now = time.time()
    locked_until = _ADMIN_LOGIN_LOCKED_UNTIL.get(ip, 0.0)
    if locked_until > now:
        return True
    if locked_until:
        _ADMIN_LOGIN_LOCKED_UNTIL.pop(ip, None)
    attempts = [t for t in _ADMIN_LOGIN_FAILURES.get(ip, []) if now - t < ADMIN_LOGIN_WINDOW_SECONDS]
    _ADMIN_LOGIN_FAILURES[ip] = attempts
    return len(attempts) >= ADMIN_LOGIN_MAX_FAILURES


def _record_admin_login_failure(ip: str) -> None:
    now = time.time()
    attempts = [t for t in _ADMIN_LOGIN_FAILURES.get(ip, []) if now - t < ADMIN_LOGIN_WINDOW_SECONDS]
    attempts.append(now)
    _ADMIN_LOGIN_FAILURES[ip] = attempts
    if len(attempts) >= ADMIN_LOGIN_MAX_FAILURES:
        _ADMIN_LOGIN_LOCKED_UNTIL[ip] = now + ADMIN_LOGIN_LOCK_SECONDS


def _clear_admin_login_failures(ip: str) -> None:
    _ADMIN_LOGIN_FAILURES.pop(ip, None)
    _ADMIN_LOGIN_LOCKED_UNTIL.pop(ip, None)


@web.middleware
async def admin_security_middleware(request: web.Request, handler):
    # Cookie-authenticated admin mutations must not be accepted from a
    # cross-site browser context. SameSite cookies are helpful, but an
    # explicit Origin/Sec-Fetch-Site check gives the panel a second CSRF line
    # of defence without requiring every legacy admin bundle to be rewritten.
    if (
        request.path.startswith("/api/admin/")
        and request.method in {"POST", "PUT", "PATCH", "DELETE"}
        and request.path != "/api/admin/login"
    ):
        fetch_site = str(request.headers.get("Sec-Fetch-Site", "")).lower().strip()
        origin = str(request.headers.get("Origin", "")).strip()
        referer = str(request.headers.get("Referer", "")).strip()
        source = origin or referer
        cross_site = fetch_site in {"cross-site", "same-origin"} and fetch_site == "cross-site"
        if source:
            parsed = urlsplit(source)
            forwarded_proto = str(request.headers.get("X-Forwarded-Proto", request.scheme)).split(",", 1)[0].strip().lower()
            expected_scheme = forwarded_proto or request.scheme
            if parsed.netloc != request.host or parsed.scheme.lower() != expected_scheme:
                return web.json_response({"ok": False, "error": "Cross-site admin request rejected."}, status=403)
        elif cross_site:
            return web.json_response({"ok": False, "error": "Cross-site admin request rejected."}, status=403)

    response = await handler(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault(
        "Content-Security-Policy-Report-Only",
        "default-src 'self'; script-src 'self' 'unsafe-inline' https://telegram.org https://*.telegram.org; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob: https:; connect-src 'self' https://api.telegram.org; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
    )
    if str(request.headers.get("X-Forwarded-Proto", request.scheme)).split(",", 1)[0].strip().lower() == "https":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if request.path.startswith("/admin") or request.path.startswith("/api/admin"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


def _normalize_media_reference(value):
    """Normalize image references so Admin, API and Mini App use one contract."""
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.startswith("tg:"):
        file_id = raw[3:].strip()
        return f"tg:{file_id}" if file_id else None
    if raw.startswith("/media/telegram/"):
        file_id = raw.split("/media/telegram/", 1)[1].strip()
        return f"tg:{file_id}" if file_id else None
    if raw.lower().startswith(("http://", "https://")):
        return raw
    # Bare Telegram file_id values are accepted by Telegram and are the most
    # common value users paste from a Bot API response.
    if re.fullmatch(r"[A-Za-z0-9_-]{20,}", raw):
        return f"tg:{raw}"
    raise ValueError("Image must be an http(s) URL or a Telegram file_id.")


def create_admin_session():
    timestamp = str(int(time.time()))
    nonce = secrets.token_urlsafe(24)
    payload = f"{timestamp}.{nonce}"
    signature = hmac.new(
        config.ADMIN_KEY.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload}.{signature}"


def is_valid_admin_session(request: web.Request):
    if not config.ADMIN_KEY:
        return False

    session = request.cookies.get(ADMIN_SESSION_COOKIE)
    if not session:
        return False

    try:
        timestamp_text, nonce, signature = session.split(".", 2)
        timestamp = int(timestamp_text)
        if not nonce or len(nonce) < 20:
            return False
    except (ValueError, TypeError):
        return False

    now = int(time.time())

    if timestamp > now + 60:
        return False

    if now - timestamp > ADMIN_SESSION_MAX_AGE:
        return False

    payload = f"{timestamp_text}.{nonce}"
    expected_signature = hmac.new(
        config.ADMIN_KEY.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return secrets.compare_digest(signature, expected_signature)


async def telegram_media_handler(request: web.Request):
    file_id = request.match_info.get("file_id", "").strip()
    if not file_id:
        return web.Response(status=400, text="Missing file_id")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://api.telegram.org/bot{config.BOT_TOKEN}/getFile",
                params={"file_id": file_id},
                timeout=15,
            ) as resp:
                payload = await resp.json(content_type=None)
        if not payload.get("ok") or not payload.get("result", {}).get("file_path"):
            return web.Response(status=404, text="Telegram media not found")
        file_path = payload["result"]["file_path"]
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://api.telegram.org/file/bot{config.BOT_TOKEN}/{file_path}",
                timeout=30,
            ) as resp:
                if resp.status != 200:
                    return web.Response(status=resp.status, text="Media download failed")
                body = await resp.read()
                return web.Response(
                    body=body,
                    content_type=resp.headers.get("Content-Type", "application/octet-stream"),
                    headers={"Cache-Control": "public, max-age=86400"},
                )
    except Exception:
        return web.Response(status=502, text="Media service unavailable")


def admin_required(handler):
    async def wrapper(request: web.Request):
        if not is_valid_admin_session(request):
            return web.json_response(
                {"ok": False, "error": "Admin authentication required"},
                status=401,
            )
        return await handler(request)

    return wrapper


async def admin_login_handler(request: web.Request):
    client_ip = _client_ip(request)
    if _admin_login_rate_limited(client_ip):
        return web.json_response(
            {"ok": False, "error": "Too many failed login attempts. Try again later."},
            status=429,
            headers={"Retry-After": str(ADMIN_LOGIN_LOCK_SECONDS)},
        )
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Invalid JSON"}, status=400)

    admin_key = str(data.get("admin_key", ""))
    if not admin_key:
        return web.json_response({"ok": False, "error": "Admin key is required"}, status=400)

    if not config.ADMIN_KEY:
        return web.json_response(
            {"ok": False, "error": "Admin authentication is not configured"},
            status=500,
        )

    if not secrets.compare_digest(admin_key, config.ADMIN_KEY):
        _record_admin_login_failure(client_ip)
        return web.json_response({"ok": False, "error": "Invalid admin key"}, status=401)

    _clear_admin_login_failures(client_ip)
    response = web.json_response({"ok": True, "message": "Admin login successful"})
    response.set_cookie(
        ADMIN_SESSION_COOKIE,
        create_admin_session(),
        max_age=ADMIN_SESSION_MAX_AGE,
        path="/",
        secure=(
            str(request.headers.get("X-Forwarded-Proto", request.scheme))
            .split(",", 1)[0]
            .strip()
            .lower() == "https"
        ),
        httponly=True,
        samesite="Strict",
    )
    return response


def serialize_row(row):
    if isinstance(row, dict):
        result = {}
        for key, value in row.items():
            if key in {"image", "banner", "first_image"} and isinstance(value, str):
                normalized = value.strip()
                if normalized.startswith("tg:"):
                    file_id = normalized[3:].strip()
                    result[key] = f"/media/telegram/{quote(file_id, safe='')}" if file_id else None
                    continue
                if normalized.startswith("/media/telegram/"):
                    file_id = normalized.split("/media/telegram/", 1)[1].strip()
                    result[key] = f"/media/telegram/{quote(file_id, safe='')}" if file_id else None
                    continue
            result[key] = serialize_row(value)
        return result
    if isinstance(row, (list, tuple)):
        return [serialize_row(value) for value in row]
    if hasattr(row, "isoformat"):
        return row.isoformat()
    return row


async def games_handler(request: web.Request):
    games = await get_all_games()
    return web.json_response({"ok": True, "games": [serialize_row(game) for game in games]})


async def categories_handler(request: web.Request):
    try:
        game_id = int(request.match_info.get("game_id"))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "Invalid game_id"}, status=400)

    categories = await get_categories(game_id)
    return web.json_response({"ok": True, "categories": [serialize_row(x) for x in categories]})


async def products_handler(request: web.Request):
    game_id = request.query.get("game_id")
    category_id = request.query.get("category_id")

    query = """
        SELECT p.*, g.name AS game_name, c.name AS category_name,
               (SELECT pi.image FROM product_images pi WHERE pi.product_id=p.id ORDER BY pi.sort_order ASC,pi.id ASC LIMIT 1) AS first_image,
               (SELECT COALESCE(json_agg(pi.image ORDER BY pi.sort_order ASC,pi.id ASC),'[]'::json) FROM product_images pi WHERE pi.product_id=p.id) AS images_json
        FROM products p
        LEFT JOIN games g ON g.id = p.game_id
        LEFT JOIN categories c ON c.id = p.category_id
        WHERE p.active = TRUE
          AND EXISTS (
              SELECT 1
              FROM inventory_items ii
              WHERE ii.product_id = p.id
                AND ii.status = 'available'
          )
    """
    params = []

    if game_id:
        try:
            game_id = int(game_id)
        except ValueError:
            return web.json_response({"ok": False, "error": "Invalid game_id"}, status=400)
        query += " AND p.game_id = ?"
        params.append(game_id)

    if category_id:
        try:
            category_id = int(category_id)
        except ValueError:
            return web.json_response({"ok": False, "error": "Invalid category_id"}, status=400)
        query += " AND p.category_id = ?"
        params.append(category_id)

    query += " ORDER BY p.featured DESC, p.created_at DESC, p.id DESC"
    products = await fetch_all(query, tuple(params))
    payload=[]
    for x in products:
        row=serialize_row(x)
        raw=row.pop("images_json",[])
        try: vals=json.loads(raw) if isinstance(raw,str) else (raw or [])
        except Exception: vals=[]
        row["images"]=[{"image":v} for v in vals if v]
        payload.append(row)
    return web.json_response({"ok": True, "products": payload})


async def product_handler(request: web.Request):
    try:
        product_id = int(request.match_info.get("product_id"))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "Invalid product_id"}, status=400)

    product = await fetch_one(
        """
        SELECT p.*, g.name AS game_name, c.name AS category_name
        FROM products p
        LEFT JOIN games g ON g.id = p.game_id
        LEFT JOIN categories c ON c.id = p.category_id
        WHERE p.id = ?
          AND p.active = TRUE
          AND EXISTS (
              SELECT 1
              FROM inventory_items ii
              WHERE ii.product_id = p.id
                AND ii.status = 'available'
          )
        """,
        (product_id,),
    )
    if not product:
        return web.json_response({"ok": False, "error": "Product not found"}, status=404)

    images = await fetch_all(
        """
        SELECT * FROM product_images
        WHERE product_id = ?
        ORDER BY sort_order ASC, id ASC
        """,
        (product_id,),
    )

    result = serialize_row(product)
    result["images"] = [serialize_row(x) for x in images]
    return web.json_response({"ok": True, "product": result})


@admin_required
async def admin_media_upload_handler(request: web.Request):
    """Upload admin-selected images to Telegram and return reusable media references."""
    try:
        reader = await request.multipart()
    except Exception:
        return web.json_response({"ok": False, "error": "A multipart/form-data upload is required."}, status=400)

    uploaded = []
    errors = []
    count = 0
    max_files = 10
    max_bytes = 5 * 1024 * 1024

    while True:
        field = await reader.next()
        if field is None:
            break
        if field.name != "files":
            try:
                while await field.read_chunk(size=64 * 1024):
                    pass
            except Exception:
                pass
            continue

        if count >= max_files:
            errors.append("Maximum 10 images per upload request.")
            try:
                while await field.read_chunk(size=64 * 1024):
                    pass
            except Exception:
                pass
            continue

        count += 1
        filename = str(field.filename or "image").strip() or "image"
        content_type = str(field.headers.get("Content-Type", "")).lower()
        if not content_type.startswith("image/"):
            errors.append(f"{filename}: only image files are allowed.")
            try:
                while await field.read_chunk(size=64 * 1024):
                    pass
            except Exception:
                pass
            continue

        chunks = []
        total = 0
        too_large = False
        while True:
            chunk = await field.read_chunk(size=64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                too_large = True
                continue
            chunks.append(chunk)

        if too_large:
            errors.append(f"{filename}: file is larger than 5 MB.")
            continue
        blob = b"".join(chunks)
        if not blob:
            errors.append(f"{filename}: empty file.")
            continue

        form = aiohttp.FormData()
        form.add_field("chat_id", str(config.ADMIN_ID))
        form.add_field("photo", blob, filename=filename, content_type=content_type)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendPhoto",
                    data=form,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as response:
                    result = await response.json()
            if not result.get("ok"):
                raise RuntimeError(result.get("description") or "Telegram rejected the image.")
            sizes = (result.get("result") or {}).get("photo") or []
            if not sizes:
                raise RuntimeError("Telegram did not return a photo file_id.")
            file_id = str(sizes[-1].get("file_id") or "").strip()
            if not file_id:
                raise RuntimeError("Telegram returned an empty file_id.")
            uploaded.append({
                "filename": filename,
                "reference": f"tg:{file_id}",
                "url": f"/media/telegram/{quote(file_id, safe='')}",
            })
        except Exception as error:
            log.exception("Admin media upload failed for %s", filename)
            errors.append(f"{filename}: {error}")

    return web.json_response({
        "ok": bool(uploaded) or not errors,
        "images": uploaded,
        "count": len(uploaded),
        "errors": errors,
    }, status=200 if uploaded or not errors else 400)


@admin_required
async def create_game_handler(request: web.Request):
    try:
        data = await request.json()
        name = str(data.get("name", "")).strip()
        description = str(data.get("description", "")).strip()
        image = _normalize_media_reference(data.get("image", ""))
        active = bool(data.get("active", True))
        sort_order = int(data.get("sort_order", 0) or 0)
        if not name:
            return web.json_response({"ok": False, "error": "Game name is required"}, status=400)
        game_id = await create_game(name, description, image=image, active=active, sort_order=sort_order)
        return web.json_response({"ok": True, "game_id": game_id})
    except ValueError as error:
        return web.json_response({"ok": False, "error": str(error)}, status=400)
    except Exception as error:
        return web.json_response({"ok": False, "error": str(error)}, status=500)


@admin_required
async def create_category_handler(request: web.Request):
    try:
        data = await request.json()
        game_id = int(data.get("game_id", 0))
        name = str(data.get("name", "")).strip()
        description = str(data.get("description", "")).strip()
        image = _normalize_media_reference(data.get("image", ""))
        active = bool(data.get("active", True))
        sort_order = int(data.get("sort_order", 0) or 0)
        if game_id <= 0:
            return web.json_response({"ok": False, "error": "Valid game_id is required"}, status=400)
        if not name:
            return web.json_response({"ok": False, "error": "Category name is required"}, status=400)
        game = await fetch_one("SELECT id FROM games WHERE id = ?", (game_id,))
        if not game:
            return web.json_response({"ok": False, "error": "Selected game does not exist."}, status=400)
        category_id = await create_category(
            game_id, name, description, image=image, active=active, sort_order=sort_order
        )
        return web.json_response({"ok": True, "category_id": category_id})
    except (TypeError, ValueError) as error:
        return web.json_response({"ok": False, "error": str(error)}, status=400)
    except Exception as error:
        return web.json_response({"ok": False, "error": str(error)}, status=500)


@admin_required
async def create_product_handler(request: web.Request):
    try:
        data = await request.json()
        name = str(data.get("name", "")).strip()
        description = str(data.get("description", "")).strip()
        game_id = int(data.get("game_id", 0))
        category_id = int(data.get("category_id", 0))
        price_stars = int(data.get("price_stars", 0))
        # Product stock is derived from available inventory. The admin UI may
        # still send a legacy stock field, but it is deliberately ignored.
        stock = 0
        featured = bool(data.get("featured"))
        active = bool(data.get("active", True))
        discount_percent = int(data.get("discount_percent", 0))
        banner = _normalize_media_reference(data.get("banner", ""))
        images = data.get("images", [])

        if not name:
            return web.json_response({"ok": False, "error": "Product name is required"}, status=400)
        if game_id <= 0:
            return web.json_response({"ok": False, "error": "Valid game_id is required"}, status=400)
        if category_id <= 0:
            return web.json_response({"ok": False, "error": "Valid category_id is required"}, status=400)
        game = await fetch_one("SELECT id FROM games WHERE id = ?", (game_id,))
        if not game:
            return web.json_response({"ok": False, "error": "Selected game does not exist."}, status=400)
        category = await fetch_one("SELECT id, game_id FROM categories WHERE id = ?", (category_id,))
        if not category:
            return web.json_response({"ok": False, "error": "Selected category does not exist."}, status=400)
        if int(category.get("game_id") or 0) != game_id:
            return web.json_response({"ok": False, "error": "Selected category belongs to another game."}, status=400)
        if price_stars <= 0:
            return web.json_response({"ok": False, "error": "Price must be greater than zero"}, status=400)
        if stock < 0:
            stock = 0
        if not 0 <= discount_percent <= 100:
            return web.json_response({"ok": False, "error": "Discount must be between 0 and 100"}, status=400)
        if not isinstance(images, list):
            return web.json_response({"ok": False, "error": "images must be a list"}, status=400)
        if len(images) > 100:
            return web.json_response({"ok": False, "error": "You can add up to 100 image URLs"}, status=400)

        clean_images = []
        seen_images = set()
        for raw_image in images:
            image = str(raw_image or "").strip()
            if not image or image in seen_images:
                continue
            try:
                image = _normalize_media_reference(image)
            except ValueError as error:
                return web.json_response({"ok": False, "error": str(error)}, status=400)
            seen_images.add(image)
            clean_images.append(image)

        product_id = await create_product(
            name=name,
            description=description,
            price_stars=price_stars,
            game_id=game_id,
            category_id=category_id,
            stock=stock,
            featured=1 if featured else 0,
        )

        await fetch_one(
            """
            UPDATE products
            SET active = ?, discount_percent = ?, banner = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            RETURNING id
            """,
            (active, discount_percent, banner, product_id),
        )

        for sort_order, image in enumerate(clean_images):
            await fetch_one(
                """
                INSERT INTO product_images (product_id, image, sort_order)
                VALUES (?, ?, ?)
                RETURNING id
                """,
                (product_id, image, sort_order),
            )

        return web.json_response({
            "ok": True,
            "product_id": product_id,
            "images_added": len(clean_images),
            "available_stock": 0,
            "store_visible": False,
            "message": "Product created. Add inventory before it becomes visible or purchasable in the Store.",
        })
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "Invalid product data"}, status=400)
    except Exception as error:
        return web.json_response({"ok": False, "error": str(error)}, status=500)


@admin_required
async def update_product_handler(request: web.Request):
    try:
        product_id = int(request.match_info.get("product_id"))
        data = await request.json()

        current = await fetch_one("SELECT * FROM products WHERE id = ?", (product_id,))
        if not current:
            return web.json_response({"ok": False, "error": "Product not found"}, status=404)

        name = str(data.get("name", current["name"] or "")).strip()
        description = str(data.get("description", current["description"] or "")).strip()
        game_id = int(data.get("game_id", current["game_id"] or 0))
        category_id = int(data.get("category_id", current["category_id"] or 0))
        price_stars = int(data.get("price_stars", current["price_stars"] or 0))
        # Stock is derived from inventory_items. Ignore any legacy/manual stock
        # value sent by the web admin so inventory remains the single source of truth.
        inventory_count_row = await fetch_one(
            "SELECT COUNT(*) AS count FROM inventory_items WHERE product_id = ? AND status = 'available'",
            (product_id,),
        )
        stock = int((inventory_count_row or {}).get("count") or 0)
        featured = bool(data.get("featured", current["featured"]))
        active = bool(data.get("active", current["active"]))
        discount_percent = int(data.get("discount_percent", current["discount_percent"] or 0))
        raw_banner = data.get("banner", current["banner"] or "")
        try:
            banner = _normalize_media_reference(raw_banner)
        except ValueError as error:
            return web.json_response({"ok": False, "error": str(error)}, status=400)
        images = data.get("images", None)

        if not name:
            return web.json_response({"ok": False, "error": "Product name is required"}, status=400)
        if game_id <= 0 or category_id <= 0:
            return web.json_response({"ok": False, "error": "Valid game and category are required"}, status=400)
        game = await fetch_one("SELECT id FROM games WHERE id = ?", (game_id,))
        if not game:
            return web.json_response({"ok": False, "error": "Selected game does not exist."}, status=400)
        category = await fetch_one("SELECT id, game_id FROM categories WHERE id = ?", (category_id,))
        if not category:
            return web.json_response({"ok": False, "error": "Selected category does not exist."}, status=400)
        if int(category.get("game_id") or 0) != game_id:
            return web.json_response({"ok": False, "error": "Selected category belongs to another game."}, status=400)
        if price_stars <= 0:
            return web.json_response({"ok": False, "error": "Price must be greater than zero"}, status=400)
        if stock < 0:
            return web.json_response({"ok": False, "error": "Stock cannot be negative"}, status=400)
        if not 0 <= discount_percent <= 100:
            return web.json_response({"ok": False, "error": "Discount must be between 0 and 100"}, status=400)

        if images is not None:
            if not isinstance(images, list):
                return web.json_response({"ok": False, "error": "images must be a list"}, status=400)
            if len(images) > 100:
                return web.json_response({"ok": False, "error": "You can add up to 100 image URLs"}, status=400)
            clean_images = []
            seen_images = set()
            for raw_image in images:
                image = str(raw_image or "").strip()
                if not image or image in seen_images:
                    continue
                try:
                    image = _normalize_media_reference(image)
                except ValueError as error:
                    return web.json_response({"ok": False, "error": str(error)}, status=400)
                seen_images.add(image)
                clean_images.append(image)
        else:
            clean_images = None

        row = await fetch_one(
            """
            UPDATE products
            SET name = ?, description = ?, price_stars = ?, game_id = ?, category_id = ?,
                stock = ?, featured = ?, active = ?, discount_percent = ?, banner = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            RETURNING id
            """,
            (
                name, description, price_stars, game_id, category_id,
                stock, featured, active, discount_percent, banner, product_id,
            ),
        )
        if not row:
            return web.json_response({"ok": False, "error": "Product update failed"}, status=500)

        if clean_images is not None:
            await fetch_one("DELETE FROM product_images WHERE product_id = ? RETURNING id", (product_id,))
            for sort_order, image in enumerate(clean_images):
                await fetch_one(
                    """
                    INSERT INTO product_images (product_id, image, sort_order)
                    VALUES (?, ?, ?)
                    RETURNING id
                    """,
                    (product_id, image, sort_order),
                )

        return web.json_response({
            "ok": True,
            "product_id": product_id,
            "images_updated": clean_images is not None,
            "message": "Product updated successfully",
        })
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "Invalid product data"}, status=400)
    except Exception as error:
        return web.json_response({"ok": False, "error": str(error)}, status=500)


@admin_required
async def toggle_product_handler(request: web.Request):
    try:
        product_id = int(request.match_info.get("product_id"))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "Invalid product_id"}, status=400)

    row = await fetch_one(
        """
        UPDATE products
        SET active = NOT active, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        RETURNING id, active
        """,
        (product_id,),
    )
    if not row:
        return web.json_response({"ok": False, "error": "Product not found"}, status=404)

    return web.json_response({"ok": True, "product_id": product_id, "active": bool(row["active"])})


@admin_required
async def admin_me_handler(request: web.Request):
    return web.json_response({"ok": True, "authenticated": True})


async def admin_logout_handler(request: web.Request):
    response = web.json_response({"ok": True})
    response.del_cookie(ADMIN_SESSION_COOKIE, path="/")
    return response
    
    
@admin_required
async def admin_order_detail_handler(request: web.Request):
    try:
        order_id = int(request.match_info.get("order_id"))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "Invalid order_id"}, status=400)

    try:
        order = await fetch_one(
            """
            SELECT
                o.id,
                o.user_id,
                o.status,
                o.total_stars,
                COALESCE((SELECT SUM(oi2.quantity * oi2.price_stars) FROM order_items oi2 WHERE oi2.order_id=o.id), 0) AS subtotal_stars,
                COALESCE(o.promo_discount_stars,0) + COALESCE(o.loyalty_discount_stars,0) + COALESCE(o.referral_discount_stars,0) AS discount_stars,
                COALESCE(o.promo_discount_stars,0) AS promo_discount_stars,
                COALESCE(o.loyalty_discount_stars,0) AS loyalty_discount_stars,
                COALESCE(o.referral_discount_stars,0) AS referral_discount_stars,
                o.promo_code_id,
                o.loyalty_redemption_id,
                o.created_at,
                o.updated_at,
                u.telegram_id,
                u.username,
                u.first_name,
                u.last_name
            FROM orders o
            LEFT JOIN users u ON u.id = o.user_id
            WHERE o.id = ?
            LIMIT 1
            """,
            (order_id,),
        )

        if not order:
            return web.json_response({"ok": False, "error": "Order not found"}, status=404)

        items = await fetch_all(
            """
            SELECT
                oi.id,
                oi.product_id,
                oi.quantity,
                oi.price_stars AS unit_price_stars,
                (oi.quantity * oi.price_stars) AS total_stars,
                p.name AS product_name
            FROM order_items oi
            LEFT JOIN products p ON p.id = oi.product_id
            WHERE oi.order_id = ?
            ORDER BY oi.id ASC
            """,
            (order_id,),
        )

        payments = await fetch_all(
            """
            SELECT
                pay.id,
                pay.amount_stars,
                pay.telegram_payment_charge_id,
                pay.provider_payment_charge_id,
                pay.status,
                pay.created_at
            FROM payments pay
            WHERE pay.order_id = ?
            ORDER BY pay.id DESC
            """,
            (order_id,),
        )

        deliveries = await fetch_all(
            """
            SELECT *
            FROM order_deliveries
            WHERE order_id = ?
            ORDER BY id DESC
            """,
            (order_id,),
        )

        payload = {
            "ok": True,
            "order": serialize_row(order),
            "items": [serialize_row(x) for x in items],
            "payments": [serialize_row(x) for x in payments],
            "deliveries": [serialize_row(x) for x in deliveries],
        }
        return web.json_response(payload)
    except Exception:
        import logging
        logging.getLogger("cpm_shop").exception("Admin order detail failed")
        return web.json_response({"ok": False, "error": "Unable to load order details."}, status=500)


@admin_required
async def admin_orders_handler(request: web.Request):
    rows = await fetch_all(
        """
        SELECT
            o.id,
            o.user_id,
            o.status,
            o.total_stars,
            o.created_at,
            (
                SELECT MIN(pay.created_at)
                FROM payments pay
                WHERE pay.order_id = o.id
                  AND pay.status = 'paid'
            ) AS paid_at,
            u.telegram_id,
            u.username,
            u.first_name,
            p.name AS product_name,
            oi.quantity
        FROM orders o
        LEFT JOIN users u ON u.id = o.user_id
        LEFT JOIN order_items oi ON oi.order_id = o.id
        LEFT JOIN products p ON p.id = oi.product_id
        ORDER BY o.created_at DESC, o.id DESC
        LIMIT 1000
        """
    )

    return web.json_response(
        {
            "ok": True,
            "orders": [serialize_row(x) for x in rows],
        }
    )


@admin_required
async def admin_payments_handler(request: web.Request):
    rows = await fetch_all(
        """
        SELECT
            pay.id,
            pay.order_id,
            pay.user_id,
            pay.amount_stars,
            pay.telegram_payment_charge_id,
            pay.provider_payment_charge_id,
            pay.status,
            pay.created_at,
            u.telegram_id,
            u.username,
            u.first_name
        FROM payments pay
        LEFT JOIN users u ON u.id = pay.user_id
        ORDER BY pay.created_at DESC, pay.id DESC
        LIMIT 1000
        """
    )

    return web.json_response(
        {
            "ok": True,
            "payments": [serialize_row(x) for x in rows],
        }
    )


@admin_required
async def admin_users_handler(request: web.Request):
    rows = await fetch_all(
        """
        SELECT
            id,
            telegram_id,
            username,
            first_name,
            last_name,
            language_code,
            created_at,
            updated_at
        FROM users
        ORDER BY created_at DESC, id DESC
        LIMIT 1000
        """
    )

    return web.json_response(
        {
            "ok": True,
            "users": [serialize_row(x) for x in rows],
        }
    )


@admin_required
async def admin_games_handler(request: web.Request):
    # Admin must be able to manage inactive games too. The public helper only
    # returns active games, which previously made deactivated games disappear.
    games = await fetch_all(
        """
        SELECT *
        FROM games
        ORDER BY sort_order ASC, id ASC
        """
    )

    return web.json_response(
        {
            "ok": True,
            "games": [serialize_row(game) for game in games],
        }
    )


@admin_required
async def admin_store_status_handler(request: web.Request):
    try:
        row = await fetch_one("SELECT key, value FROM settings WHERE key='maintenance_mode' LIMIT 1")
        maintenance = str(row.get("value") if row else "0").strip().lower() in {"1","true","yes","on"}
        return web.json_response({"ok": True, "online": not maintenance, "maintenance_mode": maintenance})
    except Exception:
        log.exception("Failed to read store status")
        return web.json_response({"ok": False, "error": "Unable to load store status."}, status=500)

@admin_required
async def admin_store_status_update_handler(request: web.Request):
    try:
        data = await request.json()
        online = bool(data.get("online", True))
        value = "0" if online else "1"
        row = await fetch_one(
            """INSERT INTO settings(key,value) VALUES('maintenance_mode',?)
               ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value
               RETURNING key,value""",
            (value,),
        )
        return web.json_response({"ok": True, "online": online, "maintenance_mode": not online, "setting": serialize_row(row)})
    except Exception:
        log.exception("Failed to update store status")
        return web.json_response({"ok": False, "error": "Unable to update store status."}, status=500)

@admin_required
async def admin_stats_handler(request: web.Request):
    row = await fetch_one(
        """
        SELECT
            (SELECT COUNT(*) FROM products) AS products,
            (SELECT COUNT(*) FROM games) AS games,
            (SELECT COUNT(*) FROM categories) AS categories,
            (SELECT COUNT(*) FROM users) AS users,
            (SELECT COUNT(*) FROM orders) AS orders,
            COALESCE((SELECT SUM(amount_stars) FROM payments WHERE status='paid'), 0) AS revenue_stars
        """
    )
    recent = await fetch_all(
        """
        SELECT o.id, o.status, o.total_stars, o.created_at,
               u.telegram_id, u.username, u.first_name,
               p.name AS product_name, oi.quantity
        FROM orders o
        LEFT JOIN users u ON u.id = o.user_id
        LEFT JOIN order_items oi ON oi.order_id = o.id
        LEFT JOIN products p ON p.id = oi.product_id
        ORDER BY o.created_at DESC, o.id DESC LIMIT 8
        """
    )
    low = await fetch_all(
        """
        SELECT id, name, stock
        FROM products
        WHERE active = TRUE AND stock <= ?
        ORDER BY stock ASC, id DESC LIMIT 8
        """ ,
        (config.LOW_STOCK_THRESHOLD,),
    )
    return web.json_response(
        {
            "ok": True,
            **serialize_row(row),
            "recent_orders": [serialize_row(x) for x in recent],
            "low_stock": [serialize_row(x) for x in low],
        }
    )


@admin_required
async def admin_analytics_handler(request: web.Request):
    """Return a compact operational dashboard for the admin panel."""
    try:
        days = max(1, min(365, int(request.query.get("days", "30"))))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "days must be an integer."}, status=400)
    summary = await fetch_one(
        """
        SELECT
          COUNT(*) FILTER (WHERE o.created_at >= CURRENT_TIMESTAMP - (? * INTERVAL '1 day')) AS orders,
          COUNT(*) FILTER (WHERE o.status = 'paid' AND o.created_at >= CURRENT_TIMESTAMP - (? * INTERVAL '1 day')) AS paid_orders,
          COALESCE(SUM(p.amount_stars) FILTER (WHERE p.status='paid' AND p.created_at >= CURRENT_TIMESTAMP - (? * INTERVAL '1 day')), 0) AS revenue_stars
        FROM orders o LEFT JOIN payments p ON p.order_id=o.id
        """,
        (days, days, days),
    )
    top_products = await fetch_all(
        """
        SELECT p.id, p.name, COALESCE(SUM(oi.quantity),0) AS units,
               COALESCE(SUM(oi.quantity * oi.price_stars),0) AS sales_stars
        FROM order_items oi
        JOIN orders o ON o.id=oi.order_id AND o.status='paid'
        JOIN products p ON p.id=oi.product_id
        WHERE o.created_at >= CURRENT_TIMESTAMP - (? * INTERVAL '1 day')
        GROUP BY p.id, p.name ORDER BY units DESC, sales_stars DESC LIMIT 10
        """,
        (days,),
    )
    daily = await fetch_all(
        """
        SELECT DATE(o.created_at) AS day,
               COUNT(*) FILTER (WHERE o.status='paid') AS paid_orders,
               COALESCE(SUM(p.amount_stars) FILTER (WHERE p.status='paid'),0) AS revenue_stars
        FROM orders o LEFT JOIN payments p ON p.order_id=o.id
        WHERE o.created_at >= CURRENT_TIMESTAMP - (? * INTERVAL '1 day')
        GROUP BY DATE(o.created_at) ORDER BY day ASC
        """,
        (days,),
    )
    return web.json_response({
        "ok": True, "days": days, "summary": serialize_row(summary or {}),
        "top_products": [serialize_row(row) for row in top_products],
        "daily": [serialize_row(row) for row in daily],
    })


@admin_required
async def admin_inventory_export_handler(request: web.Request):
    """Export inventory metadata as CSV without exposing sold item payloads."""
    status = str(request.query.get("status", "available")).strip().lower()
    if status not in {"available", "sold", "all"}:
        return web.json_response({"ok": False, "error": "Invalid inventory status."}, status=400)
    where = "" if status == "all" else "WHERE ii.status = ?"
    params = () if status == "all" else (status,)
    rows = await fetch_all(
        f"""
        SELECT ii.id, ii.product_id, p.name AS product_name, ii.status,
               ii.created_at, ii.sold_at,
               CASE WHEN ii.item_data IS NULL OR ii.item_data='' THEN 0 ELSE 1 END AS has_payload
        FROM inventory_items ii JOIN products p ON p.id=ii.product_id
        {where} ORDER BY ii.product_id ASC, ii.id ASC LIMIT 50000
        """,
        params,
    )
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["inventory_id", "product_id", "product_name", "status", "created_at", "sold_at", "has_payload"])
    for row in rows:
        writer.writerow([row.get("id"), row.get("product_id"), row.get("product_name"), row.get("status"), row.get("created_at"), row.get("sold_at"), row.get("has_payload")])
    response = web.Response(text=output.getvalue(), content_type="text/csv", charset="utf-8")
    response.headers["Content-Disposition"] = f'attachment; filename="inventory-{status}.csv"'
    response.headers["Cache-Control"] = "no-store"
    return response


@admin_required
async def admin_categories_handler(request: web.Request):
    rows = await fetch_all(
        """
        SELECT c.*, g.name AS game_name
        FROM categories c
        LEFT JOIN games g ON g.id = c.game_id
        ORDER BY c.sort_order ASC, c.id ASC
        """
    )
    return web.json_response({"ok": True, "categories": [serialize_row(x) for x in rows]})



@admin_required
async def admin_update_game_handler(request: web.Request):
    try:
        game_id = int(request.match_info.get("game_id"))
        data = await request.json()
        name = str(data.get("name", "")).strip()
        description = str(data.get("description", "")).strip()
        image = _normalize_media_reference(data.get("image", ""))
        active = bool(data.get("active", True))
        sort_order = int(data.get("sort_order", 0) or 0)
        if not name:
            return web.json_response({"ok": False, "error": "Game name is required."}, status=400)
        row = await fetch_one("UPDATE games SET name=?, description=?, image=?, active=?, sort_order=? WHERE id=? RETURNING *",
                              (name, description, image, active, sort_order, game_id))
        if not row:
            return web.json_response({"ok": False, "error": "Game not found."}, status=404)
        return web.json_response({"ok": True, "game": serialize_row(row)})
    except (TypeError, ValueError) as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    except Exception:
        log.exception("Failed to update game")
        return web.json_response({"ok": False, "error": "Unable to update game."}, status=500)

@admin_required
async def admin_delete_game_handler(request: web.Request):
    try:
        game_id = int(request.match_info.get("game_id"))
        game = await fetch_one("SELECT id, name FROM games WHERE id=?", (game_id,))
        if not game:
            return web.json_response({"ok": False, "error": "Game not found."}, status=404)
        products = await fetch_one("SELECT COUNT(*) AS count FROM products WHERE game_id=?", (game_id,))
        categories = await fetch_one("SELECT COUNT(*) AS count FROM categories WHERE game_id=?", (game_id,))
        deps = {"products": int(products["count"] or 0) if products else 0, "categories": int(categories["count"] or 0) if categories else 0}
        blockers = [f"{k}: {v}" for k,v in deps.items() if v]
        if blockers:
            return web.json_response({"ok": False, "error": "Cannot delete this game because it is still referenced by " + ", ".join(blockers) + ". Deactivate it instead.", "dependencies": deps}, status=409)
        row = await fetch_one("DELETE FROM games WHERE id=? RETURNING id", (game_id,))
        if not row:
            return web.json_response({"ok": False, "error": "Game was not deleted."}, status=409)
        return web.json_response({"ok": True, "deleted_id": int(row["id"])})
    except (TypeError, ValueError) as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    except Exception:
        log.exception("Failed to delete game")
        return web.json_response({"ok": False, "error": "Unable to delete game."}, status=500)

@admin_required
async def admin_toggle_game_handler(request: web.Request):
    try:
        game_id = int(request.match_info.get("game_id"))
        row = await fetch_one("UPDATE games SET active=NOT active WHERE id=? RETURNING *", (game_id,))
        if not row:
            return web.json_response({"ok": False, "error": "Game not found."}, status=404)
        return web.json_response({"ok": True, "game": serialize_row(row)})
    except (TypeError, ValueError) as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    except Exception:
        log.exception("Failed to toggle game")
        return web.json_response({"ok": False, "error": "Unable to update game."}, status=500)

@admin_required
async def admin_update_category_handler(request: web.Request):
    try:
        category_id = int(request.match_info.get("category_id"))
        data = await request.json()
        game_id = int(data.get("game_id"))
        name = str(data.get("name", "")).strip()
        description = str(data.get("description", "")).strip()
        image = _normalize_media_reference(data.get("image", ""))
        active = bool(data.get("active", True))
        sort_order = int(data.get("sort_order", 0) or 0)
        if game_id <= 0 or not name:
            return web.json_response({"ok": False, "error": "Valid game and category name are required."}, status=400)
        game = await fetch_one("SELECT id FROM games WHERE id = ?", (game_id,))
        if not game:
            return web.json_response({"ok": False, "error": "Selected game does not exist."}, status=400)
        row = await fetch_one("UPDATE categories SET game_id=?, name=?, description=?, image=?, active=?, sort_order=? WHERE id=? RETURNING *",
                              (game_id, name, description, image, active, sort_order, category_id))
        if not row:
            return web.json_response({"ok": False, "error": "Category not found."}, status=404)
        return web.json_response({"ok": True, "category": serialize_row(row)})
    except (TypeError, ValueError) as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    except Exception:
        log.exception("Failed to update category")
        return web.json_response({"ok": False, "error": "Unable to update category."}, status=500)

@admin_required
async def admin_delete_category_handler(request: web.Request):
    try:
        category_id = int(request.match_info.get("category_id"))
        category = await fetch_one("SELECT id, name FROM categories WHERE id=?", (category_id,))
        if not category:
            return web.json_response({"ok": False, "error": "Category not found."}, status=404)
        products = await fetch_one("SELECT COUNT(*) AS count FROM products WHERE category_id=?", (category_id,))
        deps = {"products": int(products["count"] or 0) if products else 0}
        if deps["products"]:
            return web.json_response({"ok": False, "error": f"Cannot delete this category because it is used by {deps['products']} product(s). Deactivate or move the products first.", "dependencies": deps}, status=409)
        row = await fetch_one("DELETE FROM categories WHERE id=? RETURNING id", (category_id,))
        if not row:
            return web.json_response({"ok": False, "error": "Category was not deleted."}, status=409)
        return web.json_response({"ok": True, "deleted_id": int(row["id"])})
    except (TypeError, ValueError) as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    except Exception:
        log.exception("Failed to delete category")
        return web.json_response({"ok": False, "error": "Unable to delete category."}, status=500)

@admin_required
async def admin_toggle_category_handler(request: web.Request):
    try:
        category_id = int(request.match_info.get("category_id"))
        row = await fetch_one("UPDATE categories SET active=NOT active WHERE id=? RETURNING *", (category_id,))
        if not row:
            return web.json_response({"ok": False, "error": "Category not found."}, status=404)
        return web.json_response({"ok": True, "category": serialize_row(row)})
    except (TypeError, ValueError) as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    except Exception:
        log.exception("Failed to toggle category")
        return web.json_response({"ok": False, "error": "Unable to update category."}, status=500)

@admin_required
async def admin_products_handler(request: web.Request):
    game_id = request.query.get("game_id")
    category_id = request.query.get("category_id")
    query = """
        SELECT
            p.*,
            g.name AS game_name,
            c.name AS category_name,
            (SELECT pi.image FROM product_images pi WHERE pi.product_id=p.id ORDER BY pi.sort_order ASC, pi.id ASC LIMIT 1) AS first_image,
            (SELECT COUNT(*) FROM product_images pi WHERE pi.product_id = p.id) AS image_count,
            (SELECT COUNT(*) FROM inventory_items ii WHERE ii.product_id = p.id AND ii.status = 'available') AS available_inventory
        FROM products p
        LEFT JOIN games g ON g.id = p.game_id
        LEFT JOIN categories c ON c.id = p.category_id
        WHERE TRUE
    """
    params = []

    if game_id:
        try:
            game_id = int(game_id)
        except ValueError:
            return web.json_response({"ok": False, "error": "Invalid game_id"}, status=400)
        query += " AND p.game_id = ?"
        params.append(game_id)

    if category_id:
        try:
            category_id = int(category_id)
        except ValueError:
            return web.json_response({"ok": False, "error": "Invalid category_id"}, status=400)
        query += " AND p.category_id = ?"
        params.append(category_id)

    query += " ORDER BY p.featured DESC, p.created_at DESC, p.id DESC"
    rows = await fetch_all(query, tuple(params))
    return web.json_response({"ok": True, "products": [serialize_row(x) for x in rows]})


@admin_required
async def admin_product_detail_handler(request: web.Request):
    try:
        product_id = int(request.match_info.get("product_id"))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "Invalid product_id"}, status=400)

    product = await fetch_one(
        """
        SELECT
            p.*,
            g.name AS game_name,
            c.name AS category_name,
            (SELECT COUNT(*) FROM product_images pi WHERE pi.product_id = p.id) AS image_count,
            (SELECT COUNT(*) FROM inventory_items ii WHERE ii.product_id = p.id AND ii.status = 'available') AS available_inventory,
            (SELECT COUNT(*) FROM inventory_items ii WHERE ii.product_id = p.id) AS inventory_count,
            (SELECT COUNT(*) FROM order_items oi WHERE oi.product_id = p.id) AS order_item_count,
            (SELECT COUNT(*) FROM cart_items ci WHERE ci.product_id = p.id) AS cart_item_count,
            (SELECT COUNT(*) FROM favorites f WHERE f.product_id = p.id) AS favorite_count,
            (SELECT COUNT(*) FROM reviews r WHERE r.product_id = p.id) AS review_count
        FROM products p
        LEFT JOIN games g ON g.id = p.game_id
        LEFT JOIN categories c ON c.id = p.category_id
        WHERE p.id = ?
        """,
        (product_id,),
    )
    if not product:
        return web.json_response({"ok": False, "error": "Product not found"}, status=404)

    images = await fetch_all(
        """
        SELECT * FROM product_images
        WHERE product_id = ?
        ORDER BY sort_order ASC, id ASC
        """,
        (product_id,),
    )

    result = serialize_row(product)
    result["images"] = [serialize_row(x) for x in images]
    result["dependencies"] = {
        "orders": int(product["order_item_count"] or 0),
        "inventory": int(product["inventory_count"] or 0),
        "cart_items": int(product["cart_item_count"] or 0),
        "favorites": int(product["favorite_count"] or 0),
        "reviews": int(product["review_count"] or 0),
    }
    return web.json_response({"ok": True, "product": result})


@admin_required
async def delete_product_handler(request: web.Request):
    try:
        product_id = int(request.match_info.get("product_id"))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "Invalid product_id"}, status=400)

    product = await fetch_one(
        """
        SELECT
            id,
            name,
            active,
            (SELECT COUNT(*) FROM inventory_items ii WHERE ii.product_id = products.id AND ii.status = 'available') AS available_inventory
        FROM products
        WHERE id = ?
        """,
        (product_id,),
    )
    if not product:
        return web.json_response({"ok": False, "error": "Product not found"}, status=404)

    active = bool(product["active"])
    available_inventory = int(product["available_inventory"] or 0)
    if active:
        return web.json_response({
            "ok": False,
            "error": "Deactivate the product before deleting it permanently.",
            "reason": "active",
            "available_inventory": available_inventory,
        }, status=409)
    if available_inventory > 0:
        return web.json_response({
            "ok": False,
            "error": f"This product still has {available_inventory} available inventory item(s). Remove or sell them before deleting the product.",
            "reason": "available_inventory",
            "available_inventory": available_inventory,
        }, status=409)

    try:
        # Product relations use ON DELETE CASCADE/SET NULL where appropriate.
        # This lets admins permanently clean up old inactive products while
        # keeping order/payment records intact at the database level.
        deleted = await fetch_one(
            "DELETE FROM products WHERE id = ? AND active = FALSE RETURNING id",
            (product_id,),
        )
    except Exception as error:
        log.exception("Failed to permanently delete product")
        return web.json_response({"ok": False, "error": "Unable to delete this product."}, status=500)

    if not deleted:
        return web.json_response({"ok": False, "error": "Product was not deleted."}, status=409)

    return web.json_response({
        "ok": True,
        "product_id": product_id,
        "message": "Product permanently deleted successfully.",
    })


@admin_required
async def sync_product_stock_handler(request: web.Request):
    try:
        product_id = int(request.match_info.get("product_id"))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "Invalid product_id"}, status=400)

    product = await fetch_one("SELECT id FROM products WHERE id = ?", (product_id,))
    if not product:
        return web.json_response({"ok": False, "error": "Product not found"}, status=404)

    count = await fetch_one(
        "SELECT COUNT(*) AS count FROM inventory_items WHERE product_id = ? AND status = 'available'",
        (product_id,),
    )
    available = int(count["count"] or 0) if count else 0

    await fetch_one(
        "UPDATE products SET stock = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? RETURNING id",
        (available, product_id),
    )

    return web.json_response({
        "ok": True,
        "product_id": product_id,
        "available_inventory": available,
        "stock": available,
        "message": "Product stock synchronized successfully",
    })


@admin_required
async def admin_inventory_handler(request: web.Request):
    """List inventory with optional product/status/search filters and counts."""
    product_id_text = request.query.get("product_id", "").strip()
    status = request.query.get("status", "").strip().lower()
    search = request.query.get("search", "").strip()

    if status and status not in {"available", "sold"}:
        return web.json_response(
            {"ok": False, "error": "Invalid inventory status"},
            status=400,
        )

    product_id = None
    if product_id_text:
        try:
            product_id = int(product_id_text)
        except ValueError:
            return web.json_response(
                {"ok": False, "error": "Invalid product_id"},
                status=400,
            )
        if product_id <= 0:
            return web.json_response(
                {"ok": False, "error": "Invalid product_id"},
                status=400,
            )

    conditions = ["TRUE"]
    params = []

    if product_id is not None:
        conditions.append("i.product_id = ?")
        params.append(product_id)

    if status:
        conditions.append("i.status = ?")
        params.append(status)

    if search:
        like = f"%{search}%"
        conditions.append(
            "(CAST(i.id AS TEXT) ILIKE ? OR i.item_data ILIKE ? OR p.name ILIKE ?)"
        )
        params.extend([like, like, like])

    where_sql = " AND ".join(conditions)

    rows = await fetch_all(
        f"""
        SELECT
            i.id,
            i.product_id,
            p.name AS product_name,
            i.item_data,
            i.status,
            i.created_at,
            i.sold_at
        FROM inventory_items i
        JOIN products p ON p.id = i.product_id
        WHERE {where_sql}
        ORDER BY i.created_at DESC, i.id DESC
        LIMIT 1000
        """,
        tuple(params),
    )

    counts = await fetch_one(
        f"""
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE i.status = 'available') AS available,
            COUNT(*) FILTER (WHERE i.status = 'sold') AS sold
        FROM inventory_items i
        JOIN products p ON p.id = i.product_id
        WHERE {where_sql}
        """,
        tuple(params),
    )

    return web.json_response(
        {
            "ok": True,
            "inventory": [serialize_row(x) for x in rows],
            "counts": {
                "total": int(counts["total"] or 0) if counts else 0,
                "available": int(counts["available"] or 0) if counts else 0,
                "sold": int(counts["sold"] or 0) if counts else 0,
            },
        }
    )


def _parse_inventory_preview_input(raw_text, mode="legacy"):
    """Parse legacy one-item-per-line input or numbered `1a:` account blocks."""
    text = str(raw_text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    mode = str(mode or "legacy").strip().lower()
    if mode != "marker":
        records = []
        for line in text.split("\n") if text else []:
            value = line.strip()
            if value:
                records.append({"marker": "", "item_data": value})
        return records, False, ""

    marker_re = re.compile(r"^\s*(\d+)\s*a\s*:\s*(.*)$", re.IGNORECASE)
    records = []
    current_marker = None
    current_lines = []
    parser_error = False
    parser_message = ""

    def flush():
        nonlocal current_marker, current_lines
        if current_marker is None:
            return
        value = "\n".join(current_lines).strip()
        records.append({"marker": current_marker, "item_data": value})
        current_marker = None
        current_lines = []

    for line in text.split("\n") if text else []:
        match = marker_re.match(line)
        if match:
            flush()
            current_marker = f"{match.group(1)}a:"
            first = match.group(2).strip()
            if first:
                current_lines.append(first)
            continue
        if current_marker is None:
            if line.strip():
                parser_error = True
                parser_message = "Every non-empty line must start with a marker such as 1a:, 2a:, 3a:."
            continue
        current_lines.append(line)

    flush()
    if not records and not parser_error:
        parser_error = True
        parser_message = "No inventory records were found."
    return records, parser_error, parser_message


@admin_required
async def admin_inventory_preview_handler(request: web.Request):
    """Preview inventory parsing and duplicates without writing anything."""
    try:
        data = await request.json()
        product_id = int(data.get("product_id", 0))
        if product_id <= 0:
            return web.json_response({"ok": False, "error": "Valid product_id is required"}, status=400)

        mode = str(data.get("mode") or "legacy").strip().lower()
        if mode not in {"legacy", "marker"}:
            mode = "legacy"

        if isinstance(data.get("items"), list):
            source_items = [str(value or "").strip() for value in data.get("items") if str(value or "").strip()]
            records = [{"marker": "", "item_data": value} for value in source_items]
            parser_error = False
            parser_message = ""
        else:
            records, parser_error, parser_message = _parse_inventory_preview_input(data.get("bulk_text", ""), mode)

        if len(records) > 500:
            return web.json_response({"ok": False, "error": "You can preview a maximum of 500 items at once"}, status=400)

        product = await fetch_one("SELECT id, name FROM products WHERE id = ?", (product_id,))
        if not product:
            return web.json_response({"ok": False, "error": "Product not found"}, status=404)

        candidates = []
        seen = set()
        for record in records:
            value = str(record.get("item_data") or "").strip()
            if value and value not in seen:
                seen.add(value)
                candidates.append(value)

        existing_values = set()
        if candidates:
            pool = await get_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT item_data FROM inventory_items WHERE product_id=$1 AND item_data = ANY($2::TEXT[])",
                    product_id,
                    candidates,
                )
                existing_values = {str(row["item_data"]) for row in rows}

        seen_values = set()
        output = []
        ready_items = []
        duplicate_count = 0
        existing_count = 0
        invalid_count = 0

        for index, record in enumerate(records, start=1):
            value = str(record.get("item_data") or "").strip()
            marker = str(record.get("marker") or "")
            item = {"index": index, "marker": marker, "item_data": value, "status": "ready", "reason": "Ready to import."}
            if not value:
                item["status"] = "invalid"
                item["reason"] = "Inventory item is empty."
                invalid_count += 1
            elif value in seen_values:
                item["status"] = "duplicate"
                item["reason"] = "Duplicate in this import request."
                duplicate_count += 1
            elif value in existing_values:
                item["status"] = "existing"
                item["reason"] = "This inventory item already exists for the selected product."
                existing_count += 1
            else:
                seen_values.add(value)
                ready_items.append(value)
            output.append(item)

        total_count = len(output)
        valid_count = total_count - invalid_count
        return web.json_response({
            "ok": True,
            "product_id": product_id,
            "product_name": product["name"],
            "mode": mode,
            "parser_error": parser_error,
            "parser_message": parser_message,
            "total_count": total_count,
            "valid_count": valid_count,
            "invalid_count": invalid_count,
            "duplicate_count": duplicate_count,
            "existing_count": existing_count,
            "ready_count": len(ready_items),
            "records": output,
            "ready_items": ready_items,
        })
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "Invalid inventory preview data"}, status=400)
    except Exception:
        import logging
        logging.getLogger("cpm_shop").exception("Admin inventory preview failed.")
        return web.json_response({"ok": False, "error": "Unable to preview inventory."}, status=500)


@admin_required
async def admin_add_inventory_handler(request: web.Request):
    """Add one or many inventory items. Duplicates are skipped."""
    try:
        data = await request.json()

        product_id = int(data.get("product_id", 0))
        if product_id <= 0:
            return web.json_response(
                {"ok": False, "error": "Valid product_id is required"},
                status=400,
            )

        single_item = str(data.get("item_data", "") or "").strip()
        raw_items = data.get("items")
        bulk_text = data.get("bulk_text")
        mode = str(data.get("mode") or "legacy").strip().lower()

        if bulk_text is not None:
            if mode not in {"legacy", "marker"}:
                mode = "legacy"
            records, parser_error, parser_message = _parse_inventory_preview_input(bulk_text, mode)
            if parser_error:
                return web.json_response(
                    {"ok": False, "error": parser_message or "Invalid inventory marker format"},
                    status=400,
                )
            items = [
                str(record.get("item_data") or "").strip()
                for record in records
                if str(record.get("item_data") or "").strip()
            ]
        elif raw_items is None:
            items = [single_item] if single_item else []
        else:
            # Accept both the structured list used by the Mini App and
            # newline/comma-separated bulk paste used by the admin panel.
            if isinstance(raw_items, str):
                raw_items = raw_items.replace("\r", "\n").replace(",", "\n").splitlines()
            if not isinstance(raw_items, list):
                return web.json_response(
                    {"ok": False, "error": "items must be a list or bulk text"},
                    status=400,
                )
            items = [
                str(value or "").strip()
                for value in raw_items
                if str(value or "").strip()
            ]

        # Preserve order while removing duplicates from the same request.
        unique_items = []
        seen = set()
        for item in items:
            if item in seen:
                continue
            seen.add(item)
            unique_items.append(item)

        items = unique_items

        if not items:
            return web.json_response(
                {"ok": False, "error": "At least one inventory item is required"},
                status=400,
            )

        if len(items) > 500:
            return web.json_response(
                {"ok": False, "error": "You can add a maximum of 500 items at once"},
                status=400,
            )

        product = await fetch_one(
            "SELECT id, name FROM products WHERE id = ?",
            (product_id,),
        )
        if not product:
            return web.json_response(
                {"ok": False, "error": "Product not found"},
                status=404,
            )

        inserted = 0
        skipped_existing = 0

        for item in items:
            existing = await fetch_one(
                """
                SELECT id
                FROM inventory_items
                WHERE product_id = ? AND item_data = ?
                LIMIT 1
                """,
                (product_id, item),
            )
            if existing:
                skipped_existing += 1
                continue

            await fetch_one(
                """
                INSERT INTO inventory_items (product_id, item_data, status)
                VALUES (?, ?, 'available')
                RETURNING id
                """,
                (product_id, item),
            )
            inserted += 1

        count = await fetch_one(
            """
            SELECT COUNT(*) AS n
            FROM inventory_items
            WHERE product_id = ? AND status = 'available'
            """,
            (product_id,),
        )
        available_stock = int(count["n"] or 0) if count else 0

        await fetch_one(
            """
            UPDATE products
            SET stock = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            RETURNING id
            """,
            (available_stock, product_id),
        )

        return web.json_response(
            {
                "ok": True,
                "product_id": product_id,
                "product_name": product["name"],
                "inserted": inserted,
                "skipped_existing": skipped_existing,
                "available_stock": available_stock,
                "message": f"{inserted} inventory item(s) added successfully",
            }
        )

    except (TypeError, ValueError):
        return web.json_response(
            {"ok": False, "error": "Invalid inventory data"},
            status=400,
        )
    except Exception as error:
        import logging
        logging.getLogger("cpm_shop").exception("Admin inventory insert failed.")
        return web.json_response(
            {"ok": False, "error": str(error)},
            status=500,
        )


async def _resync_product_stock(product_id: int):
    count = await fetch_one(
        """
        SELECT COUNT(*) AS n
        FROM inventory_items
        WHERE product_id = ? AND status = 'available'
        """,
        (product_id,),
    )
    available = int(count["n"] or 0) if count else 0
    await fetch_one(
        """
        UPDATE products
        SET stock = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        RETURNING id
        """,
        (available, product_id),
    )
    return available


@admin_required
async def admin_delete_inventory_handler(request: web.Request):
    """Delete only available inventory. Sold rows remain for audit/history."""
    try:
        inventory_id = int(request.match_info.get("inventory_id"))
    except (TypeError, ValueError):
        return web.json_response(
            {"ok": False, "error": "Invalid inventory_id"},
            status=400,
        )

    if inventory_id <= 0:
        return web.json_response(
            {"ok": False, "error": "Invalid inventory_id"},
            status=400,
        )

    item = await fetch_one(
        "SELECT id, product_id, status FROM inventory_items WHERE id = ?",
        (inventory_id,),
    )
    if not item:
        return web.json_response(
            {"ok": False, "error": "Inventory item not found"},
            status=404,
        )

    if item["status"] != "available":
        return web.json_response(
            {
                "ok": False,
                "error": "Sold inventory items are kept for history and cannot be deleted",
            },
            status=409,
        )

    deleted = await fetch_one(
        "DELETE FROM inventory_items WHERE id = ? AND status = 'available' RETURNING id",
        (inventory_id,),
    )
    if not deleted:
        return web.json_response(
            {"ok": False, "error": "Inventory item was not deleted"},
            status=409,
        )

    stock = await _resync_product_stock(int(item["product_id"]))

    return web.json_response(
        {
            "ok": True,
            "inventory_id": inventory_id,
            "product_id": int(item["product_id"]),
            "available_stock": stock,
        }
    )


@admin_required
async def admin_inventory_status_handler(request: web.Request):
    try:
        inventory_id = int(request.match_info.get("inventory_id"))
        data = await request.json()
        new_status = str(data.get("status", "")).strip().lower()
    except (TypeError, ValueError):
        return web.json_response(
            {"ok": False, "error": "Invalid inventory data"},
            status=400,
        )

    if inventory_id <= 0:
        return web.json_response(
            {"ok": False, "error": "Invalid inventory_id"},
            status=400,
        )

    if new_status not in {"available", "sold"}:
        return web.json_response(
            {"ok": False, "error": "Status must be available or sold"},
            status=400,
        )

    item = await fetch_one(
        "SELECT id, product_id, status FROM inventory_items WHERE id = ?",
        (inventory_id,),
    )
    if not item:
        return web.json_response(
            {"ok": False, "error": "Inventory item not found"},
            status=404,
        )

    if item["status"] == new_status:
        stock = await _resync_product_stock(int(item["product_id"]))
        return web.json_response(
            {
                "ok": True,
                "inventory_id": inventory_id,
                "status": new_status,
                "available_stock": stock,
            }
        )

    if item["status"] == "sold" and new_status == "available":
        return web.json_response(
            {
                "ok": False,
                "error": "Sold inventory items cannot be returned to available status. Add a new inventory item instead.",
            },
            status=409,
        )

    if new_status == "sold":
        await fetch_one(
            """
            UPDATE inventory_items
            SET status = 'sold', sold_at = CURRENT_TIMESTAMP
            WHERE id = ?
            RETURNING id
            """,
            (inventory_id,),
        )
    else:
        await fetch_one(
            """
            UPDATE inventory_items
            SET status = 'available', sold_at = NULL
            WHERE id = ?
            RETURNING id
            """,
            (inventory_id,),
        )

    stock = await _resync_product_stock(int(item["product_id"]))

    return web.json_response(
        {
            "ok": True,
            "inventory_id": inventory_id,
            "product_id": int(item["product_id"]),
            "status": new_status,
            "available_stock": stock,
        }
    )


@admin_required
async def admin_bulk_delete_inventory_handler(request: web.Request):
    try:
        data = await request.json()
        raw_ids = data.get("ids", [])
        if not isinstance(raw_ids, list):
            return web.json_response(
                {"ok": False, "error": "ids must be a list"},
                status=400,
            )

        ids = []
        seen = set()
        for raw_id in raw_ids:
            try:
                inventory_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if inventory_id <= 0 or inventory_id in seen:
                continue
            seen.add(inventory_id)
            ids.append(inventory_id)

        if not ids:
            return web.json_response(
                {"ok": False, "error": "Select at least one inventory item"},
                status=400,
            )

        if len(ids) > 500:
            return web.json_response(
                {"ok": False, "error": "You can delete a maximum of 500 items at once"},
                status=400,
            )

        deleted = 0
        skipped_sold = 0
        not_found = 0
        affected_products = set()

        for inventory_id in ids:
            item = await fetch_one(
                "SELECT id, product_id, status FROM inventory_items WHERE id = ?",
                (inventory_id,),
            )
            if not item:
                not_found += 1
                continue

            if item["status"] != "available":
                skipped_sold += 1
                continue

            row = await fetch_one(
                "DELETE FROM inventory_items WHERE id = ? AND status = 'available' RETURNING id",
                (inventory_id,),
            )
            if row:
                deleted += 1
                affected_products.add(int(item["product_id"]))

        stocks = {}
        for product_id in affected_products:
            stocks[str(product_id)] = await _resync_product_stock(product_id)

        return web.json_response(
            {
                "ok": True,
                "deleted": deleted,
                "skipped_sold": skipped_sold,
                "not_found": not_found,
                "affected_products": sorted(affected_products),
                "stocks": stocks,
            }
        )
    except Exception as error:
        import logging
        logging.getLogger("cpm_shop").exception("Admin bulk inventory delete failed.")
        return web.json_response(
            {"ok": False, "error": str(error)},
            status=500,
        )


def validate_telegram_init_data(init_data: str):
    if not init_data:
        raise ValueError("Telegram authentication data is missing.")

    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError as error:
        raise ValueError("Invalid Telegram authentication data.") from error

    data = dict(pairs)
    received_hash = data.pop("hash", None)
    if not received_hash:
        raise ValueError("Telegram authentication hash is missing.")

    auth_date_text = data.get("auth_date")
    if not auth_date_text:
        raise ValueError("Telegram authentication date is missing.")

    try:
        auth_date = int(auth_date_text)
    except ValueError as error:
        raise ValueError("Invalid Telegram authentication date.") from error

    now = int(time.time())
    if auth_date > now + 60:
        raise ValueError("Telegram authentication data is from the future.")
    if now - auth_date > 86400:
        raise ValueError("Telegram authentication data has expired.")

    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
    secret_key = hmac.new(b"WebAppData", config.BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

    if not secrets.compare_digest(calculated_hash, received_hash):
        raise ValueError("Invalid Telegram authentication data.")

    user_json = data.get("user")
    if not user_json:
        raise ValueError("Telegram user data is missing.")

    try:
        user = json.loads(user_json)
    except json.JSONDecodeError as error:
        raise ValueError("Invalid Telegram user data.") from error

    telegram_id = user.get("id")
    if not telegram_id:
        raise ValueError("Telegram user ID is missing.")

    return {
        "telegram_id": int(telegram_id),
        "username": user.get("username"),
        "first_name": user.get("first_name"),
        "last_name": user.get("last_name"),
        "language_code": user.get("language_code"),
    }


async def create_invoice_link(title, description, payload, price_stars):
    if int(price_stars) <= 0:
        raise ValueError("Invalid price.")

    url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/createInvoiceLink"
    data = {
        "title": str(title)[:32],
        "description": str(description)[:255],
        "payload": str(payload)[:128],
        "currency": "XTR",
        "prices": json.dumps([{"label": str(title)[:64], "amount": int(price_stars)}]),
    }

    try:
        async with aiohttp.ClientSession() as session:
            # Telegram accepts form-encoded requests for Bot API methods.
            # Using form data here keeps `prices` as the JSON-serialized
            # LabeledPrice array required by createInvoiceLink and avoids
            # ambiguity around nested JSON values on the upstream API.
            async with session.post(url, data=data, timeout=aiohttp.ClientTimeout(total=15)) as response:
                raw_body = await response.text()
                try:
                    result = json.loads(raw_body)
                except Exception as exc:
                    raise RuntimeError(
                        f"Telegram invoice API returned HTTP {response.status} with a non-JSON response: {raw_body[:300]}"
                    ) from exc
                if not result.get("ok"):
                    description_text = str(result.get("description") or "Telegram invoice creation failed.")
                    raise RuntimeError(
                        f"Telegram invoice API error (HTTP {response.status}): {description_text}"
                    )
                invoice = result.get("result")
                if not invoice:
                    raise RuntimeError("Telegram invoice API returned no invoice URL.")
                return invoice
    except asyncio.TimeoutError as exc:
        raise RuntimeError("Telegram invoice API timed out after 15 seconds.") from exc


async def _webapp_user(request: web.Request):
    body = {}
    try:
        if request.can_read_body and request.content_type == "application/json":
            body = await request.json()
            if not isinstance(body, dict):
                body = {}
    except Exception:
        body = {}
    init_data = str(
        body.get("init_data")
        or request.headers.get("X-Telegram-Init-Data", "")
        or request.query.get("init_data", "")
    ).strip()
    if not init_data:
        raise ValueError("Telegram session is required.")
    user = validate_telegram_init_data(init_data)
    await get_or_create_user(
        telegram_id=user["telegram_id"], username=user.get("username"),
        first_name=user.get("first_name"), last_name=user.get("last_name"),
        language_code=user.get("language_code"),
    )
    return user, body


async def _telegram_bot_username():
    token = getattr(config, "BOT_TOKEN", "")
    if not token:
        return ""
    cached = getattr(config, "_CPM_BOT_USERNAME", "")
    if cached:
        return cached
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://api.telegram.org/bot{token}/getMe",
                timeout=aiohttp.ClientTimeout(total=8),
            ) as response:
                data = await response.json()
        username = ((data.get("result") or {}).get("username") or "").strip()
        if username:
            setattr(config, "_CPM_BOT_USERNAME", username)
        return username
    except Exception:
        return ""


async def referral_handler(request: web.Request):
    """Return referral data without letting one optional field break the page.

    Referral history is supplemental. A missing/legacy referral table or a
    transient Telegram username lookup must not turn the whole Mini App page
    into a 500 error.
    """
    try:
        user, _ = await _webapp_user(request)
        telegram_id = int(user["telegram_id"])

        stats = {
            "referral_count": 0,
            "reward_total_percent": 0,
            "active_discount_percent": 0,
        }
        rows = []
        try:
            stats = await get_referral_stats(telegram_id) or stats
        except Exception:
            import logging
            logging.getLogger("cpm_shop").exception(
                "Referral stats unavailable | user=%s", telegram_id
            )
        try:
            rows = await get_referrals(telegram_id) or []
        except Exception:
            import logging
            logging.getLogger("cpm_shop").exception(
                "Referral history unavailable | user=%s", telegram_id
            )

        try:
            settings = await get_referral_settings()
        except Exception:
            settings = {"per_user": 1, "max_discount": 70, "days": 7}

        username = await _telegram_bot_username()
        link = (
            f"https://t.me/{username}?start=ref_{telegram_id}"
            if username
            else ""
        )
        return web.json_response({
            "ok": True,
            "referral_link": link,
            "referral_code": f"ref_{telegram_id}",
            "referral_count": int(stats.get("referral_count") or 0),
            "reward_total_percent": int(stats.get("reward_total_percent") or 0),
            "active_discount_percent": int(stats.get("active_discount_percent") or 0),
            "per_referral_percent": int(settings.get("per_user") or 0),
            "max_discount_percent": int(settings.get("max_discount") or 0),
            "discount_days": int(settings.get("days") or 0),
            "referrals": [serialize_row(x) for x in rows],
        })
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    except Exception as e:
        import logging
        logging.getLogger("cpm_shop").exception("Referral load failed")
        return web.json_response({"ok": False, "error": str(e) or "Unable to load referral data."}, status=500)


async def favorites_handler(request: web.Request):
    try:
        user, _ = await _webapp_user(request)
        rows = await get_user_favorite_products(user["telegram_id"])
        return web.json_response({
            "ok": True,
            "favorites": [serialize_row(x) for x in rows],
            "favorite_ids": [int(x["id"]) for x in rows],
        })
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    except Exception:
        import logging
        logging.getLogger("cpm_shop").exception("Favorites load failed")
        return web.json_response({"ok": False, "error": "Unable to load favorites."}, status=500)


async def favorite_add_handler(request: web.Request):
    try:
        user, body = await _webapp_user(request)
        product_id = int(body.get("product_id", 0))
        await add_favorite(user["telegram_id"], product_id)
        rows = await get_user_favorite_products(user["telegram_id"])
        return web.json_response({
            "ok": True,
            "favorite": True,
            "favorites": [serialize_row(x) for x in rows],
            "favorite_ids": [int(x["id"]) for x in rows],
        })
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    except Exception:
        import logging
        logging.getLogger("cpm_shop").exception("Favorite add failed")
        return web.json_response({"ok": False, "error": "Unable to save favorite."}, status=500)


async def favorite_remove_handler(request: web.Request):
    try:
        user, body = await _webapp_user(request)
        product_id = int(body.get("product_id", 0))
        await remove_favorite(user["telegram_id"], product_id)
        rows = await get_user_favorite_products(user["telegram_id"])
        return web.json_response({
            "ok": True,
            "favorite": False,
            "favorites": [serialize_row(x) for x in rows],
            "favorite_ids": [int(x["id"]) for x in rows],
        })
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    except Exception:
        import logging
        logging.getLogger("cpm_shop").exception("Favorite remove failed")
        return web.json_response({"ok": False, "error": "Unable to remove favorite."}, status=500)


async def loyalty_handler(request: web.Request):
    try:
        user, _ = await _webapp_user(request)
        balance = await get_loyalty_balance(user["telegram_id"])
        history = await get_loyalty_history(user["telegram_id"])
        return web.json_response({
            "ok": True,
            "balance": int(balance.get("balance") or 0),
            "earned": int(balance.get("earned") or 0),
            "spent": int(balance.get("spent") or 0),
            "reserved": int(balance.get("reserved") or 0),
            "available": int(balance.get("available") or 0),
            "level": balance.get("level") or "Bronze",
            "next_level_points": int(balance.get("next_level_points") or 0),
            "level_progress": int(balance.get("level_progress") or 0),
            "points_per_star": 1,
            "history": [serialize_row(x) for x in history],
        })
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    except Exception:
        import logging
        logging.getLogger("cpm_shop").exception("Loyalty load failed")
        return web.json_response({"ok": False, "error": "Unable to load rewards."}, status=500)


async def loyalty_redeem_handler(request: web.Request):
    try:
        user, body = await _webapp_user(request)
        points = int(body.get("points", 0) or 0)
        redemption = await create_loyalty_redemption(user["telegram_id"], points)
        return web.json_response({
            "ok": True,
            "redemption": serialize_row(redemption),
            "message": f"{points} points reserved for {int(redemption['discount_stars'])} Stars discount.",
        })
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    except Exception:
        import logging
        logging.getLogger("cpm_shop").exception("Loyalty redemption failed")
        return web.json_response({"ok": False, "error": "Unable to redeem reward points."}, status=500)


async def loyalty_redeem_cancel_handler(request: web.Request):
    try:
        user, body = await _webapp_user(request)
        redemption_id = int(body.get("redemption_id", 0) or 0)
        result = await cancel_loyalty_redemption(user["telegram_id"], redemption_id)
        if not result:
            return web.json_response({"ok": False, "error": "Reward redemption is not active."}, status=400)
        return web.json_response({"ok": True, "redemption": result})
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    except Exception:
        import logging
        logging.getLogger("cpm_shop").exception("Loyalty redemption cancellation failed")
        return web.json_response({"ok": False, "error": "Unable to cancel reward redemption."}, status=500)


async def orders_handler(request: web.Request):
    try:
        user, _ = await _webapp_user(request)
        rows = await get_user_orders(user["telegram_id"])
        return web.json_response({
            "ok": True,
            "orders": [serialize_row(x) for x in rows],
        })
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    except Exception:
        import logging
        logging.getLogger("cpm_shop").exception("Orders list failed")
        return web.json_response({"ok": False, "error": "Unable to load orders."}, status=500)


async def order_details_handler(request: web.Request):
    try:
        order_id = int(request.match_info.get("order_id"))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "Invalid order_id"}, status=400)

    try:
        user, _ = await _webapp_user(request)
        order = await get_user_order_details(order_id, user["telegram_id"])
        if not order:
            return web.json_response({"ok": False, "error": "Order not found."}, status=404)
        return web.json_response({"ok": True, "order": serialize_row(order)})
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    except Exception:
        import logging
        logging.getLogger("cpm_shop").exception("Order details failed")
        return web.json_response({"ok": False, "error": "Unable to load order."}, status=500)


async def delete_my_order_handler(request: web.Request):
    try:
        order_id = int(request.match_info.get("order_id"))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "Invalid order_id"}, status=400)

    try:
        user, _ = await _webapp_user(request)
        hidden = await hide_user_order(order_id, user["telegram_id"])
        if not hidden:
            return web.json_response({"ok": False, "error": "Order not found."}, status=404)
        return web.json_response({"ok": True, "deleted": True, "order_id": order_id})
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    except Exception:
        import logging
        logging.getLogger("cpm_shop").exception("User order deletion failed")
        return web.json_response({"ok": False, "error": "Unable to delete order."}, status=500)


async def cancel_order_handler(request: web.Request):
    try:
        order_id = int(request.match_info.get("order_id"))
        user, _ = await _webapp_user(request)
        cancelled = await cancel_pending_order(order_id, user["telegram_id"])
        if not cancelled:
            order = await fetch_one(
                """
                SELECT o.status
                FROM orders o
                JOIN users u ON u.id=o.user_id
                WHERE o.id=? AND u.telegram_id=?
                """,
                (order_id, user["telegram_id"]),
            )
            if not order:
                return web.json_response({"ok": False, "error": "Order not found."}, status=404)
            if str(order.get("status")) == "cancelled":
                return web.json_response({"ok": True, "cancelled": True, "already_cancelled": True})
            return web.json_response({"ok": False, "error": "Only pending orders can be cancelled."}, status=409)
        return web.json_response({"ok": True, "cancelled": True})
    except (TypeError, ValueError) as error:
        return web.json_response({"ok": False, "error": str(error)}, status=400)
    except Exception:
        log.exception("Order cancellation failed")
        return web.json_response({"ok": False, "error": "Unable to cancel order."}, status=500)


async def cart_get_handler(request: web.Request):
    try:
        user, _ = await _webapp_user(request)
        items = await get_cart_items(user["telegram_id"])
        total = sum(int(x["final_price_stars"] or 0) * int(x["quantity"] or 0) for x in items)
        return web.json_response({"ok": True, "items": [serialize_row(x) for x in items], "total_stars": total})
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    except Exception:
        import logging; logging.getLogger("cpm_shop").exception("Cart get failed")
        return web.json_response({"ok": False, "error": "Unable to load cart."}, status=500)


async def cart_add_handler(request: web.Request):
    try:
        user, body = await _webapp_user(request)
        items = await add_cart_item(user["telegram_id"], int(body.get("product_id", 0)), int(body.get("quantity", 1)))
        total = sum(int(x["final_price_stars"] or 0) * int(x["quantity"] or 0) for x in items)
        return web.json_response({"ok": True, "items": [serialize_row(x) for x in items], "total_stars": total})
    except ValueError as e: return web.json_response({"ok": False, "error": str(e)}, status=400)
    except Exception:
        import logging; logging.getLogger("cpm_shop").exception("Cart add failed")
        return web.json_response({"ok": False, "error": "Unable to add item to cart."}, status=500)


async def cart_update_handler(request: web.Request):
    try:
        user, body = await _webapp_user(request)
        items = await update_cart_item(user["telegram_id"], int(body.get("product_id", 0)), int(body.get("quantity", 0)))
        total = sum(int(x["final_price_stars"] or 0) * int(x["quantity"] or 0) for x in items)
        return web.json_response({"ok": True, "items": [serialize_row(x) for x in items], "total_stars": total})
    except ValueError as e: return web.json_response({"ok": False, "error": str(e)}, status=400)
    except Exception:
        import logging; logging.getLogger("cpm_shop").exception("Cart update failed")
        return web.json_response({"ok": False, "error": "Unable to update cart."}, status=500)


async def cart_remove_handler(request: web.Request):
    try:
        user, body = await _webapp_user(request)
        items = await remove_cart_item(user["telegram_id"], int(body.get("product_id", 0)))
        total = sum(int(x["final_price_stars"] or 0) * int(x["quantity"] or 0) for x in items)
        return web.json_response({"ok": True, "items": [serialize_row(x) for x in items], "total_stars": total})
    except ValueError as e: return web.json_response({"ok": False, "error": str(e)}, status=400)
    except Exception:
        import logging; logging.getLogger("cpm_shop").exception("Cart remove failed")
        return web.json_response({"ok": False, "error": "Unable to remove item."}, status=500)


async def _send_telegram_text(telegram_id, text, reply_markup=None):
    token = getattr(config, "BOT_TOKEN", "")
    if not token: return False
    try:
        async with aiohttp.ClientSession() as session:
            payload={"chat_id":int(telegram_id),"text":str(text),"parse_mode":"HTML"}
            if reply_markup: payload["reply_markup"]=reply_markup
            async with session.post(f"https://api.telegram.org/bot{token}/sendMessage",json=payload,timeout=10) as r:
                data=await r.json()
                return bool(data.get("ok"))
    except Exception:
        return False

async def notifications_handler(request: web.Request):
    try:
        user,_=await _webapp_user(request)
        rows=await get_user_notifications(user["telegram_id"])
        unread=await get_unread_notification_count(user["telegram_id"])
        return web.json_response({"ok":True,"notifications":[serialize_row(x) for x in rows],"unread_count":int(unread)})
    except ValueError as e: return web.json_response({"ok":False,"error":str(e)},status=400)
    except Exception:
        import logging; logging.getLogger("cpm_shop").exception("Notifications failed")
        return web.json_response({"ok":False,"error":"Unable to load notifications."},status=500)

async def notification_read_handler(request: web.Request):
    try:
        user,_=await _webapp_user(request); nid=int(request.match_info["notification_id"])
        row=await mark_notification_read(nid,user["telegram_id"])
        if not row: return web.json_response({"ok":False,"error":"Notification not found."},status=404)
        return web.json_response({"ok":True,"notification":serialize_row(row)})
    except ValueError as e: return web.json_response({"ok":False,"error":str(e)},status=400)

async def notifications_read_all_handler(request: web.Request):
    try:
        user,_=await _webapp_user(request); count=await mark_all_notifications_read(user["telegram_id"])
        return web.json_response({"ok":True,"updated":int(count)})
    except ValueError as e: return web.json_response({"ok":False,"error":str(e)},status=400)

async def stock_alert_add_handler(request: web.Request):
    try:
        user,_=await _webapp_user(request)
        pid=int(request.match_info['product_id'])
        await add_stock_alert(user['telegram_id'], pid)
        return web.json_response({'ok':True,'alert':True})
    except ValueError as e:
        message = str(e)
        status = 409 if message in {
            "This product is already in stock.",
            "This product is currently unavailable.",
        } else 400
        return web.json_response({'ok':False,'error':message},status=status)

async def stock_alert_remove_handler(request: web.Request):
    try:
        user,_=await _webapp_user(request); pid=int(request.match_info['product_id']); await remove_stock_alert(user['telegram_id'],pid); return web.json_response({'ok':True,'alert':False})
    except ValueError as e:return web.json_response({'ok':False,'error':str(e)},status=400)

async def stock_alert_status_handler(request: web.Request):
    try:
        user,_=await _webapp_user(request); pid=int(request.match_info['product_id']); return web.json_response({'ok':True,'alert':await has_stock_alert(user['telegram_id'],pid)})
    except ValueError as e:return web.json_response({'ok':False,'error':str(e)},status=400)

async def product_reviews_handler(request: web.Request):
    try:
        pid=int(request.match_info["product_id"]); rows=await list_product_reviews(pid)
        return web.json_response({"ok":True,"reviews":[serialize_row(x) for x in rows]})
    except (ValueError,TypeError): return web.json_response({"ok":False,"error":"Invalid product_id"},status=400)

async def product_review_payment_cancel_handler(request: web.Request):
    try:
        user, _ = await _webapp_user(request)
        review_id = int(request.match_info["review_id"])
        row = await cancel_pending_review_payment(review_id, user["telegram_id"])
        if not row:
            current = await fetch_one("SELECT payment_status FROM reviews WHERE id=?", (review_id,))
            if current and str(current.get("payment_status")) == "cancelled":
                return web.json_response({"ok": True, "cancelled": True, "already_cancelled": True})
            return web.json_response({"ok": False, "error": "Review payment is no longer pending."}, status=409)
        return web.json_response({"ok": True, "cancelled": True, "review": serialize_row(row)})
    except ValueError as error:
        return web.json_response({"ok": False, "error": str(error)}, status=400)
    except Exception:
        log.exception("Paid review cancellation failed")
        return web.json_response({"ok": False, "error": "Unable to cancel the review payment."}, status=500)


async def product_review_create_handler(request: web.Request):
    try:
        user,body=await _webapp_user(request); pid=int(request.match_info["product_id"])
        rating=max(1,min(5,int(body.get("rating",5)))); text=str(body.get("text","")).strip()
        if len(text)<2: return web.json_response({"ok":False,"error":"Review text is required."},status=400)
        product = await fetch_one("SELECT id, name, active FROM products WHERE id=?", (pid,))
        if not product or not product.get("active"):
            return web.json_response({"ok":False,"error":"Product not found or unavailable."},status=404)
        kind=str(body.get("review_type","free")).lower(); price=int(body.get("price_stars",0) or 0)
        if kind=='paid':
            configured = await fetch_one("SELECT value FROM settings WHERE key='paid_review_price_stars' LIMIT 1")
            try:
                configured_price = int(configured.get("value") or 10) if configured else 10
            except (TypeError, ValueError):
                configured_price = 10
            price = max(1, configured_price)
        status='pending_payment' if kind=='paid' else 'free'
        if kind=='paid':
            row=await create_product_review(user["telegram_id"],pid,rating,text,'paid',price,'pending_payment')
            invoice=await create_invoice_link(title="Paid Review",description=f"Featured review for product #{pid}",payload=f"review:{row['id']}",price_stars=price)
            return web.json_response({"ok":True,"review":serialize_row(row),"invoice_url":invoice,"requires_payment":True})
        row=await create_product_review(user["telegram_id"],pid,rating,text,'free',0,'free')
        return web.json_response({"ok":True,"review":serialize_row(row)})
    except ValueError as e: return web.json_response({"ok":False,"error":str(e)},status=400)
    except Exception:
        import logging; logging.getLogger("cpm_shop").exception("Review create failed")
        return web.json_response({"ok":False,"error":"Unable to submit review."},status=500)

async def review_vote_handler(request: web.Request):
    try:
        user,body=await _webapp_user(request); rid=int(request.match_info["review_id"]); vote=int(body.get("vote",1) or 1)
        row=await vote_review(user["telegram_id"],rid,vote)
        return web.json_response({"ok":True,"vote":serialize_row(row)})
    except ValueError as e: return web.json_response({"ok":False,"error":str(e)},status=400)

async def feedback_create_handler(request: web.Request):
    try:
        user,body=await _webapp_user(request); text=str(body.get("message","")).strip(); rating=int(body.get("rating",5) or 5)
        if len(text)<2: return web.json_response({"ok":False,"error":"Feedback message is required."},status=400)
        row=await create_feedback(user["telegram_id"],text,rating)
        return web.json_response({"ok":True,"feedback":serialize_row(row)})
    except ValueError as e: return web.json_response({"ok":False,"error":str(e)},status=400)

async def giveaways_handler(request: web.Request):
    rows=await fetch_all("""SELECT g.*, COUNT(DISTINCT e.id) AS entry_count
                           FROM giveaways g LEFT JOIN giveaway_entries e ON e.giveaway_id=g.id
                           WHERE g.status IN ('active','running') GROUP BY g.id
                           ORDER BY g.created_at DESC,g.id DESC LIMIT 30""")
    return web.json_response({"ok":True,"giveaways":[serialize_row(x) for x in rows]})

async def giveaway_join_handler(request: web.Request):
    try:
        user,_=await _webapp_user(request); gid=int(request.match_info["giveaway_id"])
        g=await fetch_one("SELECT id,status FROM giveaways WHERE id=?",(gid,))
        if not g or str(g.get('status')) not in {'active','running'}: return web.json_response({"ok":False,"error":"Giveaway is not active."},status=400)
        u=await get_or_create_user(user["telegram_id"]); row=await fetch_one("INSERT INTO giveaway_entries(giveaway_id,user_id) VALUES(?,?) ON CONFLICT DO NOTHING RETURNING id",(gid,u['id']))
        return web.json_response({"ok":True,"joined":bool(row),"message":"You are entered in the giveaway." if row else "You are already entered."})
    except ValueError as e: return web.json_response({"ok":False,"error":str(e)},status=400)

@admin_required
async def feedback_admin_handler(request: web.Request):
    rows = await list_feedback()
    return web.json_response({"ok": True, "feedback": [serialize_row(x) for x in rows]})

async def promo_validate_handler(request: web.Request):
    try:
        await _ensure_promo_schema_ready()
        user, _ = await _webapp_user(request)
        body = await request.json()
        code = str(body.get("code", "")).strip()
        subtotal = int(body.get("subtotal_stars", 0))
        result = await calculate_promo_discount(
            telegram_id=user["telegram_id"],
            code=code,
            subtotal_stars=subtotal,
        )
        if not result.get("valid"):
            return web.json_response(result, status=400)
        return web.json_response({"ok": True, "promo": result})
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    except Exception:
        import logging
        logging.getLogger("cpm_shop").exception("Promo validation failed")
        return web.json_response({"ok": False, "error": "Unable to validate promo code."}, status=500)


@admin_required
async def admin_promo_codes_handler(request: web.Request):
    try:
        await _ensure_promo_schema_ready()
        rows = await fetch_all(
            """SELECT p.*,
                        COALESCE((SELECT SUM(pr.quantity) FROM promo_redemptions pr WHERE pr.promo_code_id=p.id), 0) AS redemption_count,
                        u.telegram_id AS assigned_telegram_id,
                        (SELECT COUNT(*) FROM promo_allowed_users pau WHERE pau.promo_code_id=p.id) AS allowed_count,
                        CASE p.target_type
                            WHEN 'game' THEN (SELECT g.name FROM games g WHERE g.id=p.target_id)
                            WHEN 'category' THEN (SELECT c.name FROM categories c WHERE c.id=p.target_id)
                            WHEN 'product' THEN (SELECT pr.name FROM products pr WHERE pr.id=p.target_id)
                            ELSE 'All products'
                        END AS target_name
                 FROM promo_codes p
                 LEFT JOIN users u ON u.id = p.assigned_user_id
                ORDER BY p.created_at DESC, p.id DESC
                LIMIT 500"""
        )
        promo_codes = []
        for row in rows:
            item = serialize_row(dict(row))
            item["used_count"] = int(item.get("redemption_count") or 0)
            promo_codes.append(item)
        return web.json_response({"ok": True, "promo_codes": promo_codes})
    except Exception:
        import logging
        logging.getLogger("cpm_shop").exception("Failed to list promo codes")
        return web.json_response({"ok": False, "error": "Unable to load promo codes."}, status=500)


@admin_required
async def admin_create_promo_code_handler(request: web.Request):
    try:
        await _ensure_promo_schema_ready()
        data = await request.json()
        code = str(data.get("code", "")).strip()
        percent = int(data.get("discount_percent", 0) or 0)
        fixed = int(data.get("discount_stars", 0) or 0)
        max_uses = data.get("max_uses")
        if max_uses in ("", None):
            max_uses = None
        else:
            max_uses = int(max_uses)
        expires_at = data.get("expires_at") or None
        if expires_at:
            from datetime import datetime
            expires_at = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
            if expires_at.tzinfo is not None:
                expires_at = expires_at.replace(tzinfo=None)
        sale_price = int(data.get("sale_price_stars", 0) or 0)
        access_mode = str(data.get("access_mode", "public") or "public").strip().lower()
        target_type = str(data.get("target_type", "global") or "global").strip().lower()
        target_id = data.get("target_id")
        min_cart_quantity = int(data.get("min_cart_quantity", 1) or 1)
        max_uses_per_user = int(data.get("max_uses_per_user", 0) or 0)
        description = str(data.get("description", "") or "").strip()
        assigned_telegram_id = data.get("assigned_telegram_id")
        allowed_customers = data.get("allowed_customers")
        if allowed_customers in (None, "") and assigned_telegram_id not in (None, ""):
            allowed_customers = [assigned_telegram_id]
        if access_mode == "assigned" and not allowed_customers:
            return web.json_response({"ok": False, "error": "Specific Customer mode requires at least one customer."}, status=400)
        row = await create_promo_code(
            code=code,
            discount_percent=percent,
            discount_stars=fixed,
            max_uses=max_uses,
            expires_at=expires_at,
            sale_price_stars=sale_price,
            target_type=target_type,
            target_id=target_id,
            min_cart_quantity=min_cart_quantity,
            max_uses_per_user=max_uses_per_user,
            description=description,
            access_mode=access_mode,
            allowed_customers=allowed_customers,
        )
        return web.json_response({"ok": True, "promo_code": serialize_row(row)})
    except (TypeError, ValueError) as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    except Exception:
        import logging
        logging.getLogger("cpm_shop").exception("Failed to create promo code")
        return web.json_response({"ok": False, "error": "Unable to create promo code."}, status=500)



@admin_required
async def admin_update_promo_code_handler(request: web.Request):
    """Update all editable promo-code settings from the Admin Promo Codes page."""
    try:
        await _ensure_promo_schema_ready()
        promo_id = int(request.match_info.get("promo_id"))
        data = await request.json()
        if not isinstance(data, dict):
            return web.json_response({"ok": False, "error": "Invalid JSON payload."}, status=400)

        def _optional_int(value, field, minimum=None):
            if value in (None, ""):
                return None
            try:
                n = int(value)
            except (TypeError, ValueError):
                raise ValueError(f"{field} must be a valid integer.")
            if minimum is not None and n < minimum:
                raise ValueError(f"{field} must be at least {minimum}.")
            return n

        code = str(data.get("code", "")).strip()
        percent = int(data.get("discount_percent", 0) or 0)
        fixed = int(data.get("discount_stars", 0) or 0)
        max_uses = _optional_int(data.get("max_uses"), "Max uses", 1)
        sale_price = int(data.get("sale_price_stars", 0) or 0)
        access_mode = str(data.get("access_mode", "public") or "public").strip().lower()
        target_type = str(data.get("target_type", "global") or "global").strip().lower()
        target_id = _optional_int(data.get("target_id"), "Target ID", 1)
        min_cart_quantity = int(data.get("min_cart_quantity", 1) or 1)
        max_uses_per_user = int(data.get("max_uses_per_user", 0) or 0)
        description = str(data.get("description", "") or "").strip()
        active = bool(data.get("active", True))

        expires_at = data.get("expires_at") or None
        if expires_at:
            from datetime import datetime
            expires_at = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
            if expires_at.tzinfo is not None:
                expires_at = expires_at.replace(tzinfo=None)

        assigned_telegram_id = data.get("assigned_telegram_id")
        allowed_customers = data.get("allowed_customers")
        if allowed_customers in (None, "") and assigned_telegram_id not in (None, ""):
            allowed_customers = [assigned_telegram_id]
        if access_mode == "assigned" and not allowed_customers:
            return web.json_response({"ok": False, "error": "Specific Customer mode requires at least one customer."}, status=400)

        row = await update_promo_code(
            promo_id,
            {
                "code": code,
                "discount_percent": percent,
                "discount_stars": fixed,
                "max_uses": max_uses,
                "expires_at": expires_at,
                "sale_price_stars": sale_price,
                "target_type": target_type,
                "target_id": target_id,
                "min_cart_quantity": min_cart_quantity,
                "max_uses_per_user": max_uses_per_user,
                "description": description,
                "active": active,
                "access_mode": access_mode,
            },
        )

        if access_mode == "assigned":
            if allowed_customers in (None, "", []):
                raise ValueError("Specific Customer mode requires at least one customer.")
            row = await set_promo_allowed_users(promo_id, allowed_customers)
        else:
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM promo_allowed_users WHERE promo_code_id=$1", promo_id)
                row = await conn.fetchrow(
                    "UPDATE promo_codes SET assigned_user_id=NULL, access_mode=$1 WHERE id=$2 RETURNING *",
                    access_mode, promo_id,
                )
        return web.json_response({"ok": True, "promo_code": serialize_row(row)})
    except (TypeError, ValueError) as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    except Exception:
        log.exception("Failed to update promo code")
        return web.json_response({"ok": False, "error": "Unable to update promo code."}, status=500)

@admin_required
async def admin_promo_allowed_users_handler(request: web.Request):
    try:
        await _ensure_promo_schema_ready()
        promo_id = int(request.match_info.get("promo_id"))
        rows = await get_promo_allowed_users(promo_id)
        return web.json_response({"ok": True, "users": [serialize_row(x) for x in rows]})
    except (TypeError, ValueError) as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    except Exception:
        log.exception("Failed to load promo allowed users")
        return web.json_response({"ok": False, "error": "Unable to load promo customers."}, status=500)


@admin_required
async def admin_toggle_promo_code_handler(request: web.Request):
    try:
        await _ensure_promo_schema_ready()
        promo_id = int(request.match_info.get("promo_id"))
        row = await toggle_promo_code(promo_id)
        return web.json_response({"ok": True, "promo_code": serialize_row(row)})
    except (TypeError, ValueError) as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    except Exception:
        import logging
        logging.getLogger("cpm_shop").exception("Failed to toggle promo code")
        return web.json_response({"ok": False, "error": "Unable to update promo code."}, status=500)


async def _deliver_order_via_http(telegram_id: int, order_id: int):
    claim = await begin_order_delivery(order_id, telegram_id)
    if not claim:
        return {"status": "missing"}
    status = str(claim.get("status") or "")
    if status in {"delivered", "busy", "unavailable"}:
        return claim

    items = claim.get("items") or []
    if items:
        text = (
            "✅ <b>Order Delivery</b>\n\n"
            f"Order <b>#{int(order_id)}</b> is complete.\n\n"
            "📦 <b>Your item:</b>\n"
        )
        for index, item in enumerate(items, start=1):
            text += f"\n<b>{index}.</b> <code>{html.escape(str(item), quote=False)}</code>\n"
    else:
        text = (
            "✅ <b>Order Delivery</b>\n\n"
            f"Order <b>#{int(order_id)}</b> has been completed.\n\n"
            "Your order is ready. If manual delivery is required, the admin will contact you."
        )

    try:
        ok = await _send_telegram_text(telegram_id, text)
    except Exception as exc:
        ok = False
        log.exception("Telegram delivery request failed | order=%s", order_id)
        error = str(exc) or "Telegram delivery failed."
    else:
        error = "Telegram could not deliver the order message."

    if ok:
        if not await finish_order_delivery(order_id, telegram_id, True):
            return {"status": "delivered_unconfirmed", "items": items, "attempts": claim.get("attempts", 0)}
        return {"status": "delivered", "items": items, "attempts": claim.get("attempts", 0)}

    await finish_order_delivery(order_id, telegram_id, False, error)
    try:
        await create_notification(
            telegram_id,
            f"Order #{int(order_id)} was paid successfully, but delivery is still pending. Please retry delivery or contact support.",
            title="Delivery Pending",
            kind="order",
            reference_id=int(order_id),
        )
    except Exception:
        log.exception("Failed to create delivery failure notification | order=%s", order_id)
    return {"status": "failed", "error": error, "attempts": claim.get("attempts", 0)}


async def order_delivery_retry_handler(request: web.Request):
    try:
        order_id = int(request.match_info.get("order_id"))
        user, _ = await _webapp_user(request)
        # begin_order_delivery already performs the atomic claim. No separate
        # reset is needed and avoiding one prevents a concurrent retry race.
        result = await _deliver_order_via_http(user["telegram_id"], order_id)
        status = str(result.get("status") or "")
        if status == "failed":
            return web.json_response({"ok": False, "error": result.get("error") or "Delivery failed.", "delivery_pending": True}, status=502)
        if status == "busy":
            return web.json_response({"ok": False, "error": "Delivery is already being processed."}, status=409)
        if status == "missing":
            return web.json_response({"ok": False, "error": "Order delivery not found."}, status=404)
        if status == "unavailable":
            return web.json_response({"ok": False, "error": result.get("error") or "Delivery is not available."}, status=409)
        return web.json_response({"ok": True, "delivered": status in {"delivered", "delivered_unconfirmed"}, "delivery": result})
    except (TypeError, ValueError) as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    except Exception:
        log.exception("Order delivery retry failed")
        return web.json_response({"ok": False, "error": "Unable to retry order delivery."}, status=500)


async def cart_checkout_handler(request: web.Request):
    try:
        user, _ = await _webapp_user(request)
        body = await request.json()
        promo_code = str(body.get("promo_code", "")).strip() or None
        loyalty_redemption_id = body.get("loyalty_redemption_id")
        loyalty_redemption_id = int(loyalty_redemption_id) if loyalty_redemption_id not in (None, "", 0, "0") else None
        order_data = await create_pending_order_from_cart(
            user["telegram_id"],
            promo_code=promo_code,
            loyalty_redemption_id=loyalty_redemption_id,
        )
        if int(order_data["total_stars"]) == 0:
            try:
                result = await complete_free_order(
                    order_id=order_data["order_id"],
                    telegram_id=user["telegram_id"],
                )
            except Exception:
                # `create_pending_order_from_cart()` may reserve a loyalty
                # redemption.  If the free-order transaction fails, cancel
                # the pending order so the reservation is not stranded.
                await cancel_pending_order(
                    order_id=order_data["order_id"],
                    telegram_id=user["telegram_id"],
                )
                raise

            delivered_items = result.get("inventory_items") or []
            if delivered_items:
                text = (
                    "🎉 <b>Free Order Successful!</b>\n\n"
                    f"Order <b>#{order_data['order_id']}</b> is complete.\n\n"
                    "📦 <b>Your item:</b>\n"
                )
                for index, item in enumerate(delivered_items, start=1):
                    safe_item = str(item).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                    text += f"\n<b>{index}.</b> <code>{safe_item}</code>\n"
            else:
                text = (
                    "🎉 <b>Free Order Successful!</b>\n\n"
                    f"Order <b>#{order_data['order_id']}</b> has been completed.\n\n"
                    "Your order is ready. If this product requires manual delivery, the admin will contact you."
                )

            delivery_result = await _deliver_order_via_http(user["telegram_id"], order_data["order_id"])

            return web.json_response({
                "ok": True,
                "order_id": order_data["order_id"],
                "invoice_url": None,
                "free_order": True,
                "subtotal_stars": order_data["subtotal_stars"],
                "discount_stars": order_data["discount_stars"],
                "total_stars": 0,
                "promo_code": order_data.get("promo_code"),
                "loyalty_discount_stars": order_data.get("loyalty_discount_stars", 0),
                "loyalty_redemption_id": order_data.get("loyalty_redemption_id"),
                "delivery_items": delivered_items,
                "delivery_status": str(delivery_result.get("status") or "pending"),
                "delivery_pending": str(delivery_result.get("status") or "") != "delivered",
            })

        payload = f"order:{order_data['order_id']}"
        try:
            invoice_url = await create_invoice_link(
                title="YOUR SHOP Order",
                description=f"YOUR SHOP order #{order_data['order_id']}",
                payload=payload,
                price_stars=order_data["total_stars"],
            )
        except Exception as invoice_error:
            log.exception(
                "Invoice creation failed | order=%s | user=%s | total=%s | promo=%s | loyalty=%s | error=%s",
                order_data.get("order_id"),
                user.get("telegram_id"),
                order_data.get("total_stars"),
                order_data.get("promo_code"),
                order_data.get("loyalty_redemption_id"),
                invoice_error,
            )
            await cancel_pending_order(order_data["order_id"], user["telegram_id"])
            raise
        return web.json_response({
            "ok": True,
            "order_id": order_data["order_id"],
            "invoice_url": invoice_url,
            "subtotal_stars": order_data["subtotal_stars"],
            "discount_stars": order_data["discount_stars"],
            "total_stars": order_data["total_stars"],
            "promo_code": order_data.get("promo_code"),
            "loyalty_discount_stars": order_data.get("loyalty_discount_stars", 0),
            "loyalty_redemption_id": order_data.get("loyalty_redemption_id"),
        })
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    except Exception:
        import logging; logging.getLogger("cpm_shop").exception("Cart checkout failed")
        return web.json_response({"ok": False, "error": "Unable to create invoice."}, status=500)


async def purchase_product_handler(request: web.Request):
    try:
        product_id = int(request.match_info["product_id"])
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "Invalid product ID."}, status=400)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Invalid JSON."}, status=400)

    init_data = str(body.get("init_data", "")).strip()

    try:
        telegram_user = validate_telegram_init_data(init_data)
        await get_or_create_user(
            telegram_id=telegram_user["telegram_id"],
            username=telegram_user.get("username"),
            first_name=telegram_user.get("first_name"),
            last_name=telegram_user.get("last_name"),
            language_code=telegram_user.get("language_code"),
        )
        order_data = await create_pending_order(
            telegram_id=telegram_user["telegram_id"],
            product_id=product_id,
            quantity=1,
        )

        # A genuinely free product must not be sent to Telegram's invoice API.
        # `create_invoice_link()` correctly rejects zero prices, so complete the
        # pending order through the same free-order path used by cart checkout.
        if int(order_data["total_stars"]) == 0:
            try:
                result = await complete_free_order(
                    order_id=order_data["order_id"],
                    telegram_id=telegram_user["telegram_id"],
                )
                delivered_items = result.get("inventory_items") or []
                text = (
                    "🎉 <b>Free Order Successful!</b>\n\n"
                    f"Order <b>#{order_data['order_id']}</b> is complete.\n\n"
                )
                if delivered_items:
                    text += "📦 <b>Your item:</b>\n"
                    for index, item in enumerate(delivered_items, start=1):
                        safe_item = html.escape(str(item))
                        text += f"\n<b>{index}.</b> <code>{safe_item}</code>\n"
                else:
                    text += (
                        "Your order is ready. If this product requires manual delivery, "
                        "the admin will contact you."
                    )

                delivery_result = await _deliver_order_via_http(
                    telegram_user["telegram_id"],
                    order_data["order_id"],
                )

                return web.json_response({
                    "ok": True,
                    "order_id": order_data["order_id"],
                    "invoice_url": None,
                    "free_order": True,
                    "total_stars": 0,
                    "delivery_items": delivered_items,
                    "delivery_status": str(delivery_result.get("status") or "pending"),
                    "delivery_pending": str(delivery_result.get("status") or "") != "delivered",
                })
            except Exception:
                raise

        payload = f"order:{order_data['order_id']}"
        try:
            invoice_url = await create_invoice_link(
                title=order_data["name"],
                description=order_data["description"] or "YOUR SHOP product",
                payload=payload,
                price_stars=order_data["total_stars"],
            )
        except Exception:
            await cancel_pending_order(
                order_id=order_data["order_id"],
                telegram_id=telegram_user["telegram_id"],
            )
            raise

        return web.json_response(
            {
                "ok": True,
                "order_id": order_data["order_id"],
                "invoice_url": invoice_url,
                "total_stars": order_data["total_stars"],
            }
        )
    except ValueError as error:
        return web.json_response({"ok": False, "error": str(error)}, status=400)
    except Exception:
        import logging
        logging.getLogger("cpm_shop").exception("Failed to create purchase.")
        return web.json_response({"ok": False, "error": "Unable to create invoice."}, status=500)
        

@admin_required
async def admin_broadcast_handler(request: web.Request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response(
            {
                "ok": False,
                "error": "Invalid JSON.",
            },
            status=400,
        )

    message = str(
        data.get("message", "")
    ).strip()

    if not message:
        return web.json_response(
            {
                "ok": False,
                "error": "Broadcast message is required.",
            },
            status=400,
        )

    if len(message) > 5000:
        return web.json_response(
            {
                "ok": False,
                "error": "Broadcast message is too long. Maximum 5000 characters.",
            },
            status=400,
        )

    try:
        broadcast = await create_broadcast(message)

        users = await get_all_active_telegram_users()

        sent = 0
        failed = 0

        telegram_url = (
            f"https://api.telegram.org/"
            f"bot{config.BOT_TOKEN}/sendMessage"
        )

        timeout = aiohttp.ClientTimeout(total=20)

        async with aiohttp.ClientSession(
            timeout=timeout
        ) as session:

            for user in users:
                telegram_id = user.get("telegram_id")

                if not telegram_id:
                    failed += 1
                    continue

                try:
                    async with session.post(
                        telegram_url,
                        json={
                            "chat_id": int(
                                telegram_id
                            ),
                            "text": message,
                        },
                    ) as response:

                        telegram_data = (
                            await response.json()
                        )

                        if telegram_data.get("ok"):
                            sent += 1
                        else:
                            failed += 1

                except Exception:
                    failed += 1

                await asyncio.sleep(0.05)

        return web.json_response(
            {
                "ok": True,
                "broadcast_id": (
                    broadcast.get("id")
                    if broadcast
                    else None
                ),
                "total_recipients": len(users),
                "sent": sent,
                "failed": failed,
            }
        )

    except Exception:
        import logging

        logging.getLogger(
            "cpm_shop"
        ).exception(
            "Failed to send admin broadcast."
        )

        return web.json_response(
            {
                "ok": False,
                "error": "Unable to send broadcast.",
            },
            status=500,
                )
@admin_required
async def admin_list_tickets_handler(request: web.Request):
    try:
        raw_status = str(request.query.get("status", "")).strip().lower()
        status = raw_status if raw_status in {"open", "in_progress", "resolved", "closed"} else None
        search = str(request.query.get("search", "")).strip()[:80]
        try:
            page = max(1, int(request.query.get("page", "1")))
        except (TypeError, ValueError):
            page = 1
        try:
            limit = min(50, max(1, int(request.query.get("limit", "20"))))
        except (TypeError, ValueError):
            limit = 20
        total = await admin_count_tickets(status=status, search=search)
        page_count = max(1, (total + limit - 1) // limit)
        page = min(page, page_count)
        rows = await admin_list_tickets(
            status=status,
            search=search,
            limit=limit,
            offset=(page - 1) * limit,
        )
        return web.json_response({
            "ok": True,
            "tickets": [serialize_row(row) for row in rows],
            "pagination": {"page": page, "limit": limit, "total": total, "pages": page_count},
            "filters": {"status": status or "all", "search": search},
        })
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    except Exception:
        import logging
        logging.getLogger("cpm_shop").exception("Admin ticket list failed.")
        return web.json_response({"ok": False, "error": "Unable to load tickets."}, status=500)


@admin_required
async def admin_ticket_delete_handler(request: web.Request):
    try:
        ticket_id = int(request.match_info["ticket_id"])
        deleted = await admin_delete_ticket(ticket_id)
        if not deleted:
            return web.json_response({"ok": False, "error": "Ticket not found."}, status=404)
        return web.json_response({"ok": True, "ticket_id": ticket_id})
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    except Exception:
        import logging
        logging.getLogger("cpm_shop").exception("Admin ticket delete failed.")
        return web.json_response({"ok": False, "error": "Unable to delete ticket."}, status=500)


@admin_required
async def admin_ticket_detail_handler(request: web.Request):
    try:
        ticket_id = int(
            request.match_info["ticket_id"]
        )

        ticket = await admin_get_ticket(
            ticket_id
        )

        if not ticket:
            return web.json_response(
                {
                    "ok": False,
                    "error": "Ticket not found.",
                },
                status=404,
            )

        return web.json_response({
            "ok": True,
            "ticket": serialize_row(ticket),
        })

    except ValueError as e:
        return web.json_response(
            {
                "ok": False,
                "error": str(e),
            },
            status=400,
        )

    except Exception:
        import logging

        logging.getLogger(
            "cpm_shop"
        ).exception(
            "Admin ticket detail failed."
        )

        return web.json_response(
            {
                "ok": False,
                "error": "Unable to load ticket.",
            },
            status=500,
        )


@admin_required
async def admin_ticket_message_handler(request: web.Request):
    try:
        ticket_id = int(
            request.match_info["ticket_id"]
        )

        try:
            data = await request.json()
        except Exception:
            return web.json_response(
                {
                    "ok": False,
                    "error": "Invalid JSON.",
                },
                status=400,
            )

        message = str(
            data.get("message", "")
        ).strip()

        if not message:
            return web.json_response(
                {
                    "ok": False,
                    "error": "Message is required.",
                },
                status=400,
            )

        if len(message) > 5000:
            return web.json_response(
                {
                    "ok": False,
                    "error": "Message is too long.",
                },
                status=400,
            )

        ticket = await admin_add_ticket_message(
            ticket_id,
            message,
        )
        try:
            owner=await fetch_one("SELECT u.telegram_id FROM tickets t JOIN users u ON u.id=t.user_id WHERE t.id=?",(ticket_id,))
            if owner:
                await create_notification(owner['telegram_id'],message,title=f"Support Reply · Ticket #{ticket_id}",kind='ticket',reference_id=ticket_id)
                await _send_telegram_text(owner['telegram_id'],f"🎫 <b>Support replied to ticket #{ticket_id}</b>\n\n{__import__('html').escape(message)}")
        except Exception:
            import logging; logging.getLogger('cpm_shop').exception('Customer ticket notification failed')

        return web.json_response({
            "ok": True,
            "ticket": serialize_row(ticket),
        })

    except ValueError as e:
        return web.json_response(
            {
                "ok": False,
                "error": str(e),
            },
            status=400,
        )

    except Exception:
        import logging

        logging.getLogger(
            "cpm_shop"
        ).exception(
            "Admin ticket reply failed."
        )

        return web.json_response(
            {
                "ok": False,
                "error": "Unable to send ticket reply.",
            },
            status=500,
        )


@admin_required
async def admin_ticket_status_handler(request: web.Request):
    try:
        ticket_id = int(
            request.match_info["ticket_id"]
        )

        try:
            data = await request.json()
        except Exception:
            return web.json_response(
                {
                    "ok": False,
                    "error": "Invalid JSON.",
                },
                status=400,
            )

        status = str(
            data.get("status", "")
        ).strip().lower()

        if not status:
            return web.json_response(
                {
                    "ok": False,
                    "error": "Status is required.",
                },
                status=400,
            )

        ticket = await admin_update_ticket_status(
            ticket_id,
            status,
        )

        if not ticket:
            return web.json_response(
                {
                    "ok": False,
                    "error": "Ticket not found.",
                },
                status=404,
            )

        return web.json_response({
            "ok": True,
            "ticket": serialize_row(ticket),
        })

    except ValueError as e:
        return web.json_response(
            {
                "ok": False,
                "error": str(e),
            },
            status=400,
        )

    except Exception:
        import logging

        logging.getLogger(
            "cpm_shop"
        ).exception(
            "Admin ticket status update failed."
        )

        return web.json_response(
            {
                "ok": False,
                "error": "Unable to update ticket status.",
            },
            status=500,
        )   

async def tickets_handler(request: web.Request):
    try:
        user, _ = await _webapp_user(request)
        rows = await get_user_tickets(user["telegram_id"])
        return web.json_response({
            "ok": True,
            "tickets": [serialize_row(x) for x in rows],
        })
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    except Exception:
        import logging
        logging.getLogger("cpm_shop").exception("Customer ticket list failed")
        return web.json_response({"ok": False, "error": "Unable to load support tickets."}, status=500)


async def ticket_create_handler(request: web.Request):
    try:
        user, body = await _webapp_user(request)
        subject = str(body.get("subject", "")).strip()
        message = str(body.get("message", "")).strip()
        category = str(body.get("category", "general") or "general").strip()
        priority = str(body.get("priority", "normal") or "normal").strip().lower()
        order_id = body.get("order_id")

        if not subject:
            return web.json_response({"ok": False, "error": "Subject is required."}, status=400)
        if not message:
            return web.json_response({"ok": False, "error": "Message is required."}, status=400)
        if len(subject) > 160:
            return web.json_response({"ok": False, "error": "Subject is too long."}, status=400)
        if len(message) > 5000:
            return web.json_response({"ok": False, "error": "Message is too long. Maximum 5000 characters."}, status=400)

        if priority not in {"low", "normal", "high", "urgent"}:
            priority = "normal"
        if order_id in ("", None, 0, "0"):
            order_id = None
        else:
            try:
                order_id = int(order_id)
            except (TypeError, ValueError):
                return web.json_response({"ok": False, "error": "Invalid order ID."}, status=400)

        ticket = await create_ticket(
            user["telegram_id"],
            subject,
            message,
            category=category,
            priority=priority,
            order_id=order_id,
        )

        # Notify the Telegram admin even when the ticket was created from the Mini App.
        try:
            admin_id = int(getattr(config, "ADMIN_ID", 0) or 0)
            if admin_id > 0:
                customer_name = (
                    user.get("first_name")
                    or (("@" + str(user.get("username"))) if user.get("username") else "")
                    or str(user.get("telegram_id"))
                )
                note = (
                    "🎫 <b>YOUR SHOP ADMIN</b>\n\n"
                    "📩 <b>New support ticket</b>\n"
                    f"🎫 Ticket: <code>#{int(ticket['id'])}</code>\n"
                    f"👤 Customer: <b>{html.escape(str(customer_name))}</b>\n"
                    f"🆔 Telegram ID: <code>{int(user['telegram_id'])}</code>\n"
                    f"🏷️ Category: <b>{html.escape(category)}</b> · ⚡ <b>{html.escape(priority)}</b>\n"
                    f"📝 Subject: <b>{html.escape(subject)}</b>\n\n"
                    f"💬 {html.escape(message[:3500])}"
                )
                await _send_telegram_text(
                    admin_id,
                    note,
                    {"inline_keyboard": [[{"text": "🎫 Open Ticket", "callback_data": f"admin:ticket:{int(ticket['id'])}"}]]},
                )
        except Exception:
            import logging
            logging.getLogger("cpm_shop").exception("Failed to notify admin about web ticket")

        return web.json_response({
            "ok": True,
            "ticket": serialize_row(ticket),
        })
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    except Exception:
        import logging
        logging.getLogger("cpm_shop").exception("Customer ticket creation failed")
        return web.json_response({"ok": False, "error": "Unable to create support ticket."}, status=500)


async def ticket_detail_handler(request: web.Request):
    try:
        user, _ = await _webapp_user(request)
        ticket_id = int(request.match_info.get("ticket_id"))
        ticket = await get_ticket_for_user(
            ticket_id,
            user["telegram_id"],
        )
        if not ticket:
            return web.json_response({"ok": False, "error": "Ticket not found."}, status=404)

        return web.json_response({
            "ok": True,
            "ticket": serialize_row(ticket),
        })
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    except Exception:
        import logging
        logging.getLogger("cpm_shop").exception("Customer ticket detail failed")
        return web.json_response({"ok": False, "error": "Unable to load ticket."}, status=500)


async def ticket_reply_handler(request: web.Request):
    try:
        user, body = await _webapp_user(request)
        ticket_id = int(request.match_info.get("ticket_id"))
        message = str(body.get("message", "")).strip()

        if not message:
            return web.json_response({"ok": False, "error": "Message is required."}, status=400)
        if len(message) > 5000:
            return web.json_response({"ok": False, "error": "Message is too long. Maximum 5000 characters."}, status=400)

        ticket = await add_ticket_message(
            user["telegram_id"],
            ticket_id,
            message,
        )
        return web.json_response({
            "ok": True,
            "ticket": serialize_row(ticket),
        })
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    except Exception:
        import logging
        logging.getLogger("cpm_shop").exception("Customer ticket reply failed")
        return web.json_response({"ok": False, "error": "Unable to send ticket reply."}, status=500)


@admin_required
async def admin_notes_handler(request: web.Request):
    from database import list_admin_notes
    notes = await list_admin_notes()
    return web.json_response({"ok": True, "notes": [serialize_row(x) for x in notes]})


@admin_required
async def admin_create_note_handler(request: web.Request):
    from database import create_admin_note
    data = await request.json()
    title = str(data.get("title", "")).strip()
    content = str(data.get("content", "")).strip()
    if not title or not content:
        return web.json_response({"ok": False, "error": "Title and content are required"}, status=400)
    note = await create_admin_note(title, content)
    return web.json_response({"ok": True, "note": serialize_row(note)})


@admin_required
async def admin_update_note_handler(request: web.Request):
    from database import update_admin_note
    note_id = int(request.match_info["note_id"])
    data = await request.json()
    note = await update_admin_note(note_id, data.get("title", ""), data.get("content", ""))
    if not note:
        return web.json_response({"ok": False, "error": "Note not found"}, status=404)
    return web.json_response({"ok": True, "note": serialize_row(note)})


@admin_required
async def admin_delete_note_handler(request: web.Request):
    from database import delete_admin_note
    note_id = int(request.match_info["note_id"])
    note = await delete_admin_note(note_id)
    if not note:
        return web.json_response({"ok": False, "error": "Note not found"}, status=404)
    return web.json_response({"ok": True})


CONTENT_SETTING_KEYS = [
    "welcome_title", "welcome_description", "welcome_image",
    "welcome_button_1", "welcome_button_2",
    "help_title", "help_description", "help_image",
    "shop_description", "support_description", "feedback_description",
    "rewards_description", "promo_description", "orders_description",
    "notifications_description", "giveaways_description",
    "referral_description", "paid_review_price_stars",
    "promo_purchase_description", "admin_site_url",
]

@admin_required
async def admin_content_get_handler(request: web.Request):
    rows = await fetch_all("SELECT key,value FROM settings WHERE key = ANY(?)", (CONTENT_SETTING_KEYS,))
    data = {str(x.get("key")): x.get("value") for x in rows if x.get("key")}
    for k in CONTENT_SETTING_KEYS:
        data.setdefault(k, "")
    return web.json_response({"ok": True, "content": data})

@admin_required
async def admin_content_update_handler(request: web.Request):
    body = await request.json()
    if not isinstance(body, dict):
        return web.json_response({"ok": False, "error": "Invalid content payload"}, status=400)

    changed = []
    for k in CONTENT_SETTING_KEYS:
        if k not in body:
            continue
        value = "" if body[k] is None else str(body[k])
        # Keep admin text fields reasonably bounded while preserving multiline descriptions.
        value = value[:10000]
        await fetch_one(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value RETURNING key",
            (k, value),
        )
        changed.append(k)

    # Keep the running bot's welcome-button labels in sync immediately.
    try:
        import sys
        running_bot = sys.modules.get("bot")
        if running_bot is not None and hasattr(running_bot, "CONTENT_CACHE"):
            running_bot.CONTENT_CACHE.update({k: ("" if body.get(k) is None else str(body.get(k))) for k in changed})
    except Exception:
        pass

    return web.json_response({"ok": True, "updated": changed, "updated_count": len(changed)})

async def public_content_handler(request: web.Request):
    rows=await fetch_all("SELECT key,value FROM settings WHERE key LIKE ?",("%_description",))
    rows += await fetch_all("SELECT key,value FROM settings WHERE key IN ('welcome_title','welcome_image','help_title','help_image')")
    return web.json_response({"ok":True,"content":{x.get('key'):x.get('value') for x in rows}})

@admin_required
async def admin_add_product_image_handler(request: web.Request):
    try:
        pid = int(request.match_info["product_id"])
        body = await request.json()
        image = _normalize_media_reference(body.get("image", ""))
        if not image:
            return web.json_response({"ok": False, "error": "Image is required."}, status=400)

        product = await fetch_one("SELECT id FROM products WHERE id=?", (pid,))
        if not product:
            return web.json_response({"ok": False, "error": "Product not found."}, status=404)

        cnt = await fetch_one("SELECT COUNT(*) AS c FROM product_images WHERE product_id=?", (pid,))
        n = int(cnt.get("c") or 0)
        if n >= 100:
            return web.json_response({"ok": False, "error": "Maximum 100 images."}, status=400)

        existing = await fetch_one(
            "SELECT id FROM product_images WHERE product_id=? AND image=? LIMIT 1",
            (pid, image),
        )
        if existing:
            return web.json_response({"ok": True, "image": serialize_row(existing), "duplicate": True})

        row = await fetch_one(
            "INSERT INTO product_images(product_id,image,sort_order) VALUES(?,?,?) RETURNING *",
            (pid, image, n),
        )
        if n == 0:
            await fetch_one("UPDATE products SET banner=? WHERE id=? RETURNING id", (image, pid))
        return web.json_response({"ok": True, "image": serialize_row(row)})
    except ValueError as error:
        return web.json_response({"ok": False, "error": str(error)}, status=400)
    except Exception:
        log.exception("Failed to add product image")
        return web.json_response({"ok": False, "error": "Unable to add product image."}, status=500)


@admin_required
async def admin_product_images_handler(request: web.Request):
    try:
        pid = int(request.match_info["product_id"])
        product = await fetch_one("SELECT id FROM products WHERE id=?", (pid,))
        if not product:
            return web.json_response({"ok": False, "error": "Product not found."}, status=404)
        rows = await fetch_all(
            "SELECT * FROM product_images WHERE product_id=? ORDER BY sort_order ASC, id ASC",
            (pid,),
        )
        return web.json_response({"ok": True, "images": [serialize_row(x) for x in rows]})
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "Invalid product_id."}, status=400)

@admin_required
async def admin_delete_product_image_handler(request: web.Request):
    pid=int(request.match_info["product_id"]); iid=int(request.match_info["image_id"])
    if not await fetch_one("SELECT id FROM product_images WHERE id=? AND product_id=?",(iid,pid)):return web.json_response({"ok":False,"error":"Image not found"},status=404)
    await fetch_one("DELETE FROM product_images WHERE id=? RETURNING id",(iid,))
    next_img=await fetch_one("SELECT image FROM product_images WHERE product_id=? ORDER BY sort_order,id LIMIT 1",(pid,))
    await fetch_one("UPDATE products SET banner=? WHERE id=? RETURNING id",(next_img.get('image') if next_img else None,pid))
    return web.json_response({"ok":True})


async def _read_bulk_ids(request: web.Request):
    try:
        body = await request.json()
    except Exception as error:
        raise ValueError("Invalid JSON payload.") from error
    ids = body.get("ids") if isinstance(body, dict) else None
    if not isinstance(ids, list) or not ids:
        raise ValueError("ids must be a non-empty list.")
    clean = []
    for value in ids:
        try:
            item_id = int(value)
        except (TypeError, ValueError) as error:
            raise ValueError("All ids must be valid integers.") from error
        if item_id <= 0:
            raise ValueError("All ids must be positive.")
        if item_id not in clean:
            clean.append(item_id)
    if len(clean) > 200:
        raise ValueError("You can delete at most 200 items at once.")
    return clean


@admin_required
async def admin_bulk_delete_games_handler(request: web.Request):
    try:
        ids = await _read_bulk_ids(request)
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                result = await conn.fetch("SELECT id FROM games WHERE id = ANY($1::BIGINT[])", ids)
                found = {int(r["id"]) for r in result}
                deleted = []
                for game_id in ids:
                    if game_id not in found:
                        continue
                    await conn.execute("DELETE FROM games WHERE id=$1", game_id)
                    deleted.append(game_id)
        return web.json_response({"ok": True, "deleted_ids": deleted, "deleted_count": len(deleted)})
    except ValueError as error:
        return web.json_response({"ok": False, "error": str(error)}, status=400)
    except Exception:
        log.exception("Bulk game delete failed")
        return web.json_response({"ok": False, "error": "Unable to delete selected games."}, status=500)


@admin_required
async def admin_bulk_delete_categories_handler(request: web.Request):
    try:
        ids = await _read_bulk_ids(request)
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                result = await conn.fetch("SELECT id FROM categories WHERE id = ANY($1::BIGINT[])", ids)
                found = {int(r["id"]) for r in result}
                deleted = []
                for category_id in ids:
                    if category_id not in found:
                        continue
                    await conn.execute("DELETE FROM categories WHERE id=$1", category_id)
                    deleted.append(category_id)
        return web.json_response({"ok": True, "deleted_ids": deleted, "deleted_count": len(deleted)})
    except ValueError as error:
        return web.json_response({"ok": False, "error": str(error)}, status=400)
    except Exception:
        log.exception("Bulk category delete failed")
        return web.json_response({"ok": False, "error": "Unable to delete selected categories."}, status=500)


@admin_required
async def admin_bulk_delete_products_handler(request: web.Request):
    try:
        ids = await _read_bulk_ids(request)
        pool = await get_pool()
        blockers = {}
        deleted = []
        missing = []
        async with pool.acquire() as conn:
            async with conn.transaction():
                rows = await conn.fetch(
                    """
                    SELECT
                        p.id,
                        p.name,
                        p.active,
                        (SELECT COUNT(*) FROM inventory_items ii WHERE ii.product_id=p.id AND ii.status='available') AS available_inventory
                    FROM products p
                    WHERE p.id = ANY($1::BIGINT[])
                    """,
                    ids,
                )
                found = {int(r["id"]): r for r in rows}
                for product_id in ids:
                    row = found.get(product_id)
                    if not row:
                        missing.append(product_id)
                        continue
                    available = int(row["available_inventory"] or 0)
                    if bool(row["active"]):
                        blockers[product_id] = {
                            "reason": "active",
                            "message": "Product is active. Deactivate it first.",
                            "available_inventory": available,
                        }
                    elif available > 0:
                        blockers[product_id] = {
                            "reason": "available_inventory",
                            "message": f"Product still has {available} available inventory item(s).",
                            "available_inventory": available,
                        }

                for product_id in ids:
                    if product_id not in found or product_id in blockers:
                        continue
                    await conn.execute(
                        "DELETE FROM products WHERE id=$1 AND active=FALSE",
                        product_id,
                    )
                    deleted.append(product_id)

        return web.json_response({
            "ok": True,
            "deleted_ids": deleted,
            "deleted_count": len(deleted),
            "blocked": blockers,
            "blocked_count": len(blockers),
            "missing_ids": missing,
        })
    except ValueError as error:
        return web.json_response({"ok": False, "error": str(error)}, status=400)
    except Exception:
        log.exception("Bulk product delete failed")
        return web.json_response({"ok": False, "error": "Unable to delete selected products."}, status=500)


@admin_required
async def admin_bulk_delete_promo_codes_handler(request: web.Request):
    try:
        await _ensure_promo_schema_ready()
        ids = await _read_bulk_ids(request)
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                rows = await conn.fetch("SELECT id FROM promo_codes WHERE id = ANY($1::BIGINT[])", ids)
                found = {int(r["id"]) for r in rows}
                deleted = []
                for promo_id in ids:
                    if promo_id not in found:
                        continue
                    await conn.execute("DELETE FROM promo_codes WHERE id=$1", promo_id)
                    deleted.append(promo_id)
        return web.json_response({"ok": True, "deleted_ids": deleted, "deleted_count": len(deleted)})
    except ValueError as error:
        return web.json_response({"ok": False, "error": str(error)}, status=400)
    except Exception:
        log.exception("Bulk promo delete failed")
        return web.json_response({"ok": False, "error": "Unable to delete selected promo codes."}, status=500)


async def my_promo_codes_handler(request: web.Request):
    """Return promo codes available to the current customer.

    Per-user limits are measured in eligible inventory quantity, not redemption-row count.
    """
    try:
        await _ensure_promo_schema_ready()
        user, _ = await _webapp_user(request)
        app_user = await get_or_create_user(user["telegram_id"])
        rows = await fetch_all(
            """
            SELECT
                p.id, p.code, p.discount_percent, p.discount_stars, p.max_uses,
                p.used_count, p.active, p.expires_at, p.sale_price_stars,
                p.target_type, p.target_id, p.min_cart_quantity,
                p.max_uses_per_user, p.description, p.assigned_user_id,
                p.sold_to_user_id, p.sold_at, p.created_at, p.access_mode,
                CASE p.target_type
                    WHEN 'game' THEN (SELECT g.name FROM games g WHERE g.id=p.target_id)
                    WHEN 'category' THEN (SELECT c.name FROM categories c WHERE c.id=p.target_id)
                    WHEN 'product' THEN (SELECT pr.name FROM products pr WHERE pr.id=p.target_id)
                    ELSE 'All products'
                END AS target_name,
                COALESCE((
                    SELECT COALESCE(SUM(pr.quantity), 0)
                    FROM promo_redemptions pr
                    WHERE pr.promo_code_id = p.id
                      AND pr.user_id = ?
                ), 0) AS user_uses,
                (
                    SELECT MAX(pr.created_at)
                    FROM promo_redemptions pr
                    WHERE pr.promo_code_id = p.id
                      AND pr.user_id = ?
                ) AS last_used_at
            FROM promo_codes p
            WHERE p.sold_to_user_id = ?
               OR p.assigned_user_id = ?
               OR EXISTS (
                    SELECT 1 FROM promo_allowed_users pau
                    WHERE pau.promo_code_id = p.id
                      AND pau.telegram_id = ?
               )
            ORDER BY last_used_at DESC NULLS LAST, p.id DESC
            LIMIT 100
            """,
            (app_user["id"], app_user["id"], app_user["id"], app_user["id"], app_user["telegram_id"]),
        )

        promo_codes = []
        for row in rows:
            # asyncpg.fetch() returns asyncpg.Record objects. Convert each
            # record to a plain dict before serializing so aiohttp can encode
            # the Promo Shop response as JSON instead of returning HTTP 500.
            item = serialize_row(dict(row))
            used_by_user = int(row.get("user_uses") or 0)
            per_user_limit = int(row.get("max_uses_per_user") or 0)
            item["used_by_user"] = used_by_user
            item["remaining_uses"] = None if per_user_limit <= 0 else max(0, per_user_limit - used_by_user)
            item["usage_limit_per_user"] = None if per_user_limit <= 0 else per_user_limit
            expires_at = row.get("expires_at")
            expired = expires_at is not None and expires_at <= __import__("datetime").datetime.utcnow()
            access_mode = str(row.get("access_mode") or "public").strip().lower()
            owned_or_allowed = True
            if access_mode == "sale":
                owned_or_allowed = row.get("sold_to_user_id") is not None and int(row.get("sold_to_user_id")) == int(app_user["id"])
            item["is_usable"] = (
                bool(row.get("active"))
                and not expired
                and owned_or_allowed
                and (per_user_limit <= 0 or item["remaining_uses"] > 0)
            )
            promo_codes.append(item)

        return web.json_response({"ok": True, "promo_codes": promo_codes, "count": len(promo_codes)})
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    except Exception:
        log.exception("Failed to load my promo codes")
        return web.json_response({"ok": False, "error": "Unable to load your promo codes."}, status=500)

async def promo_shop_handler(request: web.Request):
    try:
        await _ensure_promo_schema_ready()
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT p.id, p.discount_percent, p.discount_stars, p.max_uses, p.used_count, p.active,
                          p.expires_at, p.sale_price_stars, p.target_type, p.target_id,
                          p.min_cart_quantity, p.max_uses_per_user, p.description, p.created_at,
                          CASE p.target_type
                              WHEN 'game' THEN (SELECT g.name FROM games g WHERE g.id=p.target_id)
                              WHEN 'category' THEN (SELECT c.name FROM categories c WHERE c.id=p.target_id)
                              WHEN 'product' THEN (SELECT pr.name FROM products pr WHERE pr.id=p.target_id)
                              ELSE 'All products'
                          END AS target_name
                     FROM promo_codes p
                    WHERE p.active=TRUE
                      AND COALESCE(p.access_mode, 'public')='sale'
                      AND p.sold_to_user_id IS NULL
                      AND p.sale_price_stars > 0
                      AND (p.max_uses IS NULL OR (SELECT COALESCE(SUM(pr.quantity), 0) FROM promo_redemptions pr WHERE pr.promo_code_id=p.id) < p.max_uses)
                      AND (p.expires_at IS NULL OR p.expires_at > CURRENT_TIMESTAMP)
                    ORDER BY p.created_at DESC, p.id DESC LIMIT 100"""
            )
        # IMPORTANT: never return the actual promo code to the public shop.
        # Keep this defensive even if the SELECT is changed later.
        items = []
        for row in rows:
            # asyncpg.fetch() returns asyncpg.Record objects. Convert each
            # record to a plain dict before JSON serialization so this public
            # Promo Shop endpoint cannot fail with HTTP 500.
            item = serialize_row(dict(row))
            item.pop("code", None)
            item.pop("sold_to_user_id", None)
            item.pop("assigned_user_id", None)
            item.pop("telegram_payment_charge_id", None)
            item["available"] = True
            items.append(item)
        return web.json_response({"ok": True, "promo_codes": items})
    except Exception:
        log.exception("Failed to load public promo shop")
        return web.json_response({"ok": False, "error": "Unable to load promo codes."}, status=500)


async def promo_purchase_handler(request: web.Request):
    try:
        await _ensure_promo_schema_ready()
        user, _ = await _webapp_user(request)
        body = await request.json()
        promo_id = int(body.get("promo_id"))
        promo = await get_promo_for_purchase(promo_id, user["telegram_id"])
        if not promo:
            return web.json_response({"ok": False, "error": "This promo code is no longer available."}, status=409)
        price = int(promo["sale_price_stars"] or 0)
        if price <= 0:
            return web.json_response({"ok": False, "error": "This promo code is not for sale."}, status=400)
        invoice_url = await create_invoice_link(
            title="YOUR SHOP Promo Code",
            description=str(promo["description"] or "Promo code purchase")[:255],
            payload=f"promo:{promo_id}",
            price_stars=price,
        )
        return web.json_response({"ok": True, "promo_id": promo_id, "invoice_url": invoice_url, "price_stars": price})
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    except Exception:
        log.exception("Promo purchase invoice creation failed")
        return web.json_response({"ok": False, "error": "Unable to create promo purchase invoice."}, status=500)


@admin_required
async def admin_referral_settings_get_handler(request: web.Request):
    try:
        settings = await get_referral_settings()
        return web.json_response({"ok": True, "settings": settings})
    except Exception:
        import logging
        logging.getLogger("cpm_shop").exception("Failed to load referral settings")
        return web.json_response({"ok": False, "error": "Unable to load referral settings."}, status=500)


@admin_required
async def admin_referral_settings_update_handler(request: web.Request):
    try:
        data = await request.json()
        per_user = int(data.get("per_user", 1))
        max_discount = int(data.get("max_discount", 70))
        days = int(data.get("days", 7))
        if per_user < 0 or per_user > 100:
            raise ValueError("Per-referral discount must be between 0 and 100.")
        if max_discount < 0 or max_discount > 100:
            raise ValueError("Maximum discount must be between 0 and 100.")
        if days < 0 or days > 3650:
            raise ValueError("Discount duration must be between 0 and 3650 days.")
        settings = await set_referral_settings(per_user, max_discount, days)
        return web.json_response({"ok": True, "settings": settings})
    except (TypeError, ValueError) as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    except Exception:
        import logging
        logging.getLogger("cpm_shop").exception("Failed to update referral settings")
        return web.json_response({"ok": False, "error": "Unable to update referral settings."}, status=500)


@admin_required
async def admin_test_order_handler(request: web.Request):
    try:
        data = await request.json()
        product_id = int(data.get("product_id", 0))
        quantity = int(data.get("quantity", 1))
        complete = bool(data.get("complete", False))
        result = await create_admin_test_order(product_id, quantity, complete=complete)

        if complete and result.get("inventory_items"):
            try:
                token = getattr(config, "BOT_TOKEN", "")
                if token:
                    text = (
                        "🧪 <b>Admin Test Order Delivered</b>\n\n"
                        f"🧾 Order: <b>#{result['order_id']}</b>\n"
                        f"📦 Product: <b>{html.escape(result['product_name'])}</b>\n"
                        f"⭐ Test total: <b>{result['total_stars']}</b>\n\n"
                        "🎁 <b>Test delivery:</b>\n"
                        f"<pre>{html.escape(chr(10).join(result['inventory_items']))}</pre>"
                    )
                    async with aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=8)
                    ) as session:
                        await session.post(
                            f"https://api.telegram.org/bot{token}/sendMessage",
                            json={
                                "chat_id": int(config.ADMIN_ID),
                                "text": text,
                                "parse_mode": "HTML",
                            },
                        )
            except Exception:
                import logging
                logging.getLogger("cpm_shop").exception(
                    "Failed to notify admin about test order delivery"
                )

        return web.json_response({"ok": True, "order": result})
    except (TypeError, ValueError) as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    except Exception:
        import logging
        logging.getLogger("cpm_shop").exception("Admin test order failed")
        return web.json_response(
            {"ok": False, "error": "Unable to create admin test order."},
            status=500,
        )


def setup_store_routes(app: web.Application):
    app.router.add_post("/api/admin/login", admin_login_handler)
    app.router.add_get("/api/admin/me", admin_me_handler)
    app.router.add_post("/api/admin/logout", admin_logout_handler)
    app.router.add_get("/api/admin/stats", admin_stats_handler)
    app.router.add_get("/api/admin/analytics", admin_analytics_handler)
    app.router.add_get("/api/admin/inventory/export", admin_inventory_export_handler)
    app.router.add_get("/api/admin/categories", admin_categories_handler)
    app.router.add_get("/api/admin/products", admin_products_handler)
    app.router.add_get("/api/admin/products/{product_id}", admin_product_detail_handler)
    app.router.add_post("/api/admin/products", create_product_handler)
    app.router.add_post("/api/admin/media/upload", admin_media_upload_handler)
    app.router.add_put("/api/admin/products/{product_id}", update_product_handler)
    app.router.add_delete("/api/admin/products/{product_id}", delete_product_handler)
    app.router.add_post("/api/admin/products/{product_id}/toggle", toggle_product_handler)
    app.router.add_post("/api/admin/products/{product_id}/sync-stock", sync_product_stock_handler)
    app.router.add_post("/api/admin/products/bulk-delete", admin_bulk_delete_products_handler)
    app.router.add_post("/api/admin/bulk-delete/products", admin_bulk_delete_products_handler)
    app.router.add_post("/api/admin/products/{product_id}/images", admin_add_product_image_handler)
    app.router.add_get("/api/admin/products/{product_id}/images", admin_product_images_handler)
    app.router.add_delete("/api/admin/products/{product_id}/images/{image_id}", admin_delete_product_image_handler)
    app.router.add_get("/api/admin/inventory", admin_inventory_handler)
    app.router.add_post("/api/admin/inventory/preview", admin_inventory_preview_handler)
    app.router.add_post("/api/admin/inventory", admin_add_inventory_handler)
    app.router.add_delete("/api/admin/inventory/{inventory_id}", admin_delete_inventory_handler)
    app.router.add_post("/api/admin/inventory/{inventory_id}/status", admin_inventory_status_handler)
    app.router.add_post("/api/admin/inventory/bulk-delete", admin_bulk_delete_inventory_handler)
    app.router.add_get("/api/admin/store/status", admin_store_status_handler)
    app.router.add_post("/api/admin/store/status", admin_store_status_update_handler)
    app.router.add_get("/api/admin/orders", admin_orders_handler)
    app.router.add_get("/api/admin/orders/{order_id}", admin_order_detail_handler)
    app.router.add_get("/api/admin/payments", admin_payments_handler)
    app.router.add_get("/api/admin/users", admin_users_handler)
    app.router.add_get("/api/admin/games", admin_games_handler)
    app.router.add_get("/media/telegram/{file_id}", telegram_media_handler)
    app.router.add_get("/api/games", games_handler)

    
    app.router.add_post("/api/admin/games", create_game_handler)
    app.router.add_put("/api/admin/games/{game_id}", admin_update_game_handler)
    app.router.add_delete("/api/admin/games/{game_id}", admin_delete_game_handler)
    app.router.add_post("/api/admin/games/{game_id}/toggle", admin_toggle_game_handler)
    app.router.add_post("/api/admin/games/bulk-delete", admin_bulk_delete_games_handler)
    app.router.add_post("/api/admin/bulk-delete/games", admin_bulk_delete_games_handler)
    app.router.add_post("/api/admin/categories", create_category_handler)
    app.router.add_put("/api/admin/categories/{category_id}", admin_update_category_handler)
    app.router.add_delete("/api/admin/categories/{category_id}", admin_delete_category_handler)
    app.router.add_post("/api/admin/categories/{category_id}/toggle", admin_toggle_category_handler)
    app.router.add_post("/api/admin/categories/bulk-delete", admin_bulk_delete_categories_handler)
    app.router.add_post("/api/admin/bulk-delete/categories", admin_bulk_delete_categories_handler)
    app.router.add_get("/api/games/{game_id}/categories", categories_handler)
    app.router.add_get("/api/products", products_handler)
    app.router.add_get("/api/products/{product_id}", product_handler)
    app.router.add_post("/api/orders", orders_handler)
    app.router.add_post("/api/favorites", favorites_handler)
    app.router.add_post("/api/favorites/add", favorite_add_handler)
    app.router.add_post("/api/favorites/remove", favorite_remove_handler)
    app.router.add_post("/api/referral", referral_handler)
    app.router.add_post("/api/loyalty", loyalty_handler)
    app.router.add_post("/api/loyalty/redeem", loyalty_redeem_handler)
    app.router.add_post("/api/loyalty/redeem/cancel", loyalty_redeem_cancel_handler)
    app.router.add_post("/api/promo/validate", promo_validate_handler)
    app.router.add_post("/api/promo/validate/", promo_validate_handler)
    app.router.add_get("/api/promo-codes", promo_shop_handler)
    app.router.add_get("/api/promo-codes/", promo_shop_handler)
    app.router.add_post("/api/promo-codes/purchase", promo_purchase_handler)
    app.router.add_get("/api/my-promo-codes", my_promo_codes_handler)
    app.router.add_get("/api/my-promo-codes/", my_promo_codes_handler)
    app.router.add_get("/api/admin/promo-codes", admin_promo_codes_handler)
    app.router.add_post("/api/admin/promo-codes", admin_create_promo_code_handler)
    app.router.add_put("/api/admin/promo-codes/{promo_id}", admin_update_promo_code_handler)
    app.router.add_patch("/api/admin/promo-codes/{promo_id}", admin_update_promo_code_handler)
    app.router.add_get("/api/admin/promo-codes/{promo_id}/allowed-users", admin_promo_allowed_users_handler)
    app.router.add_post("/api/admin/promo-codes/{promo_id}/toggle", admin_toggle_promo_code_handler)
    app.router.add_post("/api/admin/promo-codes/bulk-delete", admin_bulk_delete_promo_codes_handler)
    # Backward-compatible aliases used by the older Admin bundle.
    app.router.add_get("/api/admin/store-status", admin_store_status_handler)
    app.router.add_post("/api/admin/store-status", admin_store_status_update_handler)
    app.router.add_post("/api/orders/{order_id}", order_details_handler)
    app.router.add_get("/api/orders/{order_id}", order_details_handler)
    app.router.add_post("/api/orders/{order_id}/cancel", cancel_order_handler)
    app.router.add_post("/api/orders/{order_id}/delivery/retry", order_delivery_retry_handler)
    app.router.add_delete("/api/orders/{order_id}", delete_my_order_handler)
    app.router.add_post("/api/cart/get", cart_get_handler)
    app.router.add_post("/api/cart/add", cart_add_handler)
    app.router.add_post("/api/cart/update", cart_update_handler)
    app.router.add_post("/api/cart/remove", cart_remove_handler)
    app.router.add_post("/api/cart/checkout", cart_checkout_handler)
    app.router.add_post("/api/purchase/{product_id}", purchase_product_handler)
    app.router.add_post("/api/tickets", tickets_handler)
    app.router.add_post("/api/tickets/create", ticket_create_handler)
    app.router.add_post("/api/tickets/{ticket_id}", ticket_detail_handler)
    app.router.add_post("/api/tickets/{ticket_id}/reply", ticket_reply_handler)
    app.router.add_post("/api/admin/broadcasts",admin_broadcast_handler) 
    app.router.add_get("/api/admin/notes", admin_notes_handler)
    app.router.add_post("/api/admin/notes", admin_create_note_handler)
    app.router.add_post("/api/admin/notes/{note_id}", admin_update_note_handler)
    app.router.add_delete("/api/admin/notes/{note_id}", admin_delete_note_handler)
    app.router.add_post("/api/notifications", notifications_handler)
    app.router.add_post("/api/notifications/{notification_id}/read", notification_read_handler)
    app.router.add_post("/api/notifications/read-all", notifications_read_all_handler)
    app.router.add_post("/api/products/{product_id}/stock-alert", stock_alert_add_handler)
    app.router.add_delete("/api/products/{product_id}/stock-alert", stock_alert_remove_handler)
    app.router.add_get("/api/products/{product_id}/stock-alert", stock_alert_status_handler)
    app.router.add_get("/api/products/{product_id}/reviews", product_reviews_handler)
    app.router.add_post("/api/products/{product_id}/reviews", product_review_create_handler)
    app.router.add_post("/api/reviews/{review_id}/cancel", product_review_payment_cancel_handler)
    app.router.add_post("/api/reviews/{review_id}/vote", review_vote_handler)
    app.router.add_post("/api/feedback", feedback_create_handler)
    app.router.add_get("/api/giveaways", giveaways_handler)
    app.router.add_post("/api/giveaways/{giveaway_id}/join", giveaway_join_handler)
    app.router.add_get("/api/content", public_content_handler)
    app.router.add_get("/api/admin/feedback", feedback_admin_handler)
    app.router.add_get("/api/admin/content", admin_content_get_handler)
    app.router.add_put("/api/admin/content", admin_content_update_handler)
    app.router.add_get("/api/admin/referral-settings", admin_referral_settings_get_handler)
    app.router.add_put("/api/admin/referral-settings", admin_referral_settings_update_handler)
    app.router.add_post("/api/admin/test-order", admin_test_order_handler)

    app.router.add_get("/api/admin/tickets",admin_list_tickets_handler,)
    app.router.add_get("/api/admin/tickets/{ticket_id}",admin_ticket_detail_handler,)
    app.router.add_delete("/api/admin/tickets/{ticket_id}",admin_ticket_delete_handler,)
    app.router.add_post("/api/admin/tickets/{ticket_id}/message",admin_ticket_message_handler,)
    app.router.add_post("/api/admin/tickets/{ticket_id}/status",admin_ticket_status_handler,
)
