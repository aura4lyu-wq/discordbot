import discord
from google import genai

# ボットトークン設定
from dotenv import load_dotenv
import os
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Discordクライアント作成
client = discord.Client(intents=discord.Intents.all())

# Geminiクライアント作成
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

# ボットが起動したときに実行するイベントハンドラ
@client.event
async def on_ready():
    print(f'We have logged in as {client.user}')

# メッセージを受け取った時に起動するイベントハンドラ
@client.event
async def on_message(message):

    # メッセージ送信者がボットでないことを検証
    if message.author == client.user:
        return

    # Gemini APIで返答を生成
    response = gemini_client.models.generate_content(
        model="gemini-2.0-flash",
        contents=message.content
    )
    reply = response.text

    await message.channel.send(reply)

# ボット起動
client.run(TOKEN)
