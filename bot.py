import asyncio
import logging
import aiohttp
import html
import os
import json
import re
import secrets
from datetime import datetime, timezone
from urllib.parse import quote
from pathlib import Path

from aiohttp import web
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    InputMediaPhoto,
    CallbackQuery,
    WebAppInfo,
    Update,
    BotCommand,
)

import config
from database import (
    init_db,
    close_db,
    get_or_create_user,
    get_pending_order_for_user,
    validate_pending_order,
    expire_stale_pending_orders,
    expire_stale_pending_reviews,
    notify_stock_waiters,
    remove_stock_alert_by_id,
    complete_paid_order,
    mark_order_delivered,
    register_referral,
    get_loyalty_balance,
    create_notification,
    get_all_products,
    get_all_active_telegram_users,
    create_pending_order,
    cancel_pending_order,
    fetch_one,
    fetch_all,
    admin_list_tickets,
    admin_count_tickets,
    admin_delete_ticket,
    admin_get_ticket,
    admin_add_ticket_message,
    admin_update_ticket_status,
    create_promo_code,
    list_promo_codes,
    toggle_promo_code,
    update_promo_code,
    create_game,
    create_category,
    create_product,
    get_all_games,
    get_categories,
    get_product,
    get_product_images,
    mark_review_paid,
    get_review_for_payment,
    get_pool,
    get_promo_for_purchase,
    mark_promo_code_sold,
    get_promo_usage_counts,
    get_cart_items,
    add_cart_item,
    update_cart_item,
    remove_cart_item,
    create_pending_order_from_cart,
    complete_free_order,
    begin_order_delivery,
    finish_order_delivery,
    create_feedback,
    list_feedback,
    create_ticket,
    get_user_tickets,
    get_ticket_for_user,
    add_ticket_message,
    get_user_orders,
    get_user_order_details,
    hide_user_order,
    get_user_notifications,
    get_unread_notification_count,
    mark_notification_read,
    mark_all_notifications_read,
    get_referral_stats,
    get_referrals,
    get_loyalty_history,
    create_loyalty_redemption,
    cancel_loyalty_redemption,
    calculate_promo_discount,
    set_promo_allowed_users,
)
from api.store import admin_security_middleware, is_valid_admin_session, setup_store_routes, validate_telegram_init_data, _parse_inventory_preview_input


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("cpm_shop")

bot = Bot(
    token=config.BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)

dp = Dispatcher()


class StoreAccessMiddleware(BaseMiddleware):
    """Block customer Telegram updates when the store is offline or account is blocked."""
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if not user or is_admin_user(user.id):
            return await handler(event, data)
        try:
            row = await fetch_one("SELECT is_blocked FROM users WHERE telegram_id=?", (int(user.id),))
            if row and bool(row.get("is_blocked")):
                if isinstance(event, CallbackQuery):
                    await event.answer("🚫 Your account is blocked. Please contact support.", show_alert=True)
                elif isinstance(event, Message):
                    await event.answer("🚫 <b>Your account is blocked.</b>\n\nYou cannot use CPM SHOP while your account is blocked.")
                return None
            if not await store_enabled():
                if isinstance(event, CallbackQuery):
                    await event.answer("🛠️ Store is currently offline. Please try again later.", show_alert=True)
                elif isinstance(event, Message):
                    await event.answer("🛠️ <b>Store is currently offline.</b>\n\nPlease try again later.")
                return None
        except Exception:
            log.exception("Customer store access check failed")
            if isinstance(event, CallbackQuery):
                await event.answer("⚠️ Store is temporarily unavailable. Please try again.", show_alert=True)
            elif isinstance(event, Message):
                await event.answer("⚠️ <b>Store is temporarily unavailable.</b>\n\nPlease try again later.")
            return None
        return await handler(event, data)



# Per-admin conversation state for Telegram-only management actions.
ADMIN_FLOWS: dict[int, dict] = {}
ADMIN_REVIEW_VIEWS: dict[int, dict] = {}
ADMIN_FEEDBACK_VIEWS: dict[int, dict] = {}
# Per-admin ticket list view: pagination, status filter and search query.
ADMIN_TICKET_VIEWS: dict[int, dict] = {}
# Per-admin user directory page; keeping this state returns from user details to the same page.
ADMIN_USER_VIEWS: dict[int, dict] = {}
ADMIN_INVENTORY_VIEWS: dict[int, dict] = {}
# Customer-facing conversation state. This keeps interactive commands
# separate from the Telegram admin workflows.
USER_FLOWS: dict[int, dict] = {}
USER_CART_PROMOS: dict[int, str] = {}
USER_LOYALTY_REDEMPTIONS: dict[int, int] = {}
# Temporary multi-select state for Telegram admin bulk actions.
ADMIN_BULK_SELECTIONS: dict[int, dict[str, set[int]]] = {}

# Temporary Telegram messages are automatically removed after this period.
# Keep important order/delivery/ticket messages permanent.
TEMP_MESSAGE_TTL = max(10, int(os.getenv("TEMP_MESSAGE_TTL", "45")))
TEMP_MESSAGE_CLEANUP_DELAY = max(1, int(os.getenv("TEMP_MESSAGE_CLEANUP_DELAY", "2")))
_TEMP_MESSAGES: dict[int, set[int]] = {}


async def _delete_message_later(chat_id: int, message_id: int, delay: int = TEMP_MESSAGE_TTL):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        # The message may already be deleted, edited, or outside Telegram's
        # deletion window. Cleanup must never break the main bot flow.
        pass
    finally:
        ids = _TEMP_MESSAGES.get(chat_id)
        if ids:
            ids.discard(message_id)
            if not ids:
                _TEMP_MESSAGES.pop(chat_id, None)


async def send_temporary_message(target, text: str, reply_markup=None, delay: int = TEMP_MESSAGE_TTL):
    """Send a low-value bot message and remove it automatically."""
    sent = await target.answer(text, reply_markup=reply_markup)
    chat_id = int(sent.chat.id)
    message_id = int(sent.message_id)
    _TEMP_MESSAGES.setdefault(chat_id, set()).add(message_id)
    asyncio.create_task(_delete_message_later(chat_id, message_id, delay))
    return sent


async def delete_temporary_message(message: Message):
    """Immediately remove a tracked temporary bot message."""
    if not message or not message.chat:
        return
    chat_id = int(message.chat.id)
    message_id = int(message.message_id)
    ids = _TEMP_MESSAGES.get(chat_id)
    if not ids or message_id not in ids:
        return
    ids.discard(message_id)
    if not ids:
        _TEMP_MESSAGES.pop(chat_id, None)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


async def clear_temporary_messages(chat_id: int):
    """Best-effort cleanup of all tracked temporary bot messages."""
    ids = list(_TEMP_MESSAGES.pop(int(chat_id), set()))
    for message_id in ids:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception:
            pass


async def delete_command_later(message: Message, delay: int = TEMP_MESSAGE_CLEANUP_DELAY):
    """Best-effort cleanup for short-lived command messages."""
    if not message.chat or not message.message_id:
        return
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(
            chat_id=message.chat.id,
            message_id=message.message_id,
        )
    except Exception:
        pass


async def save_user(message: Message):
    if not message.from_user:
        return None

    user = message.from_user
    return await get_or_create_user(
        telegram_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
        language_code=user.language_code,
    )


async def broadcast_giveaway_started(giveaway_id: int):
    """Announce a giveaway only after an admin activates it."""
    giveaway = await fetch_one(
        "SELECT id,title,description,winner_count,status FROM giveaways WHERE id=?",
        (int(giveaway_id),),
    )
    if not giveaway:
        return {"sent": 0, "failed": 0}

    try:
        me = await bot.get_me()
        username = str(me.username or "").strip()
    except Exception:
        username = ""

    deep_link = (
        f"https://t.me/{username}?start=giveaway_{int(giveaway_id)}"
        if username else ""
    )
    share_text = (
        f"🎉 {str(giveaway.get('title') or 'CPM SHOP Giveaway')}\n\n"
        f"{str(giveaway.get('description') or 'Join this CPM SHOP giveaway!')}"
    )
    share_url = (
        "https://t.me/share/url?url="
        + quote(deep_link or config.WEBAPP_URL, safe="")
        + "&text="
        + quote(share_text, safe="")
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🎉 View Giveaway",
            callback_data=f"customer:giveaway:{int(giveaway_id)}",
        )],
        [InlineKeyboardButton(text="📤 Share Giveaway", url=share_url)],
    ])
    text = (
        "🎉 <b>CPM SHOP GIVEAWAY IS LIVE!</b>\n\n"
        f"<b>{html.escape(str(giveaway.get('title') or 'Giveaway'))}</b>\n\n"
        f"{html.escape(str(giveaway.get('description') or ''))}\n\n"
        f"🏆 Winners: <b>{max(1, int(giveaway.get('winner_count') or 1))}</b>\n"
        "🟢 Status: <b>ACTIVE</b>\n\n"
        "Tap below to view the giveaway and join. Good luck!"
    )

    sent = 0
    failed = 0
    users = await get_all_active_telegram_users()
    for user in users:
        try:
            await bot.send_message(
                int(user["telegram_id"]),
                text,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
            sent += 1
        except Exception:
            failed += 1
            log.exception(
                "Giveaway announcement failed | giveaway=%s user=%s",
                giveaway_id,
                user.get("telegram_id"),
            )
        await asyncio.sleep(0.04)
    return {"sent": sent, "failed": failed}


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛍️ Browse Products", callback_data="products_here")],
            [InlineKeyboardButton(
                text="📱 Open Shop",
                web_app=WebAppInfo(url=config.WEBAPP_URL) if config.WEBAPP_URL else None,
                callback_data=None if config.WEBAPP_URL else "shop_unavailable",
            )],
            [
                InlineKeyboardButton(text="🛒 My Cart", callback_data="cart"),
                InlineKeyboardButton(text="🎁 Referral", callback_data="referral"),
            ],
            [
                InlineKeyboardButton(text="⭐ Feedback", callback_data="feedback"),
                InlineKeyboardButton(text="🎉 Giveaways", callback_data="giveaways"),
            ],
            [
                InlineKeyboardButton(text="❓ Help", callback_data="help"),
                InlineKeyboardButton(text="👨‍💼 Contact Admin", callback_data="contact_admin"),
            ],
        ]
    )


@dp.message(CommandStart())
async def start_handler(message: Message):
    await save_user(message)

    referral_added = False
    text = (message.text or "").strip()
    parts = text.split(maxsplit=1)
    if len(parts) == 2:
        payload = parts[1].strip()
        if payload.startswith("giveaway_"):
            try:
                giveaway_id = int(payload.split("_", 1)[1])
                giveaway = await fetch_one(
                    "SELECT id,status FROM giveaways WHERE id=?",
                    (giveaway_id,),
                )
                if giveaway and str(giveaway.get("status") or "").lower() in {"active", "running"}:
                    await _render_customer_giveaways(message, int(message.from_user.id))
                    return
            except (TypeError, ValueError):
                pass
        elif payload.startswith("ref_"):
            raw_referrer = payload[4:].strip()
            try:
                referrer_telegram_id = int(raw_referrer)
                referral_added = await register_referral(
                    referrer_telegram_id=referrer_telegram_id,
                    referred_telegram_id=message.from_user.id,
                )
            except (TypeError, ValueError):
                referral_added = False

    title = await get_setting("welcome_title", f"🛍️ <b>Welcome to {config.SHOP_NAME}!</b>")
    description = await get_setting("welcome_description", "Your professional gaming store.\n\nChoose how you want to browse our products:")
    button1 = await get_setting("welcome_button_1", "🛍️ See Products Here")
    button2 = await get_setting("welcome_button_2", "📱 See Products in Shop")
    image = await get_setting("welcome_image", "")

    welcome = f"{title}\n\n{description}"
    if referral_added:
        welcome += "\n\n🎁 Your referral was registered successfully."
    keyboard = _welcome_keyboard(button1, button2)

    try:
        if image.strip():
            photo = image.strip()
            if photo.startswith("tg:"):
                photo = photo[3:]
            sent = await message.answer_photo(photo=photo, caption=welcome, reply_markup=keyboard)
            _TEMP_MESSAGES.setdefault(int(sent.chat.id), set()).add(int(sent.message_id))
            asyncio.create_task(_delete_message_later(int(sent.chat.id), int(sent.message_id), max(TEMP_MESSAGE_TTL, 120)))
            return
    except Exception:
        log.exception("Welcome image delivery failed; falling back to text")

    await send_temporary_message(message, welcome, reply_markup=keyboard, delay=max(TEMP_MESSAGE_TTL, 120))



@dp.callback_query(F.data == "products_here")
async def products_here_handler(callback):
    await callback.answer()
    await clear_temporary_messages(callback.message.chat.id)
    try:
        products = await get_all_products()
        if not products:
            await send_temporary_message(
                callback.message,
                "🛍️ <b>Products</b>\n\n" + await get_setting("shop_description", "No products are available right now."),
                reply_markup=main_menu(),
            )
            return

        products = products[:20]
        for product in products:
            price = int(product.get("price_stars") or 0)
            stock = int(product.get("stock") or 0)
            status = "✅ In stock" if stock > 0 else "❌ Out of stock"
            keyboard = []
            if stock > 0 and price > 0:
                keyboard.append([
                    InlineKeyboardButton(
                        text=f"⭐ Buy for {price} Stars",
                        callback_data=f"buy:{int(product['id'])}",
                    )
                ])
            if config.WEBAPP_URL:
                keyboard.append([
                    InlineKeyboardButton(
                        text="📱 View in Shop",
                        web_app=WebAppInfo(
                            url=f"{config.WEBAPP_URL}/product/{int(product['id'])}"
                        ),
                    )
                ])
            await send_temporary_message(
                callback.message,
                f"📦 <b>{html.escape(str(product.get('name') or 'Product'))}</b>\n"
                f"🎮 {html.escape(str(product.get('game_name') or 'Gaming'))}\n"
                f"⭐ <b>{price}</b> Stars\n"
                f"{status}",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
            )
    except Exception:
        log.exception("In-chat product catalog failed")
        await send_temporary_message(
            callback.message,
            "❌ Unable to load products right now. Please try again.",
            reply_markup=main_menu(),
        )


@dp.callback_query(F.data.startswith("buy:"))
async def buy_product_here_handler(callback):
    await callback.answer()
    await clear_temporary_messages(callback.message.chat.id)
    if not callback.from_user:
        return

    try:
        product_id = int(callback.data.split(":", 1)[1])
        order = await create_pending_order(
            telegram_id=callback.from_user.id,
            product_id=product_id,
            quantity=1,
        )
        from api.store import create_invoice_link
        invoice_url = await create_invoice_link(
            title=order["name"],
            description=order.get("description") or "CPM SHOP product",
            payload=f"order:{order['order_id']}",
            price_stars=int(order["total_stars"]),
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="💳 Pay with Telegram Stars", url=invoice_url)
        ]])
        await callback.message.answer(
            f"🧾 <b>Order #{order['order_id']}</b>\n\n"
            f"{html.escape(str(order['name']))}\n"
            f"Total: ⭐ <b>{int(order['total_stars'])}</b> Stars\n\n"
            "Complete payment using the button below.",
            reply_markup=keyboard,
        )
    except ValueError as error:
        await send_temporary_message(callback.message, f"❌ {html.escape(str(error))}")
    except Exception:
        log.exception("In-chat purchase failed")
        try:
            if "order" in locals() and order.get("order_id"):
                await cancel_pending_order(order["order_id"], callback.from_user.id)
        except Exception:
            pass
        await send_temporary_message(
            callback.message,
            "❌ Unable to create the order right now. Please try again.",
        )


@dp.pre_checkout_query()
async def pre_checkout_handler(pre_checkout_query):
    try:
        payload = str(pre_checkout_query.invoice_payload or "")
        if payload.startswith("review:"):
            try:
                review_id = int(payload.split(":", 1)[1])
            except (TypeError, ValueError):
                await pre_checkout_query.answer(ok=False, error_message="Invalid review payment.")
                return

            review = await fetch_one(
                """
                SELECT r.id, r.price_stars, r.payment_status, r.visible, u.telegram_id
                FROM reviews r
                JOIN users u ON u.id=r.user_id
                WHERE r.id=? AND u.telegram_id=?
                """,
                (review_id, pre_checkout_query.from_user.id),
            )
            if (
                not review
                or str(review.get("payment_status")) != "pending_payment"
                or int(review.get("price_stars") or 0) != int(pre_checkout_query.total_amount)
                or str(pre_checkout_query.currency) != "XTR"
            ):
                await pre_checkout_query.answer(ok=False, error_message="This review payment is no longer available.")
                return

            await pre_checkout_query.answer(ok=True)
            return

        if payload.startswith("promo:"):
            try:
                promo_id = int(payload.split(":", 1)[1])
            except (TypeError, ValueError):
                await pre_checkout_query.answer(ok=False, error_message="Invalid promo code purchase.")
                return
            promo = await get_promo_for_purchase(promo_id, pre_checkout_query.from_user.id)
            if (not promo or str(pre_checkout_query.currency) != "XTR" or
                int(promo.get("sale_price_stars") or 0) != int(pre_checkout_query.total_amount)):
                await pre_checkout_query.answer(ok=False, error_message="This promo code is no longer available.")
                return
            await pre_checkout_query.answer(ok=True)
            return

        if not payload.startswith("order:"):
            await pre_checkout_query.answer(
                ok=False,
                error_message="Invalid order.",
            )
            return

        try:
            order_id = int(payload.split(":", 1)[1])
        except (TypeError, ValueError):
            await pre_checkout_query.answer(
                ok=False,
                error_message="Invalid order.",
            )
            return

        user_id = pre_checkout_query.from_user.id

        valid = await validate_pending_order(
            order_id=order_id,
            telegram_id=user_id,
            total_stars=int(pre_checkout_query.total_amount),
            currency=str(pre_checkout_query.currency),
        )

        if not valid:
            await pre_checkout_query.answer(
                ok=False,
                error_message="This order is no longer available.",
            )
            return

        await pre_checkout_query.answer(ok=True)

    except Exception:
        log.exception("Pre-checkout validation failed")
        try:
            await pre_checkout_query.answer(
                ok=False,
                error_message="Unable to validate the order.",
            )
        except Exception:
            pass


async def refund_stars(telegram_user_id: int, charge_id: str):
    url = (
        "https://api.telegram.org/"
        f"bot{config.BOT_TOKEN}/refundStarPayment"
    )

    data = {
        "user_id": int(telegram_user_id),
        "telegram_payment_charge_id": str(charge_id),
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(
            url,
            json=data,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as response:
            result = await response.json()

            if not result.get("ok"):
                raise RuntimeError(
                    result.get(
                        "description",
                        "Telegram refund failed.",
                    )
                )

            return True


async def _deliver_order_to_telegram(telegram_id: int, order_id: int):
    """Deliver a committed order with DB-backed attempt locking and retry state."""
    claim = await begin_order_delivery(order_id, telegram_id)
    if not claim:
        return {"status": "missing", "error": "Order delivery was not found."}
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
        await bot.send_message(int(telegram_id), text)
    except Exception as exc:
        error = str(exc) or "Telegram delivery failed."
        await finish_order_delivery(order_id, telegram_id, False, error)
        log.exception("Order delivery failed | user=%s | order=%s", telegram_id, order_id)
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

    if not await finish_order_delivery(order_id, telegram_id, True):
        log.error("Telegram delivery succeeded but DB status update failed | order=%s", order_id)
        return {"status": "delivered_unconfirmed", "items": items}
    return {"status": "delivered", "items": items, "attempts": claim.get("attempts", 0)}


@dp.message(F.successful_payment)
async def successful_payment_handler(message: Message):
    payment = message.successful_payment

    if not payment or not message.from_user:
        return

    payload = str(payment.invoice_payload or "")

    telegram_id = message.from_user.id
    charge_id = str(payment.telegram_payment_charge_id)

    if payload.startswith("review:"):
        try:
            review_id = int(payload.split(":", 1)[1])
            if str(payment.currency) != "XTR" or int(payment.total_amount) <= 0:
                raise ValueError("Invalid review payment.")

            review_state = await get_review_for_payment(review_id, telegram_id)
            if not review_state:
                raise ValueError("Review payment is no longer available.")

            expected_amount = int(review_state.get("price_stars") or 0)
            if expected_amount <= 0 or expected_amount != int(payment.total_amount):
                raise ValueError("The paid review amount does not match the invoice.")

            existing_status = str(review_state.get("payment_status") or "")
            existing_charge = str(review_state.get("telegram_payment_charge_id") or "")
            if existing_status == "paid":
                if existing_charge and existing_charge == charge_id:
                    await message.answer("✅ <b>Your paid review is already published.</b>")
                    return
                raise ValueError("This review has already been paid with another transaction.")
            if existing_status != "pending_payment":
                raise ValueError("Review payment is no longer pending.")

            review = await mark_review_paid(review_id, telegram_id, charge_id)
            if not review:
                review_state = await get_review_for_payment(review_id, telegram_id)
                if (
                    review_state
                    and str(review_state.get("payment_status")) == "paid"
                    and str(review_state.get("telegram_payment_charge_id") or "") == charge_id
                ):
                    await message.answer("✅ <b>Your paid review is already published.</b>")
                    return
                raise ValueError("Review payment is no longer pending.")
        except Exception:
            log.exception("Paid review payment processing failed | user=%s | payload=%s", telegram_id, payload)
            try:
                await refund_stars(telegram_id, charge_id)
                await message.answer("↩️ The paid review payment has been refunded automatically.")
            except Exception:
                log.exception("Automatic paid-review refund failed | user=%s", telegram_id)
                await message.answer(
                    "⚠️ Your payment was received, but the review could not be published automatically. "
                    "Please contact support with your payment details."
                )
            return

        # The review status is committed at this point.  A Telegram message
        # failure must not turn a successfully processed payment into a refund.
        try:
            await message.answer(
                "✅ <b>Paid review published successfully.</b>\n\n"
                "Your review is now visible on the product page."
            )
        except Exception:
            log.exception("Paid review success message failed | user=%s | review=%s", telegram_id, review_id)
        log.info(
            "Paid review completed | user=%s | review=%s | charge_id=%s",
            telegram_id,
            review_id,
            charge_id,
        )
        return

    if payload.startswith("promo:"):
        try:
            promo_id = int(payload.split(":", 1)[1])
            if str(payment.currency) != "XTR" or int(payment.total_amount) <= 0:
                raise ValueError("Invalid promo payment.")

            # Successful-payment updates should be safe to process more than once.
            # If this exact charge was already committed for this user/promo, reuse
            # the existing sale record instead of treating it as a fresh purchase.
            promo = await get_promo_for_purchase(promo_id, telegram_id)
            if promo:
                if int(promo.get("sale_price_stars") or 0) != int(payment.total_amount):
                    raise ValueError("This promo code is no longer available.")
                sold = await mark_promo_code_sold(promo_id, telegram_id, charge_id)
                if not sold:
                    raise ValueError("This promo code was already sold.")
            else:
                existing = await fetch_one(
                    """SELECT p.*
                       FROM promo_codes p
                       JOIN users u ON u.id = p.sold_to_user_id
                       WHERE p.id=? AND u.telegram_id=?
                         AND p.telegram_payment_charge_id=?
                       LIMIT 1""",
                    (promo_id, telegram_id, str(charge_id)),
                )
                if not existing or int(existing.get("sale_price_stars") or 0) != int(payment.total_amount):
                    raise ValueError("This promo code is no longer available.")
                sold = existing
        except Exception:
            log.exception("Promo code payment processing failed | user=%s | payload=%s", telegram_id, payload)
            try:
                await refund_stars(telegram_id, charge_id)
                await message.answer("↩️ The promo payment could not be completed and was refunded automatically.")
            except Exception:
                log.exception("Automatic promo refund failed | user=%s", telegram_id)
                await message.answer("⚠️ Payment was received but the promo code could not be delivered. Please contact support.")
            return

        # The promo sale is committed at this point.  Sending the receipt is a
        # post-payment operation and must never cause an automatic refund.
        code = str(sold.get("code") or "").upper()
        target = _promo_target_text(sold)
        try:
            await message.answer(
                "✅ <b>Promo code purchased successfully!</b>\n\n"
                f"🎟️ <b>Code:</b> <code>{html.escape(code)}</code>\n"
                f"🎯 <b>Target:</b> {html.escape(target)}\n"
                f"💳 <b>Price:</b> ⭐ {int(sold.get('sale_price_stars') or 0)} Stars\n\n"
                "You can now use this code at checkout."
            )
        except Exception:
            log.exception("Promo purchase receipt delivery failed | user=%s | promo=%s", telegram_id, promo_id)
        return

    if not payload.startswith("order:"):
        await message.answer(
            "❌ Invalid payment order. Please contact support."
        )
        return

    try:
        order_id = int(payload.split(":", 1)[1])
    except (TypeError, ValueError):
        await message.answer(
            "❌ Invalid payment order. Please contact support."
        )
        return

    try:
        # The database transaction either completes the payment atomically or
        # rolls it back.  A successful return means the Stars charge is now
        # committed; delivery failures below must therefore NOT trigger an
        # automatic refund.
        result = await complete_paid_order(
            order_id=order_id,
            telegram_id=telegram_id,
            total_stars=int(payment.total_amount),
            currency=str(payment.currency),
            telegram_payment_charge_id=charge_id,
            provider_payment_charge_id=(
                str(payment.provider_payment_charge_id)
                if payment.provider_payment_charge_id
                else None
            ),
        )
    except ValueError as error:
        log.warning(
            "Payment validation failed | user=%s | order=%s | error=%s",
            telegram_id,
            order_id,
            error,
        )
        await message.answer(
            "❌ <b>Payment received, but the order could not be completed automatically.</b>\n\n"
            "Please contact support with your order number:\n"
            f"<b>#{order_id}</b>"
        )
        try:
            await refund_stars(telegram_id, charge_id)
            await message.answer("↩️ The payment has been refunded automatically.")
        except Exception:
            log.exception(
                "Automatic refund failed | user=%s | order=%s",
                telegram_id,
                order_id,
            )
        return
    except Exception:
        log.exception(
            "Order completion failed | user=%s | order=%s",
            telegram_id,
            order_id,
        )
        await message.answer(
            "⚠️ <b>Payment received, but there was a problem completing your order.</b>\n\n"
            "Please contact support and provide this order number:\n"
            f"<b>#{order_id}</b>"
        )
        try:
            await refund_stars(telegram_id, charge_id)
            await message.answer("↩️ The payment has been refunded automatically.")
        except Exception:
            log.exception(
                "Automatic refund failed | user=%s | order=%s",
                telegram_id,
                order_id,
            )
        return

    if result.get("duplicate_payment"):
        log.warning(
            "Duplicate successful payment detected | user=%s | order=%s | charge_id=%s",
            telegram_id,
            order_id,
            charge_id,
        )
        try:
            await refund_stars(telegram_id, charge_id)
            await message.answer(
                "↩️ <b>This payment was received after the order had already been paid.</b>\n\n"
                "The duplicate payment has been refunded automatically."
            )
        except Exception:
            log.exception(
                "Automatic duplicate-payment refund failed | user=%s | order=%s | charge_id=%s",
                telegram_id,
                order_id,
                charge_id,
            )
            await message.answer(
                "⚠️ A duplicate payment was received, but the automatic refund could not be completed. "
                "Please contact support with your order number:\n"
                f"<b>#{order_id}</b>"
            )
        return

    try:
        await create_notification(
            telegram_id,
            f"Payment for order #{order_id} was received successfully. Your order is being processed.",
            title="Payment Successful",
            kind="order",
            reference_id=order_id,
        )
    except Exception:
        log.exception("Failed to create payment notification | order=%s", order_id)

    delivery_result = await _deliver_order_to_telegram(telegram_id, order_id)
    if delivery_result.get("status") == "busy":
        await message.answer("⏳ <b>Your delivery is already being processed.</b>\n\n" f"Order <b>#{order_id}</b>")
    elif delivery_result.get("status") == "failed":
        await message.answer("⚠️ <b>Payment was successful, but delivery is pending.</b>\n\n" f"Order <b>#{order_id}</b> is safe and was not refunded. Use My Orders to retry delivery or contact support.")
    elif delivery_result.get("status") == "missing":
        await message.answer("⚠️ <b>Payment was received, but the delivery record could not be found.</b>\n\n" f"Please contact support with order <b>#{order_id}</b>.")
    elif delivery_result.get("status") == "unavailable":
        await message.answer("⚠️ <b>This order is not currently deliverable.</b>\n\n" f"Please contact support with order <b>#{order_id}</b>.")
    elif delivery_result.get("status") == "delivered_unconfirmed":
        await message.answer("✅ <b>Delivery message was sent.</b>\n\n" f"Order <b>#{order_id}</b> may still show as processing. Please refresh My Orders.")
    log.info(
        "Payment completed | user=%s | order=%s | charge_id=%s | total=%s",
        telegram_id,
        order_id,
        charge_id,
        payment.total_amount,
    )


@dp.callback_query(F.data == "cart")
async def cart_handler(callback):
    await callback.answer()
    if callback.from_user:
        await _render_customer_cart(callback.message, int(callback.from_user.id))


@dp.message(Command("cart"))
async def cart_command(message: Message):
    asyncio.create_task(delete_command_later(message))
    await _render_customer_cart(message, int(message.from_user.id))


@dp.callback_query(F.data == "referral")
async def referral_handler(callback):
    await callback.answer()
    if callback.message:
        await send_referral_message(callback.message)


@dp.message(Command("referral"))
async def referral_command(message: Message):
    asyncio.create_task(delete_command_later(message))
    await send_referral_message(message)


def admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Dashboard", callback_data="admin:dashboard"),
             InlineKeyboardButton(text="🎫 Tickets", callback_data="admin:tickets")],
            [InlineKeyboardButton(text="🛒 Orders", callback_data="admin:orders"),
             InlineKeyboardButton(text="📦 Products", callback_data="admin:products")],
            [InlineKeyboardButton(text="📋 Inventory", callback_data="admin:inventory"),
             InlineKeyboardButton(text="👥 Users", callback_data="admin:users")],
            [InlineKeyboardButton(text="🏷️ Promo Codes", callback_data="admin:promos"),
             InlineKeyboardButton(text="📢 Broadcast", callback_data="admin:broadcast")],
            [InlineKeyboardButton(text="🎮 Games", callback_data="admin:games"),
             InlineKeyboardButton(text="🗂️ Categories", callback_data="admin:categories")],
            [InlineKeyboardButton(text="💳 Payments", callback_data="admin:payments"),
             InlineKeyboardButton(text="🎉 Giveaways", callback_data="admin:giveaways")],
            [InlineKeyboardButton(text="🟢 Store ON", callback_data="admin:store:on"),
             InlineKeyboardButton(text="🔴 Store OFF", callback_data="admin:store:off")],
            [InlineKeyboardButton(text="🌐 Web Admin", callback_data="admin:web")],
            [InlineKeyboardButton(text="⚙️ More Admin Tools", callback_data="admin:more")],
        ]
    )


def is_admin_user(user_id: int | None) -> bool:
    return bool(user_id and int(user_id) == int(config.ADMIN_ID))


async def require_admin_callback(callback: CallbackQuery) -> bool:
    if not callback.from_user or not is_admin_user(callback.from_user.id):
        await callback.answer("Not authorized.", show_alert=True)
        return False
    return True


def admin_back_keyboard(target="admin:dashboard"):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Back", callback_data=target)]])

def admin_web_url(path="/admin"):
    base = (config.WEBAPP_URL or "").rstrip("/")
    return f"{base}{path}" if base else path


async def get_setting(key: str, default: str = "") -> str:
    row = await fetch_one("SELECT value FROM settings WHERE key=?", (key,))
    return str(row.get("value")) if row and row.get("value") is not None else default


async def set_setting(key: str, value: str):
    await fetch_one(
        """INSERT INTO settings(key,value) VALUES(?,?)
           ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value
           RETURNING key""",
        (key, value),
    )


async def store_enabled() -> bool:
    return (await get_setting("maintenance_mode", "0")) != "1"


dp.message.outer_middleware(StoreAccessMiddleware())
dp.callback_query.outer_middleware(StoreAccessMiddleware())


async def admin_log(action: str, admin_id: int, details: str = ""):
    try:
        await fetch_one(
            "INSERT INTO admin_logs(action,admin_id,details) VALUES(?,?,?) RETURNING id",
            (action, admin_id, details),
        )
    except Exception:
        log.exception("Failed to write admin log")


async def admin_store_gate(update: Update) -> bool:
    """Block customer updates while maintenance mode is enabled or user is blocked."""
    actor = None
    if update.message and update.message.from_user:
        actor = update.message.from_user
    elif update.callback_query and update.callback_query.from_user:
        actor = update.callback_query.from_user
    if not actor:
        return True
    if is_admin_user(actor.id):
        return True
    try:
        row = await fetch_one("SELECT is_blocked FROM users WHERE telegram_id=?", (actor.id,))
        if row and bool(row.get("is_blocked")):
            if update.callback_query:
                await bot.answer_callback_query(update.callback_query.id, text="Your account is blocked.", show_alert=True)
            elif update.message:
                await send_temporary_message(actor, "🚫 Your account is blocked. Please contact support.")
            return False
        if not await store_enabled():
            msg = "🛠️ <b>CPM SHOP is temporarily offline</b>\n\nPlease try again later."
            if update.callback_query:
                await bot.answer_callback_query(update.callback_query.id, text="Store is temporarily offline.", show_alert=True)
            elif update.message:
                await send_temporary_message(actor, msg)
            return False
    except Exception:
        log.exception("Store gate failed")
    return True


async def admin_dashboard_text() -> str:
    row = await fetch_one(
        """
        SELECT
            (SELECT COUNT(*) FROM users) AS users,
            (SELECT COUNT(*) FROM products WHERE active = TRUE) AS products,
            (SELECT COUNT(*) FROM orders) AS orders,
            (SELECT COUNT(*) FROM payments WHERE status = 'paid') AS paid_payments,
            COALESCE((SELECT SUM(amount_stars) FROM payments WHERE status = 'paid'), 0) AS revenue_stars,
            (SELECT COUNT(*) FROM tickets WHERE status IN ('open','in_progress')) AS active_tickets,
            (SELECT COUNT(*) FROM inventory_items WHERE status = 'available') AS inventory_available,
            (SELECT COUNT(*) FROM promo_codes WHERE active = TRUE) AS active_promos
        """
    ) or {}
    return (
        "📊 <b>CPM SHOP ADMIN</b>\n\n"
        f"👥 Users: <b>{int(row.get('users') or 0)}</b>\n"
        f"📦 Active Products: <b>{int(row.get('products') or 0)}</b>\n"
        f"🛒 Orders: <b>{int(row.get('orders') or 0)}</b>\n"
        f"💳 Paid Payments: <b>{int(row.get('paid_payments') or 0)}</b>\n"
        f"⭐ Revenue: <b>{int(row.get('revenue_stars') or 0)}</b> Stars\n"
        f"🎫 Open Tickets: <b>{int(row.get('active_tickets') or 0)}</b>\n"
        f"📋 Available Inventory: <b>{int(row.get('inventory_available') or 0)}</b>\n"
        f"🏷️ Active Promos: <b>{int(row.get('active_promos') or 0)}</b>\n\n"
        "Use the buttons below to manage the whole store from Telegram."
    )


