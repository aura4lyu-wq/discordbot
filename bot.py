import discord

# ボットトークン設定
from dotenv import load_dotenv
import os
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

# Discordクライアント作成
client = discord.Client(intents=discord.Intents.all())

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

    # ボットの返答
    if message.content == 'hello':
        await message.channel.send('hay')
    elif message.content == 'goodnight':
        await message.channel.send('bye')
        
# ボット起動
client.run(TOKEN)