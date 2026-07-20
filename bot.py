import os
import asyncio
import logging
from typing import Optional
from datetime import datetime

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

import aiohttp

# ═══════════════════════════════════════════════════════════════
# КОНФИГ
# ═══════════════════════════════════════════════════════════════

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
MARGIN = float(os.getenv("MARGIN", "4"))
FIXED_FEE = float(os.getenv("FIXED_FEE", "0"))

# Два ключа Wallet P2P
WALLET_KEY_RUB = os.getenv("WALLET_KEY_RUB", "")   # для USDT/RUB
WALLET_KEY_CNY = os.getenv("WALLET_KEY_CNY", "")   # для USDT/CNY

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан!")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# КАЛЬКУЛЯТОР
# ═══════════════════════════════════════════════════════════════


class Calculator:
    @staticmethod
    def rate(rub_usdt: float, usdt_cny: float, margin: float = 0, fee: float = 0) -> float:
        r = rub_usdt / usdt_cny
        r *= (1 + margin / 100)
        r += fee
        return round(r, 4)

    @staticmethod
    def total(amount: float, rub_usdt: float, usdt_cny: float, margin: float = 0, fee: float = 0) -> dict:
        r = Calculator.rate(rub_usdt, usdt_cny, margin, fee)
        return {"amount": amount, "rate": r, "total": round(r * amount, 2)}


# ═══════════════════════════════════════════════════════════════
# WALLET P2P API (два ключа)
# ═══════════════════════════════════════════════════════════════

URL = "https://p2p.walletbot.me/p2p/integration-api/v1/item/online"
CACHE_TTL = 120
_cache: Optional[tuple[float, float, float]] = None


async def _fetch(fiat: str, side: str, api_key: str) -> Optional[float]:
    """Получить минимальную цену с Wallet P2P."""
    if not api_key:
        logger.error(f"Ключ для {fiat} не задан")
        return None

    payload = {
        "cryptoCurrency": "USDT",
        "fiatCurrency": fiat,
        "side": side,
        "page": 1,
        "pageSize": 50,
    }
    headers = {
        "accept": "application/json",
        "X-API-Key": api_key,
        "Content-Type": "application/json",
    }

    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(URL, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status != 200:
                    logger.error(f"Wallet {fiat} HTTP {r.status}")
                    return None
                data = await r.json()
                if data.get("status") != "SUCCESS":
                    logger.error(f"Wallet {fiat} status: {data.get('status')}")
                    return None
                items = data.get("data", [])
                prices = [float(i["price"]) for i in items if i.get("price")]
                if not prices:
                    return None
                return min(prices)
    except Exception as e:
        logger.exception(f"Wallet {fiat} error: {e}")
        return None


async def get_rates() -> Optional[tuple[float, float]]:
    """Получить (USDT/RUB, USDT/CNY)."""
    global _cache

    if _cache:
        t, rub, cny = _cache
        if (asyncio.get_event_loop().time() - t) < CACHE_TTL:
            return rub, cny

    # USDT/RUB — ключ WALLET_KEY_RUB
    rub = await _fetch("RUB", "SELL", WALLET_KEY_RUB)
    if rub is None:
        logger.error("Не получен USDT/RUB")
        if _cache:
            rub = _cache[1]
        else:
            return None

    # USDT/CNY — ключ WALLET_KEY_CNY
    cny = await _fetch("CNY", "SELL", WALLET_KEY_CNY)
    if cny is None:
        logger.error("Не получен USDT/CNY")
        if _cache:
            cny = _cache[2]
        else:
            return None

    logger.info(f"Wallet: USDT/RUB={rub:.2f}, USDT/CNY={cny:.2f}")
    _cache = (asyncio.get_event_loop().time(), rub, cny)
    return rub, cny


# ═══════════════════════════════════════════════════════════════
# БОТ
# ═══════════════════════════════════════════════════════════════

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()


def menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💴 Пополнить AliPay", callback_data="topup")],
        [InlineKeyboardButton(text="📊 Курс", callback_data="rate")],
        [InlineKeyboardButton(text="❓ Поддержка", callback_data="support")],
    ])


def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back")],
    ])


def calc_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Создать заявку", callback_data="order")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back")],
    ])


class States:
    amount = State()
    uid = State()
    receipt = State()


# ═══════════════════════════════════════════════════════════════
# HANDLERS
# ═══════════════════════════════════════════════════════════════

