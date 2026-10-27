import os
import asyncio
import logging
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API = os.getenv("GEMINI_API_KEY")

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

@dp.message(CommandStart())
async def handle_start(message: types.Message):
    await message.answer(f"Привет, {message.from_user.first_name}! Я твой первый бот на aiogram 3.")

@dp.message()
async def handle_echo(message: types.Message):
    if message.text:
        await message.reply(f"Ты написал: {message.text}")

async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())