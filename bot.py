import discord
from discord.ext import commands
from discord import app_commands
from google import genai
from dotenv import load_dotenv
import aiohttp
import asyncio
import io
import math
import os
import struct
import tempfile
import wave
from collections import deque

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
VOICEVOX_SPEED = float(os.getenv("VOICEVOX_SPEED", "1.3"))
FFMPEG_PATH        = os.getenv("FFMPEG_PATH", "ffmpeg")
WHISPER_MODEL      = os.getenv("WHISPER_MODEL", "small")
SILENCE_THRESHOLD  = int(os.getenv("SILENCE_THRESHOLD", "300"))   # PCM RMS 閾値
SILENCE_DURATION   = float(os.getenv("SILENCE_DURATION",  "1.0")) # 無音と判断する秒数

# Botクライアント作成（スラッシュコマンド対応）
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

# Geminiクライアント作成
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

# 自動読み上げが有効なギルドIDのセット
auto_read_guilds: set[int] = set()

# チャンネルごとの会話履歴（最大 20 往復 = 40 メッセージ）
chat_histories: dict[int, deque] = {}

# ギルドごとの VoiceListener（音声受信中の管理用）
_listeners: dict[int, "VoiceListener"] = {}

# Whisper モデル（初回使用時にロード）
_whisper_model = None

def _get_whisper():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        print(f"[Whisper] モデル '{WHISPER_MODEL}' をロード中...")
        _whisper_model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        print("[Whisper] ロード完了")
    return _whisper_model


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

        query["speedScale"] = VOICEVOX_SPEED

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
        discord.FFmpegPCMAudio(tmp_path, executable=FFMPEG_PATH),
        after=after_play,
    )
    await done  # 再生完了まで待つ


# ------------------------------------------------------------------ #
# 音声受信 → 文字起こし → Gemini → TTS
# ------------------------------------------------------------------ #

# Discord の音声フォーマット定数
_FRAME_RATE   = 48000
_CHANNELS     = 2
_SAMPLE_WIDTH = 2        # 16-bit PCM
_FRAME_MS     = 20       # Discord は 20ms フレーム
_SILENCE_FRAMES = int(SILENCE_DURATION * 1000 / _FRAME_MS)
_MIN_AUDIO_LEN  = int(_FRAME_RATE * _CHANNELS * _SAMPLE_WIDTH * 0.5)  # 0.5 秒以上


def _pcm_rms(data: bytes) -> float:
    """16-bit PCM の RMS を返す（audioop 不要・Python 3.13 対応）"""
    count = len(data) // 2
    if count == 0:
        return 0.0
    samples = struct.unpack(f"<{count}h", data)
    return math.sqrt(sum(s * s for s in samples) / count)