@router.message(Command("start"))
async def start(message: Message):
    await message.answer(
        "🇨🇳 <b>AliPay Agent</b>\nДобро пожаловать!\n\nВыберите действие:",
        reply_markup=menu_kb(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "back")
async def back(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "🇨🇳 <b>AliPay Agent</b>\nДобро пожаловать!\n\nВыберите действие:",
        reply_markup=menu_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "support")
async def support(callback: CallbackQuery):
    await callback.message.edit_text(
        f"❓ <b>Поддержка</b>\n\nID админа: <code>{ADMIN_ID}</code>",
        reply_markup=back_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "rate")
async def rate(callback: CallbackQuery):
    await callback.answer("⏳ Получаю курс...")
    rates = await get_rates()
    if not rates:
        await callback.message.edit_text(
            "❌ Курс недоступен. Попробуйте позже.",
            reply_markup=back_kb(),
            parse_mode="HTML",
        )
        return

    rub, cny = rates
    cost = rub / cny
    final = Calculator.rate(rub, cny, MARGIN, FIXED_FEE)

    text = (
        f"📊 <b>Курс валют</b> — {datetime.now().strftime('%d.%m.%Y %H:%M')}\n\n"
        f"🇨🇳 CNY\n├ CNY - RUB: {cost:.2f}\n└ {cost*1000:.0f} RUB = 1000 CNY\n\n"
        f"💵 USDT\n├ USDT - CNY: {cny:.2f}\n└ {1000/cny:.0f} USDT = 1000 CNY\n\n"
        f"💳 USDT ПОКУПКА\n├ USDT: {rub:.2f}\n└ {rub*1000:.0f} RUB = 1000 USDT\n\n"
        f"<b>Итог: {final:.2f} ₽/CNY</b>"
    )
    await callback.message.edit_text(text, reply_markup=back_kb(), parse_mode="HTML")


@router.callback_query(F.data == "topup")
async def topup(callback: CallbackQuery, state: FSMContext):
    rates = await get_rates()
    if not rates:
        await callback.answer("❌ Курс недоступен!", show_alert=True)
        return

    await state.set_state(States.amount)
    await callback.message.edit_text(
        "💴 <b>Пополнить AliPay</b>\n\nВведите сумму в <b>CNY</b>\n\nНапример: <code>1500</code>",
        reply_markup=back_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(States.amount)
async def calc(message: Message, state: FSMContext):
    try:
        amount = float(message.text.strip().replace(" ", "").replace(",", "."))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите число. Например: <code>1500</code>", parse_mode="HTML")
        return

    rates = await get_rates()
    if not rates:
        await message.answer("❌ Курс недоступен. Попробуйте позже.")
        await state.clear()
        return

    rub, cny = rates
    result = Calculator.total(amount, rub, cny, MARGIN, FIXED_FEE)
    cost = rub / cny
    margin_rate = cost * (1 + MARGIN / 100)

    text = (
        f"💴 <b>Пополнение AliPay</b>\n\n"
        f"Сумма: <b>{result['amount']:.0f} CNY</b>\n"
        f"Стоимость: <b>{result['total']:,.2f} ₽</b>\n"
        f"Курс: <b>{result['rate']:.2f} ₽</b>/CNY\n\n"
        f"<i>Детали:</i>\n"
        f"USDT/RUB: {rub:.2f} ₽\n"
        f"USDT/CNY: {cny:.2f} CNY\n"
        f"Себестоимость: {cost:.2f} ₽/CNY\n"
        f"Маржа ({MARGIN}%): {margin_rate:.2f} ₽/CNY\n"
        f"\n<i>Курс действителен 5 минут.</i>"
    )
    await message.answer(text, reply_markup=calc_kb(), parse_mode="HTML")
    await state.clear()


@router.callback_query(F.data == "order")
async def order(callback: CallbackQuery, state: FSMContext):
    await state.set_state(States.uid)
    await callback.message.edit_text(
        "📝 <b>Создание заявки</b>\n\nВведите <b>UID AliPay</b> или отправьте QR-код.",
        reply_markup=back_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(States.uid)
async def uid(message: Message, state: FSMContext):
    uid = message.text.strip() if message.text else "QR"
    await state.update_data(uid=uid)
    await state.set_state(States.receipt)
    await message.answer(
        "📎 <b>Прикрепите чек оплаты</b>\n\nОтправьте фото или документ.",
        parse_mode="HTML",
    )


@router.message(States.receipt, F.photo | F.document)
async def receipt(message: Message, state: FSMContext):
    data = await state.get_data()
    uid = data.get("uid", "не указан")
    masked = uid[:4] + "****" + uid[-4:] if len(uid) > 8 else uid

    if ADMIN_ID:
        try:
            await bot.send_message(
                ADMIN_ID,
                f"🆕 <b>Новая заявка</b>\n\n"
                f"ID: #{message.from_user.id}\n"
                f"@{message.from_user.username or message.from_user.full_name}\n"
                f"UID: <code>{masked}</code>\n\nЧек получен.",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Admin notify error: {e}")

    await message.answer(
        "✅ <b>Заявка создана!</b>\n\nОжидайте подтверждения.",
        reply_markup=menu_kb(),
        parse_mode="HTML",
    )
    await state.clear()


@router.message(States.receipt)
async def wrong_receipt(message: Message):
    await message.answer("❌ Отправьте <b>фото или документ</b>.", parse_mode="HTML")


# ═══════════════════════════════════════════════════════════════
# ADMIN
# ═══════════════════════════════════════════════════════════════

@router.message(Command("status"))
async def status(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    text = "📊 <b>Статус</b>\n\n"

    # Проверка ключей
    text += "<b>Ключи:</b>\n"
    text += f"├ RUB: {'🟢' if WALLET_KEY_RUB else '🔴'} {WALLET_KEY_RUB[:8]}...\n"
    text += f"└ CNY: {'🟢' if WALLET_KEY_CNY else '🔴'} {WALLET_KEY_CNY[:8]}...\n\n"

    rates = await get_rates()
    if rates:
        rub, cny = rates
        final = Calculator.rate(rub, cny, MARGIN, FIXED_FEE)
        text += (
            f"<b>Курсы:</b>\n"
            f"├ USDT/RUB: {rub:.2f} ₽\n"
            f"├ USDT/CNY: {cny:.2f} CNY\n"
            f"├ CNY/RUB: {rub/cny:.2f} ₽\n"
            f"└ Итог: {final:.2f} ₽/CNY\n\n"
            f"Маржа: {MARGIN}%"
        )
    else:
        text += "🟡 Курсы недоступны"

    await message.answer(text, parse_mode="HTML")


# ═══════════════════════════════════════════════════════════════
# RUN
# ═══════════════════════════════════════════════════════════════

async def main():
    dp.include_router(router)
    logger.info("Бот запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())