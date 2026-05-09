"""
🤖 NIGHT TEAM BOT — discord.py
Actif uniquement entre 00h00 et 08h00 (Europe/Paris)
Cycle toutes les heures pile : alarme vocale + validation ✅
"""

import asyncio
import discord
from discord.ext import tasks
from datetime import datetime
import pytz
import os
import subprocess
import logging

# ─────────────────────────────────────────────
#  CONFIGURATION — à adapter
# ─────────────────────────────────────────────
BOT_TOKEN = os.getenv("DISCORD_TOKEN")
TEXT_CHANNEL_ID  = 1502608329036267590   # ID du salon texte
VOICE_CHANNEL_ID = 1461405605465165874   # ID du salon vocal
ALARM_FILE = "danger-alarm-sound-effect-meme.mp3" 
ADMIN_ID = 1210279264155340883  # Toi
MEMBRES_EQUIPE = {
    899395182640898048,   # __merveil229__
    1351209868790464663,  # __beheton_89888__
    1426453755410386994,  # __kenny086825__
    1166059251832197180,  # 816642
}        # "danger-alarm-sound-effect-meme.mp3"

PARIS_TZ = pytz.timezone("Europe/Paris")
discord.opus.load_opus(subprocess.run(['brew', '--prefix', 'opus'], capture_output=True, text=True).stdout.strip() + '/lib/libopus.dylib')

# ─────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("night_bot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("NightBot")

# ─────────────────────────────────────────────
#  BOT
# ─────────────────────────────────────────────
intents = discord.Intents.default()
intents.members          = True
intents.voice_states     = True
intents.message_content  = True
intents.reactions        = True

bot = discord.Client(intents=intents)

# ── État global du cycle en cours ────────────
active_cycle: dict | None = None   # None = pas de cycle actif


# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────

def now_paris() -> datetime:
    return datetime.now(PARIS_TZ)


def is_active_window():
    h = now_paris().hour
    return 0 <= h < 8


def voice_members(guild):
    vc = guild.get_channel(VOICE_CHANNEL_ID)
    if vc is None:
        return []
    return [m for m in vc.members if not m.bot and m.id in MEMBRES_EQUIPE]


# ─────────────────────────────────────────────
#  TÂCHE PLANIFICATEUR (toutes les minutes)
# ─────────────────────────────────────────────

@tasks.loop(minutes=1)
async def scheduler():
    global active_cycle

    n = now_paris()
    log.info(f"Tick {n.strftime('%H:%M')} | active_cycle={active_cycle is not None}")

    # Hors plage → on ne fait rien (et on arrête un éventuel cycle orphelin)
    if not is_active_window():
        if active_cycle:
            log.info("Hors plage — nettoyage du cycle actif.")
            await cleanup_cycle(reason="Hors plage horaire.")
        return

    # Déclenchement uniquement à chaque heure pile
    if n.minute != 0:
        return


    # Déjà un cycle actif ? on attend qu'il se termine.
    if active_cycle:
        log.info("Cycle déjà en cours — on passe.")
        return

    guild = discord.utils.get(bot.guilds)
    if guild is None:
        log.warning("Aucun serveur trouvé.")
        return

    members = voice_members(guild)
    if not members:
        log.info("Personne dans le vocal — pas de cycle.")
        return

    await start_cycle(guild, members, n)


@scheduler.before_loop
async def before_scheduler():
    await bot.wait_until_ready()
    log.info("Planificateur prêt.")


# ─────────────────────────────────────────────
#  CYCLE PRINCIPAL
# ─────────────────────────────────────────────

async def start_cycle(guild: discord.Guild, members: list[discord.Member], t: datetime):
    global active_cycle

    text_channel  = guild.get_channel(TEXT_CHANNEL_ID)
    voice_channel = guild.get_channel(VOICE_CHANNEL_ID)

    if text_channel is None or voice_channel is None:
        log.error("Salon texte ou vocal introuvable.")
        return

    # Déplacer de force les membres dans le bon vocal
    for uid in MEMBRES_EQUIPE:
        m = guild.get_member(uid)
        if m is None:
            continue
        if m.voice is not None and m.voice.channel and m.voice.channel.id != VOICE_CHANNEL_ID:
            try:
                await m.move_to(voice_channel)
                await asyncio.sleep(0.5)
            except Exception:
                pass

    mentions = " ".join(m.mention for m in members)
    heure    = t.strftime("%Hh%M")

    # 1️⃣ Message de check
    check_msg = await text_channel.send(
        f"⏰ **CHECK ÉQUIPE DE NUIT — {heure}**\n"
        f"{mentions}\n"
        f"Réagissez avec ✅ pour confirmer votre présence !"
    )
    await check_msg.add_reaction("✅")

    # 2️⃣ Connexion vocale + alarme
    vc_client = await voice_channel.connect()
    log.info(f"Connecté au vocal : {voice_channel.name}")

    active_cycle = {
        "guild"        : guild,
        "vc_client"    : vc_client,
        "text_channel" : text_channel,
        "check_msg"    : check_msg,
        "members"      : {m.id: m for m in members},
        "validated"    : set(),
        "ping_task"    : None,
    }

    # Notifier l'admin des membres absents
    absents = []
    for uid in MEMBRES_EQUIPE:
        member = guild.get_member(uid)
        if member is None or member.voice is None or member.voice.channel is None:
            absents.append(f"<@{uid}>")
    
    if absents:
        admin = await bot.fetch_user(ADMIN_ID)
        absents_str = ", ".join(absents)
        await admin.send(
            f"🔴 **Check {heure} — Membres absents du vocal :**\n{absents_str}"
        )

    # Lancement alarme en boucle
    active_cycle["alarm_task"] = asyncio.ensure_future(play_alarm_loop(vc_client))

    # Lancement des pings absents (toutes les 2 min)
    active_cycle["ping_task"]  = asyncio.ensure_future(ping_absent_loop())

    log.info(f"Cycle démarré — {len(members)} membre(s) à valider.")


