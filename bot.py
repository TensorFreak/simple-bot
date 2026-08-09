import asyncio
import os
import re
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.filters import CommandStart
from aiogram.enums import ParseMode
from aiogram.client.session.aiohttp import AiohttpSession
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from openai import AsyncOpenAI
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = None  # заполним автоматически при первом сообщении

ALLOWED_USERS = [int(uid.strip()) for uid in os.environ["ALLOWED_USERS"].split(",")]


client = AsyncOpenAI(
    api_key=os.environ["AIROUTER_API_KEY"],
    base_url="https://api.ai-router.app/v1",
)

MODEL = "deepseek/deepseek-v4-flash"

# Если используете прокси (socks5/http), раскомментируйте:
# session = AiohttpSession(proxy=os.environ["PROXY_URL"])
# bot = Bot(token=BOT_TOKEN, session=session)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


def markdown_to_telegram_html(text: str) -> str:
    # экранируем HTML-спецсимволы, чтобы не сломать парсер
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # заголовки -> жирный текст
    text = re.sub(r'^#{1,6}\s*(.+)$', r'<b>\1</b>', text, flags=re.MULTILINE)
    # **bold** -> <b>bold</b>
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    # *italic* -> <i>italic</i>
    text = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<i>\1</i>', text)
    # `code` -> <code>code</code>
    text = re.sub(r'`(.+?)`', r'<code>\1</code>', text)
    return text


async def send_formatted(chat_id: int, text: str):
    formatted = markdown_to_telegram_html(text)
    try:
        await bot.send_message(chat_id, formatted, parse_mode=ParseMode.HTML)
    except Exception as e:
        logging.warning(f"HTML parse failed, sending plain text: {e}")
        await bot.send_message(chat_id, text)


async def ask_llm(prompt: str) -> str:
    completion = await client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1500,
    )
    return completion.choices[0].message.content


async def stream_llm_to_message(chat_id: int, prompt: str):
    # первое сообщение-заглушка, которое будем дописывать редактированием
    placeholder = await bot.send_message(chat_id, "…")
    message_id = placeholder.message_id

    stream = await client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1500,
        stream=True,
    )

    buffer = ""
    last_sent = ""
    last_edit = 0.0
    MIN_INTERVAL = 1.0  # не редактируем чаще раза в секунду (flood limit Telegram)

    async for chunk in stream:
        delta = chunk.choices[0].delta.content or ""
        if not delta:
            continue
        buffer += delta

        now = asyncio.get_event_loop().time()
        if now - last_edit >= MIN_INTERVAL and buffer != last_sent:
            try:
                await bot.edit_message_text(
                    buffer, chat_id=chat_id, message_id=message_id
                )
                last_sent = buffer
                last_edit = now
            except Exception as e:
                # 429 flood / "message is not modified" — пропускаем, попробуем позже
                logging.debug(f"skip intermediate edit: {e}")

    if not buffer:
        await bot.edit_message_text(
            "(пустой ответ)", chat_id=chat_id, message_id=message_id
        )
        return

    # финальное сообщение — уже с HTML-форматированием
    formatted = markdown_to_telegram_html(buffer)
    try:
        await bot.edit_message_text(
            formatted, chat_id=chat_id, message_id=message_id,
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logging.warning(f"HTML parse failed on final edit: {e}")
        if buffer != last_sent:
            await bot.edit_message_text(
                buffer, chat_id=chat_id, message_id=message_id
            )


@dp.message(CommandStart())
async def start_handler(message: Message):
    if message.from_user.id not in ALLOWED_USERS:
        await message.answer("Извините, этот бот приватный.")
        return
    global CHAT_ID
    CHAT_ID = message.chat.id
    await message.answer("Привет! Пиши что угодно — отвечу через LLM. Раз в час буду присылать что-то полезное сам.")


@dp.message(F.text)
async def message_handler(message: Message):
    if message.from_user.id not in ALLOWED_USERS:
        await message.answer("Извините, этот бот приватный.")
        return
    global CHAT_ID
    CHAT_ID = message.chat.id
    await bot.send_chat_action(message.chat.id, "typing")
    try:
        await stream_llm_to_message(message.chat.id, message.text)
    except Exception as e:
        await message.answer(f"Ошибка при обращении к LLM: {e}")


async def periodic_message():
    if CHAT_ID is None:
        return
    try:
        text = await ask_llm("Дай один короткий полезный совет или интересный факт на сегодня.")
        await send_formatted(CHAT_ID, text)
    except Exception as e:
        logging.error(f"Periodic message failed: {e}")


async def main():
    scheduler = AsyncIOScheduler(timezone="Europe/Paris")
    scheduler.add_job(periodic_message, "interval", hours=4)
    scheduler.start()

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