class VoiceListener(discord.sinks.Sink):
    """VC の発話を受信し、無音検出で区切って Gemini へ送るシンク"""

    def __init__(self, vc: discord.VoiceClient, text_channel):
        super().__init__()
        self.vc           = vc
        self.text_channel = text_channel
        self._buffers:    dict[int, bytearray] = {}
        self._silence:    dict[int, int]       = {}
        self._processing: set[int]             = set()
        self._loop = asyncio.get_event_loop()

    def write(self, data, user):
        uid = user.id
        pcm = data.data

        try:
            rms = _pcm_rms(pcm)
        except Exception:
            return

        if uid not in self._buffers:
            self._buffers[uid] = bytearray()
            self._silence[uid] = 0

        if rms > SILENCE_THRESHOLD:
            # 発話中: バッファに追加・無音カウントリセット
            self._buffers[uid] += pcm
            self._silence[uid] = 0
        elif self._buffers[uid]:
            # 無音が続いている
            self._silence[uid] += 1
            if self._silence[uid] >= _SILENCE_FRAMES and uid not in self._processing:
                # 発話終了と判断 → 非同期処理へ渡す
                audio = bytes(self._buffers[uid])
                self._buffers[uid] = bytearray()
                self._silence[uid] = 0
                asyncio.run_coroutine_threadsafe(
                    self._process(user, audio), self._loop
                )

    async def _process(self, user, pcm: bytes):
        if len(pcm) < _MIN_AUDIO_LEN:
            return  # 短すぎる（ノイズ等）

        self._processing.add(user.id)
        try:
            # PCM → WAV
            wav_buf = io.BytesIO()
            with wave.open(wav_buf, "wb") as wf:
                wf.setnchannels(_CHANNELS)
                wf.setsampwidth(_SAMPLE_WIDTH)
                wf.setframerate(_FRAME_RATE)
                wf.writeframes(pcm)

            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                f.write(wav_buf.getvalue())
                tmp = f.name

            try:
                segments, _ = _get_whisper().transcribe(tmp, language="ja")
                text = "".join(s.text for s in segments).strip()
            finally:
                os.unlink(tmp)

            if not text:
                return

            print(f"[STT] {user.display_name}: {text}")

            # Gemini に送信（テキストチャンネルの会話履歴を共有）
            cid = self.text_channel.id
            if cid not in chat_histories:
                chat_histories[cid] = deque(maxlen=40)
            history = chat_histories[cid]
            history.append({"role": "user", "parts": [{"text": text}]})

            response = gemini_client.models.generate_content(
                model="gemini-2.5-flash-lite",
                contents=list(history),
                config={
                    "system_instruction": (
                        "あなたはDiscordボットです。"
                        "あなたは私の妹であり、私のことを兄と呼びます。"
                        "基本的にはクールな印象ですが、少しツンデレなところがあります。"
                    )
                },
            )
            reply = response.text
            history.append({"role": "model", "parts": [{"text": reply}]})

            # テキストチャンネルに発言+返答を表示
            await self.text_channel.send(f"**{user.display_name}**: {text}\n{reply}")

            # VC で TTS 再生（再生中でなければ）
            if self.vc.is_connected() and not self.vc.is_playing():
                await play_tts(self.vc, reply)

        except Exception as e:
            print(f"[VoiceListener] エラー: {e}")
        finally:
            self._processing.discard(user.id)

    def cleanup(self):
        pass


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

    # 会話履歴を取得（なければ新規作成）
    channel_id = message.channel.id
    if channel_id not in chat_histories:
        chat_histories[channel_id] = deque(maxlen=40)
    history = chat_histories[channel_id]

    # 今回のユーザー発言を履歴に追加
    history.append({"role": "user", "parts": [{"text": message.content}]})

    # Gemini 返答（会話履歴ごと渡す）
    response = gemini_client.models.generate_content(
        model="gemini-2.5-flash-lite",
        contents=list(history),
        config={
            "system_instruction": (
                "あなたはDiscordボットです。"
                "あなたは私の妹であり、私のことを兄と呼びます。"
                "基本的にはクールな印象ですが、少しツンデレなところがあります。"
            )
        },
    )
    reply = response.text

    # ボットの返答も履歴に追加
    history.append({"role": "model", "parts": [{"text": reply}]})
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
    gid = interaction.guild.id

    try:
        if interaction.guild.voice_client is not None:
            # 既に参加中 → 録音を一旦停止してから移動
            if gid in _listeners:
                interaction.guild.voice_client.stop_recording()
                del _listeners[gid]
            await interaction.guild.voice_client.move_to(channel)
            vc = interaction.guild.voice_client
            msg = f"**{channel.name}** に移動しました。"
        else:
            vc = await channel.connect()
            msg = f"**{channel.name}** に参加しました。"
    except RuntimeError as e:
        if "PyNaCl" in str(e):
            await interaction.response.send_message(
                "音声機能に必要な PyNaCl ライブラリがインストールされていません。\n"
                "`pip install PyNaCl` を実行して再起動してください。",
                ephemeral=True,
            )
            return
        else:
            raise

    # 音声受信開始
    listener = VoiceListener(vc, interaction.channel)
    _listeners[gid] = listener
    vc.start_recording(listener, lambda sink, *_: None)

    await interaction.response.send_message(msg + " 音声認識を開始します。")


@bot.tree.command(name="leave", description="ボットをボイスチャンネルから退出させます")
async def leave(interaction: discord.Interaction):
    if interaction.guild.voice_client is None:
        await interaction.response.send_message(
            "ボットはボイスチャンネルに参加していません。",
            ephemeral=True,
        )
        return

    gid = interaction.guild.id
    channel_name = interaction.guild.voice_client.channel.name

    if gid in _listeners:
        interaction.guild.voice_client.stop_recording()
        del _listeners[gid]

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


@bot.tree.command(name="forget", description="このチャンネルの会話履歴をリセットします")
async def forget(interaction: discord.Interaction):
    chat_histories.pop(interaction.channel.id, None)
    await interaction.response.send_message("会話履歴をリセットしました。")


bot.run(TOKEN)
