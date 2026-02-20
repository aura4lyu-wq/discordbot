import discord
from discord.ext import commands
from discord import app_commands
from google import genai
from dotenv import load_dotenv
import os

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Botクライアント作成（スラッシュコマンド対応）
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

# Geminiクライアント作成
gemini_client = genai.Client(api_key=GEMINI_API_KEY)


@bot.event
async def on_ready():
    # スラッシュコマンドをDiscordに登録
    await bot.tree.sync()
    print(f"We have logged in as {bot.user}")
    print("スラッシュコマンドを同期しました")


# テキストメッセージ → Gemini返答（既存機能）
@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    response = gemini_client.models.generate_content(
        model="gemini-1.5-flash",
        contents=message.content
    )
    reply = response.text
    await message.channel.send(reply)

    await bot.process_commands(message)


@bot.tree.command(name="join", description="ボットをあなたのボイスチャンネルに参加させます")
async def join(interaction: discord.Interaction):
    # 呼び出したユーザーがVCに入っているか確認
    if interaction.user.voice is None:
        await interaction.response.send_message(
            "先にボイスチャンネルに参加してからコマンドを実行してください。",
            ephemeral=True
        )
        return

    channel = interaction.user.voice.channel

    # すでに別のVCにいる場合は移動、そうでなければ接続
    if interaction.guild.voice_client is not None:
        await interaction.guild.voice_client.move_to(channel)
        await interaction.response.send_message(f"**{channel.name}** に移動しました。")
    else:
        await channel.connect()
        await interaction.response.send_message(f"**{channel.name}** に参加しました。")


@bot.tree.command(name="leave", description="ボットをボイスチャンネルから退出させます")
async def leave(interaction: discord.Interaction):
    if interaction.guild.voice_client is None:
        await interaction.response.send_message(
            "ボットはボイスチャンネルに参加していません。",
            ephemeral=True
        )
        return

    channel_name = interaction.guild.voice_client.channel.name
    await interaction.guild.voice_client.disconnect()
    await interaction.response.send_message(f"**{channel_name}** から退出しました。")


bot.run(TOKEN)