async def play_alarm_loop(vc_client: discord.VoiceClient):
    """Joue le fichier audio en boucle jusqu'à l'arrêt du cycle."""
    if not os.path.isfile(ALARM_FILE):
        log.warning(f"Fichier audio '{ALARM_FILE}' introuvable — alarme silencieuse.")
        return

    while active_cycle and vc_client.is_connected():
        source = discord.FFmpegPCMAudio(ALARM_FILE)
        vc_client.play(source)
        # Attendre la fin de la lecture avant de relancer
        while vc_client.is_playing():
            await asyncio.sleep(1)
        await asyncio.sleep(0.5)   # petite pause entre les boucles


async def ping_absent_loop():
    """Ping les membres non validés toutes les 2 minutes."""
    await asyncio.sleep(120)   # première attente avant de pinger
    while active_cycle:
        absent = get_absent_members()
        if not absent:
            break
        text_ch  = active_cycle["text_channel"]
        mentions = " ".join(m.mention for m in absent)
        await text_ch.send(f"🚨 **EN ATTENTE :** {mentions}")
        log.info(f"Ping absents : {[m.display_name for m in absent]}")
        await asyncio.sleep(120)


def get_absent_members() -> list[discord.Member]:
    if not active_cycle:
        return []
    return [
        m for uid, m in active_cycle["members"].items()
        if uid not in active_cycle["validated"]
    ]


async def cleanup_cycle(reason: str = ""):
    global active_cycle
    if active_cycle is None:
        return

    cycle = active_cycle
    active_cycle = None   # on coupe d'abord pour stopper les boucles

    # Annuler les tâches
    for key in ("alarm_task", "ping_task"):
        task = cycle.get(key)
        if task and not task.done():
            task.cancel()

    # Déconnecter du vocal
    vc_client: discord.VoiceClient = cycle["vc_client"]
    if vc_client.is_playing():
        vc_client.stop()
    if vc_client.is_connected():
        await vc_client.disconnect()
        log.info("Déconnexion du vocal.")

    if reason:
        log.info(f"Cycle terminé : {reason}")


async def check_all_validated():
    """Vérifie si tout le monde a validé et clôture le cycle si oui."""
    if active_cycle is None:
        return

    absent = get_absent_members()
    if absent:
        return   # Il reste des absents

    text_ch = active_cycle["text_channel"]
    await text_ch.send("✅ **Tout le monde est réveillé, fin du check !** Bonne nuit à tous 🌙")
    log.info("Tous les membres ont validé — fin du cycle.")
    await cleanup_cycle(reason="Tous validés.")


# ─────────────────────────────────────────────
#  ÉVÉNEMENTS
# ─────────────────────────────────────────────

@bot.event
async def on_ready():
    log.info(f"Bot connecté : {bot.user} (id={bot.user.id})")
    scheduler.start()


@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot or member.id not in MEMBRES_EQUIPE:
        return

    if active_cycle is not None:
        if before.channel and before.channel.id == VOICE_CHANNEL_ID:
            if after.channel is None or after.channel.id != VOICE_CHANNEL_ID:
                if member.id in active_cycle["members"]:
                    voice_channel = active_cycle["guild"].get_channel(VOICE_CHANNEL_ID)
                    try:
                        await member.move_to(voice_channel)
                        text_ch = active_cycle["text_channel"]
                        await text_ch.send(
                            f"⛔ {member.mention} tu ne peux pas quitter le vocal pendant un check !"
                        )
                    except Exception:
                        del active_cycle["members"][member.id]
                        active_cycle["validated"].discard(member.id)
                        log.info(f"{member.display_name} a quitté le vocal — retiré du cycle.")
                        text_ch = active_cycle["text_channel"]
                        await text_ch.send(
                            f"⚠️ {member.mention} a quitté le vocal et a été retiré du check."
                        )
                        await check_all_validated()


# ─────────────────────────────────────────────
#  LANCEMENT
# ─────────────────────────────────────────────
if __name__ == "__main__":
    bot.run(BOT_TOKEN)
