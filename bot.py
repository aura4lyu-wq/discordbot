import discord
from discord.ext import commands
from discord import app_commands
from google import genai
from dotenv import load_dotenv
import os

# PyNaCl が入っていない場合は起動時に即エラーを出す
try:
    import nacl  # noqa: F401
except ImportError:
    raise SystemExit(
        "[ERROR] PyNaCl が見つかりません。音声機能に必要です。\n"
        "  pip install PyNaCl\n"
        "を実行してから再起動してください。"
    )

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
        model="gemini-2.5-flash",
        contents=message.content,
        config={
            "system_instruction": (
                "あなたはDiscordボットです。"
                "あなたは私の妹であり、私のことを兄と呼びます。"
                "基本的にはクールな印象ですが、少しツンデレなところがあります。"
            )
        }
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
    try:
        if interaction.guild.voice_client is not None:
            await interaction.guild.voice_client.move_to(channel)
            await interaction.response.send_message(f"**{channel.name}** に移動しました。")
        else:
            await channel.connect()
            await interaction.response.send_message(f"**{channel.name}** に参加しました。")
    except RuntimeError as e:
        if "PyNaCl" in str(e):
            await interaction.response.send_message(
                "音声機能に必要な PyNaCl ライブラリがインストールされていません。\n"
                "ボットのサーバーで `pip install PyNaCl` を実行して再起動してください。",
                ephemeral=True
            )
        else:
            raise


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