@dp.callback_query(F.data == "admin:dashboard")
async def admin_dashboard_callback(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer()
    text = await admin_dashboard_text()
    try:
        await callback.message.edit_text(text, reply_markup=admin_menu())
    except Exception:
        await callback.message.answer(text, reply_markup=admin_menu())


async def render_admin_tickets(callback, page=None, status=None, search=None):
    """Render an organized, paginated Telegram ticket inbox."""
    uid = int(callback.from_user.id)
    view = ADMIN_TICKET_VIEWS.setdefault(uid, {"page": 0, "status": "all", "search": ""})
    if page is not None:
        view["page"] = max(0, int(page))
    if status is not None:
        value = str(status).strip().lower()
        view["status"] = value if value in {"all", "open", "in_progress", "resolved", "closed"} else "all"
    if search is not None:
        view["search"] = str(search).strip()[:80]

    page_size = 8
    active_status = view["status"] if view["status"] != "all" else None
    search_text = view["search"]
    total = await admin_count_tickets(status=active_status, search=search_text)
    max_page = max(0, (total - 1) // page_size)
    view["page"] = min(view["page"], max_page)
    offset = view["page"] * page_size
    tickets = await admin_list_tickets(status=active_status, search=search_text, limit=page_size, offset=offset)

    status_label = {
        "all": "All",
        "open": "Open",
        "in_progress": "In Progress",
        "resolved": "Resolved",
        "closed": "Closed",
    }[view["status"]]
    header = [
        "🎫 <b>Support Tickets</b>",
        f"Filter: <b>{status_label}</b>" + (f" · Search: <code>{html.escape(search_text)}</code>" if search_text else ""),
        f"Showing {offset + 1 if total else 0}-{min(offset + page_size, total)} of {total}",
        "",
    ]

    if not tickets:
        header.append("No tickets match the current filter.")
    else:
        for t in tickets:
            tid = int(t["id"])
            subject = str(t.get("subject") or "Untitled").replace("\n", " ")[:55]
            status = str(t.get("status") or "open")
            customer = str(t.get("first_name") or ("@" + str(t.get("username")) if t.get("username") else "") or t.get("telegram_id") or "Customer")[:25]
            message_count = int(t.get("message_count") or 0)
            icon = {"open": "🟢", "in_progress": "🟡", "resolved": "✅", "closed": "🔒"}.get(status, "🎫")
            header.append(f"{icon} <b>#{tid}</b> · {html.escape(subject)}\n   👤 {html.escape(customer)} · 💬 {message_count} · <i>{html.escape(status.replace('_', ' ').title())}</i>")

    buttons = []
    for t in tickets:
        tid = int(t["id"])
        subject = str(t.get("subject") or "Untitled").replace("\n", " ")[:34]
        icon = {"open": "🟢", "in_progress": "🟡", "resolved": "✅", "closed": "🔒"}.get(str(t.get("status") or "open"), "🎫")
        buttons.append([InlineKeyboardButton(text=f"{icon} #{tid} · {subject}", callback_data=f"admin:ticket:{tid}")])

    prev_cb = f"admin:tickets:page:{max(0, view['page'] - 1)}"
    next_cb = f"admin:tickets:page:{min(max_page, view['page'] + 1)}"
    nav = []
    if view["page"] > 0:
        nav.append(InlineKeyboardButton(text="◀️ Previous", callback_data=prev_cb))
    if view["page"] < max_page:
        nav.append(InlineKeyboardButton(text="Next ▶️", callback_data=next_cb))
    if nav:
        buttons.append(nav)

    buttons.append([
        InlineKeyboardButton(text="🔍 Search", callback_data="admin:tickets:search"),
        InlineKeyboardButton(text="🧹 Clear", callback_data="admin:tickets:clear"),
    ])
    buttons.append([
        InlineKeyboardButton(text="🟢 Open", callback_data="admin:tickets:filter:open"),
        InlineKeyboardButton(text="🟡 In Progress", callback_data="admin:tickets:filter:in_progress"),
    ])
    buttons.append([
        InlineKeyboardButton(text="✅ Resolved", callback_data="admin:tickets:filter:resolved"),
        InlineKeyboardButton(text="🔒 Closed", callback_data="admin:tickets:filter:closed"),
    ])
    buttons.append([
        InlineKeyboardButton(text="📋 All", callback_data="admin:tickets:filter:all"),
        InlineKeyboardButton(text="🔄 Refresh", callback_data="admin:tickets:refresh"),
    ])
    buttons.append([InlineKeyboardButton(text="◀️ Back", callback_data="admin:dashboard")])
    return "\n".join(header), InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.callback_query(F.data == "admin:tickets")
async def admin_tickets_callback(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer()
    try:
        ADMIN_TICKET_VIEWS[int(callback.from_user.id)] = {"page": 0, "status": "all", "search": ""}
        text, kb = await render_admin_tickets(callback)
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        log.exception("Admin ticket list failed")
        await callback.message.edit_text("🎫 <b>Support Tickets</b>\n\n❌ Unable to load tickets.", reply_markup=admin_menu())


@dp.callback_query(F.data.startswith("admin:tickets:page:"))
async def admin_tickets_page_callback(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer()
    try:
        page = int(callback.data.rsplit(":", 1)[1])
        text, kb = await render_admin_tickets(callback, page=page)
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception as e:
        await callback.answer(str(e), show_alert=True)


@dp.callback_query(F.data.startswith("admin:tickets:filter:"))
async def admin_tickets_filter_callback(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer()
    try:
        value = callback.data.rsplit(":", 1)[1]
        text, kb = await render_admin_tickets(callback, page=0, status=value)
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        log.exception("Admin ticket filter failed")
        await callback.answer("Unable to filter tickets.", show_alert=True)


@dp.callback_query(F.data == "admin:tickets:refresh")
async def admin_tickets_refresh_callback(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer()
    text, kb = await render_admin_tickets(callback)
    await callback.message.edit_text(text, reply_markup=kb)


@dp.callback_query(F.data == "admin:tickets:clear")
async def admin_tickets_clear_callback(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer("Filters cleared")
    text, kb = await render_admin_tickets(callback, page=0, status="all", search="")
    await callback.message.edit_text(text, reply_markup=kb)


@dp.callback_query(F.data == "admin:tickets:search")
async def admin_tickets_search_callback(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer()
    ADMIN_FLOWS[int(callback.from_user.id)] = {"type": "ticket_search", "step": 1}
    await callback.message.answer(
        "🔍 <b>Search Tickets</b>\n\n"
        "Send a ticket ID, subject, customer username/name, Telegram ID, or category.\n\n"
        "/canceladmin to cancel."
    )


@dp.callback_query(F.data.startswith("admin:ticket:"))
async def admin_ticket_detail_callback(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer()
    try:
        tid=int(callback.data.rsplit(":",1)[1])
        t=await admin_get_ticket(tid)
        if not t:
            raise ValueError("Ticket not found")
        text=(f"🎫 <b>Ticket #{tid}</b>\n\n<b>{html.escape(str(t.get('subject') or 'Untitled'))}</b>\n"
              f"👤 {html.escape(str(t.get('first_name') or ('@'+str(t.get('username')) if t.get('username') else '') or t.get('telegram_id') or 'Customer'))}\n"
              f"🏷️ {html.escape(str(t.get('category') or 'general'))} · ⚡ {html.escape(str(t.get('priority') or 'normal'))}\n"
              f"📌 Status: <b>{html.escape(str(t.get('status') or ''))}</b>\n\n")
        for m in t.get("messages") or []:
            author="Support" if not m.get("telegram_id") else "Customer"
            text += f"<b>{author}</b> · {html.escape(str(m.get('created_at') or ''))}\n{html.escape(str(m.get('message') or ''))}\n\n"
        kb=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Reply", callback_data=f"admin:ticketreply:{tid}")],
            [InlineKeyboardButton(text="🟢 Open", callback_data=f"admin:ticketstatus:{tid}:open"), InlineKeyboardButton(text="🟡 In Progress", callback_data=f"admin:ticketstatus:{tid}:in_progress")],
            [InlineKeyboardButton(text="✅ Resolved", callback_data=f"admin:ticketstatus:{tid}:resolved"), InlineKeyboardButton(text="🔒 Closed", callback_data=f"admin:ticketstatus:{tid}:closed")],
            [InlineKeyboardButton(text="🗑️ Delete Ticket", callback_data=f"admin:ticketdeleteconfirm:{tid}")],
            [InlineKeyboardButton(text="◀️ Tickets", callback_data="admin:tickets")],
        ])
        await callback.message.edit_text(text[:3900], reply_markup=kb)
    except Exception as e:
        await callback.message.edit_text(f"❌ {html.escape(str(e))}", reply_markup=admin_back_keyboard("admin:tickets"))


@dp.callback_query(F.data.startswith("admin:ticketdeleteconfirm:"))
async def admin_ticket_delete_confirm(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer()
    tid = int(callback.data.rsplit(":", 1)[1])
    t = await admin_get_ticket(tid)
    if not t:
        await callback.message.edit_text("❌ Ticket not found.", reply_markup=admin_back_keyboard("admin:tickets"))
        return
    subject = str(t.get("subject") or "Untitled")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Yes, Delete", callback_data=f"admin:ticketdelete:{tid}"), InlineKeyboardButton(text="❌ Cancel", callback_data=f"admin:ticket:{tid}")],
    ])
    await callback.message.edit_text(
        f"⚠️ <b>Delete Ticket #{tid}?</b>\n\n<b>{html.escape(subject[:160])}</b>\n\nAll messages in this ticket will also be deleted. This cannot be undone.",
        reply_markup=kb,
    )


@dp.callback_query(F.data.startswith("admin:ticketdelete:"))
async def admin_ticket_delete(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer()
    tid = int(callback.data.rsplit(":", 1)[1])
    row = await admin_delete_ticket(tid)
    if not row:
        await callback.message.edit_text("❌ Ticket not found.", reply_markup=admin_back_keyboard("admin:tickets"))
        return
    text, kb = await render_admin_tickets(callback, page=ADMIN_TICKET_VIEWS.get(int(callback.from_user.id), {}).get("page", 0))
    await callback.message.edit_text(f"✅ Ticket #{tid} deleted.\n\n{text}", reply_markup=kb)


@dp.callback_query(F.data.startswith("admin:ticketreply:"))
async def admin_ticket_reply_start(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer()
    tid=int(callback.data.rsplit(":",1)[1])
    ADMIN_FLOWS[callback.from_user.id]={"type":"ticket_reply","ticket_id":tid}
    view = ADMIN_TICKET_VIEWS.get(int(callback.from_user.id), {"page": 0, "status": "all", "search": ""})
    view["return_ticket"] = tid
    ADMIN_TICKET_VIEWS[int(callback.from_user.id)] = view
    await callback.message.answer(f"💬 Send your reply for ticket #{tid}.\n\n/canceladmin to cancel.")


def _admin_bulk_state(user_id: int, kind: str) -> set[int]:
    state = ADMIN_BULK_SELECTIONS.setdefault(int(user_id), {})
    return state.setdefault(kind, set())


def _clear_admin_bulk(user_id: int, kind: str | None = None):
    uid = int(user_id)
    if uid not in ADMIN_BULK_SELECTIONS:
        return
    if kind is None:
        ADMIN_BULK_SELECTIONS.pop(uid, None)
        return
    ADMIN_BULK_SELECTIONS[uid].pop(kind, None)
    if not ADMIN_BULK_SELECTIONS[uid]:
        ADMIN_BULK_SELECTIONS.pop(uid, None)


def _grid(rows, button_factory, columns=2):
    buttons = []
    current = []
    for row in rows:
        current.append(button_factory(row))
        if len(current) == columns:
            buttons.append(current)
            current = []
    if current:
        buttons.append(current)
    return buttons


async def admin_orders_data(user_id: int | None = None, bulk_mode: bool = False):
    rows = await fetch_all("""
        SELECT o.id,o.status,o.total_stars,o.created_at,u.telegram_id,u.username,u.first_name,
               COALESCE((SELECT SUM(oi.quantity) FROM order_items oi WHERE oi.order_id=o.id),0) AS qty
        FROM orders o LEFT JOIN users u ON u.id=o.user_id
        ORDER BY o.created_at DESC,o.id DESC LIMIT 40
    """)

    selected = _admin_bulk_state(user_id, "orders") if user_id is not None else set()
    if rows:
        if bulk_mode:
            lines = ["🗑️ <b>Delete Orders</b>", "", "Select the orders to delete:"]
        else:
            lines = ["🛒 <b>Orders</b>", "", f"Showing {len(rows)} recent orders. Tap an order to open details."]
    else:
        lines = ["🛒 <b>Orders</b>", "", "No orders found."]

    if not bulk_mode:
        for r in rows[:12]:
            oid = int(r["id"])
            name = str(r.get("first_name") or r.get("username") or r.get("telegram_id") or "Customer")
            total = int(r.get("total_stars") or 0)
            status = str(r.get("status") or "pending")
            lines.append(f"#{oid} • <b>{html.escape(name[:24])}</b> • {html.escape(status)} • ⭐ {total}")

        buttons = _grid(
            rows[:24],
            lambda r: InlineKeyboardButton(
                text=f"🧾 #{int(r['id'])} · ⭐ {int(r.get('total_stars') or 0)}",
                callback_data=f"admin:order:{int(r['id'])}",
            ),
            columns=2,
        )
        buttons.append([
            InlineKeyboardButton(text="🗑️ Bulk Delete", callback_data="admin:orders:bulk"),
            InlineKeyboardButton(text="🔄 Refresh", callback_data="admin:orders"),
        ])
    else:
        buttons = _grid(
            rows,
            lambda r: InlineKeyboardButton(
                text=("✅" if int(r["id"]) in selected else "⬜") + f" #{int(r['id'])} · ⭐ {int(r.get('total_stars') or 0)}",
                callback_data=f"admin:ordersel:{int(r['id'])}",
            ),
            columns=2,
        )
        buttons.append([
            InlineKeyboardButton(text=f"🗑️ Delete Selected ({len(selected)})", callback_data="admin:orders:confirm"),
            InlineKeyboardButton(text="☑️ All", callback_data="admin:orders:selectall"),
        ])
        buttons.append([
            InlineKeyboardButton(text="✖️ Cancel", callback_data="admin:orders"),
            InlineKeyboardButton(text="◀️ Back", callback_data="admin:dashboard"),
        ])

    if not bulk_mode:
        buttons.append([InlineKeyboardButton(text="◀️ Back", callback_data="admin:dashboard")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.callback_query(F.data == "admin:orders")
async def admin_orders_callback(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer()
    _clear_admin_bulk(callback.from_user.id, "orders")
    try:
        t, k = await admin_orders_data(callback.from_user.id, False)
        await callback.message.edit_text(t, reply_markup=k)
    except Exception:
        log.exception("orders")
        await callback.message.edit_text("❌ Unable to load orders.", reply_markup=admin_menu())


@dp.callback_query(F.data == "admin:orders:bulk")
async def admin_orders_bulk_start(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer()
    _clear_admin_bulk(callback.from_user.id, "orders")
    try:
        t, k = await admin_orders_data(callback.from_user.id, True)
        await callback.message.edit_text(t, reply_markup=k)
    except Exception as e:
        await callback.message.edit_text(f"❌ {html.escape(str(e))}", reply_markup=admin_back_keyboard("admin:orders"))


@dp.callback_query(F.data.startswith("admin:ordersel:"))
async def admin_orders_toggle(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer()
    oid = int(callback.data.rsplit(":", 1)[1])
    selected = _admin_bulk_state(callback.from_user.id, "orders")
    if oid in selected:
        selected.remove(oid)
    else:
        selected.add(oid)
    t, k = await admin_orders_data(callback.from_user.id, True)
    await callback.message.edit_text(t, reply_markup=k)


@dp.callback_query(F.data == "admin:orders:selectall")
async def admin_orders_select_all(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer()
    rows = await fetch_all("SELECT id FROM orders ORDER BY created_at DESC,id DESC LIMIT 40")
    selected = _admin_bulk_state(callback.from_user.id, "orders")
    selected.clear()
    selected.update(int(r["id"]) for r in rows)
    t, k = await admin_orders_data(callback.from_user.id, True)
    await callback.message.edit_text(t, reply_markup=k)


@dp.callback_query(F.data == "admin:orders:confirm")
async def admin_orders_bulk_confirm(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer()
    selected = _admin_bulk_state(callback.from_user.id, "orders")
    ids = sorted(selected)
    if not ids:
        await callback.answer("Select at least one order.", show_alert=True)
        return
    placeholders = ",".join("?" for _ in ids)
    count_row = await fetch_one(f"SELECT COUNT(*) AS c FROM orders WHERE id IN ({placeholders})", tuple(ids))
    count = int((count_row or {}).get("c") or 0)
    await callback.message.edit_text(
        f"⚠️ <b>Delete {count} order(s)?</b>\n\nThis permanently removes the selected orders from the admin order history. Related order items and delivery records may also be removed by database rules.\n\nThis cannot be undone.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"✅ Delete {count}", callback_data="admin:orders:delete")],
            [InlineKeyboardButton(text="✖️ Cancel", callback_data="admin:orders:bulk")],
        ]),
    )


@dp.callback_query(F.data == "admin:orders:delete")
async def admin_orders_bulk_delete(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer()
    selected = _admin_bulk_state(callback.from_user.id, "orders")
    ids = sorted(selected)
    if not ids:
        await callback.answer("No orders selected.", show_alert=True)
        return
    placeholders = ",".join("?" for _ in ids)
    row = await fetch_one(f"DELETE FROM orders WHERE id IN ({placeholders}) RETURNING id", tuple(ids))
    # fetch_one returns one row only for RETURNING; count again for accurate feedback.
    remaining = await fetch_one(f"SELECT COUNT(*) AS c FROM orders WHERE id IN ({placeholders})", tuple(ids))
    deleted = len(ids) - int((remaining or {}).get("c") or 0)
    _clear_admin_bulk(callback.from_user.id, "orders")
    await callback.message.edit_text(f"✅ Deleted <b>{deleted}</b> order(s).", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Orders", callback_data="admin:orders")],
        [InlineKeyboardButton(text="◀️ Admin", callback_data="admin:dashboard")],
    ]))


@dp.callback_query(F.data.startswith("admin:order:"))
async def admin_order_detail_callback(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer()
    try:
        oid=int(callback.data.rsplit(":",1)[1])
        o=await fetch_one("""SELECT o.id,o.status,o.total_stars,o.created_at,o.updated_at,u.telegram_id,u.username,u.first_name,u.last_name FROM orders o LEFT JOIN users u ON u.id=o.user_id WHERE o.id=?""",(oid,))
        if not o: raise ValueError("Order not found")
        items=await fetch_all("""SELECT oi.quantity,oi.price_stars,p.name FROM order_items oi LEFT JOIN products p ON p.id=oi.product_id WHERE oi.order_id=? ORDER BY oi.id""",(oid,))
        pay=await fetch_one("SELECT amount_stars,status,created_at FROM payments WHERE order_id=? ORDER BY id DESC LIMIT 1",(oid,))
        delivery=await fetch_one("SELECT status,created_at,delivered_at FROM order_deliveries WHERE order_id=? LIMIT 1",(oid,))
        name=str(o.get('first_name') or o.get('username') or o.get('telegram_id') or 'Customer')
        text=(f"🧾 <b>Order #{oid}</b>\n\n👤 {html.escape(name)}\n🆔 <code>{o.get('telegram_id') or '-'}</code>\n📌 Status: <b>{html.escape(str(o.get('status') or ''))}</b>\n⭐ Total: <b>{int(o.get('total_stars') or 0)}</b> Stars\n\n<b>Items</b>\n")
        for it in items: text+=f"• {html.escape(str(it.get('name') or 'Product'))} × {int(it.get('quantity') or 0)} · ⭐ {int(it.get('price_stars') or 0)}\n"
        text += "\n<b>Payment</b>\n" + (f"{html.escape(str(pay.get('status') or ''))} · ⭐ {int(pay.get('amount_stars') or 0)}\n" if pay else "No payment record.\n")
        text += "\n<b>Delivery</b>\n" + (f"{html.escape(str(delivery.get('status') or ''))}\n" if delivery else "No delivery record.\n")
        kb=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🟢 Paid",callback_data=f"admin:orderstatus:{oid}:paid"),InlineKeyboardButton(text="✅ Completed",callback_data=f"admin:orderstatus:{oid}:completed")],
            [InlineKeyboardButton(text="❌ Cancel",callback_data=f"admin:orderstatus:{oid}:cancelled"),InlineKeyboardButton(text="📦 Delivered",callback_data=f"admin:deliver:{oid}")],
            [InlineKeyboardButton(text="◀️ Orders",callback_data="admin:orders")]])
        await callback.message.edit_text(text[:3900],reply_markup=kb)
    except Exception as e:
        await callback.message.edit_text(f"❌ Unable to load order.\n{html.escape(str(e))}",reply_markup=admin_back_keyboard("admin:orders"))


@dp.callback_query(F.data.startswith("admin:orderstatus:"))
async def admin_order_status(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer()
    try:
        _,_,oid,status_value=callback.data.split(":",3)
        await fetch_one("UPDATE orders SET status=? WHERE id=? RETURNING id",(status_value,int(oid)))
        await callback.message.answer(f"✅ Order #{oid} status set to <b>{html.escape(status_value)}</b>.")
    except Exception as e: await callback.message.answer(f"❌ {html.escape(str(e))}")


@dp.callback_query(F.data.startswith("admin:deliver:"))
async def admin_mark_delivered(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer()
    try:
        oid=int(callback.data.rsplit(":",1)[1])
        order_user = await fetch_one("SELECT u.telegram_id FROM orders o JOIN users u ON u.id=o.user_id WHERE o.id=?", (oid,))
        if not order_user:
            raise ValueError("Order user not found")
        if not await __import__('database').finish_order_delivery(oid, int(order_user['telegram_id']), True):
            raise ValueError("Delivery record not found")
        await callback.message.answer(f"✅ Order #{oid} marked as delivered and completed.")
    except Exception as e: await callback.message.answer(f"❌ {html.escape(str(e))}")


async def admin_products_data():
    rows=await get_all_products(); buttons=[]; lines=["📦 <b>Products</b>",""]
    for p in rows[:25]:
        pid=int(p['id']); stock=int(p.get('stock') or 0); price=int(p.get('price_stars') or 0)
        lines.append(f"#{pid} • <b>{html.escape(str(p.get('name') or ''))}</b> • ⭐ {price} • Stock {stock}")
        buttons.append([InlineKeyboardButton(text=f"📦 #{pid} {str(p.get('name') or '')[:35]}",callback_data=f"admin:product:{pid}")])
    buttons += [[InlineKeyboardButton(text="➕ Add Product",callback_data="admin:addproduct")],[InlineKeyboardButton(text="➕ Add Inventory",callback_data="admin:addinventory")],[InlineKeyboardButton(text="◀️ Back",callback_data="admin:dashboard")]]
    return "\n".join(lines) if rows else "📦 <b>Products</b>\n\nNo products found.",InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.callback_query(F.data == "admin:products")
async def admin_products_callback(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer(); t,k=await admin_products_data(); await callback.message.edit_text(t,reply_markup=k)


@dp.callback_query(F.data.startswith("admin:product:"))
async def admin_product_detail_callback(callback: CallbackQuery):
    return await admin_product_detail_advanced(callback)

@dp.callback_query(F.data.startswith("admin:producttoggle:"))
async def admin_product_toggle(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer()
    try:
        _,_,pid,val=callback.data.split(":",3); await fetch_one("UPDATE products SET active=? WHERE id=? RETURNING id",(val.lower()=="true",int(pid))); await callback.message.answer(f"✅ Product #{pid} updated.")
    except Exception as e: await callback.message.answer(f"❌ {html.escape(str(e))}")


@dp.callback_query(F.data.startswith("admin:productsync:"))
async def admin_product_sync(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer()
    try:
        pid=int(callback.data.rsplit(":",1)[1]); row=await fetch_one("SELECT COUNT(*) AS c FROM inventory_items WHERE product_id=? AND status='available'",(pid,)); await fetch_one("UPDATE products SET stock=? WHERE id=? RETURNING id",(int(row.get('c') or 0),pid)); await callback.message.answer(f"✅ Stock synced for product #{pid}.")
    except Exception as e: await callback.message.answer(f"❌ {html.escape(str(e))}")


async def admin_inventory_data(user_id: int | None = None, bulk_mode: bool = False, status_filter: str = "all", page: int = 0, search: str = ""):
    """Render inventory in searchable, status-grouped pages.

    Status tabs keep large inventories manageable while preserving the existing
    bulk-delete/add/edit workflows. Search matches inventory ID, product name,
    or item data.
    """
    allowed_statuses = {"all", "available", "sold", "draft", "reserved"}
    status_filter = str(status_filter or "all").lower()
    if status_filter not in allowed_statuses:
        status_filter = "all"
    try:
        page = max(0, int(page))
    except Exception:
        page = 0
    search = str(search or "").strip()[:80]

    where = []
    params = []
    if status_filter != "all":
        where.append("i.status = ?")
        params.append(status_filter)
    if search:
        like = f"%{search}%"
        where.append("(CAST(i.id AS TEXT) ILIKE ? OR COALESCE(i.item_data,'') ILIKE ? OR COALESCE(p.name,'') ILIKE ?)")
        params.extend([like, like, like])
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    count_row = await fetch_one(
        f"SELECT COUNT(*) AS c FROM inventory_items i LEFT JOIN products p ON p.id=i.product_id{where_sql}",
        tuple(params),
    )
    total = int((count_row or {}).get("c") or 0)
    page_size = 10
    pages = max(1, (total + page_size - 1) // page_size)
    if page >= pages:
        page = pages - 1
    offset = page * page_size

    rows = await fetch_all(
        f"""
        SELECT i.id,i.product_id,i.status,i.item_data,i.created_at,p.name
        FROM inventory_items i LEFT JOIN products p ON p.id=i.product_id
        {where_sql}
        ORDER BY i.created_at DESC,i.id DESC
        LIMIT {page_size} OFFSET {offset}
        """,
        tuple(params),
    )

    if user_id is not None:
        ADMIN_INVENTORY_VIEWS[int(user_id)] = {
            "status": status_filter,
            "page": page,
            "search": search,
        }
    selected = _admin_bulk_state(user_id, "inventory") if user_id is not None else set()

    status_counts = {}
    for status in ("available", "sold", "draft", "reserved"):
        cr = await fetch_one("SELECT COUNT(*) AS c FROM inventory_items WHERE status=?", (status,))
        status_counts[status] = int((cr or {}).get("c") or 0)

    if bulk_mode:
        lines = ["🗑️ <b>Delete Inventory</b>", "", "Only available items can be bulk-deleted. Select items below:"]
        select_rows = [r for r in rows if str(r.get("status") or "available").lower() == "available"]
        buttons = _grid(
            select_rows,
            lambda r: InlineKeyboardButton(
                text=("✅" if int(r["id"]) in selected else "⬜") + f" #{int(r['id'])} · {str(r.get('name') or 'Product')[:20]}",
                callback_data=f"admin:invsel:{int(r['id'])}",
            ),
            columns=2,
        )
        buttons.append([
            InlineKeyboardButton(text=f"🗑️ Delete Selected ({len(selected)})", callback_data="admin:inventory:confirm"),
            InlineKeyboardButton(text="☑️ All", callback_data="admin:inventory:selectall"),
        ])
        buttons.append([
            InlineKeyboardButton(text="✖️ Cancel", callback_data="admin:inventory"),
            InlineKeyboardButton(text="◀️ Back", callback_data="admin:dashboard"),
        ])
        return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)

    filter_labels = {
        "all": "📋 All",
        "available": "🟢 Available",
        "sold": "🔴 Sold",
        "draft": "📝 Draft",
        "reserved": "🟡 Reserved",
    }
    lines = [
        "📋 <b>Inventory</b>",
        "",
        f"🟢 {status_counts['available']} Available  •  🔴 {status_counts['sold']} Sold",
        f"📝 {status_counts['draft']} Draft  •  🟡 {status_counts['reserved']} Reserved",
        "",
        f"Filter: <b>{html.escape(filter_labels[status_filter])}</b>  •  {total} item(s)",
    ]
    if search:
        lines.append(f"🔎 Search: <code>{html.escape(search)}</code>")
    lines.append(f"Page <b>{page + 1}/{pages}</b>")

    buttons = []
    buttons.append([
        InlineKeyboardButton(text="📋 All" if status_filter == "all" else "All", callback_data="admin:invview:all:0"),
        InlineKeyboardButton(text="🟢 Available" if status_filter == "available" else "Available", callback_data="admin:invview:available:0"),
        InlineKeyboardButton(text="🔴 Sold" if status_filter == "sold" else "Sold", callback_data="admin:invview:sold:0"),
    ])
    buttons.append([
        InlineKeyboardButton(text="📝 Draft" if status_filter == "draft" else "Draft", callback_data="admin:invview:draft:0"),
        InlineKeyboardButton(text="🟡 Reserved" if status_filter == "reserved" else "Reserved", callback_data="admin:invview:reserved:0"),
        InlineKeyboardButton(text="🔎 Search", callback_data="admin:inventory:search"),
    ])
    if search:
        buttons.append([InlineKeyboardButton(text="✖️ Clear Search", callback_data="admin:invsearch:clear")])

    for r in rows:
        iid = int(r["id"])
        name = str(r.get("name") or f"Product {r.get('product_id')}")
        st = str(r.get("status") or "available").lower()
        icon = {"available":"🟢", "sold":"🔴", "draft":"📝", "reserved":"🟡"}.get(st, "⚪")
        buttons.append([InlineKeyboardButton(
            text=f"{icon} #{iid} · {name[:28]}",
            callback_data=f"admin:inv:{iid}",
        )])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ Previous", callback_data=f"admin:invview:{status_filter}:{page-1}"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="Next ▶️", callback_data=f"admin:invview:{status_filter}:{page+1}"))
    if nav:
        buttons.append(nav)
    buttons.append([
        InlineKeyboardButton(text="🗑️ Bulk Delete", callback_data="admin:inventory:bulk"),
        InlineKeyboardButton(text="🔄 Refresh", callback_data=f"admin:invview:{status_filter}:{page}"),
    ])
    buttons.append([
        InlineKeyboardButton(text="➕ Add Inventory", callback_data="admin:addinventory"),
        InlineKeyboardButton(text="◀️ Back", callback_data="admin:dashboard"),
    ])
    if not rows:
        lines.append("\nNo inventory items found in this group.")
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.callback_query(F.data == "admin:inventory")
async def admin_inventory_callback(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer()
    _clear_admin_bulk(callback.from_user.id, "inventory")
    view = ADMIN_INVENTORY_VIEWS.get(int(callback.from_user.id), {"status":"all", "page":0, "search":""})
    try:
        t, k = await admin_inventory_data(callback.from_user.id, False, view.get("status","all"), view.get("page",0), view.get("search",""))
        await callback.message.edit_text(t, reply_markup=k)
    except Exception as e:
        log.exception("inventory")
        await callback.message.edit_text(f"❌ Unable to load inventory.\n{html.escape(str(e))}", reply_markup=admin_menu())


@dp.callback_query(F.data.startswith("admin:invview:"))
async def admin_inventory_view(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer()
    parts = callback.data.split(":")
    status = parts[2] if len(parts) > 2 else "all"
    page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
    old = ADMIN_INVENTORY_VIEWS.get(int(callback.from_user.id), {})
    search = old.get("search", "")
    _clear_admin_bulk(callback.from_user.id, "inventory")
    t, k = await admin_inventory_data(callback.from_user.id, False, status, page, search)
    await callback.message.edit_text(t, reply_markup=k)


@dp.callback_query(F.data == "admin:inventory:search")
async def admin_inventory_search_start(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer()
    view = ADMIN_INVENTORY_VIEWS.get(int(callback.from_user.id), {"status":"all", "page":0, "search":""})
    ADMIN_FLOWS[int(callback.from_user.id)] = {"type":"inventory_search", "step":1, "return_view":dict(view)}
    await callback.message.answer(
        "🔎 <b>Search Inventory</b>\n\n"
        "Send an Inventory ID, product name, email/username, or any text contained in the inventory data.\n\n"
        "/canceladmin to cancel."
    )


@dp.callback_query(F.data == "admin:invsearch:clear")
async def admin_inventory_search_clear(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer()
    view = ADMIN_INVENTORY_VIEWS.get(int(callback.from_user.id), {"status":"all", "page":0, "search":""})
    view["search"] = ""
    view["page"] = 0
    t, k = await admin_inventory_data(callback.from_user.id, False, view.get("status","all"), 0, "")
    await callback.message.edit_text(t, reply_markup=k)


@dp.callback_query(F.data == "admin:inventory:bulk")
async def admin_inventory_bulk_start(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer()
    _clear_admin_bulk(callback.from_user.id, "inventory")
    t, k = await admin_inventory_data(callback.from_user.id, True, **ADMIN_INVENTORY_VIEWS.get(int(callback.from_user.id), {"status":"all", "page":0, "search":""}))
    await callback.message.edit_text(t, reply_markup=k)


@dp.callback_query(F.data.startswith("admin:invsel:"))
async def admin_inventory_toggle(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer()
    iid = int(callback.data.rsplit(":", 1)[1])
    row = await fetch_one("SELECT status FROM inventory_items WHERE id=?", (iid,))
    if not row or str(row.get("status") or "available") != "available":
        await callback.answer("Only available inventory items can be selected.", show_alert=True)
        return
    selected = _admin_bulk_state(callback.from_user.id, "inventory")
    if iid in selected:
        selected.remove(iid)
    else:
        selected.add(iid)
    t, k = await admin_inventory_data(callback.from_user.id, True)
    await callback.message.edit_text(t, reply_markup=k)


@dp.callback_query(F.data == "admin:inventory:selectall")
async def admin_inventory_select_all(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer()
    rows = await fetch_all("SELECT id FROM inventory_items WHERE status='available' ORDER BY created_at DESC,id DESC LIMIT 60")
    selected = _admin_bulk_state(callback.from_user.id, "inventory")
    selected.clear()
    selected.update(int(r["id"]) for r in rows)
    t, k = await admin_inventory_data(callback.from_user.id, True)
    await callback.message.edit_text(t, reply_markup=k)


@dp.callback_query(F.data == "admin:inventory:confirm")
async def admin_inventory_bulk_confirm(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer()
    selected = _admin_bulk_state(callback.from_user.id, "inventory")
    ids = sorted(selected)
    if not ids:
        await callback.answer("Select at least one inventory item.", show_alert=True)
        return
    placeholders = ",".join("?" for _ in ids)
    existing = await fetch_all(f"SELECT id,product_id,status FROM inventory_items WHERE id IN ({placeholders})", tuple(ids))
    available_ids = [int(r["id"]) for r in existing if str(r.get("status") or "available") == "available"]
    selected.clear()
    selected.update(available_ids)
    await callback.message.edit_text(
        f"⚠️ <b>Delete {len(available_ids)} inventory item(s)?</b>\n\nSold items will be kept. Available selected items will be permanently removed.\n\nThis cannot be undone.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"✅ Delete {len(available_ids)}", callback_data="admin:inventory:delete")],
            [InlineKeyboardButton(text="✖️ Cancel", callback_data="admin:inventory:bulk")],
        ]),
    )


@dp.callback_query(F.data == "admin:inventory:delete")
async def admin_inventory_bulk_delete(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer()
    selected = _admin_bulk_state(callback.from_user.id, "inventory")
    ids = sorted(selected)
    if not ids:
        await callback.answer("No inventory items selected.", show_alert=True)
        return
    placeholders = ",".join("?" for _ in ids)
    rows = await fetch_all(f"SELECT id,product_id FROM inventory_items WHERE id IN ({placeholders}) AND status='available'", tuple(ids))
    valid_ids = [int(r["id"]) for r in rows]
    product_ids = {int(r["product_id"]) for r in rows if r.get("product_id") is not None}
    deleted = 0
    if valid_ids:
        ph = ",".join("?" for _ in valid_ids)
        deleted_row = await fetch_one(f"DELETE FROM inventory_items WHERE id IN ({ph}) AND status='available' RETURNING id", tuple(valid_ids))
        # RETURNING gives one row here, so use a count query for accurate feedback.
        check = await fetch_one(f"SELECT COUNT(*) AS c FROM inventory_items WHERE id IN ({ph})", tuple(valid_ids))
        deleted = len(valid_ids) - int((check or {}).get("c") or 0)
    for pid in product_ids:
        try:
            await fetch_one("UPDATE products SET stock=(SELECT COUNT(*) FROM inventory_items WHERE product_id=? AND status='available'), updated_at=CURRENT_TIMESTAMP WHERE id=? RETURNING id", (pid, pid))
        except Exception:
            log.exception("Failed to sync stock after bulk inventory delete for product %s", pid)
    _clear_admin_bulk(callback.from_user.id, "inventory")
    await callback.message.edit_text(f"✅ Deleted <b>{deleted}</b> inventory item(s) and synchronized affected product stock.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Inventory", callback_data="admin:inventory")],
        [InlineKeyboardButton(text="◀️ Admin", callback_data="admin:dashboard")],
    ]))


@dp.callback_query(F.data.startswith("admin:invdel:"))
async def admin_inv_delete(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await callback.answer()
    iid = int(callback.data.rsplit(":", 1)[1])
    row = await fetch_one(
        "SELECT id, product_id, status FROM inventory_items WHERE id=?",
        (iid,),
    )
    if not row:
        await callback.message.answer("❌ Inventory item not found.")
        return await admin_inventory_callback(callback)
    if str(row.get("status") or "available").lower() == "sold":
        await callback.message.answer(
            "❌ Sold inventory items are kept for order history and cannot be deleted."
        )
        return await admin_inventory_callback(callback)
    deleted = await fetch_one(
        "DELETE FROM inventory_items WHERE id=? AND status='available' RETURNING id",
        (iid,),
    )
    if not deleted:
        await callback.message.answer("❌ Inventory item could not be deleted.")
        return await admin_inventory_callback(callback)
    pid = int(row.get("product_id") or 0)
    if pid > 0:
        await fetch_one(
            "UPDATE products SET stock=(SELECT COUNT(*) FROM inventory_items WHERE product_id=? AND status='available'), updated_at=CURRENT_TIMESTAMP WHERE id=? RETURNING id",
            (pid, pid),
        )
    await callback.message.answer(f"✅ Inventory #{iid} deleted and stock synchronized.")
    await admin_inventory_callback(callback)


@dp.callback_query(F.data.startswith("admin:invstatus:"))
async def admin_inv_status(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await callback.answer()
    iid = int(callback.data.rsplit(":", 1)[1])
    row = await fetch_one(
        "SELECT id, product_id, status FROM inventory_items WHERE id=?",
        (iid,),
    )
    if not row:
        await callback.message.answer("❌ Inventory item not found.")
        return await admin_inventory_callback(callback)
    current = str(row.get("status") or "available").lower()
    if current == "sold":
        await callback.message.answer(
            "❌ Sold inventory items cannot be returned to available. Add a new inventory item instead."
        )
        return await admin_inventory_callback(callback)

    pid = int(row.get("product_id") or 0)
    updated = await fetch_one(
        "UPDATE inventory_items SET status='sold', sold_at=CURRENT_TIMESTAMP WHERE id=? AND status='available' RETURNING id",
        (iid,),
    )
    if not updated:
        await callback.message.answer("❌ Inventory status could not be changed.")
        return await admin_inventory_callback(callback)
    if pid > 0:
        await fetch_one(
            "UPDATE products SET stock=(SELECT COUNT(*) FROM inventory_items WHERE product_id=? AND status='available'), updated_at=CURRENT_TIMESTAMP WHERE id=? RETURNING id",
            (pid, pid),
        )
    await callback.message.answer(f"✅ Inventory #{iid}: available → sold. Stock synchronized.")
    await admin_inventory_callback(callback)


@dp.callback_query(F.data == "admin:users")
async def admin_users_callback(callback: CallbackQuery):
    return await admin_users_callback_advanced(callback)

@dp.callback_query(F.data.startswith("admin:userblock:"))
async def admin_user_block(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer()
    _, _, uid, val = callback.data.split(":", 3)
    await fetch_one(
        "UPDATE users SET is_blocked=? WHERE id=? RETURNING id",
        (val == "true", int(uid)),
    )
    await callback.message.answer("✅ User updated.")
    # The callback was already answered; redraw directly and preserve the current page.
    await render_admin_users(callback)


@dp.callback_query(F.data == "admin:payments")
async def admin_payments_callback(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer(); rows=await fetch_all("SELECT id,order_id,user_id,amount_stars,status,created_at FROM payments ORDER BY created_at DESC,id DESC LIMIT 25")
    lines=["💳 <b>Payments</b>",""]
    for r in rows: lines.append(f"#{int(r['id'])} · Order #{r.get('order_id') or '-'} · ⭐ {int(r.get('amount_stars') or 0)} · <b>{html.escape(str(r.get('status') or ''))}</b>")
    lines.append("\nPayments are linked to orders and Telegram Stars.")
    await callback.message.edit_text("\n".join(lines),reply_markup=admin_back_keyboard())


@dp.callback_query(F.data == "admin:promos")
async def admin_promos_callback(callback: CallbackQuery):
    return await admin_promos_callback_advanced(callback)

@dp.callback_query(F.data.startswith("admin:promotoggle:"))
async def admin_promo_toggle(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer(); rid=int(callback.data.rsplit(":",1)[1]); await toggle_promo_code(rid); await callback.message.answer("✅ Promo code updated."); await admin_promos_callback(callback)


@dp.callback_query(F.data == "admin:games")
async def admin_games_callback(callback: CallbackQuery):
    return await admin_games_callback_advanced(callback)

@dp.callback_query(F.data == "admin:categories")
async def admin_categories_callback(callback: CallbackQuery):
    return await admin_categories_callback_advanced(callback)

@dp.callback_query(F.data == "admin:broadcast")
async def admin_broadcast_callback(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer(); ADMIN_FLOWS[callback.from_user.id]={"type":"broadcast"}; await callback.message.edit_text("📢 <b>Broadcast</b>\n\nSend the message you want to broadcast to all active Telegram users.\n\n/canceladmin to cancel.")


async def _new_promo_settings_text(flow):
    data = flow.setdefault("data", {})
    mode = str(data.get("access_mode") or "public")
    mode_label = {"public":"🌐 Public", "sale":"💳 For Sale", "assigned":"👤 Specific Customers"}.get(mode, mode.title())
    target = str(data.get("target_type") or "global")
    target_label = "All products" if target == "global" else f"{target.title()} #{data.get('target_id')}"
    discount = f"{int(data.get('discount_percent') or 0)}%" if data.get("discount_percent") else f"⭐ {int(data.get('discount_stars') or 0)} Stars"
    sale_price = f"⭐ {int(data.get('sale_price_stars') or 0)}" if mode == "sale" else "Not for sale"
    max_uses = "Unlimited" if not data.get("max_uses") else str(data.get("max_uses"))
    per_user = "Unlimited" if not data.get("max_uses_per_user") else str(data.get("max_uses_per_user"))
    return (
        "🏷️ <b>New Promo Code</b>\n\n"
        f"Code: <code>{_esc(data.get('code') or '')}</code>\n"
        f"Discount: <b>{discount}</b>\n"
        f"🎯 Target: <b>{_esc(target_label)}</b>\n"
        f"💳 Sale Mode: <b>{_esc(mode_label)}</b>\n"
        f"💰 Sale Price: <b>{sale_price}</b>\n"
        f"🔢 Uses: <b>{_esc(max_uses)}</b>\n"
        f"👤 Uses/User: <b>{_esc(per_user)}</b>\n"
        f"🛒 Min Qty: <b>{int(data.get('min_cart_quantity') or 1)}</b>\n"
        f"⏰ Expiry: <b>{_esc(data.get('expires_at') or 'Never')}</b>\n"
        f"📝 Description: <b>{_esc((data.get('description') or 'None')[:160])}</b>"
    )


def _new_promo_settings_keyboard(flow):
    data = flow.setdefault("data", {})
    mode = str(data.get("access_mode") or "public")
    rows = [
        [InlineKeyboardButton(text="🎯 Target", callback_data="admin:newpromotarget")],
        [InlineKeyboardButton(text="💳 Sale Mode", callback_data="admin:newpromomode")],
        [InlineKeyboardButton(text="🔢 Uses", callback_data="admin:newpromouses"), InlineKeyboardButton(text="👤 Uses/User", callback_data="admin:newpromoperuser")],
        [InlineKeyboardButton(text="🛒 Min Qty", callback_data="admin:newpromominqty"), InlineKeyboardButton(text="⏰ Expiry", callback_data="admin:newpromoexpiry")],
        [InlineKeyboardButton(text="📝 Description", callback_data="admin:newpromodescription")],
    ]
    if mode == "sale":
        rows.insert(2, [InlineKeyboardButton(text="💰 Sale Price", callback_data="admin:newpromoprice")])
    if mode == "assigned":
        rows.insert(2, [InlineKeyboardButton(text="👥 Customers", callback_data="admin:newpromocustomers")])
    rows += [[InlineKeyboardButton(text="✅ Create Promo", callback_data="admin:newpromocreate")], [InlineKeyboardButton(text="❌ Cancel", callback_data="admin:newpromocancel")]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_new_promo_settings(callback: CallbackQuery, notice=None):
    flow = ADMIN_FLOWS.get(int(callback.from_user.id))
    if not flow or flow.get("type") != "promo":
        await callback.message.edit_text("❌ This promo creation session expired.", reply_markup=admin_menu())
        return
    text = _new_promo_settings_text(flow)
    if notice:
        text = f"{notice}\n\n{text}"
    await callback.message.edit_text(text, reply_markup=_new_promo_settings_keyboard(flow))


@dp.callback_query(F.data == "admin:addpromo")
async def admin_addpromo(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer()
    ADMIN_FLOWS[callback.from_user.id] = {
        "type":"promo", "step":1, "data":{
            "target_type":"global", "target_id":None, "access_mode":"public",
            "max_uses":None, "max_uses_per_user":1, "min_cart_quantity":1,
            "sale_price_stars":0, "description":"", "expires_at":None, "allowed_customers":None,
        }
    }
    await callback.message.answer("🏷️ <b>New Promo Code</b>\n\nSend the promo code name (example: <code>CPM10</code>).\n\n/canceladmin to cancel.")


@dp.callback_query(F.data == "admin:newpromodiscpercent")
async def admin_new_promo_percent(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer(); flow=ADMIN_FLOWS.get(callback.from_user.id)
    if not flow or flow.get("type")!="promo": return await callback.message.answer("❌ Promo creation session expired.")
    flow["data"]["discount_type"]="percent"; flow["step"]=2
    await callback.message.edit_text("📊 <b>Percentage Discount</b>\n\nSend the discount percent, from 1 to 100.")


@dp.callback_query(F.data == "admin:newpromodiscstars")
async def admin_new_promo_stars(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer(); flow=ADMIN_FLOWS.get(callback.from_user.id)
    if not flow or flow.get("type")!="promo": return await callback.message.answer("❌ Promo creation session expired.")
    flow["data"]["discount_type"]="stars"; flow["step"]=2
    await callback.message.edit_text("⭐ <b>Fixed Stars Discount</b>\n\nSend the Stars discount amount.")


@dp.callback_query(F.data == "admin:newpromotarget")
async def admin_new_promo_target(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer()
    await callback.message.edit_text("🎯 <b>Select Promo Target</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 All Products", callback_data="admin:newpromotargetset:global:0")],
        [InlineKeyboardButton(text="🎮 Game", callback_data="admin:newpromotargetlist:game")],
        [InlineKeyboardButton(text="🗂️ Category", callback_data="admin:newpromotargetlist:category")],
        [InlineKeyboardButton(text="📦 Product", callback_data="admin:newpromotargetlist:product")],
        [InlineKeyboardButton(text="◀️ Back", callback_data="admin:newpromosettings")],
    ]))


@dp.callback_query(F.data == "admin:newpromosettings")
async def admin_new_promo_settings(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer(); await _show_new_promo_settings(callback)


@dp.callback_query(F.data.startswith("admin:newpromotargetlist:"))
async def admin_new_promo_target_list(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer(); target=callback.data.rsplit(":",1)[1]
    if target == "game":
        rows=await fetch_all("SELECT id,name FROM games WHERE active=TRUE ORDER BY id DESC LIMIT 50"); title="🎮 Select Game"
    elif target == "category":
        rows=await fetch_all("SELECT c.id,c.name,g.name AS game_name FROM categories c LEFT JOIN games g ON g.id=c.game_id WHERE c.active=TRUE ORDER BY c.id DESC LIMIT 50"); title="🗂️ Select Category"
    else:
        rows=await fetch_all("SELECT id,name FROM products WHERE active=TRUE ORDER BY id DESC LIMIT 50"); title="📦 Select Product"
    buttons=[]
    for row in rows:
        label=str(row.get("name") or f"#{row['id']}")[:36]
        if target=="category" and row.get("game_name"): label=f"{label} · {str(row['game_name'])[:16]}"
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"admin:newpromotargetset:{target}:{int(row['id'])}")])
    buttons.append([InlineKeyboardButton(text="◀️ Target Types", callback_data="admin:newpromotarget")])
    await callback.message.edit_text(title, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data.startswith("admin:newpromotargetset:"))
async def admin_new_promo_target_set(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer(); _,_,target,raw_id=callback.data.split(":",3)
    flow=ADMIN_FLOWS.get(callback.from_user.id)
    if not flow or flow.get("type")!="promo": return await callback.message.answer("❌ Promo creation session expired.")
    flow["data"]["target_type"]=target; flow["data"]["target_id"]=None if target=="global" else int(raw_id)
    await _show_new_promo_settings(callback,"✅ Target updated.")


@dp.callback_query(F.data == "admin:newpromomode")
async def admin_new_promo_mode(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer()
    await callback.message.edit_text("💳 <b>Select Sale Mode</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Public (not sold)", callback_data="admin:newpromomodeset:public")],
        [InlineKeyboardButton(text="💳 For Sale in Promo Shop", callback_data="admin:newpromomodeset:sale")],
        [InlineKeyboardButton(text="👤 Specific Customers", callback_data="admin:newpromomodeset:assigned")],
        [InlineKeyboardButton(text="◀️ Back", callback_data="admin:newpromosettings")],
    ]))


@dp.callback_query(F.data.startswith("admin:newpromomodeset:"))
async def admin_new_promo_mode_set(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer(); mode=callback.data.rsplit(":",1)[1]; flow=ADMIN_FLOWS.get(callback.from_user.id)
    if not flow or flow.get("type")!="promo": return await callback.message.answer("❌ Promo creation session expired.")
    flow["data"]["access_mode"]=mode
    if mode!="sale": flow["data"]["sale_price_stars"]=0
    if mode!="assigned": flow["data"]["allowed_customers"]=None
    await _show_new_promo_settings(callback,"✅ Sale mode updated.")


def _new_promo_text_prompt(flow, field, prompt):
    flow["field"]=field; flow["step"]=3
    return prompt + "\n\n/canceladmin to cancel."


for _field, _callback, _label, _prompt in [
    ("max_uses","admin:newpromouses","🔢 Max Uses","Send maximum total uses, or 0 for unlimited."),
    ("max_uses_per_user","admin:newpromoperuser","👤 Uses/User","Send maximum uses per customer, or 0 for unlimited."),
    ("min_cart_quantity","admin:newpromominqty","🛒 Min Qty","Send the minimum eligible cart quantity."),
    ("expires_at","admin:newpromoexpiry","⏰ Expiry","Send expiry timestamp, or - for no expiry. Example: 2026-12-31 23:59:00"),
    ("description","admin:newpromodescription","📝 Description","Send the promo description. Send - to leave it empty."),
    ("sale_price_stars","admin:newpromoprice","💰 Sale Price","Send the selling price in Telegram Stars."),
    ("allowed_customers","admin:newpromocustomers","👥 Customers","Send customer Telegram IDs or @usernames, one per line."),
]:
    async def _handler(callback: CallbackQuery, _field=_field, _prompt=_prompt):
        if not await require_admin_callback(callback): return
        await callback.answer(); flow=ADMIN_FLOWS.get(callback.from_user.id)
        if not flow or flow.get("type")!="promo": return await callback.message.answer("❌ Promo creation session expired.")
        if _field=="sale_price_stars" and flow["data"].get("access_mode")!="sale": return await callback.answer("Select For Sale mode first.",show_alert=True)
        if _field=="allowed_customers" and flow["data"].get("access_mode")!="assigned": return await callback.answer("Select Specific Customers mode first.",show_alert=True)
        await callback.message.edit_text(_new_promo_text_prompt(flow,_field,_prompt))
    _handler.__name__ = f"admin_new_promo_{_field}"
    dp.callback_query.register(_handler, F.data == _callback)


@dp.callback_query(F.data == "admin:newpromocreate")
async def admin_new_promo_create(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer(); flow=ADMIN_FLOWS.get(callback.from_user.id)
    if not flow or flow.get("type")!="promo": return await callback.message.answer("❌ Promo creation session expired.")
    d=flow.get("data",{})
    try:
        if d.get("access_mode")=="sale" and int(d.get("sale_price_stars") or 0)<=0: raise ValueError("Set a Sale Price before creating a For Sale promo.")
        if d.get("access_mode")=="assigned" and not d.get("allowed_customers"): raise ValueError("Add at least one customer for Specific Customers mode.")
        p=await create_promo_code(d.get("code"),d.get("discount_percent") or 0,d.get("discount_stars") or 0,d.get("max_uses"),d.get("expires_at"),d.get("sale_price_stars") or 0,d.get("target_type") or "global",d.get("target_id"),d.get("min_cart_quantity") or 1,d.get("max_uses_per_user") or 0,d.get("description") or "",d.get("access_mode") or "public",None,d.get("allowed_customers"))
        ADMIN_FLOWS.pop(callback.from_user.id,None)
        await callback.message.edit_text(f"✅ Promo <code>{_esc(p.get('code'))}</code> created successfully.",reply_markup=admin_menu())
    except Exception as exc:
        await _show_new_promo_settings(callback,f"❌ {_esc(exc)}")


@dp.callback_query(F.data == "admin:newpromocancel")
async def admin_new_promo_cancel(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer("Cancelled."); ADMIN_FLOWS.pop(callback.from_user.id,None); await callback.message.edit_text("❌ Promo creation cancelled.",reply_markup=admin_menu())

async def _game_picker_keyboard(prefix: str, back: str = "admin:dashboard", limit: int = 40):
    rows = await fetch_all("SELECT id,name,active FROM games ORDER BY sort_order ASC,id DESC LIMIT ?", (int(limit),))
    buttons = []
    for r in rows:
        if not r.get("active", True):
            continue
        gid = int(r["id"])
        buttons.append([InlineKeyboardButton(text=f"🎮 {str(r.get('name') or 'Game')[:38]}", callback_data=f"{prefix}:{gid}")])
    if not buttons:
        buttons.append([InlineKeyboardButton(text="⚠️ No games available", callback_data="admin:noop")])
    buttons.append([InlineKeyboardButton(text="◀️ Back", callback_data=back)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def _category_picker_keyboard(game_id: int, prefix: str, back: str = "admin:products", limit: int = 40):
    rows = await fetch_all("SELECT id,name,active FROM categories WHERE game_id=? ORDER BY sort_order ASC,id DESC LIMIT ?", (int(game_id), int(limit)))
    buttons = []
    for r in rows:
        if not r.get("active", True):
            continue
        cid = int(r["id"])
        buttons.append([InlineKeyboardButton(text=f"🗂️ {str(r.get('name') or 'Category')[:38]}", callback_data=f"{prefix}:{cid}")])
    if not buttons:
        buttons.append([InlineKeyboardButton(text="⚠️ No categories for this game", callback_data="admin:noop")])
    buttons.append([InlineKeyboardButton(text="◀️ Back", callback_data=back)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def _product_picker_keyboard(prefix: str, back: str = "admin:dashboard", limit: int = 50):
    rows = await fetch_all("SELECT id,name,game_id,category_id FROM products ORDER BY created_at DESC,id DESC LIMIT ?", (int(limit),))
    buttons = []
    for r in rows:
        pid = int(r["id"])
        label = str(r.get("name") or "Product")[:34]
        buttons.append([InlineKeyboardButton(text=f"📦 {label}", callback_data=f"{prefix}:{pid}")])
    if not buttons:
        buttons.append([InlineKeyboardButton(text="⚠️ No products available", callback_data="admin:noop")])
    buttons.append([InlineKeyboardButton(text="◀️ Back", callback_data=back)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

@dp.callback_query(F.data == "admin:noop")
async def admin_noop(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer()

@dp.callback_query(F.data == "admin:addgame")
async def admin_addgame(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer()
    ADMIN_FLOWS[callback.from_user.id]={"type":"game","step":1,"data":{}}
    await callback.message.answer("🎮 <b>Add Game</b>\n\nSend the game name.")

@dp.callback_query(F.data == "admin:addcategory")
async def admin_addcategory(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer()
    ADMIN_FLOWS[callback.from_user.id]={"type":"category","step":1,"data":{}}
    await callback.message.edit_text("🗂️ <b>Select the game for this category:</b>", reply_markup=await _game_picker_keyboard("admin:newcatgame", "admin:categories"))

@dp.callback_query(F.data.startswith("admin:newcatgame:"))
async def admin_new_category_game(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer()
    gid=int(callback.data.rsplit(":",1)[1])
    flow=ADMIN_FLOWS.get(callback.from_user.id)
    if not flow or flow.get("type")!="category":
        await callback.message.answer("❌ This admin action expired.")
        return
    flow["data"]["game_id"]=gid; flow["step"]=2
    await callback.message.edit_text("🗂️ <b>Add Category</b>\n\nSend the category name.")

@dp.callback_query(F.data == "admin:addproduct")
async def admin_addproduct(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer()
    ADMIN_FLOWS[callback.from_user.id]={"type":"product","step":1,"data":{},"images":[]}
    await callback.message.answer("📦 <b>Add Product</b>\n\nSend product name.")

@dp.callback_query(F.data == "admin:addinventory")
async def admin_addinventory(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer()
    ADMIN_FLOWS[callback.from_user.id]={"type":"inventory","step":1}
    await callback.message.edit_text("📋 <b>Select the product for the inventory:</b>\n\nNo Product ID is required.", reply_markup=await _product_picker_keyboard("admin:newinventoryproduct", "admin:products"))

@dp.callback_query(F.data.startswith("admin:newinventoryproduct:"))
async def admin_new_inventory_product(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer()
    pid=int(callback.data.rsplit(":",1)[1])
    if not await get_product(pid):
        await callback.message.answer("❌ Product not found.")
        return
    ADMIN_FLOWS[callback.from_user.id]={"type":"inventory","step":2,"data":{"product_id":pid}}
    await callback.message.edit_text(
        "📋 <b>Add Inventory</b>\n\n"
        "Send inventory accounts in this format:\n\n"
        "<code>1a:email:password</code>\n"
        "<code>2a:email:password</code>\n"
        "<code>3a:email:password:with:colons</code>\n\n"
        "The marker starts a new account. Multi-line passwords are supported.\n"
        "Legacy one-item-per-line text is also accepted.\n\n"
        "Nothing is saved until you press <b>Confirm Import</b>.\n\n"
        "/canceladmin to cancel."
    )

@dp.callback_query(F.data == "admin:inventory:cancelimport")
async def admin_inventory_cancel_import(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer("Cancelled.")
    ADMIN_FLOWS.pop(callback.from_user.id, None)
    _clear_admin_bulk(callback.from_user.id, "inventory")
    try:
        text, keyboard = await admin_inventory_data(callback.from_user.id, False)
        await callback.message.edit_text(
            "❌ <b>Inventory import cancelled.</b>\n\n" + text,
            reply_markup=keyboard,
        )
    except Exception as exc:
        log.exception("inventory cancel return failed")
        await callback.message.edit_text(
            f"❌ Inventory import cancelled.\n\nUnable to load inventory: {html.escape(str(exc))}",
            reply_markup=admin_menu(),
        )

@dp.callback_query(F.data == "admin:inventory:editpreview")
async def admin_inventory_edit_preview(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer()
    flow = ADMIN_FLOWS.get(callback.from_user.id) or {}
    if flow.get("type") != "inventory" or flow.get("step") != 3:
        await callback.answer("This inventory preview has expired.", show_alert=True)
        return
    ready = list((flow.get("data") or {}).get("ready_items") or [])
    if not ready:
        await callback.answer("There are no ready items to edit.", show_alert=True)
        return
    buttons = []
    for idx, value in enumerate(ready[:50]):
        first = str(value).split("\n", 1)[0]
        if len(first) > 34:
            first = first[:34] + "…"
        buttons.append([InlineKeyboardButton(text=f"✏️ #{idx + 1} {first}", callback_data=f"admin:inventory:edititem:{idx}")])
    buttons.append([InlineKeyboardButton(text="◀️ Back to Preview", callback_data="admin:inventory:backpreview")])
    await callback.message.edit_text(
        "✏️ <b>Edit Inventory Before Confirm</b>\n\n"
        "Select the account you want to edit. Nothing is saved yet.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )

@dp.callback_query(F.data.startswith("admin:inventory:edititem:"))
async def admin_inventory_edit_item(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer()
    flow = ADMIN_FLOWS.get(callback.from_user.id) or {}
    if flow.get("type") != "inventory" or flow.get("step") != 3:
        await callback.answer("This inventory preview has expired.", show_alert=True)
        return
    try:
        idx = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Invalid item.", show_alert=True)
        return
    ready = list((flow.get("data") or {}).get("ready_items") or [])
    if idx < 0 or idx >= len(ready):
        await callback.answer("Item not found.", show_alert=True)
        return
    flow["step"] = 4
    flow.setdefault("data", {})["edit_index"] = idx
    current = str(ready[idx])
    await callback.message.edit_text(
        f"✏️ <b>Edit Inventory Item #{idx + 1}</b>\n\n"
        "Send the replacement text. You may send a marker block or just the account data.\n\n"
        "Example:\n"
        "<code>1a:email:password</code>\n\n"
        "Multi-line passwords are supported. Nothing is saved until <b>Confirm Import</b>.\n\n"
        f"<b>Current:</b>\n<code>{html.escape(current[:3500])}</code>\n\n"
        "/canceladmin to cancel."
    )

@dp.callback_query(F.data == "admin:inventory:backpreview")
async def admin_inventory_back_preview(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer()
    flow = ADMIN_FLOWS.get(callback.from_user.id) or {}
    if flow.get("type") != "inventory" or flow.get("step") not in {3, 4}:
        await callback.answer("This inventory preview has expired.", show_alert=True)
        return
    flow["step"] = 3
    data = flow.get("data") or {}
    ready = list(data.get("ready_items") or [])
    await callback.message.edit_text(
        "📋 <b>Inventory Preview</b>\n\n"
        f"Pending items: <b>{len(ready)}</b>\n\n"
        "Nothing has been saved yet.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Edit Item", callback_data="admin:inventory:editpreview")],
            [InlineKeyboardButton(text="✅ Confirm Import", callback_data="admin:inventory:confirmimport")],
            [InlineKeyboardButton(text="❌ Cancel", callback_data="admin:inventory:cancelimport")],
        ])
    )

@dp.callback_query(F.data == "admin:inventory:confirmimport")
async def admin_inventory_confirm_import(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    flow = ADMIN_FLOWS.get(callback.from_user.id) or {}
    if flow.get("type") != "inventory" or flow.get("step") != 3:
        await callback.answer("This inventory preview has expired.", show_alert=True)
        return

    data = flow.get("data") or {}
    product_id = int(data.get("product_id") or 0)
    ready_items = list(data.get("ready_items") or [])
    if not product_id or not ready_items:
        await callback.answer("There are no items ready to import.", show_alert=True)
        return

    await callback.answer("Importing…")
    inserted = 0
    skipped = 0
    try:
        for item in ready_items:
            existing = await fetch_one(
                "SELECT id FROM inventory_items WHERE product_id=? AND item_data=? LIMIT 1",
                (product_id, item),
            )
            if existing:
                skipped += 1
                continue
            await fetch_one(
                "INSERT INTO inventory_items(product_id,item_data,status) VALUES(?,?,'available') RETURNING id",
                (product_id, item),
            )
            inserted += 1

        await _sync_product_stock(product_id)
        row = await fetch_one(
            "SELECT name, stock FROM products WHERE id=?",
            (product_id,),
        )
        ADMIN_FLOWS.pop(callback.from_user.id, None)

        await admin_log("inventory_import", callback.from_user.id, f"{product_id}:{inserted}")
        await callback.message.edit_text(
            "✅ <b>Inventory imported successfully.</b>\n\n"
            f"📦 Product: <b>{_esc(row.get('name') if row else product_id)}</b>\n"
            f"✅ Added: <b>{inserted}</b>\n"
            f"⚠️ Skipped as existing: <b>{skipped}</b>\n"
            f"📊 Available stock: <b>{int(row.get('stock') or 0) if row else 0}</b>",
            reply_markup=admin_menu(),
        )
    except Exception as exc:
        log.exception("Telegram inventory import failed")
        await callback.message.answer(
            f"❌ Import failed: <code>{html.escape(str(exc))}</code>\n\n"
            "The preview is still available. You can press Confirm Import again.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Confirm Import", callback_data="admin:inventory:confirmimport")],
                [InlineKeyboardButton(text="❌ Cancel", callback_data="admin:inventory:cancelimport")],
            ]),
        )

@dp.callback_query(F.data == "admin:web")
async def admin_web_callback(callback: CallbackQuery):
    return await admin_web_callback_fixed(callback)

# ========================= ADVANCED TELEGRAM ADMIN =========================

def _esc(v):
    return html.escape(str(v if v is not None else ""))


async def _sync_product_stock(product_id: int):
    row = await fetch_one(
        "SELECT COUNT(*) AS c FROM inventory_items WHERE product_id=? AND status='available'",
        (product_id,),
    )
    await fetch_one(
        "UPDATE products SET stock=? WHERE id=? RETURNING id",
        (int(row.get("c") or 0), product_id),
    )


def _confirm_delete(kind: str, item_id: int, label: str):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⚠️ Yes, Delete", callback_data=f"admin:del:{kind}:{item_id}")],
            [InlineKeyboardButton(text="❌ Cancel", callback_data=f"admin:canceldelete:{kind}:{item_id}")],
        ]
    )


def _edit_button(kind: str, item_id: int, field: str, label: str):
    return InlineKeyboardButton(text=label, callback_data=f"admin:edit:{kind}:{item_id}:{field}")


async def _set_store_status_from_admin(callback: CallbackQuery, online: bool):
    if not await require_admin_callback(callback):
        return
    await set_setting("maintenance_mode", "0" if online else "1")
    status = "🟢 <b>STORE ONLINE</b>" if online else "🔴 <b>STORE OFFLINE</b>"
    note = "Customers can use the shop and bot again." if online else "Customers receive an offline message. Admin functions remain available."
    await admin_log("store_online" if online else "store_offline", callback.from_user.id)
    await callback.answer("Store is online." if online else "Store is offline.", show_alert=True)
    await callback.message.edit_text(
        f"⚙️ <b>Store Status</b>\n\n{status}\n\n{note}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🟢 Turn Store ON", callback_data="admin:store:on")],
            [InlineKeyboardButton(text="🔴 Turn Store OFF", callback_data="admin:store:off")],
            [InlineKeyboardButton(text="◀️ Dashboard", callback_data="admin:dashboard")],
        ]),
    )


@dp.callback_query(F.data == "admin:store:on")
async def admin_store_on(callback: CallbackQuery):
    await _set_store_status_from_admin(callback, True)


@dp.callback_query(F.data == "admin:store:off")
async def admin_store_off(callback: CallbackQuery):
    await _set_store_status_from_admin(callback, False)


@dp.callback_query(F.data == "admin:storetoggle")
async def admin_store_toggle_legacy(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await _set_store_status_from_admin(callback, not await store_enabled())


@dp.callback_query(F.data == "admin:web")
async def admin_web_callback_fixed(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await callback.answer()
    url = admin_web_url("/admin")
    await callback.message.edit_text(
        "🌐 <b>Web Admin Panel</b>\n\n"
        "Open the secure web admin login. After login you can use the full dashboard for large-scale management.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Open Web Admin", url=url)],
            [InlineKeyboardButton(text="◀️ Dashboard", callback_data="admin:dashboard")],
        ]),
    )


# ------------------------- Products: edit/delete -------------------------
@dp.callback_query(F.data.startswith("admin:product:") & ~F.data.startswith("admin:producttoggle:"))
async def admin_product_detail_advanced(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await callback.answer()
    pid = int(callback.data.rsplit(":", 1)[1])
    p = await get_product(pid)
    if not p:
        await callback.message.edit_text("❌ Product not found.", reply_markup=admin_back_keyboard("admin:products"))
        return
    active = bool(p.get("active"))
    featured = bool(p.get("featured"))
    text = (
        f"📦 <b>{_esc(p.get('name'))}</b>\n\n"
        f"🆔 <code>{pid}</code>\n"
        f"💰 Price: ⭐ <b>{int(p.get('price_stars') or 0)}</b>\n"
        f"📦 Stock: <b>{int(p.get('stock') or 0)}</b>\n"
        f"🎮 Game: {_esc(p.get('game_name') or '-')}\n"
        f"🗂️ Category: {_esc(p.get('category_name') or '-')}\n"
        f"⭐ Featured: {'Yes' if featured else 'No'}\n"
        f"🟢 Active: {'Yes' if active else 'No'}\n\n"
        f"📝 {_esc(p.get('description') or 'No description')[:500]}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_edit_button("p", pid, "name", "✏️ Name"), _edit_button("p", pid, "description", "📝 Description")],
        [_edit_button("p", pid, "price_stars", "💰 Price"), _edit_button("p", pid, "stock", "📦 Stock")],
        [_edit_button("p", pid, "game_id", "🎮 Game"), _edit_button("p", pid, "category_id", "🗂️ Category")],
        [_edit_button("p", pid, "discount_percent", "🏷️ Discount"), _edit_button("p", pid, "featured", "⭐ Featured")],
        [InlineKeyboardButton(text="🖼️ Images", callback_data=f"admin:productimages:{pid}"), InlineKeyboardButton(text="🖼️ Banner", callback_data=f"admin:productbanner:{pid}")],
        [InlineKeyboardButton(text=("🔴 Disable" if active else "🟢 Enable"), callback_data=f"admin:producttoggle:{pid}:{str(not active).lower()}")],
        [InlineKeyboardButton(text="🔄 Sync Stock", callback_data=f"admin:productsync:{pid}")],
        [InlineKeyboardButton(text="🗑️ Delete Product", callback_data=f"admin:confirmdel:p:{pid}")],
        [InlineKeyboardButton(text="◀️ Products", callback_data="admin:products")],
    ])
    await callback.message.edit_text(text, reply_markup=kb)


@dp.callback_query(F.data.startswith("admin:confirmdel:"))
async def admin_confirm_delete(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await callback.answer()
    _, _, kind, item_id = callback.data.split(":", 3)
    labels = {"p": "product", "i": "inventory item", "g": "game", "c": "category", "r": "promo code"}
    label = labels.get(kind, "item")
    await callback.message.edit_text(
        f"⚠️ <b>Delete {label} #{item_id}?</b>\n\nThis action cannot be undone.",
        reply_markup=_confirm_delete(kind, int(item_id), label),
    )


@dp.callback_query(F.data.startswith("admin:canceldelete:"))
async def admin_cancel_delete(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await callback.answer("Cancelled")
    _, _, kind, item_id = callback.data.split(":", 3)
    targets = {"p": "admin:product:" + item_id, "i": "admin:inventory", "g": "admin:games", "c": "admin:categories", "r": "admin:promos"}
    if kind == "p":
        callback.data = targets["p"]
        await admin_product_detail_advanced(callback)
    elif kind == "i":
        callback.data = "admin:inventory"
        await admin_inventory_callback(callback)
    elif kind == "g":
        callback.data = "admin:games"
        await admin_games_callback_advanced(callback)
    elif kind == "c":
        callback.data = "admin:categories"
        await admin_categories_callback_advanced(callback)
    else:
        callback.data = "admin:promos"
        await admin_promos_callback_advanced(callback)


@dp.callback_query(F.data.startswith("admin:del:"))
async def admin_delete_item(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await callback.answer()
    _, _, kind, raw_id = callback.data.split(":", 3)
    try:
        item_id = int(raw_id)
    except (TypeError, ValueError):
        await callback.message.edit_text("❌ Invalid item ID.", reply_markup=admin_menu())
        return

    target = {
        "p": "admin:products",
        "i": "admin:inventory",
        "g": "admin:games",
        "c": "admin:categories",
        "r": "admin:promos",
    }.get(kind)
    if not target:
        await callback.message.edit_text("❌ Unknown delete target.", reply_markup=admin_menu())
        return

    try:
        if kind == "i":
            row = await fetch_one(
                "SELECT id, product_id, status FROM inventory_items WHERE id=?",
                (item_id,),
            )
            if not row:
                raise ValueError("Inventory item not found.")
            if str(row.get("status") or "available").lower() == "sold":
                raise ValueError("Sold inventory items are kept for order history and cannot be deleted.")
            deleted = await fetch_one(
                "DELETE FROM inventory_items WHERE id=? AND status='available' RETURNING id",
                (item_id,),
            )
            if not deleted:
                raise ValueError("Inventory item could not be deleted.")
            pid = int(row.get("product_id") or 0)
            if pid > 0:
                await fetch_one(
                    "UPDATE products SET stock=(SELECT COUNT(*) FROM inventory_items WHERE product_id=? AND status='available'), updated_at=CURRENT_TIMESTAMP WHERE id=? RETURNING id",
                    (pid, pid),
                )

        elif kind == "p":
            row = await fetch_one(
                """SELECT p.id, p.name, p.active,
                          (SELECT COUNT(*) FROM inventory_items WHERE product_id=p.id AND status='available') AS available_inventory,
                          (SELECT COUNT(*) FROM order_items WHERE product_id=p.id) AS order_items
                     FROM products p WHERE p.id=?""",
                (item_id,),
            )
            if not row:
                raise ValueError("Product not found.")
            if bool(row.get("active")):
                raise ValueError("Deactivate the product before deleting it.")
            if int(row.get("available_inventory") or 0) > 0:
                raise ValueError("Delete or sell all available inventory before deleting this product.")
            await fetch_one("DELETE FROM products WHERE id=? RETURNING id", (item_id,))

        elif kind == "g":
            row = await fetch_one(
                """SELECT g.id,
                          (SELECT COUNT(*) FROM categories WHERE game_id=g.id) AS categories,
                          (SELECT COUNT(*) FROM products WHERE game_id=g.id) AS products
                     FROM games g WHERE g.id=?""",
                (item_id,),
            )
            if not row:
                raise ValueError("Game not found.")
            if int(row.get("categories") or 0) or int(row.get("products") or 0):
                raise ValueError("This game still has linked categories or products. Remove those dependencies first.")
            await fetch_one("DELETE FROM games WHERE id=? RETURNING id", (item_id,))

        elif kind == "c":
            row = await fetch_one(
                "SELECT c.id, (SELECT COUNT(*) FROM products WHERE category_id=c.id) AS products FROM categories c WHERE c.id=?",
                (item_id,),
            )
            if not row:
                raise ValueError("Category not found.")
            if int(row.get("products") or 0):
                raise ValueError("This category still has linked products. Remove those products first.")
            await fetch_one("DELETE FROM categories WHERE id=? RETURNING id", (item_id,))

        else:
            deleted = await fetch_one("DELETE FROM promo_codes WHERE id=? RETURNING id", (item_id,))
            if not deleted:
                raise ValueError("Promo code not found.")

        await admin_log(f"delete_{kind}", callback.from_user.id, str(item_id))
        await callback.message.edit_text(
            f"✅ Item #{item_id} deleted successfully.",
            reply_markup=admin_back_keyboard(target),
        )
    except ValueError as e:
        await callback.message.edit_text(
            f"❌ {html.escape(str(e))}",
            reply_markup=admin_back_keyboard(target),
        )
    except Exception:
        log.exception("Admin delete failed: %s #%s", kind, item_id)
        await callback.message.edit_text(
            "❌ The item could not be deleted because it is still used by another part of the store.",
            reply_markup=admin_back_keyboard(target),
        )


# ------------------------- Inventory: detail/edit -------------------------
@dp.callback_query(F.data.startswith("admin:inv:") & ~F.data.startswith("admin:invdel:") & ~F.data.startswith("admin:invstatus:"))
async def admin_inventory_detail(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await callback.answer()
    iid = int(callback.data.rsplit(":", 1)[1])
    row = await fetch_one("SELECT i.*,p.name AS product_name FROM inventory_items i LEFT JOIN products p ON p.id=i.product_id WHERE i.id=?", (iid,))
    if not row:
        await callback.message.edit_text("❌ Inventory item not found.", reply_markup=admin_back_keyboard("admin:inventory"))
        return
    text = (
        f"📋 <b>Inventory #{iid}</b>\n\n"
        f"📦 Product: <b>{_esc(row.get('product_name') or row.get('product_id'))}</b>\n"
        f"🆔 Product ID: <code>{row.get('product_id')}</code>\n"
        f"📌 Status: <b>{_esc(row.get('status'))}</b>\n\n"
        f"<b>Item Data</b>\n<code>{_esc(row.get('item_data') or '')[:1200]}</code>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_edit_button("i", iid, "product_id", "📦 Product"), _edit_button("i", iid, "item_data", "✏️ Data")],
        [_edit_button("i", iid, "status", "📌 Status")],
        [InlineKeyboardButton(text="🗑️ Delete", callback_data=f"admin:confirmdel:i:{iid}")],
        [InlineKeyboardButton(text="◀️ Inventory", callback_data="admin:inventory")],
    ])
    await callback.message.edit_text(text, reply_markup=kb)


# ------------------------- Games: full CRUD -------------------------
async def _game_detail_text(game_id: int):
    return await fetch_one("SELECT * FROM games WHERE id=?", (game_id,))


@dp.callback_query(F.data.startswith("admin:game:"))
async def admin_game_detail(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await callback.answer()
    gid = int(callback.data.rsplit(":", 1)[1])
    g = await _game_detail_text(gid)
    if not g:
        await callback.message.edit_text("❌ Game not found.", reply_markup=admin_back_keyboard("admin:games"))
        return
    active = bool(g.get("active"))
    text = f"🎮 <b>{_esc(g.get('name'))}</b>\n\n🆔 <code>{gid}</code>\n🟢 Active: {'Yes' if active else 'No'}\n🔢 Sort: {int(g.get('sort_order') or 0)}\n🖼️ Image: {_esc(g.get('image') or '-')}\n\n📝 {_esc(g.get('description') or 'No description')[:800]}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_edit_button("g", gid, "name", "✏️ Name"), _edit_button("g", gid, "description", "📝 Description")],
        [_edit_button("g", gid, "image", "🖼️ Image"), _edit_button("g", gid, "sort_order", "🔢 Sort")],
        [InlineKeyboardButton(text=("🔴 Disable" if active else "🟢 Enable"), callback_data=f"admin:geditactive:{gid}:{str(not active).lower()}")],
        [InlineKeyboardButton(text="🗑️ Delete Game", callback_data=f"admin:confirmdel:g:{gid}")],
        [InlineKeyboardButton(text="◀️ Games", callback_data="admin:games")],
    ])
    await callback.message.edit_text(text, reply_markup=kb)


@dp.callback_query(F.data.startswith("admin:geditactive:"))
async def admin_game_active(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await callback.answer()
    _, _, gid, value = callback.data.split(":", 3)
    await fetch_one("UPDATE games SET active=? WHERE id=? RETURNING id", (value == "true", int(gid)))
    await admin_game_detail(callback)


# ------------------------- Categories: full CRUD -------------------------
@dp.callback_query(F.data.startswith("admin:cat:"))
async def admin_category_detail(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await callback.answer()
    cid = int(callback.data.rsplit(":", 1)[1])
    c = await fetch_one("SELECT c.*,g.name AS game_name FROM categories c LEFT JOIN games g ON g.id=c.game_id WHERE c.id=?", (cid,))
    if not c:
        await callback.message.edit_text("❌ Category not found.", reply_markup=admin_back_keyboard("admin:categories"))
        return
    active = bool(c.get("active"))
    text = f"🗂️ <b>{_esc(c.get('name'))}</b>\n\n🆔 <code>{cid}</code>\n🎮 Game: {_esc(c.get('game_name') or c.get('game_id'))}\n🟢 Active: {'Yes' if active else 'No'}\n🔢 Sort: {int(c.get('sort_order') or 0)}\n🖼️ Image: {_esc(c.get('image') or '-')}\n\n📝 {_esc(c.get('description') or 'No description')[:800]}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_edit_button("c", cid, "game_id", "🎮 Game"), _edit_button("c", cid, "name", "✏️ Name")],
        [_edit_button("c", cid, "description", "📝 Description"), _edit_button("c", cid, "image", "🖼️ Image")],
        [_edit_button("c", cid, "sort_order", "🔢 Sort")],
        [InlineKeyboardButton(text=("🔴 Disable" if active else "🟢 Enable"), callback_data=f"admin:ceditactive:{cid}:{str(not active).lower()}")],
        [InlineKeyboardButton(text="🗑️ Delete Category", callback_data=f"admin:confirmdel:c:{cid}")],
        [InlineKeyboardButton(text="◀️ Categories", callback_data="admin:categories")],
    ])
    await callback.message.edit_text(text, reply_markup=kb)


@dp.callback_query(F.data.startswith("admin:ceditactive:"))
async def admin_category_active(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await callback.answer()
    _, _, cid, value = callback.data.split(":", 3)
    await fetch_one("UPDATE categories SET active=? WHERE id=? RETURNING id", (value == "true", int(cid)))
    await admin_category_detail(callback)


# Replace simple Games/Categories list behavior with clickable detail rows.
async def _render_games(callback):
    rows = await fetch_all("SELECT id,name,active FROM games ORDER BY sort_order,id DESC LIMIT 40")
    lines = ["🎮 <b>Games</b>", ""]
    buttons = []
    for r in rows:
        gid = int(r["id"])
        name = str(r.get("name") or "Game")
        lines.append(f"#{gid} • <b>{_esc(name)}</b> · {'🟢' if r.get('active') else '🔴'}")
        buttons.append([InlineKeyboardButton(text=f"🎮 #{gid} {name[:32]}", callback_data=f"admin:game:{gid}")])
    buttons += [[InlineKeyboardButton(text="➕ Add Game", callback_data="admin:addgame")], [InlineKeyboardButton(text="🔄 Refresh", callback_data="admin:games"), InlineKeyboardButton(text="◀️ Back", callback_data="admin:dashboard")]]
    return "\n".join(lines) if rows else "🎮 <b>Games</b>\n\nNo games.", InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.callback_query(F.data == "admin:games")
async def admin_games_callback_advanced(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await callback.answer()
    text, kb = await _render_games(callback)
    await callback.message.edit_text(text, reply_markup=kb)


async def _render_categories(callback):
    rows = await fetch_all("SELECT c.id,c.name,c.active,g.name AS game_name FROM categories c LEFT JOIN games g ON g.id=c.game_id ORDER BY c.sort_order,c.id DESC LIMIT 40")
    lines = ["🗂️ <b>Categories</b>", ""]
    buttons = []
    for r in rows:
        cid = int(r["id"])
        name = str(r.get("name") or "Category")
        game = str(r.get("game_name") or "-")
        lines.append(f"#{cid} • <b>{_esc(name)}</b> · {_esc(game)} · {'🟢' if r.get('active') else '🔴'}")
        buttons.append([InlineKeyboardButton(text=f"🗂️ #{cid} {name[:25]}", callback_data=f"admin:cat:{cid}")])
    buttons += [[InlineKeyboardButton(text="➕ Add Category", callback_data="admin:addcategory")], [InlineKeyboardButton(text="🔄 Refresh", callback_data="admin:categories"), InlineKeyboardButton(text="◀️ Back", callback_data="admin:dashboard")]]
    return "\n".join(lines) if rows else "🗂️ <b>Categories</b>\n\nNo categories.", InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.callback_query(F.data == "admin:categories")
async def admin_categories_callback_advanced(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await callback.answer()
    text, kb = await _render_categories(callback)
    await callback.message.edit_text(text, reply_markup=kb)


# ------------------------- Promo Codes: full CRUD -------------------------
@dp.callback_query(F.data.startswith("admin:promo:"))
async def admin_promo_detail(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await callback.answer()
    rid = int(callback.data.rsplit(":", 1)[1])
    r = await fetch_one("SELECT * FROM promo_codes WHERE id=?", (rid,))
    if not r:
        await callback.message.edit_text("❌ Promo code not found.", reply_markup=admin_back_keyboard("admin:promos"))
        return
    usage_row = await fetch_one("SELECT COALESCE(SUM(quantity),0) AS used_count FROM promo_redemptions WHERE promo_code_id=?", (rid,))
    actual_used_count = int((usage_row or {}).get("used_count") or 0)
    disc = f"{int(r.get('discount_percent') or 0)}%" if int(r.get('discount_percent') or 0) else f"⭐ {int(r.get('discount_stars') or 0)} Stars"
    text = f"🏷️ <b>{_esc(r.get('code'))}</b>\n\n🆔 <code>{rid}</code>\n💸 Discount: <b>{disc}</b>\n📦 Inventory Uses: <b>{actual_used_count}</b> / {_esc(r.get('max_uses') if r.get('max_uses') is not None else '∞')}\n🟢 Active: {'Yes' if r.get('active') else 'No'}\n⏰ Expires: {_esc(r.get('expires_at') or 'Never')}"
    target_type = str(r.get("target_type") or "global").lower()
    target_id = r.get("target_id")
    if target_type == "global":
        target_label = "🌐 All products"
    else:
        target_label = f"🎯 {target_type.title()} #{target_id}"
        try:
            table = {"game":"games", "category":"categories", "product":"products"}.get(target_type)
            if table and target_id:
                tr = await fetch_one(f"SELECT name FROM {table} WHERE id=?", (int(target_id),))
                if tr and tr.get("name"):
                    target_label += f" · {_esc(tr['name'])}"
        except Exception:
            pass
    price = int(r.get("sale_price_stars") or 0)
    desc = str(r.get("description") or "")
    min_qty = int(r.get("min_cart_quantity") or 1)
    per_user_limit = int(r.get("max_uses_per_user") or 0)
    per_user = "Unlimited" if per_user_limit <= 0 else str(per_user_limit)
    text = (
        f"🏷️ <b>{_esc(r.get('code'))}</b>\n\n"
        f"🆔 <code>{rid}</code>\n"
        f"💸 Discount: <b>{disc}</b>\n"
        f"💰 Sale Price: <b>{('⭐ '+str(price)) if price else 'Not for sale'}</b>\n"
        f"💳 Sale Mode: <b>{_esc({'sale':'For Sale','assigned':'Specific Customers','public':'Public'}.get(str(r.get('access_mode') or 'public'),'Public'))}</b>\n"
        f"🎯 Target: <b>{target_label}</b>\n"
        f"🛒 Min Quantity: <b>{min_qty}</b>\n"
        f"👤 Uses/User: <b>{per_user}</b>\n"
        f"📦 Inventory Uses: <b>{actual_used_count}</b> / {_esc(r.get('max_uses') if r.get('max_uses') is not None else '∞')}\n"
        f"🟢 Active: {'Yes' if r.get('active') else 'No'}\n"
        f"⏰ Expires: {_esc(r.get('expires_at') or 'Never')}\n"
        f"📝 Description: <b>{_esc(desc[:180] if desc else 'None')}</b>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_edit_button("r", rid, "code", "✏️ Code"), _edit_button("r", rid, "sale_price_stars", "💰 Price")],
        [InlineKeyboardButton(text="💳 Sale Mode", callback_data=f"admin:promomode:{rid}"), InlineKeyboardButton(text="🎯 Target", callback_data=f"admin:promotarget:{rid}")],
        [_edit_button("r", rid, "discount_percent", "📊 Percent"), _edit_button("r", rid, "discount_stars", "⭐ Stars")],
        [_edit_button("r", rid, "description", "📝 Description"), _edit_button("r", rid, "max_uses", "🔢 Max Uses")],
        [_edit_button("r", rid, "max_uses_per_user", "👤 Uses/User"), _edit_button("r", rid, "min_cart_quantity", "🛒 Min Qty")],
        [_edit_button("r", rid, "expires_at", "⏰ Expiry")],
        [InlineKeyboardButton(text=("🔴 Disable" if r.get('active') else "🟢 Enable"), callback_data=f"admin:promoactive:{rid}:{str(not bool(r.get('active'))).lower()}")],
        [InlineKeyboardButton(text="🗑️ Delete Promo", callback_data=f"admin:confirmdel:r:{rid}")],
        [InlineKeyboardButton(text="◀️ Promo Codes", callback_data="admin:promos")],
    ])
    await callback.message.edit_text(text, reply_markup=kb)


@dp.callback_query(F.data.startswith("admin:promomode:"))
async def admin_promo_mode_menu(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer(); rid=int(callback.data.rsplit(":",1)[1])
    await callback.message.edit_text("💳 <b>Select Sale Mode</b>",reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Public (not sold)",callback_data=f"admin:promomodeset:{rid}:public")],
        [InlineKeyboardButton(text="💳 For Sale in Promo Shop",callback_data=f"admin:promomodeset:{rid}:sale")],
        [InlineKeyboardButton(text="👤 Specific Customers",callback_data=f"admin:promomodeset:{rid}:assigned")],
        [InlineKeyboardButton(text="◀️ Back",callback_data=f"admin:promo:{rid}")],
    ]))

@dp.callback_query(F.data.startswith("admin:promomodeset:"))
async def admin_promo_mode_set(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer(); _,_,rid,mode=callback.data.split(":",3); rid=int(rid)
    try:
        if mode not in {"public","sale","assigned"}: raise ValueError("Invalid sale mode.")
        if mode=="sale":
            row=await fetch_one("SELECT sale_price_stars FROM promo_codes WHERE id=?",(rid,))
            if not row or int(row.get('sale_price_stars') or 0)<=0:
                await callback.message.edit_text("💳 <b>For Sale</b>\n\nSend the selling price in Telegram Stars.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Back",callback_data=f"admin:promo:{rid}")]]))
                ADMIN_FLOWS[callback.from_user.id]={"type":"promo_mode_price","step":1,"id":rid}
                return
            await update_promo_code(rid,{"access_mode":"sale"})
            await admin_promo_detail(callback)
            return
        if mode=="assigned":
            ADMIN_FLOWS[callback.from_user.id]={"type":"promo_mode_customers","step":1,"id":rid}
            await callback.message.edit_text("👤 <b>Specific Customers</b>\n\nSend Telegram IDs or @usernames, one per line.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Back",callback_data=f"admin:promo:{rid}")]]))
            return
        await update_promo_code(rid,{"access_mode":mode,"sale_price_stars":0 if mode!="sale" else None})
        await admin_promo_detail(callback)
    except Exception as exc: await callback.message.answer(f"❌ {_esc(exc)}")


@dp.callback_query(F.data.startswith("admin:promotarget:"))
async def admin_promo_target_menu(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await callback.answer()
    rid = int(callback.data.rsplit(":", 1)[1])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 All Products", callback_data=f"admin:promotargetset:{rid}:global:0")],
        [InlineKeyboardButton(text="🎮 Game", callback_data=f"admin:promotargetlist:{rid}:game")],
        [InlineKeyboardButton(text="🗂️ Category", callback_data=f"admin:promotargetlist:{rid}:category")],
        [InlineKeyboardButton(text="📦 Product", callback_data=f"admin:promotargetlist:{rid}:product")],
        [InlineKeyboardButton(text="◀️ Back", callback_data=f"admin:promo:{rid}")],
    ])
    await callback.message.edit_text("🎯 <b>Select Promo Target</b>\n\nChoose what this promo code applies to:", reply_markup=kb)


@dp.callback_query(F.data.startswith("admin:promotargetlist:"))
async def admin_promo_target_list(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await callback.answer()
    _, _, rid, target = callback.data.split(":", 3)
    rid = int(rid)
    if target == "game":
        rows = await fetch_all("SELECT id,name FROM games WHERE active=TRUE ORDER BY id DESC LIMIT 50")
        title = "🎮 Select Game"
    elif target == "category":
        rows = await fetch_all("SELECT c.id,c.name,g.name AS game_name FROM categories c LEFT JOIN games g ON g.id=c.game_id WHERE c.active=TRUE ORDER BY c.id DESC LIMIT 50")
        title = "🗂️ Select Category"
    else:
        rows = await fetch_all("SELECT id,name FROM products WHERE active=TRUE ORDER BY id DESC LIMIT 50")
        title = "📦 Select Product"
    buttons = []
    for row in rows:
        label = str(row.get("name") or f"#{row['id']}")[:38]
        if target == "category" and row.get("game_name"):
            label = f"{label} · {str(row['game_name'])[:18]}"
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"admin:promotargetset:{rid}:{target}:{int(row['id'])}")])
    buttons.append([InlineKeyboardButton(text="◀️ Target Types", callback_data=f"admin:promotarget:{rid}")])
    await callback.message.edit_text(title, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data.startswith("admin:promotargetset:"))
async def admin_promo_target_set(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await callback.answer()
    _, _, rid, target, raw_id = callback.data.split(":", 4)
    rid, raw_id = int(rid), int(raw_id)
    try:
        await update_promo_code(rid, {"target_type": target, "target_id": None if target == "global" else raw_id})
        await callback.message.answer("✅ Promo target updated.")
    except Exception as exc:
        await callback.message.answer(f"❌ {_esc(exc)}")
    await admin_promo_detail(callback)


@dp.callback_query(F.data.startswith("admin:promoactive:"))
async def admin_promo_active(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await callback.answer()
    _, _, rid, value = callback.data.split(":", 3)
    await fetch_one("UPDATE promo_codes SET active=? WHERE id=? RETURNING id", (value == "true", int(rid)))
    await admin_promo_detail(callback)


async def _render_promos(callback):
    rows = await list_promo_codes(limit=40)
    lines = ["🏷️ <b>Promo Codes</b>", ""]
    buttons = []
    for r in rows:
        rid = int(r["id"])
        code = str(r.get("code") or "")
        disc = f"{int(r.get('discount_percent') or 0)}%" if int(r.get('discount_percent') or 0) else f"⭐ {int(r.get('discount_stars') or 0)}"
        lines.append(f"#{rid} <code>{_esc(code)}</code> · {disc} · {'🟢' if r.get('active') else '🔴'}")
        buttons.append([InlineKeyboardButton(text=f"🏷️ {code[:28]}", callback_data=f"admin:promo:{rid}")])
    buttons += [[InlineKeyboardButton(text="➕ Add Promo Code", callback_data="admin:addpromo")], [InlineKeyboardButton(text="🔄 Refresh", callback_data="admin:promos"), InlineKeyboardButton(text="◀️ Back", callback_data="admin:dashboard")]]
    return "\n".join(lines) if rows else "🏷️ <b>Promo Codes</b>\n\nNo promo codes.", InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.callback_query(F.data == "admin:promos")
async def admin_promos_callback_advanced(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await callback.answer()
    text, kb = await _render_promos(callback)
    await callback.message.edit_text(text, reply_markup=kb)


# ------------------------- Users: search/detail/actions -------------------------
@dp.callback_query(F.data == "admin:usersearch")
async def admin_user_search_start(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await callback.answer()
    ADMIN_FLOWS[callback.from_user.id] = {"type": "user_search", "step": 1}
    await callback.message.answer("👥 Send Telegram ID, @username, or name to search.\n\n/canceladmin to cancel.")


@dp.callback_query(F.data.startswith("admin:user:"))
async def admin_user_detail(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await callback.answer()
    uid = int(callback.data.rsplit(":", 1)[1])
    u = await fetch_one("SELECT * FROM users WHERE id=?", (uid,))
    if not u:
        await callback.message.edit_text("❌ User not found.", reply_markup=admin_back_keyboard("admin:users"))
        return
    stats = await fetch_one("""
        SELECT
          (SELECT COUNT(*) FROM orders WHERE user_id=?) AS orders,
          COALESCE((SELECT SUM(amount_stars) FROM payments WHERE user_id=? AND status='paid'),0) AS spent,
          (SELECT COUNT(*) FROM tickets WHERE user_id=?) AS tickets,
          (SELECT COUNT(*) FROM referrals WHERE referrer_user_id=?) AS referrals
    """, (uid, uid, uid, uid)) or {}
    blocked = bool(u.get("is_blocked"))
    name = u.get("first_name") or u.get("username") or u.get("telegram_id")
    text = (
        f"👤 <b>{_esc(name)}</b>\n\n"
        f"🆔 DB ID: <code>{uid}</code>\n"
        f"📱 Telegram ID: <code>{u.get('telegram_id')}</code>\n"
        f"🔗 Username: @{_esc(u.get('username') or '-') }\n"
        f"🌐 Language: {_esc(u.get('language_code') or '-')}\n"
        f"📅 Joined: {_esc(u.get('created_at') or '-')}\n"
        f"📦 Orders: <b>{int(stats.get('orders') or 0)}</b>\n"
        f"💰 Spent: <b>{int(stats.get('spent') or 0)} Stars</b>\n"
        f"🎫 Tickets: <b>{int(stats.get('tickets') or 0)}</b>\n"
        f"🎁 Referrals: <b>{int(stats.get('referrals') or 0)}</b>\n"
        f"📌 Status: <b>{'🚫 Blocked' if blocked else '🟢 Active'}</b>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=("🟢 Unblock User" if blocked else "🚫 Block User"), callback_data=f"admin:userblock:{uid}:{str(not blocked).lower()}")],
        [InlineKeyboardButton(text="💬 Message User", callback_data=f"admin:usermsg:{uid}")],
        [InlineKeyboardButton(text="🧾 User Orders", callback_data=f"admin:userorders:{uid}")],
        [InlineKeyboardButton(text="🔄 Refresh", callback_data=f"admin:user:{uid}")],
        [InlineKeyboardButton(text="◀️ Users", callback_data="admin:users")],
    ])
    await callback.message.edit_text(text, reply_markup=kb)


@dp.callback_query(F.data.startswith("admin:usermsg:"))
async def admin_user_message_start(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await callback.answer()
    uid = int(callback.data.rsplit(":", 1)[1])
    ADMIN_FLOWS[callback.from_user.id] = {"type": "user_message", "step": 1, "user_id": uid}
    await callback.message.answer(f"💬 Send the message for user #{uid}.\n\n/canceladmin to cancel.")


@dp.callback_query(F.data.startswith("admin:userorders:"))
async def admin_user_orders(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await callback.answer()
    uid = int(callback.data.rsplit(":", 1)[1])
    rows = await fetch_all("SELECT id,status,total_stars,created_at FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 25", (uid,))
    lines = [f"🧾 <b>Orders for User #{uid}</b>", ""]
    buttons = []
    for r in rows:
        oid = int(r["id"])
        lines.append(f"#{oid} · {int(r.get('total_stars') or 0)} ⭐ · {_esc(r.get('status'))}")
        buttons.append([InlineKeyboardButton(text=f"🧾 Order #{oid}", callback_data=f"admin:order:{oid}")])
    buttons.append([InlineKeyboardButton(text="◀️ User", callback_data=f"admin:user:{uid}")])
    await callback.message.edit_text("\n".join(lines) if rows else "No orders found.", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


async def render_admin_users(callback: CallbackQuery, page: int | None = None):
    """Render the admin user directory in 50-user pages and a compact 3-column grid."""
    admin_id = int(callback.from_user.id)
    view = ADMIN_USER_VIEWS.setdefault(admin_id, {"page": 0})
    if page is not None:
        view["page"] = max(0, int(page))

    page_size = 50
    count_row = await fetch_one("SELECT COUNT(*) AS c FROM users") or {}
    total = max(0, int(count_row.get("c") or 0))
    page_count = max(1, (total + page_size - 1) // page_size)
    view["page"] = min(max(0, int(view.get("page", 0))), page_count - 1)
    current_page = view["page"]
    offset = current_page * page_size

    rows = await fetch_all(
        """SELECT id,telegram_id,username,first_name,is_blocked,created_at
           FROM users
           ORDER BY created_at DESC NULLS LAST, id DESC
           LIMIT ? OFFSET ?""",
        (page_size, offset),
    )

    first_item = offset + 1 if total else 0
    last_item = min(offset + len(rows), total)
    text = (
        "👥 <b>Users</b>\n\n"
        f"Total users: <b>{total:,}</b>\n"
        f"Page <b>{current_page + 1} / {page_count}</b>\n"
        f"Showing <b>{first_item:,}-{last_item:,}</b>\n\n"
        "Tap a user to open details.\n"
        "🟢 Active · 🚫 Blocked"
    )

    buttons = []
    row_buttons = []
    for r in rows:
        uid = int(r["id"])
        raw_name = str(r.get("first_name") or r.get("username") or r.get("telegram_id") or "User")
        name = " ".join(raw_name.replace("\n", " ").replace("\r", " ").split())
        prefix = "🚫" if r.get("is_blocked") else "🟢"
        label = f"{prefix} #{uid} {name}"[:64]
        row_buttons.append(
            InlineKeyboardButton(text=label, callback_data=f"admin:user:{uid}")
        )
        if len(row_buttons) == 3:
            buttons.append(row_buttons)
            row_buttons = []
    if row_buttons:
        buttons.append(row_buttons)

    navigation = []
    if current_page > 0:
        navigation.append(
            InlineKeyboardButton(
                text="◀️ Previous",
                callback_data=f"admin:users:page:{current_page - 1}",
            )
        )
    if current_page < page_count - 1:
        navigation.append(
            InlineKeyboardButton(
                text="Next ▶️",
                callback_data=f"admin:users:page:{current_page + 1}",
            )
        )
    if navigation:
        buttons.append(navigation)

    buttons.append([InlineKeyboardButton(text="🔎 Search User", callback_data="admin:usersearch")])
    buttons.append([
        InlineKeyboardButton(text="🔄 Refresh", callback_data="admin:users:refresh"),
        InlineKeyboardButton(text="◀️ Back", callback_data="admin:dashboard"),
    ])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data == "admin:users")
async def admin_users_callback_advanced(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await callback.answer()
    try:
        await render_admin_users(callback)
    except Exception:
        log.exception("Failed to render paginated admin users list")
        await callback.message.edit_text(
            "❌ Unable to load users right now.",
            reply_markup=admin_back_keyboard("admin:dashboard"),
        )


@dp.callback_query(F.data.startswith("admin:users:page:"))
async def admin_users_page_callback(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    try:
        page = int(callback.data.rsplit(":", 1)[1])
        await callback.answer()
        await render_admin_users(callback, page=page)
    except (TypeError, ValueError):
        await callback.answer("Invalid page.", show_alert=True)
    except Exception:
        log.exception("Failed to change admin users page")
        await callback.message.edit_text(
            "❌ Unable to load this page. Please retry.",
            reply_markup=admin_back_keyboard("admin:users"),
        )


@dp.callback_query(F.data == "admin:users:refresh")
async def admin_users_refresh_callback(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await callback.answer()
    try:
        await render_admin_users(callback)
    except Exception:
        log.exception("Failed to refresh admin users page")
        await callback.message.edit_text(
            "❌ Unable to refresh users. Please retry.",
            reply_markup=admin_back_keyboard("admin:users"),
        )


# ------------------------- Giveaway delivery state -------------------------
_GIVEAWAY_DRAW_LOCKS = {}


def _giveaway_lock(giveaway_id: int):
    lock = _GIVEAWAY_DRAW_LOCKS.get(int(giveaway_id))
    if lock is None:
        lock = asyncio.Lock()
        _GIVEAWAY_DRAW_LOCKS[int(giveaway_id)] = lock
    return lock


def _giveaway_time_has_ended(value) -> bool:
    if not value:
        return False
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip().replace('Z', '+00:00')
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt <= datetime.now(timezone.utc)


async def _deliver_giveaway_winner(winner_id: int) -> bool:
    row = await fetch_one(
        """SELECT w.*, g.title, u.telegram_id
           FROM giveaway_winners w
           JOIN giveaways g ON g.id=w.giveaway_id
           JOIN users u ON u.id=w.user_id
           WHERE w.id=?""",
        (int(winner_id),),
    )
    if not row:
        return False

    status = str(row.get('delivery_status') or 'pending').lower()
    if status == 'delivered':
        return True

    await fetch_one(
        "UPDATE giveaway_winners SET delivery_status='delivering', delivery_error='' WHERE id=? RETURNING id",
        (int(winner_id),),
    )
    try:
        prize = str(row.get('prize') or 'Giveaway prize')
        await bot.send_message(
            int(row['telegram_id']),
            "🏆 <b>Congratulations!</b>\n\n"
            f"You are a winner of <b>{html.escape(str(row.get('title') or 'Giveaway'))}</b>.\n\n"
            "🎁 <b>Your prize:</b>\n"
            f"<pre>{html.escape(prize)}</pre>\n\n"
            "Thank you for participating in CPM SHOP!",
        )
    except Exception as exc:
        error = str(exc)[:1000]
        await fetch_one(
            "UPDATE giveaway_winners SET delivery_status='failed', delivery_error=? WHERE id=? RETURNING id",
            (error, int(winner_id)),
        )
        log.exception("Giveaway prize delivery failed | winner=%s", winner_id)
        return False

    await fetch_one(
        "UPDATE giveaway_winners SET delivery_status='delivered', delivered_at=CURRENT_TIMESTAMP, delivery_error='' WHERE id=? RETURNING id",
        (int(winner_id),),
    )
    return True


async def _retry_failed_giveaway_deliveries(giveaway_id: int):
    rows = await fetch_all(
        "SELECT id FROM giveaway_winners WHERE giveaway_id=? AND delivery_status IN ('pending','failed') ORDER BY id",
        (int(giveaway_id),),
    )
    delivered = 0
    failed = 0
    for row in rows:
        if await _deliver_giveaway_winner(int(row['id'])):
            delivered += 1
        else:
            failed += 1
    return delivered, failed

# ------------------------- Giveaways -------------------------
@dp.callback_query(F.data == "admin:giveaways")
async def admin_giveaways_callback(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await callback.answer()
    rows = await fetch_all("SELECT id,title,status,winner_count,starts_at,ends_at,created_at FROM giveaways ORDER BY id DESC LIMIT 25")
    lines = ["🎉 <b>Giveaways</b>", ""]
    buttons = []
    for r in rows:
        gid = int(r["id"])
        entry = await fetch_one("SELECT COUNT(*) AS c FROM giveaway_entries WHERE giveaway_id=?", (gid,))
        winners = await fetch_one("SELECT COUNT(*) AS c FROM giveaway_winners WHERE giveaway_id=?", (gid,))
        lines.append(f"#{gid} • <b>{_esc(r.get('title'))}</b> · {_esc(r.get('status'))} · 👥 {int(entry.get('c') or 0)} · 🏆 {int(winners.get('c') or 0)}")
        buttons.append([InlineKeyboardButton(text=f"🎉 #{gid} {str(r.get('title') or '')[:28]}", callback_data=f"admin:giveaway:{gid}")])
    buttons += [[InlineKeyboardButton(text="➕ Create Giveaway", callback_data="admin:addgiveaway")], [InlineKeyboardButton(text="🔄 Refresh", callback_data="admin:giveaways"), InlineKeyboardButton(text="◀️ Back", callback_data="admin:dashboard")]]
    await callback.message.edit_text("\n".join(lines) if rows else "🎉 <b>Giveaways</b>\n\nNo giveaways yet.", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data.startswith("admin:giveaway:"))
async def admin_giveaway_detail(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    try:
        await callback.answer()
    except Exception:
        # The callback may already have been answered by an action handler.
        pass
    gid = int(callback.data.rsplit(":", 1)[1])
    g = await fetch_one("SELECT * FROM giveaways WHERE id=?", (gid,))
    if not g:
        await callback.message.edit_text("❌ Giveaway not found.", reply_markup=admin_back_keyboard("admin:giveaways"))
        return
    entry = await fetch_one("SELECT COUNT(*) AS c FROM giveaway_entries WHERE giveaway_id=?", (gid,))
    winners = await fetch_all("SELECT u.telegram_id,u.username,u.first_name FROM giveaway_winners w JOIN users u ON u.id=w.user_id WHERE w.giveaway_id=? ORDER BY w.id", (gid,))
    text = (
        f"🎉 <b>{_esc(g.get('title'))}</b>\n\n"
        f"🆔 <code>{gid}</code>\n📌 Status: <b>{_esc(g.get('status'))}</b>\n"
        f"🏆 Winner count: <b>{int(g.get('winner_count') or 1)}</b>\n"
        f"👥 Entries: <b>{int(entry.get('c') or 0)}</b>\n"
        f"🕐 Starts: {_esc(g.get('starts_at') or '-') }\n🕐 Ends: {_esc(g.get('ends_at') or '-')}\n\n"
        f"📝 {_esc(g.get('description') or 'No description')[:800]}"
    )
    if winners:
        winner_rows = await fetch_all("SELECT u.username,u.telegram_id,w.delivery_status,w.delivery_error FROM giveaway_winners w JOIN users u ON u.id=w.user_id WHERE w.giveaway_id=? ORDER BY w.id", (gid,))
        text += "\n\n<b>🏆 Winners</b>\n" + "\n".join(
            f"• @{_esc(w.get('username') or '-')} · <code>{w.get('telegram_id')}</code> · {_esc(w.get('delivery_status') or 'pending')}"
            for w in winner_rows
        )
    status = str(g.get("status") or "draft")
    buttons = []
    if status != "active":
        buttons.append([InlineKeyboardButton(text="🟢 Start Giveaway", callback_data=f"admin:giveawaystart:{gid}")])
    else:
        buttons.append([InlineKeyboardButton(text="🔴 End Giveaway", callback_data=f"admin:giveawayend:{gid}")])
    buttons.append([InlineKeyboardButton(text="🏆 Draw Winners", callback_data=f"admin:giveawaydraw:{gid}")])
    if winners:
        buttons.append([InlineKeyboardButton(text="🔄 Retry Failed Deliveries", callback_data=f"admin:giveawayretry:{gid}")])
    buttons.append([InlineKeyboardButton(text="🗑️ Delete Giveaway", callback_data=f"admin:giveawaydelete:{gid}")])
    buttons.append([InlineKeyboardButton(text="◀️ Giveaways", callback_data="admin:giveaways")])
    await callback.message.edit_text(text[:3900], reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data == "admin:addgiveaway")
async def admin_addgiveaway(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await callback.answer()
    ADMIN_FLOWS[callback.from_user.id] = {"type": "giveaway", "step": 1, "data": {}}
    await callback.message.answer(
        "🎉 <b>Create Giveaway</b>\n\n"
        "Send the giveaway title.\n\n/canceladmin to cancel."
    )


@dp.callback_query(F.data.startswith("admin:giveawaystart:"))
async def admin_giveaway_start(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await callback.answer("Starting giveaway…")
    gid = int(callback.data.rsplit(":", 1)[1])

    # Activate only once. Repeated/stale clicks must not rebroadcast the same giveaway.
    activated = await fetch_one(
        "UPDATE giveaways SET status='active' WHERE id=? AND status!='active' RETURNING id",
        (gid,),
    )
    if not activated:
        giveaway = await fetch_one("SELECT id,status FROM giveaways WHERE id=?", (gid,))
        if not giveaway:
            await callback.message.answer("❌ Giveaway not found.", reply_markup=admin_back_keyboard("admin:giveaways"))
            return
        await callback.message.answer(
            "ℹ️ This giveaway is already active; no duplicate announcement was sent.",
            reply_markup=admin_menu(),
        )
        await admin_giveaway_detail(callback)
        return

    await admin_log("giveaway_start", callback.from_user.id, str(gid))
    # Only the explicit Start Giveaway action sends announcements to customers.
    try:
        announcement = await broadcast_giveaway_started(gid)
        await callback.message.answer(
            "📢 <b>Giveaway activated and announcement completed.</b>\n\n"
            f"✅ Sent: <b>{int(announcement.get('sent') or 0)}</b>\n"
            f"❌ Failed: <b>{int(announcement.get('failed') or 0)}</b>",
            reply_markup=admin_menu(),
        )
    except Exception as broadcast_error:
        log.exception("Giveaway start broadcast failed | giveaway=%s", gid)
        await callback.message.answer(
            "⚠️ <b>Giveaway is active, but the announcement failed.</b>\n\n"
            "You can retry after checking the bot logs.\n"
            f"<code>{html.escape(str(broadcast_error))}</code>",
            reply_markup=admin_menu(),
        )
    await admin_giveaway_detail(callback)


@dp.callback_query(F.data.startswith("admin:giveawayend:"))
async def admin_giveaway_end(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await callback.answer()
    gid = int(callback.data.rsplit(":", 1)[1])
    await fetch_one("UPDATE giveaways SET status='ended',ends_at=COALESCE(ends_at,CURRENT_TIMESTAMP) WHERE id=? RETURNING id", (gid,))
    await admin_log("giveaway_end", callback.from_user.id, str(gid))
    await admin_giveaway_detail(callback)


@dp.callback_query(F.data.startswith("admin:giveawaydraw:"))
async def admin_giveaway_draw(callback: CallbackQuery):
    """Safely draw once, only after the giveaway has ended, then deliver winners."""
    if not await require_admin_callback(callback):
        return
    gid = int(callback.data.rsplit(":", 1)[1])
    lock = _giveaway_lock(gid)
    if lock.locked():
        await callback.answer("This giveaway is already being processed.", show_alert=True)
        return

    async with lock:
        await callback.answer()
        try:
            g = await fetch_one("SELECT * FROM giveaways WHERE id=?", (gid,))
            if not g:
                await callback.message.answer("❌ Giveaway not found.")
                return

            status = str(g.get("status") or "draft").lower()
            if status not in {"ended", "active"}:
                await callback.answer("Start the giveaway before drawing winners.", show_alert=True)
                return
            if status == "active" and not _giveaway_time_has_ended(g.get("ends_at")):
                await callback.answer("The giveaway has not ended yet.", show_alert=True)
                return
            if status == "active":
                await fetch_one(
                    "UPDATE giveaways SET status='ended', ends_at=COALESCE(ends_at,CURRENT_TIMESTAMP) WHERE id=? RETURNING id",
                    (gid,),
                )

            existing = await fetch_one(
                "SELECT COUNT(*) AS c FROM giveaway_winners WHERE giveaway_id=?", (gid,)
            )
            if existing and int(existing.get("c") or 0) > 0:
                delivered, failed = await _retry_failed_giveaway_deliveries(gid)
                await callback.message.answer(
                    f"ℹ️ Winners were already drawn.\n📦 Retried: {delivered} delivered, {failed} still failed."
                )
                await admin_giveaway_detail(callback)
                return

            count = max(1, int(g.get("winner_count") or g.get("winners_count") or 1))
            entries = await fetch_all("SELECT user_id FROM giveaway_entries WHERE giveaway_id=?", (gid,))
            if not entries:
                await callback.answer("No entries yet.", show_alert=True)
                return
            winners = secrets.SystemRandom().sample(entries, min(count, len(entries)))

            raw_prize = str(g.get("prize") or "").strip()
            prize_pool = []
            if raw_prize:
                try:
                    parsed = json.loads(raw_prize)
                    prize_pool = [str(x).strip() for x in parsed] if isinstance(parsed, list) else [raw_prize]
                    prize_pool = [x for x in prize_pool if x]
                except Exception:
                    prize_pool = [raw_prize]
            if not prize_pool:
                prize_pool = ["Giveaway prize"]

            product_id = g.get("product_id")
            created_winners = []
            for position, winner in enumerate(winners, start=1):
                user_id = int(winner["user_id"])
                user = await fetch_one("SELECT telegram_id FROM users WHERE id=?", (user_id,))
                if not user:
                    continue
                prize_value = prize_pool[min(position - 1, len(prize_pool) - 1)]

                if product_id:
                    item = await fetch_one(
                        "SELECT id,item_data,secret_data,data FROM inventory_items WHERE product_id=? AND status='available' ORDER BY id ASC LIMIT 1",
                        (int(product_id),),
                    )
                    if item:
                        reserved = await fetch_one(
                            "UPDATE inventory_items SET status='sold', sold_at=CURRENT_TIMESTAMP, sold_order_id=? WHERE id=? AND status='available' RETURNING id,item_data,secret_data,data",
                            (f"GIVEAWAY-{gid}-{user_id}-{position}", int(item['id'])),
                        )
                        if reserved:
                            prize_value = str(reserved.get('item_data') or reserved.get('secret_data') or reserved.get('data') or prize_value).strip()
                    else:
                        prize_value = "Prize temporarily unavailable. Please contact support."

                row = await fetch_one(
                    """INSERT INTO giveaway_winners(giveaway_id,user_id,prize,delivery_status)
                       VALUES(?,?,?,'pending') ON CONFLICT(giveaway_id,user_id) DO NOTHING RETURNING id""",
                    (gid, user_id, prize_value),
                )
                if row and row.get("id"):
                    created_winners.append(int(row["id"]))

            delivered = 0
            failed = 0
            for winner_id in created_winners:
                if await _deliver_giveaway_winner(winner_id):
                    delivered += 1
                else:
                    failed += 1

            await admin_log("giveaway_draw", callback.from_user.id, str(gid))
            await callback.message.answer(
                f"🏆 <b>Winners drawn:</b> {len(created_winners)}\n"
                f"📦 <b>Prizes delivered:</b> {delivered}\n"
                f"⚠️ <b>Delivery issues:</b> {failed}"
            )
            await admin_giveaway_detail(callback)
        except Exception as exc:
            log.exception("Giveaway draw/delivery failed | giveaway=%s", gid)
            await callback.message.answer(
                "❌ <b>Giveaway draw failed.</b>\n\n"
                f"<code>{html.escape(str(exc))}</code>"
            )


@dp.callback_query(F.data.startswith("admin:giveawayretry:"))
async def admin_giveaway_retry(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    gid = int(callback.data.rsplit(":", 1)[1])
    lock = _giveaway_lock(gid)
    if lock.locked():
        await callback.answer("This giveaway is already being processed.", show_alert=True)
        return
    async with lock:
        await callback.answer()
        delivered, failed = await _retry_failed_giveaway_deliveries(gid)
        await callback.message.answer(f"🔄 Retry complete: {delivered} delivered, {failed} still failed.")
        await admin_giveaway_detail(callback)

@dp.callback_query(F.data.startswith("admin:giveawaydelete:"))
async def admin_giveaway_delete(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await callback.answer()
    gid = int(callback.data.rsplit(":", 1)[1])
    await fetch_one("DELETE FROM giveaways WHERE id=? RETURNING id", (gid,))
    await admin_log("giveaway_delete", callback.from_user.id, str(gid))
    await admin_giveaways_callback(callback)


@dp.callback_query(F.data.startswith("admin:productimages:"))
async def admin_product_images_menu(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await callback.answer()
    pid = int(callback.data.rsplit(":", 1)[1])
    product = await get_product(pid)
    if not product:
        await callback.message.edit_text("❌ Product not found.", reply_markup=admin_back_keyboard("admin:products"))
        return
    images = await get_product_images(pid)
    lines = [f"🖼️ <b>Product #{pid} Images</b>", f"📦 {_esc(product.get('name') or '')}", ""]
    if not images:
        lines.append("No images added yet.")
    else:
        for i, image in enumerate(images, 1):
            ref = str(image.get("image") or "")
            label = ref[:45] + ("…" if len(ref) > 45 else "")
            lines.append(f"{i}. <code>{_esc(label)}</code>")
    buttons = [[InlineKeyboardButton(text="➕ Add Image", callback_data=f"admin:addproductimage:{pid}")]]
    for image in images:
        iid = int(image["id"])
        buttons.append([InlineKeyboardButton(text=f"🗑️ Delete Image #{iid}", callback_data=f"admin:deleteproductimage:{pid}:{iid}")])
    buttons.append([InlineKeyboardButton(text="◀️ Product", callback_data=f"admin:product:{pid}")])
    await callback.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data.startswith("admin:doneproductimages:"))
async def admin_done_product_images(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await callback.answer("Images saved.")
    pid = int(callback.data.rsplit(":", 1)[1])
    ADMIN_FLOWS.pop(callback.from_user.id, None)
    await admin_product_images_menu(callback)


@dp.callback_query(F.data.startswith("admin:addproductimage:"))
async def admin_add_product_image_start(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await callback.answer()
    pid = int(callback.data.rsplit(":", 1)[1])
    if not await get_product(pid):
        await callback.message.answer("❌ Product not found.")
        return
    ADMIN_FLOWS[callback.from_user.id] = {"type": "product_image", "product_id": pid}
    await callback.message.answer(
        f"🖼️ <b>Add images to product #{pid}</b>\n\n"
        "Send one or more Telegram photos. Every photo will be added to this product. "
        "You can send a Telegram album too.\n\n"
        "When you finish, tap <b>Done</b>.\n"
        "/canceladmin to cancel.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Done", callback_data=f"admin:doneproductimages:{pid}")],
            [InlineKeyboardButton(text="🖼️ View Images", callback_data=f"admin:productimages:{pid}")],
        ])
    )


@dp.callback_query(F.data.startswith("admin:productbanner:"))
async def admin_product_banner_start(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await callback.answer()
    pid = int(callback.data.rsplit(":", 1)[1])
    if not await get_product(pid):
        await callback.message.answer("❌ Product not found.")
        return
    ADMIN_FLOWS[callback.from_user.id] = {"type": "product_banner", "product_id": pid}
    await callback.message.answer(
        f"🖼️ <b>Set banner for product #{pid}</b>\n\n"
        "Send a Telegram photo. It will become the product banner and will also be added to the product images if needed.\n\n"
        "/canceladmin to cancel."
    )


@dp.callback_query(F.data.startswith("admin:deleteproductimage:"))
async def admin_delete_product_image(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await callback.answer()
    parts = callback.data.split(":")
    pid, iid = int(parts[2]), int(parts[3])
    row = await fetch_one("SELECT id,image FROM product_images WHERE id=? AND product_id=?", (iid, pid))
    if not row:
        await callback.answer("Image not found.", show_alert=True)
        return
    await fetch_one("DELETE FROM product_images WHERE id=? RETURNING id", (iid,))
    next_img = await fetch_one("SELECT image FROM product_images WHERE product_id=? ORDER BY sort_order ASC,id ASC LIMIT 1", (pid,))
    await fetch_one("UPDATE products SET banner=? WHERE id=? RETURNING id", (next_img.get("image") if next_img else None, pid))
    await admin_log("delete_product_image", callback.from_user.id, f"{pid}:{iid}")
    await admin_product_images_menu(callback)


@dp.callback_query(F.data.startswith("admin:edit:"))
async def admin_edit_field_start(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await callback.answer()
    _, _, kind, raw_id, field = callback.data.split(":", 4)
    item_id = int(raw_id)
    prompts = {
        "name": "Send the new name.",
        "description": "Send the new description.",
        "price_stars": "Send the new price in Telegram Stars.",
        "stock": "Send the new stock quantity.",
        "game_id": "Send the Game ID, or 0 for none.",
        "category_id": "Send the Category ID, or 0 for none.",
        "discount_percent": "Send discount percent (0-100).",
        "featured": "Send yes/no for Featured.",
        "product_id": "Send the Product ID.",
        "item_data": "Send the new inventory item data.",
        "status": "Send status: available, sold, draft, or reserved.",
        "image": "Send image URL, or - to clear it.",
        "banner": "Send banner URL, or - to clear it.",
        "sort_order": "Send sort order number.",
        "code": "Send the new promo code.",
        "discount_stars": "Send fixed Stars discount.",
        "max_uses": "Send max uses, or 0 for unlimited.",
        "expires_at": "Send expiry timestamp, or - for no expiry. Example: 2026-12-31 23:59:00",
    }
    ADMIN_FLOWS[callback.from_user.id] = {"type": "edit_field", "step": 1, "kind": kind, "id": item_id, "field": field}

    # Game/category images can now be sent directly as a Telegram photo.
    # Keep URL/file_id text editing available as a fallback.
    if kind in {"g", "c"} and field == "image":
        label = "game" if kind == "g" else "category"
        await callback.message.answer(
            f"🖼️ <b>Edit {label} image</b>\n\n"
            "Send the new image directly as a Telegram photo. "
            "You can also send a direct image URL or Telegram file_id.\n\n"
            "/canceladmin to cancel."
        )
        return

    # Relationship fields use selectable buttons instead of asking the admin to type IDs.
    if kind == "p" and field == "game_id":
        await callback.message.edit_text("🎮 <b>Select the new game:</b>", reply_markup=await _game_picker_keyboard("admin:editproductgame", "admin:product:" + str(item_id)))
        return
    if kind == "p" and field == "category_id":
        product = await get_product(item_id)
        if not product or not product.get("game_id"):
            await callback.message.answer("❌ Select a game for this product first.")
            return
        await callback.message.edit_text("🗂️ <b>Select the new category:</b>", reply_markup=await _category_picker_keyboard(int(product["game_id"]), "admin:editproductcategory", "admin:product:" + str(item_id)))
        return
    if kind == "c" and field == "game_id":
        await callback.message.edit_text("🎮 <b>Select the new game:</b>", reply_markup=await _game_picker_keyboard("admin:editcategorygame", "admin:cat:" + str(item_id)))
        return
    if kind == "i" and field == "product_id":
        await callback.message.edit_text("📦 <b>Select the new product:</b>", reply_markup=await _product_picker_keyboard("admin:editinventoryproduct", "admin:inventory"))
        return

    await callback.message.answer(
        f"✏️ <b>Edit #{item_id}</b>\n\n"
        f"{prompts.get(field, 'Send the new value.')}\n\n"
        "/canceladmin to cancel."
    )



@dp.callback_query(F.data.startswith("admin:editproductgame:"))
async def admin_edit_product_game(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer(); gid=int(callback.data.rsplit(":",1)[1]); flow=ADMIN_FLOWS.get(callback.from_user.id)
    if not flow: return
    pid=int(flow["id"]); await fetch_one("UPDATE products SET game_id=?, category_id=NULL WHERE id=? RETURNING id",(gid,pid)); ADMIN_FLOWS.pop(callback.from_user.id,None); await admin_log("edit_p",callback.from_user.id,f"{pid}:game_id={gid}"); await admin_product_detail_advanced(callback)

@dp.callback_query(F.data.startswith("admin:editproductcategory:"))
async def admin_edit_product_category(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer(); cid=int(callback.data.rsplit(":",1)[1]); flow=ADMIN_FLOWS.get(callback.from_user.id)
    if not flow: return
    pid=int(flow["id"]); await fetch_one("UPDATE products SET category_id=? WHERE id=? RETURNING id",(cid,pid)); ADMIN_FLOWS.pop(callback.from_user.id,None); await admin_log("edit_p",callback.from_user.id,f"{pid}:category_id={cid}"); await admin_product_detail_advanced(callback)

@dp.callback_query(F.data.startswith("admin:editcategorygame:"))
async def admin_edit_category_game(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer(); gid=int(callback.data.rsplit(":",1)[1]); flow=ADMIN_FLOWS.get(callback.from_user.id)
    if not flow: return
    cid=int(flow["id"]); await fetch_one("UPDATE categories SET game_id=? WHERE id=? RETURNING id",(gid,cid)); ADMIN_FLOWS.pop(callback.from_user.id,None); await admin_log("edit_c",callback.from_user.id,f"{cid}:game_id={gid}"); await admin_category_detail(callback)

@dp.callback_query(F.data.startswith("admin:editinventoryproduct:"))
async def admin_edit_inventory_product(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer(); pid=int(callback.data.rsplit(":",1)[1]); flow=ADMIN_FLOWS.get(callback.from_user.id)
    if not flow: return
    iid=int(flow["id"]); old=await fetch_one("SELECT product_id FROM inventory_items WHERE id=?",(iid,)); await fetch_one("UPDATE inventory_items SET product_id=? WHERE id=? RETURNING id",(pid,iid))
    if old and old.get("product_id"): await _sync_product_stock(int(old["product_id"]))
    await _sync_product_stock(pid); ADMIN_FLOWS.pop(callback.from_user.id,None); await admin_log("edit_i",callback.from_user.id,f"{iid}:product_id={pid}"); await admin_inventory_callback(callback)

@dp.callback_query(F.data == "admin:more")
async def admin_more_callback(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await callback.answer()
    rows = [
        [InlineKeyboardButton(text="📝 Admin Notes", callback_data="admin:notes"),
         InlineKeyboardButton(text="✏️ Content Editor", callback_data="admin:content")],
        [InlineKeyboardButton(text="⭐ Customer Feedback", callback_data="admin:feedback"),
         InlineKeyboardButton(text="🎫 Ticket Center", callback_data="admin:tickets")],
        [InlineKeyboardButton(text="⭐ Reviews", callback_data="admin:reviews"),
         InlineKeyboardButton(text="🐞 Bug Reports", callback_data="admin:bugs")],
        [InlineKeyboardButton(text="📜 Admin Logs", callback_data="admin:logs"),
         InlineKeyboardButton(text="⚙️ Settings", callback_data="admin:settings")],
        [InlineKeyboardButton(text="🌐 Web Admin", callback_data="admin:web")],
        [InlineKeyboardButton(text="◀️ Dashboard", callback_data="admin:dashboard")],
    ]
    await callback.message.edit_text(
        "⚙️ <b>More Admin Tools</b>\n\n"
        "Manage the store directly from Telegram.\n\n"
        "📝 Notes · ✏️ Customer-facing content · ⭐ Feedback · 🎫 Tickets",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@dp.callback_query(F.data == "admin:notes")
async def admin_notes_callback(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await callback.answer()
    rows = await __import__('database').list_admin_notes(50)
    buttons = [[InlineKeyboardButton(text="➕ New Note", callback_data="admin:notenew")]]
    lines = ["📝 <b>Admin Notes</b>", "", "Private notes for store administration.", ""]
    for row in rows:
        nid = int(row["id"])
        title = str(row.get("title") or "Untitled")[:45]
        preview = str(row.get("content") or "").replace("\n", " ")[:70]
        lines.append(f"• <b>#{nid}</b> {_esc(title)} — {_esc(preview)}")
        buttons.append([InlineKeyboardButton(text=f"📝 #{nid} {title}", callback_data=f"admin:note:{nid}")])
    buttons.append([InlineKeyboardButton(text="◀️ More", callback_data="admin:more")])
    await callback.message.edit_text("\n".join(lines) if rows else "📝 <b>Admin Notes</b>\n\nNo notes yet.", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data == "admin:notenew")
async def admin_note_new_callback(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer()
    ADMIN_FLOWS[callback.from_user.id] = {"type": "admin_note", "mode": "create", "step": 1, "data": {}}
    await callback.message.answer("📝 <b>New Admin Note</b>\n\nSend the note title.\n\n/canceladmin to cancel.")


@dp.callback_query(F.data.startswith("admin:note:"))
async def admin_note_detail_callback(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer()
    nid = int(callback.data.rsplit(":", 1)[1])
    row = await __import__('database').get_admin_note(nid)
    if not row:
        await callback.message.edit_text("❌ Note not found.", reply_markup=admin_back_keyboard("admin:notes"))
        return
    text = f"📝 <b>Admin Note #{nid}</b>\n\n<b>{_esc(row.get('title') or 'Untitled')}</b>\n\n{_esc(row.get('content') or '')[:3500]}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Edit", callback_data=f"admin:noteedit:{nid}"), InlineKeyboardButton(text="🗑️ Delete", callback_data=f"admin:notedelete:{nid}")],
        [InlineKeyboardButton(text="◀️ Notes", callback_data="admin:notes")],
    ])
    await callback.message.edit_text(text, reply_markup=kb)


@dp.callback_query(F.data.startswith("admin:noteedit:"))
async def admin_note_edit_callback(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer()
    nid = int(callback.data.rsplit(":", 1)[1])
    row = await __import__('database').get_admin_note(nid)
    if not row:
        await callback.message.answer("❌ Note not found.")
        return
    ADMIN_FLOWS[callback.from_user.id] = {"type": "admin_note", "mode": "edit", "step": 1, "note_id": nid, "data": {"title": str(row.get('title') or ""), "content": str(row.get('content') or "")}}
    await callback.message.answer(f"✏️ <b>Edit Note #{nid}</b>\n\nSend the new title.\n\nCurrent: <b>{_esc(row.get('title') or '')}</b>\n\n/canceladmin to cancel.")


@dp.callback_query(F.data.startswith("admin:notedelete:"))
async def admin_note_delete_callback(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer()
    nid = int(callback.data.rsplit(":", 1)[1])
    await __import__('database').delete_admin_note(nid)
    await admin_log("delete_admin_note", callback.from_user.id, str(nid))
    await admin_notes_callback(callback)


CONTENT_EDITOR_KEYS = [
    ("welcome_title", "Welcome Title"),
    ("welcome_description", "Welcome Description"),
    ("welcome_image", "Welcome Image"),
    ("welcome_button_1", "Start Button 1"),
    ("welcome_button_2", "Start Button 2"),
    ("help_title", "Help Title"),
    ("help_description", "Help Description"),
    ("shop_description", "Shop Description"),
    ("support_description", "Support Description"),
    ("help_image", "Help Image"),
    ("rewards_description", "Rewards Description"),
    ("promo_description", "Promo Description"),
    ("orders_description", "Orders Description"),
    ("notifications_description", "Notifications Description"),
    ("giveaways_description", "Giveaways Description"),
    ("referral_description", "Referral Description"),
    ("feedback_description", "Feedback Description"),
    ("contact_admin_description", "Contact Admin Description"),
    ("promo_purchase_description", "Promo Purchase Description"),
]


@dp.callback_query(F.data == "admin:content")
async def admin_content_callback(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer()
    lines = ["✏️ <b>Content Editor</b>", "", "Select what you want to edit:"]
    buttons = []
    for key, label in CONTENT_EDITOR_KEYS:
        value = await get_setting(key, "")
        preview = value.replace("\n", " ")[:45] if value else "(empty)"
        buttons.append([InlineKeyboardButton(text=f"✏️ {label}", callback_data=f"admin:contentedit:{key}")])
        lines.append(f"• <b>{_esc(label)}</b>: {_esc(preview)}")
    buttons.append([InlineKeyboardButton(text="◀️ More", callback_data="admin:more")])
    await callback.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data.startswith("admin:contentedit:"))
async def admin_content_edit_callback(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer()
    key = callback.data.split(":", 2)[2]
    allowed = {k for k, _ in CONTENT_EDITOR_KEYS}
    if key not in allowed:
        await callback.message.answer("❌ Invalid content field.")
        return
    current = await get_setting(key, "")
    label = dict(CONTENT_EDITOR_KEYS).get(key, key)
    ADMIN_FLOWS[callback.from_user.id] = {"type": "content_edit", "step": 1, "key": key}
    if key in {"welcome_image", "help_image"}:
        await callback.message.answer(
            f"🖼️ <b>{_esc(label)}</b>\n\n"
            "Send the new image as a Telegram photo.\n\n"
            "The uploaded photo will be saved directly using its Telegram file ID. "
            "Links are not required.\n\n"
            "Use /canceladmin to cancel."
        )
    else:
        await callback.message.answer(f"✏️ <b>{_esc(label)}</b>\n\nCurrent value:\n<code>{_esc(current)[:2500]}</code>\n\nSend the new value. Use /canceladmin to cancel.")


async def _render_admin_feedback(callback: CallbackQuery, page: int = 0, status_filter: str = "all", search: str = ""):
    if not await require_admin_callback(callback): return
    uid=int(callback.from_user.id); status_filter=status_filter if status_filter in {"all","visible","hidden"} else "all"; search=str(search or "").strip()[:80]; page=max(0,int(page))
    where=[]; params=[]
    if status_filter=="hidden": where.append("f.status='hidden'")
    elif status_filter=="visible": where.append("COALESCE(f.status,'new')<>'hidden'")
    if search:
        where.append("(CAST(f.id AS TEXT)=? OR LOWER(COALESCE(f.message,'')) LIKE LOWER(?) OR LOWER(COALESCE(u.username,'')) LIKE LOWER(?) OR LOWER(COALESCE(u.first_name,'')) LIKE LOWER(?))")
        q=f"%{search}%"; params.extend([search,q,q,q])
    cond=(" WHERE "+" AND ".join(where)) if where else ""
    tr=await fetch_one(f"SELECT COUNT(*) AS c FROM feedback f LEFT JOIN users u ON u.id=f.user_id{cond}",tuple(params)); total=int((tr or {}).get('c') or 0); per=8; pages=max(1,(total+per-1)//per); page=min(page,pages-1)
    rows=await fetch_all(f"""SELECT f.id,f.rating,f.message,f.status,f.created_at,f.updated_at,u.telegram_id,u.username,u.first_name
                             FROM feedback f LEFT JOIN users u ON u.id=f.user_id {cond}
                             ORDER BY f.created_at DESC,f.id DESC LIMIT ? OFFSET ?""",tuple(params+[per,page*per]))
    ADMIN_FEEDBACK_VIEWS[uid]={"page":page,"status":status_filter,"search":search}
    counts=await fetch_all("SELECT CASE WHEN status='hidden' THEN 'hidden' ELSE 'visible' END AS bucket,COUNT(*) AS c FROM feedback GROUP BY CASE WHEN status='hidden' THEN 'hidden' ELSE 'visible' END")
    visible_count=sum(int(x.get('c') or 0) for x in counts if x.get('bucket')=='visible'); hidden_count=sum(int(x.get('c') or 0) for x in counts if x.get('bucket')=='hidden')
    lines=["⭐ <b>Customer Feedback</b>","",f"🟢 Visible: <b>{visible_count}</b>  •  ⚫ Hidden: <b>{hidden_count}</b>",f"Filter: <b>{'All' if status_filter=='all' else status_filter.title()}</b>  •  Page <b>{page+1}/{pages}</b>"]
    if search: lines.append(f"🔎 Search: <b>{_esc(search)}</b>")
    buttons=[[InlineKeyboardButton(text="📋 All",callback_data="admin:feedbackfilter:all:0"),InlineKeyboardButton(text="🟢 Visible",callback_data="admin:feedbackfilter:visible:0"),InlineKeyboardButton(text="⚫ Hidden",callback_data="admin:feedbackfilter:hidden:0")],[InlineKeyboardButton(text="🔎 Search",callback_data="admin:feedbacksearch")]]
    for r in rows:
        rid=int(r['id']); name=str(r.get('first_name') or r.get('username') or r.get('telegram_id') or 'User'); state='⚫' if str(r.get('status') or '').lower()=='hidden' else '🟢'; preview=str(r.get('message') or '').replace('\n',' ')[:45]
        lines.append(f"{state} <b>#{rid}</b> · {'⭐'*int(r.get('rating') or 0)} · {_esc(name)} · {_esc(preview)}")
        buttons.append([InlineKeyboardButton(text=f"{state} Feedback #{rid}",callback_data=f"admin:feedbackview:{rid}")])
    nav=[]
    if page>0: nav.append(InlineKeyboardButton(text="◀️ Previous",callback_data=f"admin:feedbackpage:{page-1}"))
    if page<pages-1: nav.append(InlineKeyboardButton(text="Next ▶️",callback_data=f"admin:feedbackpage:{page+1}"))
    if nav: buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="◀️ More",callback_data="admin:more")])
    await callback.message.edit_text("\n".join(lines) if rows else "⭐ <b>Customer Feedback</b>\n\nNo feedback found.",reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@dp.callback_query(F.data == "admin:feedback")
async def admin_feedback_callback(callback: CallbackQuery):
    await callback.answer(); await _render_admin_feedback(callback,0,"all","")

@dp.callback_query(F.data.startswith("admin:feedbackfilter:"))
async def admin_feedback_filter(callback: CallbackQuery):
    await callback.answer(); parts=callback.data.split(":"); await _render_admin_feedback(callback,int(parts[-1]),parts[-2],"")

@dp.callback_query(F.data.startswith("admin:feedbackpage:"))
async def admin_feedback_page(callback: CallbackQuery):
    await callback.answer(); view=ADMIN_FEEDBACK_VIEWS.get(int(callback.from_user.id),{"status":"all","search":""}); await _render_admin_feedback(callback,int(callback.data.rsplit(":",1)[1]),view.get('status','all'),view.get('search',''))

@dp.callback_query(F.data == "admin:feedbacksearch")
async def admin_feedback_search_start(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer(); ADMIN_FLOWS[int(callback.from_user.id)]={"type":"feedback_search","step":1}; await callback.message.answer("🔎 <b>Search Feedback</b>\n\nSend feedback ID, customer username/name, or text.\n\n/canceladmin to cancel.")

@dp.callback_query(F.data.startswith("admin:feedbackview:"))
async def admin_feedback_view(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer(); rid=int(callback.data.rsplit(":",1)[1]); row=await fetch_one("SELECT f.*,u.telegram_id,u.username,u.first_name FROM feedback f LEFT JOIN users u ON u.id=f.user_id WHERE f.id=?",(rid,))
    if not row: await callback.message.edit_text("❌ Feedback not found.",reply_markup=admin_back_keyboard("admin:feedback")); return
    name=str(row.get('first_name') or row.get('username') or row.get('telegram_id') or 'User'); hidden=str(row.get('status') or '').lower()=='hidden'
    text=(f"⭐ <b>Feedback #{rid}</b>\n\n👤 {_esc(name)}\n🆔 <code>{row.get('telegram_id') or '-'}</code>\n⭐ {'⭐'*int(row.get('rating') or 0)}\nStatus: <b>{'Hidden' if hidden else 'Visible'}</b>\n\n💬 {_esc(row.get('message') or '')[:3500]}")
    kb=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Edit Text",callback_data=f"admin:feedbackedit:{rid}"),InlineKeyboardButton(text="⭐ Rating",callback_data=f"admin:feedbackrating:{rid}")],
        [InlineKeyboardButton(text=("🟢 Enable" if hidden else "⚫ Disable"),callback_data=f"admin:feedbacktoggle:{rid}"),InlineKeyboardButton(text="🗑️ Delete",callback_data=f"admin:feedbackdelete:{rid}")],
        [InlineKeyboardButton(text="◀️ Feedback",callback_data="admin:feedback")],
    ])
    await callback.message.edit_text(text,reply_markup=kb)

@dp.callback_query(F.data.startswith("admin:feedbacktoggle:"))
async def admin_feedback_toggle(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer(); rid=int(callback.data.rsplit(":",1)[1]); await fetch_one("UPDATE feedback SET status=CASE WHEN COALESCE(status,'new')='hidden' THEN 'active' ELSE 'hidden' END,updated_at=CURRENT_TIMESTAMP WHERE id=? RETURNING id",(rid,)); await admin_log("feedback_toggle",callback.from_user.id,str(rid)); await admin_feedback_view(callback)

@dp.callback_query(F.data.regexp(r"^admin:feedbackdelete:\d+$"))
async def admin_feedback_delete_prompt(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer(); rid=int(callback.data.rsplit(":",1)[1]); await callback.message.edit_text(f"🗑️ <b>Delete Feedback #{rid}?</b>\n\nThis permanently removes the feedback.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⚠️ Yes, Delete",callback_data=f"admin:feedbackdeleteconfirm:{rid}")],[InlineKeyboardButton(text="◀️ Cancel",callback_data=f"admin:feedbackview:{rid}")]]))

@dp.callback_query(F.data.startswith("admin:feedbackdeleteconfirm:"))
async def admin_feedback_delete_confirm(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer(); rid=int(callback.data.rsplit(":",1)[1]); await fetch_one("DELETE FROM feedback WHERE id=? RETURNING id",(rid,)); await admin_log("feedback_delete",callback.from_user.id,str(rid)); view=ADMIN_FEEDBACK_VIEWS.get(int(callback.from_user.id),{"page":0,"status":"all","search":""}); await _render_admin_feedback(callback,view.get('page',0),view.get('status','all'),view.get('search',''))

@dp.callback_query(F.data.startswith("admin:feedbackedit:"))
async def admin_feedback_edit_start(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer(); rid=int(callback.data.rsplit(":",1)[1]); row=await fetch_one("SELECT message FROM feedback WHERE id=?",(rid,))
    if not row: await callback.message.answer("❌ Feedback not found."); return
    ADMIN_FLOWS[int(callback.from_user.id)]={"type":"feedback_edit","step":1,"id":rid}; await callback.message.answer(f"✏️ <b>Edit Feedback #{rid}</b>\n\nCurrent:\n<code>{_esc(str(row.get('message') or ''))[:3000]}</code>\n\nSend the new feedback text.\n\n/canceladmin to cancel.")

@dp.callback_query(F.data.startswith("admin:feedbackrating:"))
async def admin_feedback_rating(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer(); rid=int(callback.data.rsplit(":",1)[1]); await callback.message.edit_text(f"⭐ <b>Set rating for Feedback #{rid}</b>",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⭐"*n,callback_data=f"admin:feedbacksetrating:{rid}:{n}") for n in range(1,6)],[InlineKeyboardButton(text="◀️ Back",callback_data=f"admin:feedbackview:{rid}")]]))

@dp.callback_query(F.data.startswith("admin:feedbacksetrating:"))
async def admin_feedback_set_rating(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer(); parts=callback.data.split(":"); rid=int(parts[-2]); rating=max(1,min(5,int(parts[-1]))); await fetch_one("UPDATE feedback SET rating=?,updated_at=CURRENT_TIMESTAMP WHERE id=? RETURNING id",(rating,rid)); await admin_log("feedback_rating",callback.from_user.id,f"{rid}:{rating}"); await admin_feedback_view(callback)

async def _render_admin_reviews(callback: CallbackQuery, page: int = 0, visibility: str = "all", search: str = ""):
    if not await require_admin_callback(callback): return
    uid=int(callback.from_user.id); visibility=visibility if visibility in {"all","visible","hidden"} else "all"; search=str(search or "").strip()[:80]; page=max(0,int(page)); where=[]; params=[]
    if visibility=="visible": where.append("r.visible=TRUE")
    elif visibility=="hidden": where.append("r.visible=FALSE")
    if search:
        where.append("(CAST(r.id AS TEXT)=? OR LOWER(COALESCE(r.text,'')) LIKE LOWER(?) OR LOWER(COALESCE(p.name,'')) LIKE LOWER(?) OR LOWER(COALESCE(u.username,'')) LIKE LOWER(?) OR LOWER(COALESCE(u.first_name,'')) LIKE LOWER(?))"); q=f"%{search}%"; params.extend([search,q,q,q,q])
    cond=(" WHERE "+" AND ".join(where)) if where else ""; tr=await fetch_one(f"SELECT COUNT(*) AS c FROM reviews r LEFT JOIN products p ON p.id=r.product_id LEFT JOIN users u ON u.id=r.user_id{cond}",tuple(params)); total=int((tr or {}).get('c') or 0); per=7; pages=max(1,(total+per-1)//per); page=min(page,pages-1)
    rows=await fetch_all(f"""SELECT r.id,r.rating,r.text,r.visible,r.created_at,p.name AS product_name,u.telegram_id,u.username,u.first_name FROM reviews r LEFT JOIN products p ON p.id=r.product_id LEFT JOIN users u ON u.id=r.user_id {cond} ORDER BY r.created_at DESC,r.id DESC LIMIT ? OFFSET ?""",tuple(params+[per,page*per]))
    ADMIN_REVIEW_VIEWS[uid]={"page":page,"visibility":visibility,"search":search}; counts=await fetch_one("SELECT COALESCE(SUM(CASE WHEN visible THEN 1 ELSE 0 END),0) AS visible,COALESCE(SUM(CASE WHEN NOT visible THEN 1 ELSE 0 END),0) AS hidden FROM reviews")
    lines=["⭐ <b>Reviews</b>","",f"🟢 Visible: <b>{int(counts.get('visible') or 0)}</b>  •  🔴 Hidden: <b>{int(counts.get('hidden') or 0)}</b>",f"Filter: <b>{visibility.title()}</b>  •  Page <b>{page+1}/{pages}</b>"]
    if search: lines.append(f"🔎 Search: <b>{_esc(search)}</b>")
    buttons=[[InlineKeyboardButton(text="📋 All",callback_data="admin:reviewfilter:all:0"),InlineKeyboardButton(text="🟢 Visible",callback_data="admin:reviewfilter:visible:0"),InlineKeyboardButton(text="🔴 Hidden",callback_data="admin:reviewfilter:hidden:0")],[InlineKeyboardButton(text="🔎 Search",callback_data="admin:reviewsearch")]]
    for r in rows:
        rid=int(r['id']); name=str(r.get('first_name') or r.get('username') or r.get('telegram_id') or 'User'); state='🟢' if r.get('visible') else '🔴'; preview=str(r.get('text') or '').replace('\n',' ')[:42]; lines.append(f"{state} <b>#{rid}</b> · {'⭐'*int(r.get('rating') or 0)} · {_esc(r.get('product_name') or '-')} · {_esc(name)}"); buttons.append([InlineKeyboardButton(text=f"{state} Review #{rid}",callback_data=f"admin:reviewview:{rid}")])
    nav=[]
    if page>0: nav.append(InlineKeyboardButton(text="◀️ Previous",callback_data=f"admin:reviewpage:{page-1}"))
    if page<pages-1: nav.append(InlineKeyboardButton(text="Next ▶️",callback_data=f"admin:reviewpage:{page+1}"))
    if nav: buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="◀️ More",callback_data="admin:more")]); await callback.message.edit_text("\n".join(lines) if rows else "⭐ <b>Reviews</b>\n\nNo reviews found.",reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@dp.callback_query(F.data == "admin:reviews")
async def admin_reviews_callback(callback: CallbackQuery):
    await callback.answer(); await _render_admin_reviews(callback,0,"all","")

@dp.callback_query(F.data.startswith("admin:reviewfilter:"))
async def admin_review_filter(callback: CallbackQuery):
    await callback.answer(); parts=callback.data.split(":"); await _render_admin_reviews(callback,int(parts[-1]),parts[-2],"")

@dp.callback_query(F.data.startswith("admin:reviewpage:"))
async def admin_review_page(callback: CallbackQuery):
    await callback.answer(); view=ADMIN_REVIEW_VIEWS.get(int(callback.from_user.id),{"visibility":"all","search":""}); await _render_admin_reviews(callback,int(callback.data.rsplit(":",1)[1]),view.get('visibility','all'),view.get('search',''))

@dp.callback_query(F.data == "admin:reviewsearch")
async def admin_review_search_start(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer(); ADMIN_FLOWS[int(callback.from_user.id)]={"type":"review_search","step":1}; await callback.message.answer("🔎 <b>Search Reviews</b>\n\nSend review ID, product name, customer, or review text.\n\n/canceladmin to cancel.")

@dp.callback_query(F.data.startswith("admin:reviewview:"))
async def admin_review_view(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer(); rid=int(callback.data.rsplit(":",1)[1]); row=await fetch_one("SELECT r.*,p.name AS product_name,u.telegram_id,u.username,u.first_name FROM reviews r LEFT JOIN products p ON p.id=r.product_id LEFT JOIN users u ON u.id=r.user_id WHERE r.id=?",(rid,))
    if not row: await callback.message.edit_text("❌ Review not found.",reply_markup=admin_back_keyboard("admin:reviews")); return
    name=str(row.get('first_name') or row.get('username') or row.get('telegram_id') or 'User'); visible=bool(row.get('visible')); text=(f"⭐ <b>Review #{rid}</b>\n\n📦 Product: <b>{_esc(row.get('product_name') or '-')}</b>\n👤 {_esc(name)}\n🆔 <code>{row.get('telegram_id') or '-'}</code>\n⭐ {'⭐'*int(row.get('rating') or 0)}\nStatus: <b>{'Visible' if visible else 'Hidden'}</b>\n\n💬 {_esc(row.get('text') or '')[:3500]}")
    await callback.message.edit_text(text,reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✏️ Edit Text",callback_data=f"admin:reviewedit:{rid}"),InlineKeyboardButton(text="⭐ Rating",callback_data=f"admin:reviewrating:{rid}")],[InlineKeyboardButton(text=("🔴 Hide" if visible else "🟢 Show"),callback_data=f"admin:reviewtoggle:{rid}"),InlineKeyboardButton(text="🗑️ Delete",callback_data=f"admin:reviewdelete:{rid}")],[InlineKeyboardButton(text="◀️ Reviews",callback_data="admin:reviews")]]))

@dp.callback_query(F.data.startswith("admin:reviewtoggle:"))
async def admin_review_toggle(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer(); rid=int(callback.data.rsplit(":",1)[1]); await fetch_one("UPDATE reviews SET visible=NOT visible WHERE id=? RETURNING id",(rid,)); await admin_log("review_toggle",callback.from_user.id,str(rid)); await admin_review_view(callback)

@dp.callback_query(F.data.regexp(r"^admin:reviewdelete:\d+$"))
async def admin_review_delete_prompt(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer(); rid=int(callback.data.rsplit(":",1)[1]); await callback.message.edit_text(f"🗑️ <b>Delete Review #{rid}?</b>\n\nThis permanently removes the review and its votes.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⚠️ Yes, Delete",callback_data=f"admin:reviewdeleteconfirm:{rid}")],[InlineKeyboardButton(text="◀️ Cancel",callback_data=f"admin:reviewview:{rid}")]]))

@dp.callback_query(F.data.startswith("admin:reviewdeleteconfirm:"))
async def admin_review_delete_confirm(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer(); rid=int(callback.data.rsplit(":",1)[1]); await fetch_one("DELETE FROM reviews WHERE id=? RETURNING id",(rid,)); await admin_log("review_delete",callback.from_user.id,str(rid)); view=ADMIN_REVIEW_VIEWS.get(int(callback.from_user.id),{"page":0,"visibility":"all","search":""}); await _render_admin_reviews(callback,view.get('page',0),view.get('visibility','all'),view.get('search',''))

@dp.callback_query(F.data.startswith("admin:reviewedit:"))
async def admin_review_edit_start(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer(); rid=int(callback.data.rsplit(":",1)[1]); row=await fetch_one("SELECT text FROM reviews WHERE id=?",(rid,))
    if not row: await callback.message.answer("❌ Review not found."); return
    ADMIN_FLOWS[int(callback.from_user.id)]={"type":"review_edit","step":1,"id":rid}; await callback.message.answer(f"✏️ <b>Edit Review #{rid}</b>\n\nCurrent:\n<code>{_esc(str(row.get('text') or ''))[:3000]}</code>\n\nSend the new review text.\n\n/canceladmin to cancel.")

@dp.callback_query(F.data.startswith("admin:reviewrating:"))
async def admin_review_rating(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer(); rid=int(callback.data.rsplit(":",1)[1]); await callback.message.edit_text(f"⭐ <b>Set rating for Review #{rid}</b>",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⭐"*n,callback_data=f"admin:reviewsetrating:{rid}:{n}") for n in range(1,6)],[InlineKeyboardButton(text="◀️ Back",callback_data=f"admin:reviewview:{rid}")]]))

@dp.callback_query(F.data.startswith("admin:reviewsetrating:"))
async def admin_review_set_rating(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer(); parts=callback.data.split(":"); rid=int(parts[-2]); rating=max(1,min(5,int(parts[-1]))); await fetch_one("UPDATE reviews SET rating=? WHERE id=? RETURNING id",(rating,rid)); await admin_log("review_rating",callback.from_user.id,f"{rid}:{rating}"); await admin_review_view(callback)


@dp.callback_query(F.data == "admin:bugs")
async def admin_bugs_callback(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await callback.answer()
    rows = await fetch_all("""
        SELECT b.id,b.title,b.description,b.status,b.created_at,u.telegram_id,u.username,u.first_name
        FROM bug_reports b LEFT JOIN users u ON u.id=b.user_id
        ORDER BY b.created_at DESC,b.id DESC LIMIT 25
    """)
    lines = ["🐞 <b>Bug Reports</b>", ""]
    buttons = []
    for r in rows:
        bid = int(r["id"])
        lines.append(f"#{bid} · <b>{_esc(r.get('title') or 'Untitled')[:55]}</b> · {_esc(r.get('status') or 'open')}")
        buttons.append([InlineKeyboardButton(text=f"🐞 #{bid}", callback_data=f"admin:bug:{bid}")])
    buttons.append([InlineKeyboardButton(text="◀️ More", callback_data="admin:more")])
    await callback.message.edit_text("\n".join(lines) if rows else "🐞 <b>Bug Reports</b>\n\nNo bug reports found.", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data.startswith("admin:bug:"))
async def admin_bug_detail(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await callback.answer()
    bid = int(callback.data.rsplit(":", 1)[1])
    r = await fetch_one("SELECT b.*,u.telegram_id,u.username,u.first_name FROM bug_reports b LEFT JOIN users u ON u.id=b.user_id WHERE b.id=?", (bid,))
    if not r:
        await callback.message.edit_text("❌ Bug report not found.", reply_markup=admin_back_keyboard("admin:bugs"))
        return
    text = f"🐞 <b>Bug #{bid}</b>\n\n<b>{_esc(r.get('title') or 'Untitled')}</b>\n\n{_esc(r.get('description') or '')[:2500]}\n\n📌 Status: <b>{_esc(r.get('status') or 'open')}</b>\n👤 <code>{r.get('telegram_id') or '-'}</code>"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 Open", callback_data=f"admin:bugstatus:{bid}:open"), InlineKeyboardButton(text="🟡 In Progress", callback_data=f"admin:bugstatus:{bid}:in_progress")],
        [InlineKeyboardButton(text="✅ Resolved", callback_data=f"admin:bugstatus:{bid}:resolved"), InlineKeyboardButton(text="🔒 Closed", callback_data=f"admin:bugstatus:{bid}:closed")],
        [InlineKeyboardButton(text="◀️ Bugs", callback_data="admin:bugs")],
    ])
    await callback.message.edit_text(text, reply_markup=kb)


@dp.callback_query(F.data.startswith("admin:bugstatus:"))
async def admin_bug_status(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await callback.answer()
    _, _, bid, status = callback.data.split(":", 3)
    await fetch_one("UPDATE bug_reports SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=? RETURNING id", (status, int(bid)))
    await admin_bug_detail(callback)


@dp.callback_query(F.data == "admin:logs")
async def admin_logs_callback(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await callback.answer()
    rows = await fetch_all("SELECT action,admin_id,details,created_at FROM admin_logs ORDER BY created_at DESC,id DESC LIMIT 40")
    lines = ["📜 <b>Admin Logs</b>", ""]
    for r in rows:
        lines.append(f"• <b>{_esc(r.get('action'))}</b> · admin <code>{r.get('admin_id')}</code> · {_esc(r.get('details') or '')[:90]} · {_esc(r.get('created_at'))}")
    await callback.message.edit_text("\n".join(lines) if rows else "📜 <b>Admin Logs</b>\n\nNo logs yet.", reply_markup=admin_back_keyboard("admin:more"))


@dp.callback_query(F.data == "admin:settings")
async def admin_settings_callback(callback: CallbackQuery):
    if not await require_admin_callback(callback):
        return
    await callback.answer()
    maintenance = await get_setting("maintenance_mode", "0")
    shop_name = await get_setting("shop_name", config.SHOP_NAME)
    currency = await get_setting("currency", "XTR")
    await callback.message.edit_text(
        "⚙️ <b>Store Settings</b>\n\n"
        f"🏪 Shop name: <b>{_esc(shop_name)}</b>\n"
        f"💳 Currency: <b>{_esc(currency)}</b>\n"
        f"🛠️ Maintenance: <b>{'ON' if maintenance == '1' else 'OFF'}</b>\n\n"
        "Use the Store button on the main Admin panel to switch customer access on/off.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🟢/🔴 Toggle Store", callback_data="admin:storetoggle")],
            [InlineKeyboardButton(text="◀️ More", callback_data="admin:more")],
        ]),
    )


@dp.message(Command("canceladmin"))
async def cancel_admin_flow(message: Message):
    if not message.from_user or not is_admin_user(message.from_user.id): return
    user_id = message.from_user.id
    flow = ADMIN_FLOWS.get(user_id) or {}
    flow_type = flow.get("type")
    ADMIN_FLOWS.pop(user_id, None)

    # Inventory flows should return to the Inventory screen instead of
    # dropping the admin back at the global dashboard.
    if flow_type in {"inventory", "inventory_search"}:
        _clear_admin_bulk(user_id, "inventory")
        try:
            text, keyboard = await admin_inventory_data(user_id, False)
            await message.answer(
                "✅ <b>Inventory action cancelled.</b>\n\n" + text,
                reply_markup=keyboard,
            )
            return
        except Exception as exc:
            log.exception("inventory /canceladmin return failed")
            await message.answer(
                f"✅ Inventory action cancelled.\n\nUnable to load inventory: {html.escape(str(exc))}",
                reply_markup=admin_menu(),
            )
            return

    if flow_type in {"feedback_search", "feedback_edit"}:
        view=ADMIN_FEEDBACK_VIEWS.get(user_id,{"page":0,"status":"all","search":""})
        class _FeedbackCancel:
            def __init__(self,user_id): self.from_user=type("U",(),{"id":user_id})()
        await _render_admin_feedback(_FeedbackCancel(user_id),view.get("page",0),view.get("status","all"),view.get("search","")); return

    if flow_type in {"review_search", "review_edit"}:
        view=ADMIN_REVIEW_VIEWS.get(user_id,{"page":0,"visibility":"all","search":""})
        class _ReviewCancel:
            def __init__(self,user_id): self.from_user=type("U",(),{"id":user_id})()
        await _render_admin_reviews(_ReviewCancel(user_id),view.get("page",0),view.get("visibility","all"),view.get("search","")); return

    if flow_type in {"ticket_search", "ticket_reply"}:
        try:
            text, keyboard = await render_admin_tickets(
                type("TicketCancelContext", (), {"from_user": type("U", (), {"id": user_id})()})()
            )
            await message.answer(
                "✅ <b>Ticket action cancelled.</b>\n\n" + text,
                reply_markup=keyboard,
            )
            return
        except Exception as exc:
            log.exception("ticket /canceladmin return failed")
            await message.answer(
                f"✅ Ticket action cancelled.\n\nUnable to load tickets: {html.escape(str(exc))}",
                reply_markup=admin_menu(),
            )
            return

    await message.answer("✅ Admin action cancelled.", reply_markup=admin_menu())


@dp.callback_query(F.data.startswith("admin:newproductgame:"))
async def admin_new_product_game(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer()
    gid=int(callback.data.rsplit(":",1)[1]); flow=ADMIN_FLOWS.get(callback.from_user.id)
    if not flow or flow.get("type")!="product": return
    flow["data"]["game_id"]=gid; flow["step"]=5
    await callback.message.edit_text("🗂️ <b>Select the category:</b>",reply_markup=await _category_picker_keyboard(gid,"admin:newproductcategory","admin:products"))

@dp.callback_query(F.data.startswith("admin:newproductcategory:"))
async def admin_new_product_category(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer()
    cid=int(callback.data.rsplit(":",1)[1]); flow=ADMIN_FLOWS.get(callback.from_user.id)
    if not flow or flow.get("type")!="product": return
    flow["data"]["category_id"]=cid; flow["step"]=5
    await callback.message.edit_text("📦 <b>Send starting stock quantity:</b>")

@dp.callback_query(F.data.startswith("admin:newproductfeatured:"))
async def admin_new_product_featured(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer(); flow=ADMIN_FLOWS.get(callback.from_user.id)
    if not flow or flow.get("type")!="product": return
    flow["data"]["featured"]=callback.data.endswith(":1"); flow["step"]=7
    await callback.message.edit_text("🏷️ Send discount percent (0 for none).")

async def _finish_game_creation(user_id, image=None):
    flow=ADMIN_FLOWS.get(user_id) or {}; d=flow.get("data",{})
    gid=await create_game(d.get("name",""), d.get("description",""))
    if image:
        await fetch_one("UPDATE games SET image=? WHERE id=? RETURNING id",(image,gid))
    ADMIN_FLOWS.pop(user_id,None)
    return gid

async def _finish_category_creation(user_id, image=None):
    flow=ADMIN_FLOWS.get(user_id) or {}; d=flow.get("data",{})
    cid=await create_category(int(d["game_id"]),d.get("name",""),d.get("description",""))
    if image:
        await fetch_one("UPDATE categories SET image=? WHERE id=? RETURNING id",(image,cid))
    ADMIN_FLOWS.pop(user_id,None)
    return cid

async def _finish_product_creation(user_id):
    flow=ADMIN_FLOWS.get(user_id) or {}; d=flow.get("data",{}); images=flow.get("images",[])
    pid=await create_product(d["name"],d.get("description",""),int(d["price"]),int(d["game_id"]),int(d["category_id"]),int(d.get("stock",0)),1 if d.get("featured") else 0)
    await fetch_one("UPDATE products SET discount_percent=?, banner=? WHERE id=? RETURNING id",(int(d.get("discount",0)),d.get("banner"),pid))
    for order,img in enumerate(images):
        await fetch_one("INSERT INTO product_images(product_id,image,sort_order) VALUES(?,?,?) RETURNING id",(pid,img,order))
    ADMIN_FLOWS.pop(user_id,None)
    return pid

@dp.callback_query(F.data == "admin:skip_game_image")
async def admin_skip_game_image(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer(); gid=await _finish_game_creation(callback.from_user.id); await callback.message.edit_text(f"✅ Game created: <b>#{gid}</b>",reply_markup=admin_menu())

@dp.callback_query(F.data == "admin:skip_category_image")
async def admin_skip_category_image(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer(); cid=await _finish_category_creation(callback.from_user.id); await callback.message.edit_text(f"✅ Category created: <b>#{cid}</b>",reply_markup=admin_menu())

@dp.callback_query(F.data == "admin:skip_product_banner")
async def admin_skip_product_banner(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer(); flow=ADMIN_FLOWS.get(callback.from_user.id)
    if not flow: return
    flow["data"]["banner"]=None; flow["step"]=10
    await callback.message.edit_text("🖼️ Send product images one by one. Tap Done when finished.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Done",callback_data="admin:finish_product_images")]]))

@dp.callback_query(F.data == "admin:finish_product_images")
async def admin_finish_product_images(callback: CallbackQuery):
    if not await require_admin_callback(callback): return
    await callback.answer(); flow=ADMIN_FLOWS.get(callback.from_user.id)
    if not flow or flow.get("type")!="product": return
    pid=await _finish_product_creation(callback.from_user.id); await callback.message.edit_text(f"✅ Product created: <b>#{pid}</b>",reply_markup=admin_menu())

@dp.message(F.photo)
async def admin_photo_flow(message: Message):
    if not message.from_user or not is_admin_user(message.from_user.id):
        return
    flow = ADMIN_FLOWS.get(message.from_user.id)
    if not flow:
        return

    file_id = str(message.photo[-1].file_id)
    media = f"tg:{file_id}"

    if flow.get("type") == "content_edit" and flow.get("key") in {"welcome_image", "help_image"}:
        key = str(flow.get("key"))
        label = dict(CONTENT_EDITOR_KEYS).get(key, key)
        try:
            await set_setting(key, media)
            ADMIN_FLOWS.pop(message.from_user.id, None)
            await admin_log("content_update", message.from_user.id, f"{key}:telegram:{file_id}")
            await message.answer(
                f"✅ <b>{_esc(label)}</b> updated.\n\n"
                "🖼️ The Telegram photo was saved successfully.\n"
                "The image will now be used automatically." ,
                reply_markup=admin_menu(),
            )
        except Exception as exc:
            log.exception("Telegram content image update failed")
            await message.answer(
                f"❌ Could not update {_esc(label)}.\n\n"
                f"<code>{html.escape(str(exc))}</code>\n\n"
                "Send another photo or /canceladmin."
            )
        return

    # Handle direct Telegram-photo editing for existing Games/Categories.
    # This is intentionally scoped to the image field only, so no unrelated
    # admin photo flows are changed.
    if (
        flow.get("type") == "edit_field"
        and flow.get("field") == "image"
        and flow.get("kind") in {"g", "c"}
    ):
        kind = flow["kind"]
        item_id = int(flow["id"])
        table = "games" if kind == "g" else "categories"
        label = "Game" if kind == "g" else "Category"
        try:
            row = await fetch_one(
                f"UPDATE {table} SET image=? WHERE id=? RETURNING id",
                (media, item_id),
            )
            if not row:
                raise ValueError(f"{label} #{item_id} not found.")
            ADMIN_FLOWS.pop(message.from_user.id, None)
            await admin_log(
                "edit_" + kind,
                message.from_user.id,
                f"{item_id}:image=telegram:{file_id}",
            )
            await message.answer(
                f"✅ {label} <b>#{item_id}</b> image updated from Telegram.\n\n"
                "The image is stored using its Telegram file_id and will be served by CPM SHOP.",
                reply_markup=admin_menu(),
            )
        except Exception as exc:
            log.exception("Telegram photo image edit failed")
            await message.answer(
                f"❌ Could not update {label.lower()} image.\n\n"
                f"<code>{html.escape(str(exc))}</code>",
                reply_markup=admin_menu(),
            )
        return

    if flow.get("type") == "game" and flow.get("step") == 3:
        gid=await _finish_game_creation(message.from_user.id, media)
        await message.answer(f"✅ Game created: <b>#{gid}</b>",reply_markup=admin_menu())
        return
    if flow.get("type") == "category" and flow.get("step") == 4:
        cid=await _finish_category_creation(message.from_user.id, media)
        await message.answer(f"✅ Category created: <b>#{cid}</b>",reply_markup=admin_menu())
        return
    if flow.get("type") == "product" and flow.get("step") == 8:
        flow["data"]["banner"]=media; flow["step"]=10
        await message.answer("✅ Banner saved. Now send product images one by one, then tap Done.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Done",callback_data="admin:finish_product_images")]]))
        return
    if flow.get("type") == "product" and flow.get("step") == 10:
        images=flow.setdefault("images",[])
        if media not in images:
            if len(images)>=100:
                await message.answer("❌ Maximum 100 product images.")
                return
            images.append(media)
        await message.answer(f"✅ Image added ({len(images)}/100). Send another or tap Done.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Done",callback_data="admin:finish_product_images")]]))
        return

    if flow.get("type") not in {"product_image", "product_banner"}:
        return
    pid = int(flow["product_id"])
    product = await get_product(pid)
    if not product:
        ADMIN_FLOWS.pop(message.from_user.id, None)
        await message.answer("❌ Product not found.", reply_markup=admin_menu())
        return
    file_id = str(message.photo[-1].file_id)
    try:
        if flow.get("type") == "product_banner":
            exists = await fetch_one("SELECT id FROM product_images WHERE product_id=? AND image=?", (pid, f"tg:{file_id}"))
            if not exists:
                count = await fetch_one("SELECT COUNT(*) AS c FROM product_images WHERE product_id=?", (pid,))
                sort_order = int(count.get("c") or 0)
                await fetch_one("INSERT INTO product_images(product_id,image,sort_order) VALUES(?,?,?) RETURNING id", (pid, f"tg:{file_id}", sort_order))
            await fetch_one("UPDATE products SET banner=? WHERE id=? RETURNING id", (f"tg:{file_id}", pid))
            action = "banner set"
        else:
            count = await fetch_one("SELECT COUNT(*) AS c FROM product_images WHERE product_id=?", (pid,))
            if int(count.get("c") or 0) >= 100:
                raise ValueError("A product can have a maximum of 100 images.")
            exists = await fetch_one("SELECT id FROM product_images WHERE product_id=? AND image=?", (pid, f"tg:{file_id}"))
            if exists:
                raise ValueError("This image is already attached to the product.")
            sort_order = int(count.get("c") or 0)
            await fetch_one("INSERT INTO product_images(product_id,image,sort_order) VALUES(?,?,?) RETURNING id", (pid, f"tg:{file_id}", sort_order))
            if sort_order == 0:
                await fetch_one("UPDATE products SET banner=? WHERE id=? RETURNING id", (f"tg:{file_id}", pid))
            action = "image added"
        if flow.get("type") == "product_banner":
            ADMIN_FLOWS.pop(message.from_user.id, None)
            await admin_log("product_image_" + action.split()[1], message.from_user.id, f"{pid}:{file_id}")
            await message.answer(f"✅ Product #{pid}: {action}.", reply_markup=admin_menu())
        else:
            # Keep the flow active. This allows multiple separate photos and Telegram albums.
            ADMIN_FLOWS[message.from_user.id] = {"type": "product_image", "product_id": pid}
            await admin_log("product_image_added", message.from_user.id, f"{pid}:{file_id}")
            count_row = await fetch_one("SELECT COUNT(*) AS c FROM product_images WHERE product_id=?", (pid,))
            count = int((count_row or {}).get("c") or 0)
            await message.answer(
                f"✅ Image added to product #{pid}.\n\n📸 Total images: <b>{count}</b>\n\nSend another photo, or tap <b>Done</b>.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="➕ Add Another Image", callback_data=f"admin:addproductimage:{pid}")],
                    [InlineKeyboardButton(text="✅ Done", callback_data=f"admin:doneproductimages:{pid}")],
                    [InlineKeyboardButton(text="🖼️ View Images", callback_data=f"admin:productimages:{pid}")],
                ])
            )
    except Exception as exc:
        log.exception("Admin product photo flow failed")
        await message.answer(f"❌ {html.escape(str(exc))}\n\nSend another photo or /canceladmin.")


@dp.message(F.text & ~F.text.startswith("/"))
async def admin_text_flow(message: Message):
    if not message.from_user:
        return
    if not is_admin_user(message.from_user.id):
        await _handle_customer_text_flow(message)
        return
    await clear_temporary_messages(message.chat.id)
    flow=ADMIN_FLOWS.get(message.from_user.id)
    if not flow: return
    text=(message.text or '').strip()
    try:
        typ=flow['type']; step=int(flow.get('step',1)); data=flow.setdefault('data',{})

        if typ == "ticket_search":
            query = text[:80]
            ADMIN_FLOWS.pop(message.from_user.id, None)
            view = ADMIN_TICKET_VIEWS.setdefault(int(message.from_user.id), {"page": 0, "status": "all", "search": ""})
            view["page"] = 0
            view["search"] = query
            class _TicketRender:
                def __init__(self, user_id, chat_message):
                    self.from_user = type("U", (), {"id": user_id})()
                    self.message = chat_message
            dummy = _TicketRender(message.from_user.id, message)
            ttext, tkb = await render_admin_tickets(dummy)
            await message.answer(ttext, reply_markup=tkb)
            return

        if typ == "inventory_search":
            view = ADMIN_INVENTORY_VIEWS.get(int(message.from_user.id), {"status":"all", "page":0, "search":""})
            view["page"] = 0
            view["search"] = text[:80]
            ADMIN_FLOWS.pop(message.from_user.id, None)
            t, k = await admin_inventory_data(message.from_user.id, False, view.get("status","all"), 0, view.get("search",""))
            await message.answer(t, reply_markup=k)
            return

        if typ == "feedback_search":
            view=ADMIN_FEEDBACK_VIEWS.setdefault(int(message.from_user.id), {"page":0,"status":"all","search":""})
            view.update({"page":0,"search":text[:80]})
            ADMIN_FLOWS.pop(message.from_user.id,None)
            class _FeedbackRender:
                def __init__(self,user_id,chat_message):
                    self.from_user=type("U",(),{"id":user_id})()
                    self.message=chat_message
            dummy=_FeedbackRender(message.from_user.id,message)
            await _render_admin_feedback(dummy,0,view.get("status","all"),view.get("search",""))
            return

        if typ == "review_search":
            view=ADMIN_REVIEW_VIEWS.setdefault(int(message.from_user.id), {"page":0,"visibility":"all","search":""})
            view.update({"page":0,"search":text[:80]})
            ADMIN_FLOWS.pop(message.from_user.id,None)
            class _ReviewRender:
                def __init__(self,user_id,chat_message):
                    self.from_user=type("U",(),{"id":user_id})()
                    self.message=chat_message
            dummy=_ReviewRender(message.from_user.id,message)
            await _render_admin_reviews(dummy,0,view.get("visibility","all"),view.get("search",""))
            return

        if typ == "feedback_edit":
            rid=int(flow["id"])
            row=await fetch_one("UPDATE feedback SET message=?,updated_at=CURRENT_TIMESTAMP WHERE id=? RETURNING id",(text[:5000],rid))
            if not row: raise ValueError("Feedback not found.")
            ADMIN_FLOWS.pop(message.from_user.id,None); await admin_log("feedback_edit",message.from_user.id,str(rid))
            await message.answer(f"✅ Feedback <b>#{rid}</b> updated.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="👁️ Open Feedback",callback_data=f"admin:feedbackview:{rid}")],[InlineKeyboardButton(text="◀️ Customer Feedback",callback_data="admin:feedback")]]))
            return

        if typ == "review_edit":
            rid=int(flow["id"])
            row=await fetch_one("UPDATE reviews SET text=? WHERE id=? RETURNING id",(text[:3000],rid))
            if not row: raise ValueError("Review not found.")
            ADMIN_FLOWS.pop(message.from_user.id,None); await admin_log("review_edit",message.from_user.id,str(rid))
            await message.answer(f"✅ Review <b>#{rid}</b> updated.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="👁️ Open Review",callback_data=f"admin:reviewview:{rid}")],[InlineKeyboardButton(text="◀️ Reviews",callback_data="admin:reviews")]]))
            return

        if typ == 'user_search':
            q = text.lstrip('@')
            rows = await fetch_all(
                """SELECT id,telegram_id,username,first_name,is_blocked
                   FROM users
                  WHERE CAST(telegram_id AS TEXT)=?
                     OR LOWER(COALESCE(username,'')) LIKE LOWER(?)
                     OR LOWER(COALESCE(first_name,'')) LIKE LOWER(?)
                  ORDER BY created_at DESC LIMIT 15""",
                (q, f'%{q}%', f'%{q}%'),
            )
            ADMIN_FLOWS.pop(message.from_user.id, None)
            if not rows:
                await message.answer('❌ No users found.', reply_markup=admin_menu())
                return
            lines = ['🔎 <b>User Search</b>', '']
            buttons = []
            for r in rows:
                uid = int(r['id']); name = str(r.get('first_name') or r.get('username') or r.get('telegram_id') or 'User')
                lines.append(f"#{uid} · {_esc(name)} · <code>{r.get('telegram_id')}</code> · {'🚫' if r.get('is_blocked') else '🟢'}")
                buttons.append([InlineKeyboardButton(text=f'👤 #{uid} {name[:30]}', callback_data=f'admin:user:{uid}')])
            buttons.append([InlineKeyboardButton(text='◀️ Users', callback_data='admin:users')])
            await message.answer('\n'.join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
            return

        if typ == 'user_message':
            uid = int(flow['user_id'])
            u = await fetch_one('SELECT telegram_id FROM users WHERE id=?', (uid,))
            if not u:
                ADMIN_FLOWS.pop(message.from_user.id, None)
                await message.answer('❌ User not found.', reply_markup=admin_menu())
                return
            try:
                await bot.send_message(int(u['telegram_id']), f'📩 <b>Message from CPM SHOP Admin</b>\n\n{html.escape(text)}')
                result = '✅ Message sent.'
            except Exception as exc:
                result = f'❌ Could not send message: {_esc(exc)}'
            ADMIN_FLOWS.pop(message.from_user.id, None)
            await admin_log('user_message', message.from_user.id, str(uid))
            await message.answer(result, reply_markup=admin_menu())
            return

        if typ == "promo_mode_price":
            rid=int(flow["id"]); price=int(text)
            if price<=0: raise ValueError("Sale price must be greater than zero.")
            await update_promo_code(rid,{"access_mode":"sale","sale_price_stars":price})
            ADMIN_FLOWS.pop(message.from_user.id,None)
            await message.answer("✅ Promo is now listed for sale in the Promo Shop.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏷️ Open Promo",callback_data=f"admin:promo:{rid}")]]))
            return
        if typ == "promo_mode_customers":
            rid=int(flow["id"])
            await set_promo_allowed_users(rid,text)
            await update_promo_code(rid,{"access_mode":"assigned","sale_price_stars":0})
            ADMIN_FLOWS.pop(message.from_user.id,None)
            await message.answer("✅ Promo is now limited to the specified customers.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏷️ Open Promo",callback_data=f"admin:promo:{rid}")]]))
            return

        if typ == 'edit_field':
            kind = flow['kind']; item_id = int(flow['id']); field = flow['field']
            allow = {
                'p': {'name':'text','description':'text','price_stars':'int','stock':'int','game_id':'nullable_int','category_id':'nullable_int','discount_percent':'int','featured':'bool','banner':'text'},
                'i': {'product_id':'int','item_data':'text','status':'status'},
                'g': {'name':'text','description':'text','image':'text','sort_order':'int'},
                'c': {'game_id':'int','name':'text','description':'text','image':'text','sort_order':'int'},
                'r': {'code':'code','discount_percent':'int','discount_stars':'int','max_uses':'nullable_int','expires_at':'datetime','sale_price_stars':'int','min_cart_quantity':'int','max_uses_per_user':'int','description':'text'},
            }
            typemap = allow.get(kind, {})
            value_type = typemap.get(field)
            if not value_type:
                ADMIN_FLOWS.pop(message.from_user.id, None)
                await message.answer('❌ This field cannot be edited.')
                return
            if value_type == 'int':
                value = int(text)
            elif value_type == 'nullable_int':
                value = None if text.lower() in {'0','none','null','-'} else int(text)
            elif value_type == 'bool':
                value = text.lower() in {'1','true','yes','y','on'}
            elif value_type == 'status':
                value = text.lower()
                if value not in {'available','sold','draft','reserved'}:
                    raise ValueError('Status must be available, sold, draft, or reserved.')
            elif value_type == 'code':
                value = text.upper().replace(' ', '')
                if not value:
                    raise ValueError('Promo code cannot be empty.')
            elif value_type == 'datetime':
                value = None if text.lower() in {'none','null','-',''} else text
            else:
                value = text
            if kind == 'r':
                promo_changes = {field: value}
                # Setting a positive sale price means the admin is explicitly
                # listing this promo for sale; keep the mode and price in sync.
                if field == 'sale_price_stars':
                    promo_changes['access_mode'] = 'sale' if int(value or 0) > 0 else 'public'
                await update_promo_code(item_id, promo_changes)
            else:
                table = {'p':'products','i':'inventory_items','g':'games','c':'categories'}[kind]
                await fetch_one(f'UPDATE {table} SET {field}=? WHERE id=? RETURNING id', (value, item_id))
            if kind == 'i' and field in {'product_id','status'}:
                old_product = None
                # Recalculate stock for all affected products.
                row = await fetch_one('SELECT product_id FROM inventory_items WHERE id=?', (item_id,))
                if row and row.get('product_id'):
                    await _sync_product_stock(int(row['product_id']))
            ADMIN_FLOWS.pop(message.from_user.id, None)
            await admin_log('edit_'+kind, message.from_user.id, f'{item_id}:{field}')
            await message.answer(f'✅ Updated <b>{_esc(field)}</b> for #{item_id}.', reply_markup=admin_menu())
            return

        if typ == 'giveaway':
            if step == 1:
                data['title'] = text
                flow['step'] = 2
                await message.answer('📝 Send the giveaway description.')
                return
            if step == 2:
                data['description'] = text
                flow['step'] = 3
                await message.answer('🏆 Send the number of winners (example: 5).')
                return
            if step == 3:
                try:
                    winners = max(1, int(text))
                except ValueError:
                    await message.answer('❌ Please send a valid whole number, for example: 5.')
                    return
                data['winner_count'] = winners
                data['prizes'] = []
                flow['step'] = 4
                await message.answer(
                    '🎁 <b>Prize #1</b>\n\n'
                    'Send the exact prize that will be delivered to winner #1.\n\n'
                    'For an account, use for example:\n'
                    '<code>Username: example\nPassword: your-password</code>\n\n'
                    'This information is private and will be sent automatically to the selected winner.'
                )
                return
            if step == 4:
                prizes = data.setdefault('prizes', [])
                prizes.append(text)
                total_winners = int(data.get('winner_count') or 1)
                if len(prizes) < total_winners:
                    next_number = len(prizes) + 1
                    await message.answer(
                        f'🎁 <b>Prize #{next_number}</b>\n\n'
                        'Send the exact prize for this winner.\n'
                        'For an account, send the username/email and password together.'
                    )
                    return
                prize_payload = json.dumps(prizes, ensure_ascii=False)
                row = await fetch_one(
                    "INSERT INTO giveaways(title,description,prize,status,winner_count,starts_at) VALUES(?,?,?,'draft',?,NULL) RETURNING id",
                    (data['title'], data['description'], prize_payload, total_winners),
                )
                gid = int(row['id'])
                ADMIN_FLOWS.pop(message.from_user.id, None)
                await admin_log('giveaway_create', message.from_user.id, str(gid))
                await message.answer(
                    f'✅ <b>Giveaway created.</b> ID: <code>{gid}</code>\n\n'
                    f'🏆 Winners: <b>{total_winners}</b>\n'
                    '🎁 Individual prizes saved. They will be delivered automatically after Draw Winners.',
                    reply_markup=admin_menu(),
                )
                await message.answer(
                    '📝 <b>Giveaway saved as a draft.</b>\n\n'
                    'No announcement has been sent to users. When you are ready, open this giveaway in Admin → Giveaways and press <b>🟢 Start Giveaway</b>.',
                    reply_markup=admin_menu(),
                )
                return

        if typ == 'admin_note':
            if step == 1:
                data['title'] = text
                flow['step'] = 2
                await message.answer("📝 Now send the note content.")
                return
            if step == 2:
                data['content'] = text
                db = __import__('database')
                if flow.get('mode') == 'edit':
                    note = await db.update_admin_note(int(flow['note_id']), data['title'], data['content'])
                    action = 'update_admin_note'
                else:
                    note = await db.create_admin_note(data['title'], data['content'])
                    action = 'create_admin_note'
                ADMIN_FLOWS.pop(message.from_user.id, None)
                await admin_log(action, message.from_user.id, str(note.get('id') if note else ''))
                await message.answer("✅ Admin note saved.", reply_markup=admin_menu())
                return

        if typ == 'content_edit':
            key = str(flow.get('key') or '')
            allowed = {k for k, _ in CONTENT_EDITOR_KEYS}
            if key not in allowed:
                ADMIN_FLOWS.pop(message.from_user.id, None)
                await message.answer("❌ Invalid content field.", reply_markup=admin_menu())
                return
            if key in {"welcome_image", "help_image"}:
                await message.answer(
                    f"🖼️ <b>{_esc(dict(CONTENT_EDITOR_KEYS).get(key, key))}</b> requires a Telegram photo.\n\n"
                    "Please send the image as a photo, not as a link.\n"
                    "Use /canceladmin to cancel."
                )
                return
            await set_setting(key, text)
            ADMIN_FLOWS.pop(message.from_user.id, None)
            await admin_log('content_update', message.from_user.id, key)
            await message.answer(f"✅ <b>{_esc(dict(CONTENT_EDITOR_KEYS).get(key, key))}</b> updated.", reply_markup=admin_menu())
            return

        if typ=='ticket_reply':
            await admin_add_ticket_message(int(flow['ticket_id']),text); ADMIN_FLOWS.pop(message.from_user.id,None); await message.answer("✅ Reply sent.",reply_markup=admin_menu()); return
        if typ=='broadcast':
            users=await __import__('database').get_all_active_telegram_users(); sent=0; failed=0
            for u in users:
                try: await bot.send_message(int(u['telegram_id']),text); sent+=1
                except Exception: failed+=1
                await asyncio.sleep(0.04)
            try: await __import__('database').create_broadcast(text)
            except Exception: pass
            ADMIN_FLOWS.pop(message.from_user.id,None); await message.answer(f"📢 Broadcast finished.\n\n✅ Sent: <b>{sent}</b>\n❌ Failed: <b>{failed}</b>",reply_markup=admin_menu()); return
        if typ=='promo':
            if step==1:
                data['code']=text.upper().replace(' ','')
                if not data['code']: raise ValueError("Promo code cannot be empty.")
                flow['step']=1.5
                await message.answer("💸 <b>Choose Discount Type</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📊 Percentage",callback_data="admin:newpromodiscpercent")],[InlineKeyboardButton(text="⭐ Fixed Stars",callback_data="admin:newpromodiscstars")]]))
                return
            if step==2:
                value=int(text)
                if value<=0: raise ValueError("Discount must be greater than zero.")
                if data.get('discount_type')=='percent':
                    if value>100: raise ValueError("Percentage discount cannot exceed 100%.")
                    data['discount_percent']=value; data['discount_stars']=0
                else:
                    data['discount_stars']=value; data['discount_percent']=0
                flow['step']=3; flow.pop('field',None)
                await message.answer(_new_promo_settings_text(flow),reply_markup=_new_promo_settings_keyboard(flow))
                return
            if step==3 and flow.get('field'):
                field=flow.pop('field')
                if field in {'max_uses','max_uses_per_user','min_cart_quantity','sale_price_stars'}:
                    value=int(text or 0)
                    if field=='max_uses' and value<0: raise ValueError("Max uses cannot be negative.")
                    if field=='max_uses_per_user' and value<0: raise ValueError("Uses/User cannot be negative.")
                    if field=='min_cart_quantity' and value<1: raise ValueError("Minimum quantity must be at least 1.")
                    if field=='sale_price_stars' and value<1: raise ValueError("Sale price must be greater than zero.")
                    data[field]=None if field=='max_uses' and value==0 else value
                elif field=='description': data[field]='' if text.strip()=='-' else text[:2000]
                elif field=='expires_at':
                    raw_expiry = text.strip()
                    if raw_expiry.lower() in {'','-','none','null'}:
                        data[field] = None
                    else:
                        try:
                            parsed_expiry = datetime.fromisoformat(raw_expiry.replace('Z', '+00:00'))
                            if parsed_expiry.tzinfo is not None:
                                parsed_expiry = parsed_expiry.replace(tzinfo=None)
                            data[field] = parsed_expiry
                        except ValueError:
                            raise ValueError('Invalid expiry timestamp. Use YYYY-MM-DD HH:MM:SS.')
                elif field=='allowed_customers': data[field]=text
                flow['step']=3
                await message.answer(_new_promo_settings_text(flow),reply_markup=_new_promo_settings_keyboard(flow))
                return
        if typ=='game':
            if step==1:
                data['name']=text; flow['step']=2
                await message.answer("📝 Send game description.\n\nSend - to leave it empty.")
                return
            if step==2:
                data['description']="" if text.strip()=="-" else text.strip(); flow['step']=3
                await message.answer("🖼️ Send the game image/banner now, or tap Skip.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⏭️ Skip Image", callback_data="admin:skip_game_image")]]))
                return

        if typ=='category':
            if step==2:
                data['name']=text; flow['step']=3
                await message.answer("📝 Send category description.\n\nSend - to leave it empty.")
                return
            if step==3:
                data['description']="" if text.strip()=="-" else text.strip(); flow['step']=4
                await message.answer("🖼️ Send the category image now, or tap Skip.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⏭️ Skip Image", callback_data="admin:skip_category_image")]]))
                return

        if typ=='product':
            if step==1: data['name']=text; flow['step']=2; await message.answer("📝 Send product description.\n\nSend - to leave it empty."); return
            if step==2: data['description']="" if text.strip()=="-" else text.strip(); flow['step']=3; await message.answer("💰 Send price in Telegram Stars."); return
            if step==3:
                try: data['price']=int(text)
                except ValueError: await message.answer("❌ Price must be a whole number."); return
                if data['price']<=0: await message.answer("❌ Price must be greater than 0."); return
                flow['step']=4
                await message.answer("🎮 <b>Select the game:</b>",reply_markup=await _game_picker_keyboard("admin:newproductgame","admin:products")); return
            if step==5:
                try: data['stock']=int(text)
                except ValueError: await message.answer("❌ Stock must be a whole number."); return
                if data['stock']<0: await message.answer("❌ Stock cannot be negative."); return
                flow['step']=6; await message.answer("⭐ Featured?",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Yes",callback_data="admin:newproductfeatured:1"),InlineKeyboardButton(text="No",callback_data="admin:newproductfeatured:0")]])); return
            if step==7:
                try: data['discount']=int(text or 0)
                except ValueError: await message.answer("❌ Discount must be a whole number."); return
                if not 0<=data['discount']<=100: await message.answer("❌ Discount must be 0-100."); return
                flow['step']=8
                await message.answer("🖼️ Send the product banner photo, or tap Skip.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⏭️ Skip Banner",callback_data="admin:skip_product_banner")]])); return
            if step==9:
                flow['step']=10
                await message.answer("🖼️ Send product images one by one. Tap Done when finished.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Done",callback_data="admin:finish_product_images")]])); return

        if typ=='inventory':
            if step==1:
                data['product_id']=int(text)
                flow['step']=2
                await message.answer(
                    "📋 <b>Add Inventory</b>\n\n"
                    "Send inventory accounts using:\n"
                    "<code>1a:email:password</code>\n"
                    "<code>2a:email:password</code>\n"
                    "<code>3a:email:password:with:colons</code>\n\n"
                    "Markers start new accounts. Multi-line passwords are supported.\n"
                    "Nothing is saved until <b>Confirm Import</b>."
                )
                return
            if step==2:
                product_id=int(data.get('product_id') or 0)
                marker_mode = bool(re.search(r'(^|\n)\s*\d+\s*a\s*:', text, re.IGNORECASE))
                mode = "marker" if marker_mode else "legacy"
                records, parser_error, parser_message = _parse_inventory_preview_input(text, mode)
                if parser_error:
                    await message.answer(
                        f"❌ <b>Invalid inventory format.</b>\n\n{html.escape(parser_message)}\n\n"
                        "Use one marker per account, for example:\n"
                        "<code>1a:email:password</code>\n"
                        "<code>2a:email:password</code>"
                    )
                    return
                if not records:
                    await message.answer("❌ No inventory items found.")
                    return
                if len(records)>500:
                    await message.answer("❌ Maximum 500 inventory items can be imported at once.")
                    return

                seen=set()
                duplicate_count=0
                ready=[]
                existing_count=0
                invalid_count=0
                for record in records:
                    item=str(record.get("item_data") or "").strip()
                    if not item:
                        invalid_count += 1
                        continue
                    if item in seen:
                        duplicate_count += 1
                        continue
                    seen.add(item)
                    existing=await fetch_one(
                        "SELECT id FROM inventory_items WHERE product_id=? AND item_data=? LIMIT 1",
                        (product_id,item),
                    )
                    if existing:
                        existing_count += 1
                        continue
                    ready.append(item)

                flow['step']=3
                data['ready_items']=ready
                data['mode']=mode
                data['preview_total']=len(records)
                data['preview_duplicates']=duplicate_count
                data['preview_existing']=existing_count
                data['preview_invalid']=invalid_count

                if not ready:
                    await message.answer(
                        "📋 <b>Inventory Preview</b>\n\n"
                        f"Total: <b>{len(records)}</b>\n"
                        f"Invalid: <b>{invalid_count}</b>\n"
                        f"Duplicates: <b>{duplicate_count}</b>\n"
                        f"Already exists: <b>{existing_count}</b>\n"
                        "Ready to import: <b>0</b>\n\n"
                        "Nothing will be imported.",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="❌ Cancel", callback_data="admin:inventory:cancelimport")]
                        ])
                    )
                    return

                await message.answer(
                    "📋 <b>Inventory Preview</b>\n\n"
                    f"Total: <b>{len(records)}</b>\n"
                    f"Invalid: <b>{invalid_count}</b>\n"
                    f"Duplicates: <b>{duplicate_count}</b>\n"
                    f"Already exists: <b>{existing_count}</b>\n"
                    f"Ready to import: <b>{len(ready)}</b>\n\n"
                    "Nothing has been saved yet. Press <b>Confirm Import</b> to add the ready items.",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="✏️ Edit Item Before Confirm", callback_data="admin:inventory:editpreview")],
                        [InlineKeyboardButton(text="✅ Confirm Import", callback_data="admin:inventory:confirmimport")],
                        [InlineKeyboardButton(text="❌ Cancel", callback_data="admin:inventory:cancelimport")]
                    ])
                )
                return
            if step==4:
                idx = int(data.get("edit_index", -1))
                ready = list(data.get("ready_items") or [])
                if idx < 0 or idx >= len(ready):
                    flow['step'] = 3
                    await message.answer("❌ The selected inventory item no longer exists. Please choose Edit again.")
                    return

                replacement_text = text
                replacement_mode = "marker" if re.search(r'^\s*\d+\s*a\s*:', replacement_text, re.IGNORECASE) else "legacy"
                if replacement_mode == "marker":
                    replacement_records, edit_error, edit_message = _parse_inventory_preview_input(replacement_text, "marker")
                    if edit_error or len(replacement_records) != 1:
                        await message.answer(
                            "❌ Please send exactly one inventory account when editing an item.\n\n"
                            "Example:\n<code>1a:email:password</code>"
                        )
                        return
                    replacement = str(replacement_records[0].get("item_data") or "").strip()
                else:
                    replacement = replacement_text.strip()

                if not replacement:
                    await message.answer("❌ The replacement item cannot be empty.")
                    return
                if replacement in ready and ready.index(replacement) != idx:
                    await message.answer("❌ This replacement duplicates another item in the current import.")
                    return

                product_id = int(data.get("product_id") or 0)
                existing = await fetch_one(
                    "SELECT id FROM inventory_items WHERE product_id=? AND item_data=? LIMIT 1",
                    (product_id, replacement),
                )
                if existing:
                    await message.answer("❌ This inventory item already exists for the selected product.")
                    return

                ready[idx] = replacement
                data['ready_items'] = ready
                data['edit_index'] = None
                flow['step'] = 3
                await message.answer(
                    "✅ <b>Item updated in preview.</b>\n\n"
                    f"Pending items: <b>{len(ready)}</b>\n\n"
                    "Nothing has been saved yet.",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="✏️ Edit Item Before Confirm", callback_data="admin:inventory:editpreview")],
                        [InlineKeyboardButton(text="✅ Confirm Import", callback_data="admin:inventory:confirmimport")],
                        [InlineKeyboardButton(text="❌ Cancel", callback_data="admin:inventory:cancelimport")],
                    ])
                )
                return

            if step==3:
                await message.answer(
                    "ℹ️ A preview is waiting for confirmation. Press <b>Edit Item</b>, <b>Confirm Import</b> or <b>Cancel</b>."
                )
                return
    except Exception as e:
        log.exception('Admin flow failed')
        await message.answer(f"❌ {html.escape(str(e))}\n\nSend another value or /canceladmin.")


def _customer_nav(*rows: list[InlineKeyboardButton], main: bool = True) -> InlineKeyboardMarkup:
    keyboard = [list(row) for row in rows if row]
    if main:
        keyboard.append([InlineKeyboardButton(text="🏠 Main Menu", callback_data="customer:main")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


async def _customer_screen(target: Message, text: str, reply_markup=None):
    """Render a customer screen without creating duplicate messages on callbacks."""
    sender = getattr(target, "from_user", None)
    if sender is not None and bool(getattr(sender, "is_bot", False)):
        try:
            return await target.edit_text(text, reply_markup=reply_markup)
        except Exception as exc:
            if "message is not modified" in str(exc).lower():
                return target
    return await target.answer(text, reply_markup=reply_markup)


def _customer_shop_button() -> InlineKeyboardButton:
    if config.WEBAPP_URL:
        return InlineKeyboardButton(text="📱 Open Shop", web_app=WebAppInfo(url=config.WEBAPP_URL))
    return InlineKeyboardButton(text="📱 Open Shop", callback_data="shop_unavailable")


def _status_label(value: str | None) -> str:
    raw = str(value or "unknown").strip().lower()
    return {
        "pending": "⏳ Pending",
        "paid": "💳 Paid",
        "processing": "⚙️ Processing",
        "completed": "✅ Completed",
        "delivered": "📦 Delivered",
        "cancelled": "🚫 Cancelled",
        "canceled": "🚫 Cancelled",
        "failed": "❌ Failed",
        "resolved": "✅ Resolved",
        "closed": "🔒 Closed",
        "open": "🟢 Open",
        "in_progress": "🟡 In Progress",
        "active": "🟢 Active",
        "ended": "🏁 Ended",
        "draft": "📝 Draft",
    }.get(raw, raw.replace("_", " ").title())


def _clear_user_flow(user_id: int) -> None:
    USER_FLOWS.pop(int(user_id), None)


@dp.callback_query(F.data == "customer:main")
async def customer_main_menu(callback: CallbackQuery):
    if not callback.from_user:
        return
    await callback.answer()
    uid = int(callback.from_user.id)
    _clear_user_flow(uid)
    USER_CART_PROMOS.pop(uid, None)
    reserved_reward = USER_LOYALTY_REDEMPTIONS.pop(uid, None)
    if reserved_reward:
        try:
            await cancel_loyalty_redemption(uid, reserved_reward)
        except Exception:
            log.exception("Failed to release reserved reward when leaving customer menu")
    text = f"🏠 <b>{html.escape(config.SHOP_NAME)}</b>\n\nChoose a section below."
    try:
        await callback.message.edit_text(text, reply_markup=main_menu())
    except Exception:
        await callback.message.answer(text, reply_markup=main_menu())


# ------------------------- Referral -------------------------
async def send_referral_message(message: Message):
    if not message.from_user:
        return
    uid = int(message.from_user.id)
    try:
        me = await bot.get_me()
        username = me.username or ""
        link = f"https://t.me/{username}?start=ref_{uid}" if username else ""
        stats = await get_referral_stats(uid)
        referrals = await get_referrals(uid, limit=5)
        description = await get_setting(
            "referral_description",
            "Invite friends to CPM SHOP with your personal link."
        )
        text = (
            "🎁 <b>Referral Program</b>\n\n"
            f"{html.escape(description)}\n\n"
            f"👥 Referrals: <b>{int(stats.get('referral_count') or 0)}</b>\n"
            f"🏷️ Active referral discount: <b>{int(stats.get('active_discount_percent') or 0)}%</b>"
        )
        if referrals:
            text += "\n\n<b>Recent referrals</b>\n"
            for row in referrals:
                name = row.get("referred_first_name") or row.get("referred_username") or row.get("referred_telegram_id") or "Customer"
                text += f"• {html.escape(str(name))} · {html.escape(str(row.get('created_at') or ''))}\n"
        else:
            text += "\n\nNo referrals yet."
        rows = []
        if link:
            share_url = "https://t.me/share/url?url=" + quote(link, safe="") + "&text=" + quote("Join CPM SHOP using my referral link:", safe="")
            rows.append([InlineKeyboardButton(text="🔗 Share Referral Link", url=share_url)])
        rows.append([InlineKeyboardButton(text="🔄 Refresh", callback_data="referral")])
        rows.append([_customer_shop_button()])
        await _customer_screen(message, text, reply_markup=_customer_nav(*rows))
    except Exception:
        log.exception("Referral message failed")
        await _customer_screen(message, 
            "🎁 <b>Referral Program</b>\n\nUnable to load referral data right now.",
            reply_markup=_customer_nav([InlineKeyboardButton(text="🔄 Retry", callback_data="referral")]),
        )


# ------------------------- Help -------------------------
async def _send_help(target: Message):
    title = await get_setting("help_title", "❓ Help")
    description = await get_setting(
        "help_description",
        f"Welcome to {config.SHOP_NAME}.\n\nUse the buttons below to browse products, review your orders, get support, or open the shop."
    )
    text = f"{title}\n\n{description}"
    keyboard = _customer_nav(
        [InlineKeyboardButton(text="🛍️ Browse Products", callback_data="products_here")],
        [_customer_shop_button()],
        [InlineKeyboardButton(text="🧾 My Orders", callback_data="customer:orders")],
        [InlineKeyboardButton(text="👨‍💼 Contact Admin", callback_data="contact_admin")],
    )
    image = str(await get_setting("help_image", "") or "").strip()
    if image:
        photo = image[3:] if image.startswith("tg:") else image
        try:
            sender = getattr(target, "from_user", None)
            if sender is not None and bool(getattr(sender, "is_bot", False)):
                await target.edit_media(
                    media=InputMediaPhoto(media=photo, caption=text, parse_mode="HTML"),
                    reply_markup=keyboard,
                )
            else:
                await target.answer_photo(photo=photo, caption=text, reply_markup=keyboard)
            return
        except Exception:
            log.exception("Help image delivery failed; falling back to text")
    await _customer_screen(target, text, reply_markup=keyboard)


@dp.callback_query(F.data == "help")
async def help_handler(callback: CallbackQuery):
    await callback.answer()
    await _send_help(callback.message)


@dp.message(Command("help"))
async def help_command(message: Message):
    asyncio.create_task(delete_command_later(message))
    await _send_help(message)


# ------------------------- Support / Contact Admin -------------------------
def _support_keyboard(tickets: list[dict] | None = None, category: str = "general") -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="✍️ Send New Message", callback_data=f"customer:support:new:{category}")]]
    for ticket in (tickets or [])[:6]:
        tid = int(ticket["id"])
        subject = str(ticket.get("subject") or "Support")[:32]
        rows.append([InlineKeyboardButton(text=f"🎫 #{tid} · {subject}", callback_data=f"customer:ticket:{tid}")])
    rows.append([InlineKeyboardButton(text="🔄 Refresh Tickets", callback_data=f"customer:support:list:{category}")])
    rows.append([InlineKeyboardButton(text="🏠 Main Menu", callback_data="customer:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_support(target: Message, category: str = "general"):
    uid = int(target.from_user.id) if target.from_user else int(target.chat.id)
    tickets = await get_user_tickets(uid, limit=10)
    open_tickets = [t for t in tickets if str(t.get("status") or "").lower() not in {"closed", "resolved"}]
    description = await get_setting(
        "contact_admin_description",
        "Send your message below and CPM SHOP support will create a ticket for you."
    )
    text = (
        "👨‍💼 <b>Contact Admin</b>\n\n"
        f"{html.escape(description)}\n\n"
        "✍️ <b>You can write your message now.</b>\n"
        "Your next message will be sent to the support team as a new ticket."
    )
    if open_tickets:
        text += f"\n\n🎫 You have <b>{len(open_tickets)}</b> active ticket(s)."
    USER_FLOWS[uid] = {"type": "support_new", "category": category, "step": 1}
    await _customer_screen(target, text, reply_markup=_support_keyboard(open_tickets, category))


@dp.callback_query(F.data == "contact_admin")
async def contact_admin_handler(callback: CallbackQuery):
    await callback.answer()
    await _show_support(callback.message, "general")


@dp.message(Command("contactadmin"))
async def contact_admin_command(message: Message):
    asyncio.create_task(delete_command_later(message))
    await _show_support(message, "general")


@dp.callback_query(F.data.startswith("customer:support:new:"))
async def customer_support_new(callback: CallbackQuery):
    if not callback.from_user:
        return
    await callback.answer()
    category = callback.data.rsplit(":", 1)[1] or "general"
    USER_FLOWS[int(callback.from_user.id)] = {"type": "support_new", "category": category, "step": 1}
    await callback.message.edit_text(
        "👨‍💼 <b>New Support Ticket</b>\n\n"
        "✍️ Send your message now.\n"
        "Please include your order number when the message is about an order or payment.",
        reply_markup=_customer_nav([InlineKeyboardButton(text="❌ Cancel", callback_data="customer:flow:cancel")]),
    )


@dp.callback_query(F.data.startswith("customer:support:list:"))
async def customer_support_list(callback: CallbackQuery):
    if not callback.from_user:
        return
    await callback.answer()
    category = callback.data.rsplit(":", 1)[1] or "general"
    tickets = await get_user_tickets(callback.from_user.id, limit=10)
    active = [t for t in tickets if str(t.get("status") or "").lower() not in {"closed", "resolved"}]
    await callback.message.edit_text(
        "🎫 <b>Your Support Tickets</b>\n\n"
        + ("No active tickets." if not active else "Select a ticket to view the conversation."),
        reply_markup=_support_keyboard(active, category),
    )


@dp.callback_query(F.data.startswith("customer:ticket:"))
async def customer_ticket_detail(callback: CallbackQuery):
    if not callback.from_user:
        return
    await callback.answer()
    tid = int(callback.data.rsplit(":", 1)[1])
    ticket = await get_ticket_for_user(tid, callback.from_user.id)
    if not ticket:
        await callback.message.edit_text("❌ Ticket not found.", reply_markup=_customer_nav([InlineKeyboardButton(text="◀️ Support", callback_data="contact_admin")]))
        return
    text = (
        f"🎫 <b>Ticket #{tid}</b>\n\n"
        f"<b>{html.escape(str(ticket.get('subject') or 'Support'))}</b>\n"
        f"📌 {_status_label(ticket.get('status'))}\n"
    )
    for m in ticket.get("messages") or []:
        author = "🧑 You" if m.get("telegram_id") else "👨‍💼 Support"
        text += f"\n<b>{author}</b> · {html.escape(str(m.get('created_at') or ''))}\n{html.escape(str(m.get('message') or ''))}\n"
    rows = []
    if str(ticket.get("status") or "").lower() not in {"closed", "resolved"}:
        rows.append([InlineKeyboardButton(text="💬 Reply", callback_data=f"customer:ticketreply:{tid}")])
    rows.append([InlineKeyboardButton(text="◀️ My Tickets", callback_data="customer:support:list:general")])
    rows.append([InlineKeyboardButton(text="🏠 Main Menu", callback_data="customer:main")])
    await callback.message.edit_text(text[:3900], reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.callback_query(F.data.startswith("customer:ticketreply:"))
async def customer_ticket_reply_start(callback: CallbackQuery):
    if not callback.from_user:
        return
    await callback.answer()
    tid = int(callback.data.rsplit(":", 1)[1])
    ticket = await get_ticket_for_user(tid, callback.from_user.id)
    if not ticket:
        await callback.message.answer("❌ Ticket not found.", reply_markup=_customer_nav([InlineKeyboardButton(text="👨‍💼 Support", callback_data="contact_admin")]))
        return
    if str(ticket.get("status") or "").lower() in {"closed", "resolved"}:
        await callback.message.answer("🔒 This ticket is closed. Please create a new ticket.", reply_markup=_customer_nav([InlineKeyboardButton(text="✍️ New Ticket", callback_data="customer:support:new:general")]))
        return
    USER_FLOWS[int(callback.from_user.id)] = {"type": "support_reply", "ticket_id": tid, "step": 1}
    await callback.message.edit_text(
        f"💬 <b>Reply to Ticket #{tid}</b>\n\nSend your message now.",
        reply_markup=_customer_nav([InlineKeyboardButton(text="❌ Cancel", callback_data=f"customer:ticket:{tid}")]),
    )


# ------------------------- Feedback -------------------------
@dp.callback_query(F.data == "feedback")
async def feedback_handler(callback: CallbackQuery):
    await callback.answer()
    rows = await list_feedback(limit=10)
    description = await get_setting(
        "feedback_description",
        "Recent customer feedback from CPM SHOP."
    )
    lines = ["⭐ <b>Customer Feedback</b>", "", html.escape(description)]
    for row in rows:
        name = row.get("first_name") or row.get("username") or "Customer"
        rating = max(1, min(5, int(row.get("rating") or 5)))
        msg = str(row.get("message") or "").strip()
        lines.append(f"\n<b>{'⭐' * rating}</b> · {html.escape(str(name))}\n{html.escape(msg[:300])}")
    text = "\n".join(lines) if rows else "⭐ <b>Customer Feedback</b>\n\nNo feedback yet."
    await callback.message.edit_text(
        text[:3900],
        reply_markup=_customer_nav(
            [InlineKeyboardButton(text="✍️ Write Feedback", callback_data="feedback:add")],
            [InlineKeyboardButton(text="🔄 Refresh", callback_data="feedback")],
        ),
    )


@dp.message(Command("feedback"))
async def feedback_command(message: Message):
    asyncio.create_task(delete_command_later(message))
    rows = await list_feedback(limit=10)
    description = await get_setting("feedback_description", "Recent customer feedback from CPM SHOP.")
    lines = ["⭐ <b>Customer Feedback</b>", "", html.escape(description)]
    for row in rows:
        name = row.get("first_name") or row.get("username") or "Customer"
        rating = max(1, min(5, int(row.get("rating") or 5)))
        msg = str(row.get("message") or "").strip()
        lines.append(f"\n<b>{'⭐' * rating}</b> · {html.escape(str(name))}\n{html.escape(msg[:300])}")
    await message.answer(
        ("\n".join(lines) if rows else "⭐ <b>Customer Feedback</b>\n\nNo feedback yet.")[:3900],
        reply_markup=_customer_nav(
            [InlineKeyboardButton(text="✍️ Write Feedback", callback_data="feedback:add")],
            [InlineKeyboardButton(text="🔄 Refresh", callback_data="feedback")],
        ),
    )


@dp.callback_query(F.data == "feedback:add")
async def add_feedback_start(callback: CallbackQuery):
    if not callback.from_user:
        return
    await callback.answer()
    USER_FLOWS[int(callback.from_user.id)] = {"type": "feedback", "step": 1, "rating": 5}
    await callback.message.edit_text(
        "✍️ <b>Add Feedback</b>\n\n"
        "Send your feedback in your next message.\n"
        "Your rating starts at <b>5⭐</b>. You can change it below before submitting.",
        reply_markup=_customer_nav(
            [
                InlineKeyboardButton(text="⭐ 1", callback_data="feedback:rating:1"),
                InlineKeyboardButton(text="⭐ 2", callback_data="feedback:rating:2"),
                InlineKeyboardButton(text="⭐ 3", callback_data="feedback:rating:3"),
                InlineKeyboardButton(text="⭐ 4", callback_data="feedback:rating:4"),
                InlineKeyboardButton(text="⭐ 5", callback_data="feedback:rating:5"),
            ],
            [InlineKeyboardButton(text="❌ Cancel", callback_data="customer:flow:cancel")],
        ),
    )


@dp.message(Command("addfeedback"))
async def add_feedback_command(message: Message):
    asyncio.create_task(delete_command_later(message))
    USER_FLOWS[int(message.from_user.id)] = {"type": "feedback", "step": 1, "rating": 5}
    price = await get_setting("paid_review_price_stars", "")
    price_note = f"\n\n💫 Product paid-review price: ⭐ {html.escape(price)} Stars" if price.strip() else ""
    await message.answer(
        "✍️ <b>Add Feedback</b>\n\n"
        "Send your feedback in your next message.\n"
        "Your rating starts at <b>5⭐</b>. You can change it below before submitting."
        f"{price_note}",
        reply_markup=_customer_nav(
            [InlineKeyboardButton(text="⭐ 1", callback_data="feedback:rating:1"), InlineKeyboardButton(text="⭐ 2", callback_data="feedback:rating:2")],
            [InlineKeyboardButton(text="⭐ 3", callback_data="feedback:rating:3"), InlineKeyboardButton(text="⭐ 4", callback_data="feedback:rating:4"), InlineKeyboardButton(text="⭐ 5", callback_data="feedback:rating:5")],
            [InlineKeyboardButton(text="❌ Cancel", callback_data="customer:flow:cancel")],
        ),
    )


@dp.callback_query(F.data.startswith("feedback:rating:"))
async def feedback_rating(callback: CallbackQuery):
    if not callback.from_user:
        return
    await callback.answer()
    flow = USER_FLOWS.get(int(callback.from_user.id))
    if not flow or flow.get("type") != "feedback":
        await callback.message.answer("Start /addfeedback first.", reply_markup=_customer_nav([InlineKeyboardButton(text="✍️ Add Feedback", callback_data="feedback:add")]))
        return
    rating = max(1, min(5, int(callback.data.rsplit(":", 1)[1])))
    flow["rating"] = rating
    await callback.message.edit_text(
        "✍️ <b>Add Feedback</b>\n\n"
        f"Selected rating: <b>{'⭐' * rating}</b>\n\n"
        "Now send your feedback message.",
        reply_markup=_customer_nav([InlineKeyboardButton(text="❌ Cancel", callback_data="customer:flow:cancel")]),
    )


# ------------------------- Orders -------------------------
async def _render_customer_orders(message: Message, user_id: int):
    rows = await get_user_orders(user_id, limit=12)
    if not rows:
        await _customer_screen(message, 
            "🧾 <b>My Orders</b>\n\nYou have no orders yet.",
            reply_markup=_customer_nav(
                [InlineKeyboardButton(text="🛍️ Browse Products", callback_data="products_here")],
                [_customer_shop_button()],
            ),
        )
        return
    description = await get_setting(
        "orders_description",
        "View your current and previous orders and check their status."
    )
    lines = ["🧾 <b>My Orders</b>", "", html.escape(description), "", "Select an order to view its full details."]
    buttons = []
    for row in rows:
        oid = int(row["order_id"])
        total = int(row.get("total_stars") or 0)
        status = _status_label(row.get("status"))
        qty = int(row.get("total_quantity") or 0)
        lines.append(f"\n<b>#{oid}</b> · ⭐ {total} · {status} · {qty} item(s)")
        buttons.append([InlineKeyboardButton(text=f"🧾 Order #{oid} · ⭐ {total}", callback_data=f"customer:order:{oid}")])
    buttons.append([InlineKeyboardButton(text="🔄 Refresh", callback_data="customer:orders")])
    buttons.append([InlineKeyboardButton(text="🏠 Main Menu", callback_data="customer:main")])
    await _customer_screen(message, "\n".join(lines)[:3900], reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data == "customer:orders")
async def customer_orders_callback(callback: CallbackQuery):
    if not callback.from_user:
        return
    await callback.answer()
    await _render_customer_orders(callback.message, int(callback.from_user.id))


@dp.message(Command("orders"))
async def orders_command(message: Message):
    asyncio.create_task(delete_command_later(message))
    await _render_customer_orders(message, int(message.from_user.id))


def _customer_order_detail_text(order):
    oid = int(order.get("order_id") or order.get("id"))
    text = (
        f"🧾 <b>Order #{oid}</b>\n\n"
        f"📌 Status: <b>{_status_label(order.get('status'))}</b>\n"
        f"⭐ Total: <b>{int(order.get('total_stars') or 0)}</b>\n"
        f"🎟️ Promo Discount: <b>{int(order.get('promo_discount_stars') or 0)}</b> Stars\n"
        f"📅 Created: {_esc(order.get('created_at') or '-')}\n\n"
        "<b>Items</b>\n"
    )
    for item in order.get("items") or []:
        text += f"• {html.escape(str(item.get('product_name') or 'Product'))} × {int(item.get('quantity') or 0)} · ⭐ {int(item.get('price_stars') or 0)} each\n"
    payment = order.get("payment") or {}
    if payment:
        text += f"\n💳 Payment: <b>{_status_label(payment.get('status'))}</b> · ⭐ {int(payment.get('amount_stars') or 0)}\n"
    delivery = order.get("delivery") or {}
    if delivery:
        text += f"📦 Delivery: <b>{_status_label(delivery.get('delivery_status'))}</b>\n"
        if delivery.get("delivery_error"):
            text += f"⚠️ {html.escape(str(delivery.get('delivery_error'))[:300])}\n"
        if delivery.get("delivery_attempts"):
            text += f"🔁 Attempts: <b>{int(delivery.get('delivery_attempts') or 0)}</b>\n"
    delivered = order.get("delivery_items") or []
    if delivered:
        text += "\n<b>Delivered Items</b>\n"
        for item in delivered[:20]:
            text += f"<code>{html.escape(str(item))}</code>\n"
    return text[:3900]


def _customer_order_detail_buttons(order, oid):
    delivery = order.get("delivery") or {}
    delivery_status = str(delivery.get("delivery_status") or "").lower()
    order_status = str(order.get("status") or "").lower()
    rows = []
    if order_status in {"paid", "processing"} and delivery_status not in {"delivered", "delivering"}:
        rows.append([InlineKeyboardButton(text="🔄 Retry Delivery", callback_data=f"customer:orderdelivery:{oid}")])
    rows.append([InlineKeyboardButton(text="🙈 Hide Order", callback_data=f"customer:orderhide:{oid}")])
    rows.append([InlineKeyboardButton(text="◀️ My Orders", callback_data="customer:orders")])
    rows.append([InlineKeyboardButton(text="🏠 Main Menu", callback_data="customer:main")])
    return rows


@dp.callback_query(F.data.startswith("customer:order:"))
async def customer_order_detail(callback: CallbackQuery):
    if not callback.from_user:
        return
    await callback.answer()
    oid = int(callback.data.rsplit(":", 1)[1])
    order = await get_user_order_details(oid, callback.from_user.id)
    if not order:
        await callback.message.edit_text("❌ Order not found.", reply_markup=_customer_nav([InlineKeyboardButton(text="◀️ My Orders", callback_data="customer:orders")]))
        return
    await callback.message.edit_text(
        _customer_order_detail_text(order),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=_customer_order_detail_buttons(order, oid)),
    )


@dp.callback_query(F.data.startswith("customer:orderdelivery:"))
async def customer_order_delivery_retry(callback: CallbackQuery):
    if not callback.from_user:
        return
    oid = int(callback.data.rsplit(":", 1)[1])
    result = await _deliver_order_to_telegram(int(callback.from_user.id), oid)
    status = str(result.get("status") or "")
    if status == "delivered":
        await callback.answer("Delivery completed.", show_alert=True)
    elif status == "busy":
        await callback.answer("Delivery is already being processed.", show_alert=True)
    elif status == "failed":
        await callback.answer("Delivery failed. Please retry or contact support.", show_alert=True)
    else:
        await callback.answer("Delivery is not available for this order.", show_alert=True)
    order = await get_user_order_details(oid, callback.from_user.id)
    if order:
        await callback.message.edit_text(
            _customer_order_detail_text(order),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=_customer_order_detail_buttons(order, oid)),
        )


@dp.callback_query(F.data.startswith("customer:orderhide:"))
async def customer_order_hide(callback: CallbackQuery):
    if not callback.from_user:
        return
    await callback.answer()
    oid = int(callback.data.rsplit(":", 1)[1])
    if not await hide_user_order(oid, callback.from_user.id):
        await callback.answer("Order was not hidden.", show_alert=True)
        return
    await _render_customer_orders(callback.message, int(callback.from_user.id))


# ------------------------- Notifications -------------------------
async def _render_customer_notifications(message: Message, user_id: int):
    rows = await get_user_notifications(user_id, limit=12)
    unread = await get_unread_notification_count(user_id)
    if not rows:
        await _customer_screen(message, 
            "🔔 <b>Notifications</b>\n\nYou have no notifications yet.",
            reply_markup=_customer_nav([InlineKeyboardButton(text="🔄 Refresh", callback_data="customer:notifications")]),
        )
        return
    description = await get_setting(
        "notifications_description",
        "Stay updated with order changes, replies, promotions and other store notifications."
    )
    lines = ["🔔 <b>Notifications</b>", "", html.escape(description), f"\nUnread: <b>{unread}</b>", ""]
    buttons = []
    for row in rows:
        nid = int(row["id"])
        status = str(row.get("status") or "unread").lower()
        icon = "🔵" if status == "unread" else "⚪"
        title = str(row.get("title") or "Notification")[:38]
        preview = str(row.get("message") or "").replace("\n", " ")[:70]
        lines.append(f"{icon} <b>{html.escape(title)}</b> · {html.escape(str(row.get('created_at') or ''))}\n{html.escape(preview)}")
        buttons.append([InlineKeyboardButton(text=f"{icon} {title}", callback_data=f"customer:notif:{nid}")])
    if unread:
        buttons.append([InlineKeyboardButton(text="✅ Mark All as Read", callback_data="customer:notifreadall")])
    buttons.append([InlineKeyboardButton(text="🔄 Refresh", callback_data="customer:notifications")])
    buttons.append([InlineKeyboardButton(text="🏠 Main Menu", callback_data="customer:main")])
    await _customer_screen(message, "\n".join(lines)[:3900], reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data == "customer:notifications")
async def customer_notifications_callback(callback: CallbackQuery):
    if not callback.from_user:
        return
    await callback.answer()
    await _render_customer_notifications(callback.message, int(callback.from_user.id))


@dp.message(Command("notifications"))
async def notifications_command(message: Message):
    asyncio.create_task(delete_command_later(message))
    await _render_customer_notifications(message, int(message.from_user.id))


@dp.callback_query(F.data.startswith("customer:notif:"))
async def customer_notification_detail(callback: CallbackQuery):
    if not callback.from_user:
        return
    await callback.answer()
    nid = int(callback.data.rsplit(":", 1)[1])
    row = await mark_notification_read(nid, callback.from_user.id)
    if not row:
        await callback.message.edit_text("❌ Notification not found.", reply_markup=_customer_nav([InlineKeyboardButton(text="◀️ Notifications", callback_data="customer:notifications")]))
        return
    item = await fetch_one(
        "SELECT title,message,kind,created_at FROM notifications n JOIN users u ON u.id=n.user_id WHERE n.id=? AND u.telegram_id=?",
        (nid, callback.from_user.id),
    )
    if not item:
        await callback.message.edit_text("❌ Notification not found.", reply_markup=_customer_nav([InlineKeyboardButton(text="◀️ Notifications", callback_data="customer:notifications")]))
        return
    text = (
        f"🔔 <b>{html.escape(str(item.get('title') or 'Notification'))}</b>\n\n"
        f"{html.escape(str(item.get('message') or ''))}\n\n"
        f"<small>{html.escape(str(item.get('created_at') or ''))}</small>"
    )
    await callback.message.edit_text(text[:3900], reply_markup=_customer_nav([InlineKeyboardButton(text="◀️ Notifications", callback_data="customer:notifications")]))


@dp.callback_query(F.data == "customer:notifreadall")
async def customer_notifications_read_all(callback: CallbackQuery):
    if not callback.from_user:
        return
    await callback.answer()
    await mark_all_notifications_read(callback.from_user.id)
    await _render_customer_notifications(callback.message, int(callback.from_user.id))


# ------------------------- Giveaways -------------------------
async def _render_customer_giveaways(message: Message, user_id: int):
    user = await get_or_create_user(user_id)
    rows = await fetch_all(
        """
        SELECT g.id,g.title,g.description,g.status,g.winner_count,g.starts_at,g.ends_at,
               CASE WHEN e.id IS NULL THEN FALSE ELSE TRUE END AS joined,
               (SELECT COUNT(*) FROM giveaway_entries ge WHERE ge.giveaway_id=g.id) AS entries
        FROM giveaways g
        LEFT JOIN giveaway_entries e ON e.giveaway_id=g.id AND e.user_id=?
        WHERE g.status='active'
          AND (g.starts_at IS NULL OR g.starts_at<=CURRENT_TIMESTAMP)
          AND (g.ends_at IS NULL OR g.ends_at>CURRENT_TIMESTAMP)
        ORDER BY g.id DESC LIMIT 20
        """,
        (int(user["id"]),),
    )
    if not rows:
        await _customer_screen(message, 
            "🎉 <b>Giveaways</b>\n\nThere are no active giveaways right now.",
            reply_markup=_customer_nav([InlineKeyboardButton(text="🔄 Refresh", callback_data="customer:giveaways")]),
        )
        return
    description = await get_setting(
        "giveaways_description",
        "Join available CPM SHOP giveaways and check their rules and prizes."
    )
    lines = ["🎉 <b>Active Giveaways</b>", "", html.escape(description), "", "Select a giveaway to view its rules and join."]
    buttons = []
    for g in rows:
        gid = int(g["id"])
        joined = bool(g.get("joined"))
        lines.append(f"\n🎁 <b>{html.escape(str(g.get('title') or 'Giveaway'))}</b> · 👥 {int(g.get('entries') or 0)} · {'✅ Joined' if joined else 'Not joined'}")
        buttons.append([InlineKeyboardButton(text=f"🎉 {str(g.get('title') or 'Giveaway')[:34]}", callback_data=f"customer:giveaway:{gid}")])
    buttons.append([InlineKeyboardButton(text="🔄 Refresh", callback_data="customer:giveaways")])
    buttons.append([InlineKeyboardButton(text="🏠 Main Menu", callback_data="customer:main")])
    await _customer_screen(message, "\n".join(lines)[:3900], reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data == "customer:giveaways")
async def customer_giveaways_callback(callback: CallbackQuery):
    if not callback.from_user:
        return
    await callback.answer()
    await _render_customer_giveaways(callback.message, int(callback.from_user.id))


@dp.message(Command("giveaways"))
async def giveaways_command(message: Message):
    asyncio.create_task(delete_command_later(message))
    await _render_customer_giveaways(message, int(message.from_user.id))


@dp.callback_query(F.data.startswith("customer:giveaway:"))
async def customer_giveaway_detail(callback: CallbackQuery):
    if not callback.from_user:
        return
    await callback.answer()
    gid = int(callback.data.rsplit(":", 1)[1])
    user = await get_or_create_user(callback.from_user.id)
    g = await fetch_one(
        "SELECT g.*, EXISTS(SELECT 1 FROM giveaway_entries e WHERE e.giveaway_id=g.id AND e.user_id=?) AS joined, (SELECT COUNT(*) FROM giveaway_entries ge WHERE ge.giveaway_id=g.id) AS entries FROM giveaways g WHERE g.id=?",
        (int(user["id"]), gid),
    )
    if not g:
        await callback.message.edit_text("❌ Giveaway not found.", reply_markup=_customer_nav([InlineKeyboardButton(text="◀️ Giveaways", callback_data="customer:giveaways")]))
        return
    joined = bool(g.get("joined"))
    text = (
        f"🎉 <b>{html.escape(str(g.get('title') or 'Giveaway'))}</b>\n\n"
        f"📌 Status: <b>{_status_label(g.get('status'))}</b>\n"
        f"🏆 Winners: <b>{int(g.get('winner_count') or 1)}</b>\n"
        f"👥 Entries: <b>{int(g.get('entries') or 0)}</b>\n"
        f"🕐 Ends: {html.escape(str(g.get('ends_at') or 'When the giveaway ends'))}\n\n"
        f"📝 {html.escape(str(g.get('description') or 'No additional rules provided.'))}\n\n"
        "🎁 Prize details are provided to the winners directly."
    )
    rows = []
    if str(g.get("status") or "").lower() == "active" and not joined:
        rows.append([InlineKeyboardButton(text="🎟️ Join Giveaway", callback_data=f"customer:giveawayjoin:{gid}")])
    elif joined:
        rows.append([InlineKeyboardButton(text="✅ You Joined", callback_data=f"customer:giveaway:{gid}")])
    rows.append([InlineKeyboardButton(text="◀️ Giveaways", callback_data="customer:giveaways")])
    rows.append([InlineKeyboardButton(text="🏠 Main Menu", callback_data="customer:main")])
    await callback.message.edit_text(text[:3900], reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.callback_query(F.data.startswith("customer:giveawayjoin:"))
async def customer_giveaway_join(callback: CallbackQuery):
    if not callback.from_user:
        return
    await callback.answer()
    gid = int(callback.data.rsplit(":", 1)[1])
    user = await get_or_create_user(callback.from_user.id)
    g = await fetch_one("SELECT id,title,status FROM giveaways WHERE id=?", (gid,))
    if not g or str(g.get("status") or "").lower() != "active":
        await callback.answer("This giveaway is not active.", show_alert=True)
        return
    row = await fetch_one(
        "INSERT INTO giveaway_entries(giveaway_id,user_id) VALUES(?,?) ON CONFLICT(giveaway_id,user_id) DO NOTHING RETURNING id",
        (gid, int(user["id"])),
    )
    await callback.answer("🎉 You joined the giveaway!" if row else "You are already entered.", show_alert=True)
    await customer_giveaway_detail(callback)


# ------------------------- Promo Shop -------------------------
def _promo_target_text(row: dict) -> str:
    target = str(row.get("target_type") or "global").lower()
    tid = row.get("target_id")
    if target == "global" or not tid:
        return "All products"
    name = str(row.get("target_name") or "").strip()
    return f"{target.title()}: {name} (#{int(tid)})" if name else f"{target.title()} #{int(tid)}"


async def _render_customer_promoshop(message: Message, user_id: int):
    rows = await fetch_all(
        """
        SELECT p.id,p.discount_percent,p.discount_stars,p.sale_price_stars,p.target_type,p.target_id,
               p.min_cart_quantity,p.max_uses_per_user,p.description,p.expires_at,
               CASE p.target_type
                   WHEN 'game' THEN (SELECT g.name FROM games g WHERE g.id=p.target_id)
                   WHEN 'category' THEN (SELECT c.name FROM categories c WHERE c.id=p.target_id)
                   WHEN 'product' THEN (SELECT pr.name FROM products pr WHERE pr.id=p.target_id)
                   ELSE 'All products'
               END AS target_name
        FROM promo_codes p
        WHERE p.active=TRUE
          AND COALESCE(p.access_mode,'public')='sale'
          AND p.sale_price_stars>0
          AND p.sold_to_user_id IS NULL
          AND (p.max_uses IS NULL OR (SELECT COALESCE(SUM(pr.quantity),0) FROM promo_redemptions pr WHERE pr.promo_code_id=p.id)<p.max_uses)
          AND (p.expires_at IS NULL OR p.expires_at>CURRENT_TIMESTAMP)
        ORDER BY p.created_at DESC,p.id DESC LIMIT 30
        """
    )
    if not rows:
        await _customer_screen(message, 
            "🏷️ <b>Promo Shop</b>\n\nNo promo codes are currently available for purchase.",
            reply_markup=_customer_nav([InlineKeyboardButton(text="🔄 Refresh", callback_data="customer:promoshop")]),
        )
        return
    description = await get_setting("promo_description", "Browse promo codes available for purchase.")
    purchase_description = await get_setting("promo_purchase_description", "Purchase a promo code, then use it at checkout.")
    lines = ["🏷️ <b>Promo Shop</b>", "", html.escape(description), html.escape(purchase_description)]
    buttons = []
    for row in rows:
        rid = int(row["id"])
        percent = int(row.get("discount_percent") or 0)
        fixed = int(row.get("discount_stars") or 0)
        discount = f"{percent}% off" if percent else f"⭐ {fixed} off"
        price = int(row.get("sale_price_stars") or 0)
        target = _promo_target_text(row)
        note = str(row.get("description") or "").strip()
        lines.append(f"\n🎟️ <b>Promo #{rid}</b> · {html.escape(discount)} · ⭐ {price}\n🎯 {html.escape(target)} · Min qty {int(row.get('min_cart_quantity') or 1)}\n{html.escape(note[:180]) if note else 'No additional description.'}")
        buttons.append([InlineKeyboardButton(text=f"💳 Buy Promo #{rid} · ⭐ {price}", callback_data=f"customer:promobuy:{rid}")])
    buttons.append([InlineKeyboardButton(text="🔄 Refresh", callback_data="customer:promoshop")])
    buttons.append([InlineKeyboardButton(text="🏠 Main Menu", callback_data="customer:main")])
    await _customer_screen(message, "\n".join(lines)[:3900], reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data == "customer:promoshop")
async def customer_promoshop_callback(callback: CallbackQuery):
    if not callback.from_user:
        return
    await callback.answer()
    await _render_customer_promoshop(callback.message, int(callback.from_user.id))


@dp.message(Command("promoshop"))
async def promoshop_command(message: Message):
    asyncio.create_task(delete_command_later(message))
    await _render_customer_promoshop(message, int(message.from_user.id))


@dp.callback_query(F.data.startswith("customer:promobuy:"))
async def customer_promobuy(callback: CallbackQuery):
    if not callback.from_user:
        return
    await callback.answer()
    rid = int(callback.data.rsplit(":", 1)[1])
    try:
        promo = await get_promo_for_purchase(rid, callback.from_user.id)
        if not promo:
            await callback.message.edit_text("❌ This promo code is no longer available.", reply_markup=_customer_nav([InlineKeyboardButton(text="◀️ Promo Shop", callback_data="customer:promoshop")]))
            return
        price = int(promo.get("sale_price_stars") or 0)
        if price <= 0:
            raise ValueError("This promo code is not for sale.")
        from api.store import create_invoice_link
        invoice_url = await create_invoice_link(
            title="CPM SHOP Promo Code",
            description=str(promo.get("description") or "Promo code purchase")[:255],
            payload=f"promo:{rid}",
            price_stars=price,
        )
        target = _promo_target_text(promo)
        text = (
            "🏷️ <b>Promo Purchase</b>\n\n"
            f"🎟️ Promo ID: <code>#{rid}</code>\n"
            f"🎯 Target: <b>{html.escape(target)}</b>\n"
            f"💸 Discount: <b>{html.escape(''+(str(int(promo.get('discount_percent') or 0))+'%' if int(promo.get('discount_percent') or 0) else '⭐ '+str(int(promo.get('discount_stars') or 0))))}</b>\n"
            f"💳 Price: <b>⭐ {price}</b>\n\n"
            "The actual promo code is hidden until the purchase is completed."
        )
        await callback.message.edit_text(text, reply_markup=_customer_nav([InlineKeyboardButton(text=f"💳 Pay ⭐ {price}", url=invoice_url)], [InlineKeyboardButton(text="◀️ Promo Shop", callback_data="customer:promoshop")]))
    except Exception:
        log.exception("Customer promo purchase failed | user=%s | promo=%s", callback.from_user.id, rid)
        await callback.message.edit_text("❌ Unable to create the promo purchase invoice right now.", reply_markup=_customer_nav([InlineKeyboardButton(text="🔄 Retry", callback_data=f"customer:promobuy:{rid}"), InlineKeyboardButton(text="◀️ Promo Shop", callback_data="customer:promoshop")]))


# ------------------------- Cart -------------------------
async def _render_customer_cart(message: Message, user_id: int):
    items = await get_cart_items(user_id)
    if not items:
        USER_CART_PROMOS.pop(user_id, None)
        await _customer_screen(message, 
            "🛒 <b>My Cart</b>\n\nYour cart is empty.",
            reply_markup=_customer_nav(
                [InlineKeyboardButton(text="🛍️ Browse Products", callback_data="products_here")],
                [_customer_shop_button()],
            ),
        )
        return
    subtotal = sum(int(i.get("final_price_stars") or 0) * int(i.get("quantity") or 0) for i in items)
    promo_code = USER_CART_PROMOS.get(user_id)
    discount = 0
    total = subtotal
    promo_note = ""
    if promo_code:
        try:
            promo_result = await calculate_promo_discount(user_id, promo_code, subtotal, cart_items=items)
            if promo_result.get("valid"):
                discount = int(promo_result.get("discount_stars") or 0)
                total = int(promo_result.get("total_stars") or 0)
                promo_note = f"\n🏷️ Promo <code>{html.escape(promo_code)}</code>: -⭐ {discount}"
            else:
                USER_CART_PROMOS.pop(user_id, None)
                promo_code = None
        except Exception:
            log.exception("Cart promo preview failed")
            USER_CART_PROMOS.pop(user_id, None)
            promo_code = None
    loyalty_id = USER_LOYALTY_REDEMPTIONS.get(user_id)
    loyalty_note = ""
    if loyalty_id:
        red = await fetch_one("SELECT points_spent,discount_stars,status,expires_at FROM loyalty_redemptions lr JOIN users u ON u.id=lr.user_id WHERE lr.id=? AND u.telegram_id=?", (loyalty_id,user_id))
        if red and str(red.get("status")) == "reserved":
            total = max(0, total - int(red.get("discount_stars") or 0))
            loyalty_note = f"\n⭐ Reward: -⭐ {int(red.get('discount_stars') or 0)}"
        else:
            USER_LOYALTY_REDEMPTIONS.pop(user_id, None)
            loyalty_id = None
    lines = ["🛒 <b>My Cart</b>", ""]
    buttons = []
    for item in items:
        pid = int(item["product_id"])
        qty = int(item["quantity"] or 0)
        unit = int(item.get("final_price_stars") or 0)
        name = str(item.get("name") or "Product")[:42]
        lines.append(f"📦 <b>{html.escape(name)}</b> × {qty} · ⭐ {unit * qty}")
        buttons.append([
            InlineKeyboardButton(text="➖", callback_data=f"customer:cart:minus:{pid}"),
            InlineKeyboardButton(text=f"× {qty}", callback_data=f"customer:cart:noop:{pid}"),
            InlineKeyboardButton(text="➕", callback_data=f"customer:cart:plus:{pid}"),
            InlineKeyboardButton(text="🗑️", callback_data=f"customer:cart:remove:{pid}"),
        ])
    lines.append(f"\nSubtotal: ⭐ <b>{subtotal}</b>{promo_note}{loyalty_note}\n<b>Total: ⭐ {total}</b>")
    buttons.append([InlineKeyboardButton(text="🏷️ Apply Promo Code", callback_data="customer:cart:promo")])
    if promo_code:
        buttons.append([InlineKeyboardButton(text="✖️ Remove Promo", callback_data="customer:cart:promoremove")])
    if loyalty_id:
        buttons.append([InlineKeyboardButton(text="✖️ Remove Reward", callback_data="customer:cart:rewardremove")])
    buttons.append([InlineKeyboardButton(text=f"💳 Checkout · ⭐ {total}", callback_data="customer:cart:checkout")])
    buttons.append([InlineKeyboardButton(text="🔄 Refresh", callback_data="customer:cart")])
    buttons.append([InlineKeyboardButton(text="🏠 Main Menu", callback_data="customer:main")])
    await _customer_screen(message, "\n".join(lines)[:3900], reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data.startswith("customer:cart:plus:"))
async def customer_cart_plus(callback: CallbackQuery):
    if not callback.from_user:
        return
    await callback.answer()
    pid = int(callback.data.rsplit(":",1)[1])
    items = await get_cart_items(callback.from_user.id)
    current = next((int(x.get("quantity") or 0) for x in items if int(x.get("product_id") or 0)==pid), 0)
    try:
        await update_cart_item(callback.from_user.id, pid, current + 1)
    except Exception as exc:
        await callback.answer(str(exc), show_alert=True)
    await _render_customer_cart(callback.message, int(callback.from_user.id))


@dp.callback_query(F.data.startswith("customer:cart:minus:"))
async def customer_cart_minus(callback: CallbackQuery):
    if not callback.from_user:
        return
    await callback.answer()
    pid = int(callback.data.rsplit(":",1)[1])
    items = await get_cart_items(callback.from_user.id)
    current = next((int(x.get("quantity") or 0) for x in items if int(x.get("product_id") or 0)==pid), 0)
    try:
        await update_cart_item(callback.from_user.id, pid, current - 1)
    except Exception as exc:
        await callback.answer(str(exc), show_alert=True)
    await _render_customer_cart(callback.message, int(callback.from_user.id))


@dp.callback_query(F.data.startswith("customer:cart:remove:"))
async def customer_cart_remove(callback: CallbackQuery):
    if not callback.from_user:
        return
    await callback.answer()
    pid = int(callback.data.rsplit(":",1)[1])
    await remove_cart_item(callback.from_user.id, pid)
    await _render_customer_cart(callback.message, int(callback.from_user.id))


@dp.callback_query(F.data.startswith("customer:cart:noop:"))
async def customer_cart_noop(callback: CallbackQuery):
    await callback.answer()


@dp.callback_query(F.data == "customer:cart:promo")
async def customer_cart_promo_start(callback: CallbackQuery):
    if not callback.from_user:
        return
    await callback.answer()
    USER_FLOWS[int(callback.from_user.id)] = {"type": "cart_promo", "step": 1}
    await callback.message.edit_text(
        "🏷️ <b>Apply Promo Code</b>\n\nSend the promo code now.\n\nYou can cancel without changing your cart.",
        reply_markup=_customer_nav([InlineKeyboardButton(text="❌ Cancel", callback_data="customer:cart")]),
    )


@dp.callback_query(F.data == "customer:cart:promoremove")
async def customer_cart_promo_remove(callback: CallbackQuery):
    if not callback.from_user:
        return
    await callback.answer()
    USER_CART_PROMOS.pop(int(callback.from_user.id), None)
    await _render_customer_cart(callback.message, int(callback.from_user.id))


@dp.callback_query(F.data == "customer:cart:rewardremove")
async def customer_cart_reward_remove(callback: CallbackQuery):
    if not callback.from_user:
        return
    await callback.answer()
    uid = int(callback.from_user.id)
    rid = USER_LOYALTY_REDEMPTIONS.pop(uid, None)
    if rid:
        await cancel_loyalty_redemption(uid, rid)
    await _render_customer_cart(callback.message, uid)


@dp.callback_query(F.data == "customer:cart:checkout")
async def customer_cart_checkout(callback: CallbackQuery):
    if not callback.from_user:
        return
    await callback.answer()
    uid = int(callback.from_user.id)
    promo = USER_CART_PROMOS.get(uid)
    loyalty_id = USER_LOYALTY_REDEMPTIONS.get(uid)
    order = None
    try:
        order = await create_pending_order_from_cart(uid, promo_code=promo, loyalty_redemption_id=loyalty_id)
        total = int(order.get("total_stars") or 0)
        if total == 0:
            result = await complete_free_order(int(order["order_id"]), uid)
            USER_CART_PROMOS.pop(uid, None)
            USER_LOYALTY_REDEMPTIONS.pop(uid, None)
            delivered = result.get("inventory_items") or []
            text = f"✅ <b>Free Order Completed</b>\n\nOrder <b>#{int(order['order_id'])}</b> is complete."
            if delivered:
                text += "\n\n<b>Your items:</b>\n" + "\n".join(f"<code>{html.escape(str(x))}</code>" for x in delivered)
            delivery_result = await _deliver_order_to_telegram(uid, int(order["order_id"]))
            if delivery_result.get("status") == "failed":
                text += "\n\n⚠️ Delivery is still pending. You can retry it from the order details."
            await callback.message.edit_text(text[:3900], reply_markup=_customer_nav([InlineKeyboardButton(text="🧾 My Orders", callback_data="customer:orders")], [_customer_shop_button()]))
            return
        from api.store import create_invoice_link
        invoice_url = await create_invoice_link(
            title=f"CPM SHOP Order #{int(order['order_id'])}",
            description="CPM SHOP cart checkout",
            payload=f"order:{int(order['order_id'])}",
            price_stars=total,
        )
        USER_CART_PROMOS.pop(uid, None)
        USER_LOYALTY_REDEMPTIONS.pop(uid, None)
        await callback.message.edit_text(
            f"🧾 <b>Order #{int(order['order_id'])}</b>\n\nTotal: ⭐ <b>{total}</b>\n\nComplete payment using Telegram Stars.",
            reply_markup=_customer_nav([InlineKeyboardButton(text=f"💳 Pay ⭐ {total}", url=invoice_url)], [InlineKeyboardButton(text="◀️ Cart", callback_data="cart")]),
        )
    except ValueError as exc:
        await callback.message.edit_text(f"❌ {html.escape(str(exc))}", reply_markup=_customer_nav([InlineKeyboardButton(text="◀️ Cart", callback_data="cart")]))
    except Exception:
        log.exception("Customer cart checkout failed | user=%s", uid)
        if order and order.get("order_id"):
            try:
                await cancel_pending_order(int(order["order_id"]), uid)
            except Exception:
                pass
        await callback.message.edit_text("❌ Unable to create the order invoice right now. Your cart was not charged.", reply_markup=_customer_nav([InlineKeyboardButton(text="🔄 Retry Checkout", callback_data="customer:cart:checkout")], [InlineKeyboardButton(text="◀️ Cart", callback_data="cart")]))


# ------------------------- Loyalty Rewards -------------------------
@dp.message(Command("rewards"))
async def rewards_command(message: Message):
    asyncio.create_task(delete_command_later(message))
    await _render_rewards(message, int(message.from_user.id))


async def _render_rewards(message: Message, user_id: int):
    try:
        balance = await get_loyalty_balance(user_id)
        description = await get_setting(
            "rewards_description",
            "Earn reward points from completed purchases and use them for Star discounts."
        )
        text = (
            "⭐ <b>Loyalty Rewards</b>\n\n"
            f"{html.escape(description)}\n\n"
            f"Available: <b>{int(balance.get('available') or 0)}</b> points\n"
            f"Earned: <b>{int(balance.get('earned') or 0)}</b>\n"
            f"Spent: <b>{int(balance.get('spent') or 0)}</b>\n"
            f"Level: <b>{html.escape(str(balance.get('level') or 'Bronze'))}</b>\n"
            f"Progress: <b>{int(balance.get('level_progress') or 0)}%</b>\n\n"
            "Redeem points for a Star discount, then use the reward during cart checkout."
        )
        rows = [
            [InlineKeyboardButton(text="⭐ 100 → 1 Star", callback_data="customer:reward:100"), InlineKeyboardButton(text="⭐ 500 → 5 Stars", callback_data="customer:reward:500")],
            [InlineKeyboardButton(text="⭐ 1000 → 10 Stars", callback_data="customer:reward:1000")],
            [InlineKeyboardButton(text="📜 Reward History", callback_data="customer:rewardhistory")],
            [InlineKeyboardButton(text="🛒 Open Cart", callback_data="cart")],
        ]
        rid = USER_LOYALTY_REDEMPTIONS.get(user_id)
        if rid:
            rows.insert(3, [InlineKeyboardButton(text="✖️ Cancel Reserved Reward", callback_data="customer:rewardcancel")])
        rows.append([InlineKeyboardButton(text="🔄 Refresh", callback_data="customer:rewards")])
        rows.append([InlineKeyboardButton(text="🏠 Main Menu", callback_data="customer:main")])
        await _customer_screen(message, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception:
        log.exception("Rewards render failed")
        await _customer_screen(message, "⭐ <b>Loyalty Rewards</b>\n\nUnable to load your reward balance right now.", reply_markup=_customer_nav([InlineKeyboardButton(text="🔄 Retry", callback_data="customer:rewards")]))


@dp.callback_query(F.data == "customer:rewards")
async def customer_rewards_callback(callback: CallbackQuery):
    if not callback.from_user:
        return
    await callback.answer()
    await _render_rewards(callback.message, int(callback.from_user.id))


@dp.callback_query(F.data.startswith("customer:reward:"))
async def customer_reward_redeem(callback: CallbackQuery):
    if not callback.from_user:
        return
    await callback.answer()
    uid = int(callback.from_user.id)
    points = int(callback.data.rsplit(":",1)[1])
    old = USER_LOYALTY_REDEMPTIONS.pop(uid, None)
    if old:
        try:
            await cancel_loyalty_redemption(uid, old)
        except Exception:
            pass
    try:
        reward = await create_loyalty_redemption(uid, points)
        USER_LOYALTY_REDEMPTIONS[uid] = int(reward["id"])
        await callback.message.edit_text(
            "✅ <b>Reward Reserved</b>\n\n"
            f"⭐ Points used: <b>{int(reward.get('points_spent') or points)}</b>\n"
            f"💸 Discount: <b>⭐ {int(reward.get('discount_stars') or 0)}</b>\n\n"
            "Open your cart and checkout to use this reward.",
            reply_markup=_customer_nav([InlineKeyboardButton(text="🛒 Open Cart", callback_data="cart")], [InlineKeyboardButton(text="⭐ Rewards", callback_data="customer:rewards")]),
        )
    except Exception as exc:
        await callback.message.edit_text(f"❌ {html.escape(str(exc))}", reply_markup=_customer_nav([InlineKeyboardButton(text="◀️ Rewards", callback_data="customer:rewards")]))


@dp.callback_query(F.data == "customer:rewardcancel")
async def customer_reward_cancel(callback: CallbackQuery):
    if not callback.from_user:
        return
    await callback.answer()
    uid = int(callback.from_user.id)
    rid = USER_LOYALTY_REDEMPTIONS.pop(uid, None)
    if rid:
        await cancel_loyalty_redemption(uid, rid)
    await _render_rewards(callback.message, uid)


@dp.callback_query(F.data == "customer:rewardhistory")
async def customer_reward_history(callback: CallbackQuery):
    if not callback.from_user:
        return
    await callback.answer()
    rows = await get_loyalty_history(callback.from_user.id, limit=15)
    lines = ["📜 <b>Reward History</b>", ""]
    for row in rows:
        amount = int(row.get("amount") or 0)
        icon = "➕" if amount > 0 else "➖"
        lines.append(f"{icon} <b>{amount}</b> · {html.escape(str(row.get('reason') or 'Reward activity'))} · {html.escape(str(row.get('created_at') or ''))}")
    await callback.message.edit_text("\n".join(lines)[:3900] if rows else "📜 <b>Reward History</b>\n\nNo reward activity yet.", reply_markup=_customer_nav([InlineKeyboardButton(text="◀️ Rewards", callback_data="customer:rewards")]))


# ------------------------- Payment Support -------------------------
@dp.message(Command("paysupport"))
async def paysupport_command(message: Message):
    asyncio.create_task(delete_command_later(message))
    uid = int(message.from_user.id)
    USER_FLOWS[uid] = {"type": "support_new", "category": "payment", "step": 1}
    username = str(config.SUPPORT_USERNAME or "").lstrip("@")
    contact_line = f"\n\nSupport username: @{username}" if username else ""
    description = await get_setting(
        "contact_admin_description",
        "Send your payment or order problem to the support team."
    )
    await message.answer(
        "💳 <b>Payment Support</b>\n\n"
        f"{html.escape(description)}\n\n"
        "Send your payment/order problem in the next message.\n"
        "Please include the order number when available."
        f"{contact_line}",
        reply_markup=_customer_nav([InlineKeyboardButton(text="❌ Cancel", callback_data="customer:flow:cancel")], [_customer_shop_button()]),
    )


# ------------------------- Admin Promo Command -------------------------
@dp.message(Command("promos"))
async def promos_command(message: Message):
    if not message.from_user or not is_admin_user(message.from_user.id):
        await message.answer("❌ You are not authorized to manage promo codes.")
        return
    text, kb = await _render_promos(None)
    await message.answer(text, reply_markup=kb)


@dp.callback_query(F.data == "customer:flow:cancel")
async def customer_flow_cancel(callback: CallbackQuery):
    if not callback.from_user:
        return
    await callback.answer("Cancelled")
    uid = int(callback.from_user.id)
    _clear_user_flow(uid)
    await callback.message.edit_text(
        "✅ <b>Action cancelled.</b>",
        reply_markup=_customer_nav([InlineKeyboardButton(text="🏠 Main Menu", callback_data="customer:main")], main=False),
    )


# ------------------------- Customer text input router -------------------------
async def _handle_customer_text_flow(message: Message):
    if not message.from_user:
        return False
    uid = int(message.from_user.id)
    flow = USER_FLOWS.get(uid)
    if not flow:
        return False
    text = (message.text or "").strip()
    if not text:
        await message.answer("✍️ Please send a non-empty message.", reply_markup=_customer_nav([InlineKeyboardButton(text="❌ Cancel", callback_data="customer:flow:cancel")]))
        return True
    typ = flow.get("type")
    try:
        if typ == "feedback":
            feedback = await create_feedback(uid, text, int(flow.get("rating") or 5))
            _clear_user_flow(uid)
            rating = int((feedback or {}).get("rating") or flow.get("rating") or 5)
            try:
                await bot.send_message(
                    int(config.ADMIN_ID),
                    "⭐ <b>CPM SHOP ADMIN</b>\n\n"
                    "📩 <b>New customer feedback</b>\n"
                    f"👤 User: <code>{uid}</code>\n"
                    f"⭐ Rating: <b>{'⭐' * rating}</b>\n"
                    f"💬 {html.escape(text[:3500])}",
                )
            except Exception:
                log.exception("Failed to notify admin about feedback")
            await message.answer(
                "✅ <b>Feedback submitted successfully.</b>\n\nThank you for helping improve CPM SHOP.",
                reply_markup=_customer_nav([InlineKeyboardButton(text="✍️ Add Another Feedback", callback_data="feedback:add")]),
            )
            return True

        if typ == "support_new":
            category = str(flow.get("category") or "general")
            ticket = await create_ticket(uid, "Customer Support", text, category=category, priority="high" if category == "payment" else "normal")
            _clear_user_flow(uid)
            tid = int(ticket["id"])
            try:
                await bot.send_message(
                    int(config.ADMIN_ID),
                    "🎫 <b>CPM SHOP ADMIN</b>\n\n"
                    "📩 <b>New support ticket</b>\n"
                    f"🎫 Ticket: <code>#{tid}</code>\n"
                    f"👤 User: <code>{uid}</code>\n"
                    f"🏷️ Category: <b>{html.escape(category)}</b>\n\n"
                    f"💬 {html.escape(text[:3500])}",
                )
            except Exception:
                log.exception("Failed to notify admin about support ticket")
            await message.answer(
                f"✅ <b>Support ticket #{tid} created.</b>\n\nThe support team has received your message.",
                reply_markup=_customer_nav([InlineKeyboardButton(text="🎫 Open Ticket", callback_data=f"customer:ticket:{tid}")], [InlineKeyboardButton(text="👨‍💼 Contact Admin", callback_data="contact_admin")]),
            )
            return True

        if typ == "support_reply":
            tid = int(flow["ticket_id"])
            ticket = await add_ticket_message(uid, tid, text)
            _clear_user_flow(uid)
            try:
                await bot.send_message(
                    int(config.ADMIN_ID),
                    "💬 <b>CPM SHOP ADMIN</b>\n\n"
                    f"New customer reply on ticket <code>#{tid}</code>\n"
                    f"👤 User: <code>{uid}</code>\n\n{html.escape(text[:3500])}",
                )
            except Exception:
                log.exception("Failed to notify admin about ticket reply")
            await message.answer(
                f"✅ <b>Reply sent to ticket #{tid}.</b>",
                reply_markup=_customer_nav([InlineKeyboardButton(text="🎫 Open Ticket", callback_data=f"customer:ticket:{tid}")], [InlineKeyboardButton(text="🏠 Main Menu", callback_data="customer:main")]),
            )
            return True

        if typ == "cart_promo":
            items = await get_cart_items(uid)
            if not items:
                _clear_user_flow(uid)
                await message.answer("🛒 Your cart is empty.", reply_markup=_customer_nav([InlineKeyboardButton(text="🛒 Cart", callback_data="cart")]))
                return True
            subtotal = sum(int(i.get("final_price_stars") or 0) * int(i.get("quantity") or 0) for i in items)
            result = await calculate_promo_discount(uid, text.upper().replace(" ", ""), subtotal, cart_items=items)
            if not result.get("valid"):
                await message.answer(f"❌ {html.escape(str(result.get('error') or 'Invalid promo code.'))}\n\nTry another code or cancel.", reply_markup=_customer_nav([InlineKeyboardButton(text="❌ Cancel", callback_data="cart")]))
                return True
            USER_CART_PROMOS[uid] = text.upper().replace(" ", "")
            _clear_user_flow(uid)
            await message.answer(
                "✅ <b>Promo code applied.</b>\n\n"
                f"Discount: ⭐ <b>{int(result.get('discount_stars') or 0)}</b>\n"
                f"New total: ⭐ <b>{int(result.get('total_stars') or 0)}</b>",
                reply_markup=_customer_nav([InlineKeyboardButton(text="🛒 Review Cart", callback_data="cart")], [InlineKeyboardButton(text="🏠 Main Menu", callback_data="customer:main")]),
            )
            return True

    except ValueError as exc:
        await message.answer(f"❌ {html.escape(str(exc))}", reply_markup=_customer_nav([InlineKeyboardButton(text="❌ Cancel", callback_data="customer:flow:cancel")]))
        return True
    except Exception:
        log.exception("Customer text flow failed | user=%s | flow=%s", uid, typ)
        await message.answer("❌ Something went wrong while processing your message. Please try again.", reply_markup=_customer_nav([InlineKeyboardButton(text="❌ Cancel", callback_data="customer:flow:cancel")]))
        return True
    return False


@dp.message(Command("admin"))
async def admin_command(message: Message):
    if not message.from_user:
        return

    if message.from_user.id != config.ADMIN_ID:
        await message.answer(
            "❌ You are not authorized to access the Admin Panel."
        )
        return

    try:
        await message.answer(
            await admin_dashboard_text(),
            reply_markup=admin_menu(),
        )
    except Exception:
        log.exception("Admin dashboard failed")
        await message.answer(
            "👨‍💼 <b>CPM SHOP ADMIN</b>\n\n"
            "The admin dashboard could not be loaded right now.",
            reply_markup=admin_menu(),
        )


def _welcome_keyboard(button1: str, button2: str) -> InlineKeyboardMarkup:
    """Clean, focused keyboard shown immediately after /start.

    Keep the welcome screen limited to the two primary shopping actions.
    Secondary features remain available from the regular bot menu, so the
    first impression does not look like an admin dashboard.
    """
    b1 = button1.strip() or "🛍️ Browse Products"
    b2 = button2.strip() or "📱 Open Shop"
    rows = [
        [InlineKeyboardButton(text=b1[:64], callback_data="products_here")],
        [InlineKeyboardButton(
            text=b2[:64],
            web_app=WebAppInfo(url=config.WEBAPP_URL) if config.WEBAPP_URL else None,
            callback_data=None if config.WEBAPP_URL else "shop_unavailable",
        )],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

@dp.callback_query(F.data == "shop_unavailable")
async def shop_unavailable_handler(callback: CallbackQuery):
    await callback.answer("Shop link is not configured yet.", show_alert=True)

async def health_handler(request: web.Request):
    """Report a healthy service only when the application can reach PostgreSQL."""
    try:
        db = await fetch_one("SELECT 1 AS db_ok")
        if not db or int(db.get("db_ok") or 0) != 1:
            raise RuntimeError("Database health query returned an invalid result.")
        return web.json_response(
            {
                "ok": True,
                "service": config.SHOP_NAME,
                "database": "ok",
            },
            status=200,
        )
    except Exception as exc:
        log.exception("Health check failed")
        return web.json_response(
            {
                "ok": False,
                "service": config.SHOP_NAME,
                "database": "unavailable",
                "error": str(exc)[:300],
            },
            status=503,
        )


async def process_telegram_update(update: Update):
    """Process a Telegram update in the background.

    The webhook endpoint must answer Telegram immediately. Waiting for
    database queries or other handler work here makes every button feel
    slow and can also cause Telegram to retry the same update.
    """
    try:
        if not await admin_store_gate(update):
            return
        await dp.feed_update(bot, update)
    except Exception:
        log.exception("Telegram update processing failed")


async def telegram_webhook(request: web.Request):
    try:
        import hashlib
        expected_secret = config.TELEGRAM_WEBHOOK_SECRET or hashlib.sha256(
            config.BOT_TOKEN.encode("utf-8")
        ).hexdigest()[:64]
        provided_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not expected_secret or not secrets.compare_digest(provided_secret, expected_secret):
            return web.json_response({"ok": False, "error": "Unauthorized webhook request."}, status=401)

        data = await request.json()
        update = Update.model_validate(
            data,
            context={"bot": bot},
        )

        # Pre-checkout queries must be answered within Telegram's short
        # confirmation window, so process those synchronously. Other updates
        # remain backgrounded so normal button clicks stay fast.
        if update.pre_checkout_query is not None:
            await process_telegram_update(update)
        else:
            asyncio.create_task(process_telegram_update(update))

        return web.json_response({"ok": True})

    except Exception:
        log.exception("Webhook request parsing failed")
        return web.json_response(
            {
                "ok": False,
                "error": "Webhook request parsing failed",
            },
            status=500,
        )

async def ensure_giveaway_schema():
    """Run idempotent PostgreSQL migrations required by giveaway delivery.

    IMPORTANT: ALTER TABLE does not support RETURNING. Older versions of this
    migration incorrectly sent ALTER TABLE through fetch_one(... RETURNING ...),
    so the migration failed before the new prize columns could be created.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "ALTER TABLE giveaways ADD COLUMN IF NOT EXISTS prize TEXT NOT NULL DEFAULT ''"
        )
        await conn.execute(
            "ALTER TABLE giveaways ADD COLUMN IF NOT EXISTS product_id BIGINT"
        )
        await conn.execute(
            "ALTER TABLE giveaways ADD COLUMN IF NOT EXISTS winner_count INTEGER NOT NULL DEFAULT 1"
        )
        await conn.execute(
            "ALTER TABLE giveaway_winners ADD COLUMN IF NOT EXISTS prize TEXT NOT NULL DEFAULT ''"
        )
        await conn.execute(
            "ALTER TABLE giveaway_winners ADD COLUMN IF NOT EXISTS delivery_status TEXT NOT NULL DEFAULT 'pending'"
        )
        await conn.execute(
            "ALTER TABLE giveaway_winners ADD COLUMN IF NOT EXISTS delivery_error TEXT NOT NULL DEFAULT ''"
        )
        await conn.execute(
            "ALTER TABLE giveaway_winners ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMP"
        )
        await conn.execute(
            "ALTER TABLE inventory_items ADD COLUMN IF NOT EXISTS sold_order_id TEXT"
        )

        # Preserve winner-count values from older schemas when present.
        columns = await conn.fetch(
            """SELECT column_name FROM information_schema.columns
               WHERE table_schema=current_schema() AND table_name='giveaways'"""
        )
        names = {str(r['column_name']) for r in columns}
        if 'winners_count' in names:
            await conn.execute(
                """UPDATE giveaways SET winner_count=winners_count
                   WHERE (winner_count IS NULL OR winner_count=1) AND winners_count IS NOT NULL"""
            )


async def _run_maintenance_cycle():
    """Run bounded maintenance work without blocking Telegram update handling."""
    batch = int(config.MAINTENANCE_BATCH_SIZE)

    expired_orders = await expire_stale_pending_orders(
        config.PENDING_ORDER_TTL_MINUTES,
        batch,
    )
    for row in expired_orders:
        order_id = int(row["order_id"])
        telegram_id = int(row["telegram_id"])
        message = (
            f"⏱️ <b>Order #{order_id} expired</b>\n\n"
            "The unpaid order was automatically cancelled because the payment window expired. "
            "You can create a new order from CPM SHOP."
        )
        try:
            await create_notification(
                telegram_id,
                f"Order #{order_id} was automatically cancelled because it was not paid in time.",
                title="Order Expired",
                kind="order",
                reference_id=order_id,
            )
        except Exception:
            log.exception("Failed to create expired-order notification | order=%s", order_id)
        try:
            await bot.send_message(telegram_id, message)
        except Exception:
            log.info("Could not send expired-order Telegram message | order=%s | user=%s", order_id, telegram_id)

    expired_reviews = await expire_stale_pending_reviews(
        config.PENDING_ORDER_TTL_MINUTES,
        batch,
    )
    if expired_reviews:
        log.info("Cancelled %s stale paid-review payment(s).", len(expired_reviews))

    # Notify stock-alert subscribers. Subscribers are removed only after the
    # Telegram message succeeds, so temporary Telegram failures remain retryable.
    products = await fetch_all(
        """SELECT DISTINCT p.id AS product_id
           FROM products p
           JOIN stock_alerts s ON s.product_id=p.id
           WHERE p.active=TRUE AND p.stock>0
           ORDER BY p.id ASC
           LIMIT ?""",
        (batch,),
    )
    for product in products:
        waiters = await notify_stock_waiters(int(product["product_id"]), batch)
        for waiter in waiters:
            alert_id = int(waiter["alert_id"])
            telegram_id = int(waiter["telegram_id"])
            product_id = int(waiter["product_id"])
            product_name = html.escape(str(waiter.get("name") or "Product"), quote=False)
            text_message = (
                "🔔 <b>Back in stock</b>\n\n"
                f"<b>{product_name}</b> is available again.\n\n"
                "Open CPM SHOP to order it while stock is available."
            )
            try:
                await bot.send_message(telegram_id, text_message)
            except Exception:
                log.exception(
                    "Stock alert Telegram delivery failed | alert=%s | product=%s | user=%s",
                    alert_id, product_id, telegram_id,
                )
                continue
            try:
                await create_notification(
                    telegram_id,
                    f"{waiter.get('name') or 'Product'} is back in stock.",
                    title="Back in Stock",
                    kind="stock",
                    reference_id=product_id,
                )
            except Exception:
                log.exception("Failed to create stock notification | product=%s | user=%s", product_id, telegram_id)
            try:
                await remove_stock_alert_by_id(alert_id)
            except Exception:
                log.exception("Failed to remove delivered stock alert | alert=%s", alert_id)
            if config.STOCK_ALERT_SEND_DELAY_SECONDS > 0:
                await asyncio.sleep(config.STOCK_ALERT_SEND_DELAY_SECONDS)


async def background_maintenance_loop():
    while True:
        try:
            await _run_maintenance_cycle()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Background maintenance cycle failed")
        try:
            await asyncio.sleep(config.MAINTENANCE_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise


async def background_startup(app: web.Application):
    try:
        await init_db()
        await ensure_giveaway_schema()
        log.info("Database initialized successfully.")
        if app.get("maintenance_task") is None:
            app["maintenance_task"] = asyncio.create_task(background_maintenance_loop())
            log.info("Background maintenance loop started.")
    except Exception:
        log.exception("Database initialization failed.")

    try:
        from aiogram.types import BotCommandScopeDefault, BotCommandScopeChat

        customer_commands = [
            BotCommand(command="start", description="Start CPM SHOP"),
            BotCommand(command="help", description="Help & store guide"),
            BotCommand(command="contactadmin", description="Contact support"),
            BotCommand(command="feedback", description="View customer feedback"),
            BotCommand(command="addfeedback", description="Send your feedback"),
            BotCommand(command="cart", description="View and manage your cart"),
            BotCommand(command="referral", description="Referral program"),
            BotCommand(command="orders", description="View your orders"),
            BotCommand(command="notifications", description="View notifications"),
            BotCommand(command="giveaways", description="View and join giveaways"),
            BotCommand(command="promoshop", description="Buy promo codes"),
            BotCommand(command="rewards", description="Manage loyalty rewards"),
            BotCommand(command="paysupport", description="Payment support"),
        ]
        admin_commands = customer_commands + [
            BotCommand(command="admin", description="Open Telegram Admin Panel"),
            BotCommand(command="promos", description="Manage promo codes in Telegram"),
        ]
        await bot.set_my_commands(customer_commands, scope=BotCommandScopeDefault())
        await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=int(config.ADMIN_ID)))

        log.info("Bot commands configured.")

    except Exception:
        log.exception(
            "Failed to configure bot commands."
        )

    if not config.WEBAPP_URL:
        log.error(
            "WEBAPP_URL is empty. Telegram webhook cannot be configured."
        )
        return

    try:
        webhook_url = f"{config.WEBAPP_URL.rstrip('/')}/telegram/webhook"

        allowed_updates = [
            "message",
            "callback_query",
            "pre_checkout_query",
        ]

        import hashlib
        webhook_secret = config.TELEGRAM_WEBHOOK_SECRET or hashlib.sha256(
            config.BOT_TOKEN.encode("utf-8")
        ).hexdigest()[:64]

        await bot.set_webhook(
            url=webhook_url,
            allowed_updates=allowed_updates,
            drop_pending_updates=False,
            secret_token=webhook_secret,
        )

        info = await bot.get_webhook_info()

        log.info(
            "Telegram webhook configured successfully: %s",
            webhook_url,
        )
        log.info(
            "Telegram webhook info: url=%s pending=%s last_error=%s",
            info.url,
            info.pending_update_count,
            info.last_error_message,
        )

    except Exception:
        log.exception(
            "Failed to configure Telegram webhook."
        )


async def on_startup(app: web.Application):
    app["startup_task"] = asyncio.create_task(
        background_startup(app)
    )

    log.info(
        "Web server startup completed. "
        "Background initialization started."
    )


async def on_cleanup(app: web.Application):
    maintenance = app.get("maintenance_task")
    if maintenance and not maintenance.done():
        maintenance.cancel()
        try:
            await maintenance
        except asyncio.CancelledError:
            pass

    task = app.get("startup_task")
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    await close_db()
    await bot.session.close()

async def admin_root_handler(request: web.Request):
    """Open the admin dashboard when a valid session already exists.

    Returning the login page for every visit to /admin caused navigation buttons
    on secondary admin pages to look like the session had expired. Keep /admin
    as the stable admin entry point and route authenticated users directly to
    the dashboard.
    """
    if is_valid_admin_session(request):
        raise web.HTTPFound("/admin/panel")

    return web.FileResponse(
        Path(__file__).parent / "webapp" / "admin" / "login.html"
    )


async def admin_static_page_from_dir(request: web.Request, directory: str, filename: str):
    if not is_valid_admin_session(request):
        raise web.HTTPFound("/admin")
    return web.FileResponse(Path(__file__).parent / "webapp" / directory / filename)


@web.middleware
async def customer_web_store_access_middleware(request: web.Request, handler):
    """Block authenticated Mini App customer API traffic when offline or blocked."""
    path = request.path
    if not path.startswith("/api/") or path.startswith("/api/admin/"):
        return await handler(request)
    init_data = str(request.headers.get("X-Telegram-Init-Data", "") or request.query.get("init_data", "")).strip()
    if not init_data:
        return await handler(request)
    try:
        user = validate_telegram_init_data(init_data)
        row = await fetch_one("SELECT is_blocked FROM users WHERE telegram_id=? LIMIT 1", (user["telegram_id"],))
        if row and bool(row.get("is_blocked")):
            return web.json_response({"ok": False, "blocked": True, "error": "🚫 Your account is blocked. You cannot use CPM SHOP."}, status=403)
        if not await store_enabled():
            return web.json_response({"ok": False, "offline": True, "error": "🛠️ Store is currently offline. Please try again later."}, status=503)
    except ValueError:
        return await handler(request)
    except Exception:
        log.exception("Customer Mini App store access check failed")
        return web.json_response({"ok": False, "offline": True, "error": "⚠️ Store is temporarily unavailable. Please try again later."}, status=503)
    return await handler(request)


def create_app():
    app = web.Application(
        client_max_size=50 * 1024 * 1024,
        middlewares=[customer_web_store_access_middleware, admin_security_middleware],
    )

    setup_store_routes(app)

    app.router.add_get(
        "/health",
        health_handler,
        allow_head=False,
    )

    app.router.add_post(
        "/telegram/webhook",
        telegram_webhook,
    )

    app.router.add_get(
        "/",
        lambda request: web.FileResponse(
            Path(__file__).parent / "webapp" / "index.html"
        ),
    )

    app.router.add_get("/admin", admin_root_handler)

    async def admin_panel_page(request: web.Request):
        if not is_valid_admin_session(request):
            raise web.HTTPFound("/admin")
        return web.FileResponse(
            Path(__file__).parent / "webapp" / "admin" / "index.html"
        )

    app.router.add_get("/admin/panel", admin_panel_page)

    async def admin_static_page(request: web.Request, filename: str):
        if not is_valid_admin_session(request):
            raise web.HTTPFound("/admin")
        return web.FileResponse(
            Path(__file__).parent / "webapp" / "admin" / filename
        )

    app.router.add_get(
        "/admin/content",
        lambda request: admin_static_page(request, "content.html"),
    )

    app.router.add_get(
        "/admin/notes",
        lambda request: admin_static_page(request, "notes.html"),
    )

    app.router.add_get(
        "/admin/tickets.html",
        lambda request: admin_static_page(request, "tickets.html"),
    )
    app.router.add_get(
        "/admin/tickets",
        lambda request: admin_static_page(request, "tickets.html"),
    )

    app.router.add_get(
        "/admin/feedback.html",
        lambda request: admin_static_page(request, "feedback.html"),
    )
    app.router.add_get(
        "/admin/feedback",
        lambda request: admin_static_page(request, "feedback.html"),
    )

    app.router.add_get(
        "/admin/promos",
        lambda request: admin_static_page_from_dir(request, "admin_promos", "index.html"),
    )

    app.router.add_get(
        "/admin/broadcasts",
        lambda request: admin_static_page(request, "broadcasts.html"),
    )
    app.router.add_static(
        "/static/",
        Path(__file__).parent / "webapp",
    )

    app.router.add_get(
        "/product/{product_id}",
        lambda request: web.FileResponse(
            Path(__file__).parent / "webapp" / "product.html"
        ),
    )

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    return app


if __name__ == "__main__":
    config.validate_config()
    application = create_app()
    web.run_app(
        application,
        host="0.0.0.0",
        port=config.PORT,
    )
