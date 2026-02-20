import discord
from discord.ext import commands
from discord import app_commands
from google import genai
from dotenv import load_dotenv
import aiohttp
import asyncio
import os
import tempfile

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
VOICEVOX_URL = os.getenv("VOICEVOX_URL", "http://localhost:50021")
VOICEVOX_SPEAKER = int(os.getenv("VOICEVOX_SPEAKER", "1"))

# Botクライアント作成（スラッシュコマンド対応）
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

# Geminiクライアント作成
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

# 自動読み上げが有効なギルドIDのセット
auto_read_guilds: set[int] = set()


# ------------------------------------------------------------------ #
# VOICEVOX ヘルパー
# ------------------------------------------------------------------ #

async def voicevox_tts(text: str, speaker: int = VOICEVOX_SPEAKER) -> bytes:
    """VOICEVOX API でテキストを WAV バイト列に変換する"""
    async with aiohttp.ClientSession() as session:
        # 1. 音声クエリ生成
        async with session.post(
            f"{VOICEVOX_URL}/audio_query",
            params={"text": text, "speaker": speaker},
        ) as resp:
            resp.raise_for_status()
            query = await resp.json()

        # 2. 音声合成
        async with session.post(
            f"{VOICEVOX_URL}/synthesis",
            params={"speaker": speaker},
            json=query,
        ) as resp:
            resp.raise_for_status()
            return await resp.read()


async def play_tts(voice_client: discord.VoiceClient, text: str) -> None:
    """テキストを TTS 変換して VC で再生する（再生完了まで待機）"""
    wav_data = await voicevox_tts(text)

    # 一時ファイルに書き出して FFmpeg で再生
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(wav_data)
        tmp_path = f.name

    done = asyncio.get_event_loop().create_future()

    def after_play(error: Exception | None) -> None:
        os.unlink(tmp_path)
        if error:
            print(f"[TTS] 再生エラー: {error}")
        done.get_loop().call_soon_threadsafe(
            done.set_exception if error else done.set_result,
            error if error else None,
        )

    voice_client.play(
        discord.FFmpegPCMAudio(tmp_path, executable="/usr/bin/ffmpeg"),
        after=after_play,
    )
    await done  # 再生完了まで待つ


# ------------------------------------------------------------------ #
# Bot イベント
# ------------------------------------------------------------------ #

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"We have logged in as {bot.user}")
    print("スラッシュコマンドを同期しました")


@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return

    # Gemini 返答（テキストチャンネル）
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
        contents=message.content,
    )
    reply = response.text
    await message.channel.send(reply)

    # 自動読み上げモードが有効なら VC でも読み上げ
    if (
        message.guild
        and message.guild.id in auto_read_guilds
        and message.guild.voice_client
        and not message.guild.voice_client.is_playing()
    ):
        try:
            await play_tts(message.guild.voice_client, reply)
        except Exception as e:
            print(f"[自動読み上げ] エラー: {e}")

    await bot.process_commands(message)


# ------------------------------------------------------------------ #
# スラッシュコマンド
# ------------------------------------------------------------------ #

@bot.tree.command(name="join", description="ボットをあなたのボイスチャンネルに参加させます")
async def join(interaction: discord.Interaction):
    if interaction.user.voice is None:
        await interaction.response.send_message(
            "先にボイスチャンネルに参加してからコマンドを実行してください。",
            ephemeral=True,
        )
        return

    channel = interaction.user.voice.channel

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
                "`pip install PyNaCl` を実行して再起動してください。",
                ephemeral=True,
            )
        else:
            raise


@bot.tree.command(name="leave", description="ボットをボイスチャンネルから退出させます")
async def leave(interaction: discord.Interaction):
    if interaction.guild.voice_client is None:
        await interaction.response.send_message(
            "ボットはボイスチャンネルに参加していません。",
            ephemeral=True,
        )
        return

    channel_name = interaction.guild.voice_client.channel.name
    await interaction.guild.voice_client.disconnect()
    await interaction.response.send_message(f"**{channel_name}** から退出しました。")


@bot.tree.command(name="speak", description="テキストをボイスチャンネルで読み上げます")
@app_commands.describe(text="読み上げるテキスト")
async def speak(interaction: discord.Interaction, text: str):
    vc: discord.VoiceClient | None = interaction.guild.voice_client

    if vc is None:
        await interaction.response.send_message(
            "先に `/join` でボットをボイスチャンネルに参加させてください。",
            ephemeral=True,
        )
        return

    if vc.is_playing():
        await interaction.response.send_message(
            "現在再生中です。終わるまでお待ちください。",
            ephemeral=True,
        )
        return

    # 合成に時間がかかるので defer してから応答
    await interaction.response.defer()

    try:
        await play_tts(vc, text)
        await interaction.followup.send(f"読み上げました: 「{text}」")
    except aiohttp.ClientConnectorError:
        await interaction.followup.send(
            "VOICEVOX エンジンに接続できませんでした。\n"
            "VOICEVOX を起動してから再試行してください。"
        )
    except Exception as e:
        await interaction.followup.send(f"エラーが発生しました: {e}")


@bot.tree.command(name="autoread", description="Gemini の返答を VC で自動読み上げする機能をオン/オフします")
async def autoread(interaction: discord.Interaction):
    gid = interaction.guild.id
    if gid in auto_read_guilds:
        auto_read_guilds.discard(gid)
        await interaction.response.send_message("自動読み上げを **オフ** にしました。")
    else:
        auto_read_guilds.add(gid)
        await interaction.response.send_message("自動読み上げを **オン** にしました。")


bot.run(TOKEN)
