import discord
import anthropic

# ボットトークン設定
from dotenv import load_dotenv
import os
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# Discordクライアント作成
client = discord.Client(intents=discord.Intents.all())

# Anthropicクライアント作成
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

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

    # Claude APIで返答を生成
    response = anthropic_client.messages.create(
        model="claude-opus-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": message.content}]
    )
    reply = response.content[0].text

    await message.channel.send(reply)

# ボット起動
client.run(TOKEN)
