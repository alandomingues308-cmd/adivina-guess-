import os
import json
import random
import re
import io
import asyncio
import threading
from flask import Flask
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from PIL import Image

# 1. Servidor Web Flask para Render (Keep-Alive)
app = Flask('')

@app.route('/')
def home():
    return "Bot status: Alive & Ready!"

def run_flask():
    # Render asigna automáticamente una variable de entorno PORT (por defecto 8080)
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = threading.Thread(target=run_flask)
    t.daemon = True
    t.start()

# 2. Cargar variables de entorno (.env)
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OSU_CLIENT_ID = os.getenv("OSU_CLIENT_ID")
OSU_CLIENT_SECRET = os.getenv("OSU_CLIENT_SECRET")

OSU_TOKEN = None

# Sesión HTTP global reutilizable para no saturar RAM
http_session: aiohttp.ClientSession = None

# Manejo independiente de rachas
racha_actual = {
    "osu": {"user_id": None, "streak": 0},
    "phigros": {"user_id": None, "streak": 0}
}

# 3. Configuración del Bot
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


# 4. Lectura y Guardado de Datos
def cargar_canciones(juego: str):
    archivo = "phigros_songs.json" if juego == "phigros" else "beatmaps.json"
    try:
        with open(archivo, "r", encoding="utf-8") as f:
            datos = json.load(f)
            
            # Normalizar lista plana []
            if isinstance(datos, list):
                clasificado = {"facil": [], "normal": [], "dificil": []}
                for item in datos:
                    dif = str(item.get("dificultad", "normal")).lower().strip()
                    if dif in clasificado:
                        clasificado[dif].append(item)
                    else:
                        clasificado["normal"].append(item)
                return clasificado
            
            # Normalizar diccionario {}
            if isinstance(datos, dict):
                for key in ["facil", "normal", "dificil"]:
                    if key not in datos:
                        datos[key] = []
                return datos

            return {"facil": [], "normal": [], "dificil": []}
    except Exception as e:
        print(f"❌ Error al cargar {archivo}: {e}")
        return {"facil": [], "normal": [], "dificil": []}

def cargar_puntos(juego: str):
    archivo = "puntos_phigros.json" if juego == "phigros" else "puntos.json"
    if not os.path.exists(archivo):
        return {}
    try:
        with open(archivo, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def guardar_puntos(juego: str, puntos_db):
    archivo = "puntos_phigros.json" if juego == "phigros" else "puntos.json"
    with open(archivo, "w", encoding="utf-8") as f:
        json.dump(puntos_db, f, indent=4)

def sumar_puntos(juego: str, user_id: int, user_name: str, puntos: int) -> int:
    puntos_db = cargar_puntos(juego)
    uid_str = str(user_id)
    
    if uid_str not in puntos_db:
        puntos_db[uid_str] = {"nombre": user_name, "puntos": 0}
    elif isinstance(puntos_db[uid_str], int):
        puntos_db[uid_str] = {"nombre": user_name, "puntos": puntos_db[uid_str]}

    puntos_db[uid_str]["nombre"] = user_name
    puntos_db[uid_str]["puntos"] = puntos_db[uid_str].get("puntos", 0) + puntos
    
    guardar_puntos(juego, puntos_db)
    return puntos_db[uid_str]["puntos"]

def cargar_stats(juego: str):
    archivo = "stats_phigros.json" if juego == "phigros" else "stats.json"
    if not os.path.exists(archivo):
        return {}
    try:
        with open(archivo, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def guardar_stats(juego: str, stats_db):
    archivo = "stats_phigros.json" if juego == "phigros" else "stats.json"
    with open(archivo, "w", encoding="utf-8") as f:
        json.dump(stats_db, f, indent=4)

def registrar_victoria_stats(juego: str, user_id: int, dificultad_str: str, racha_conseguida: int):
    stats_db = cargar_stats(juego)
    uid_str = str(user_id)

    if uid_str not in stats_db:
        stats_db[uid_str] = {"facil": 0, "normal": 0, "dificil": 0, "max_racha": 0}

    dif = dificultad_str.lower().strip()
    if dif in ["facil", "easy"]:
        stats_db[uid_str]["facil"] += 1
    elif dif in ["normal", "medium"]:
        stats_db[uid_str]["normal"] += 1
    elif dif in ["dificil", "hard", "insane", "expert"]:
        stats_db[uid_str]["dificil"] += 1

    if racha_conseguida > stats_db[uid_str].get("max_racha", 0):
        stats_db[uid_str]["max_racha"] = racha_conseguida

    guardar_stats(juego, stats_db)

def registrar_racha(juego: str, user_id: int) -> int:
    global racha_actual
    state = racha_actual[juego]
    if state["user_id"] == user_id:
        state["streak"] += 1
    else:
        state["user_id"] = user_id
        state["streak"] = 1
    return state["streak"]

def reiniciar_racha(juego: str):
    global racha_actual
    racha_actual[juego]["user_id"] = None
    racha_actual[juego]["streak"] = 0

def extraer_beatmapset_id(url: str) -> str:
    match = re.search(r"beatmapsets/(\d+)", url)
    return match.group(1) if match else None

async def obtener_token_osu():
    global OSU_TOKEN, http_session
    if not OSU_CLIENT_ID or not OSU_CLIENT_SECRET or not http_session:
        return
    
    url = "https://osu.ppy.sh/oauth/token"
    payload = {
        "client_id": OSU_CLIENT_ID,
        "client_secret": OSU_CLIENT_SECRET,
        "grant_type": "client_credentials",
        "scope": "public"
    }
    headers = {"Content-Type": "application/json", "Accept": "application/json"}

    try:
        async with http_session.post(url, json=payload, headers=headers) as resp:
            if resp.status == 200:
                data = await resp.json()
                OSU_TOKEN = data.get("access_token")
    except Exception as e:
        print(f"❌ Error al obtener token de osu!: {e}")

async def obtener_info_beatmapset(set_id: str):
    global OSU_TOKEN, http_session
    if not OSU_TOKEN:
        await obtener_token_osu()
    if not OSU_TOKEN or not http_session:
        return None

    url = f"https://osu.ppy.sh/api/v2/beatmapsets/{set_id}"
    headers = {
        "Authorization": f"Bearer {OSU_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    try:
        async with http_session.get(url, headers=headers) as resp:
            if resp.status == 200:
                return await resp.json()
            return None
    except Exception:
        return None

# Optimizado para liberar RAM de inmediato con el bloque 'with'
def recortar_imagen(imagen_bytes):
    with Image.open(io.BytesIO(imagen_bytes)).convert("RGB") as img:
        ancho, alto = img.size

        ancho_crop = int(ancho * random.uniform(0.3, 0.4))
        alto_crop = int(alto * random.uniform(0.3, 0.4))

        x_max = max(1, ancho - ancho_crop)
        y_max = max(1, alto - alto_crop)
        left = random.randint(0, x_max)
        top = random.randint(0, y_max)

        with img.crop((left, top, left + ancho_crop, top + alto_crop)) as img_recortada:
            buffer = io.BytesIO()
            # quality=85 y optimize=True reducen drásticamente el uso de memoria
            img_recortada.save(buffer, format="JPEG", quality=85, optimize=True)
    
    buffer.seek(0)
    return buffer


# 5. Componentes de Interfaz UI
class DificultadSelect(discord.ui.Select):
    def __init__(self, juego: str):
        self.juego = juego
        options = [
            discord.SelectOption(label="🟢 Fácil (1 pt)", value="facil", description="Jugar en dificultad Fácil"),
            discord.SelectOption(label="🟡 Normal (3 pts)", value="normal", description="Jugar en dificultad Normal"),
            discord.SelectOption(label="🔴 Difícil (5 pts)", value="dificil", description="Jugar en dificultad Difícil"),
            discord.SelectOption(label="🎲 Aleatorio", value="aleatorio", description="Cualquier dificultad")
        ]
        super().__init__(placeholder="🔁 Selecciona dificultad para volver a jugar...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        self.disabled = True
        await interaction.response.edit_message(view=self.view)
        
        valor_elegido = self.values[0]
        dificultad_choice = None
        
        if valor_elegido != "aleatorio":
            nombres = {"facil": "🟢 Fácil", "normal": "🟡 Normal", "dificil": "🔴 Difícil"}
            dificultad_choice = app_commands.Choice(name=nombres[valor_elegido], value=valor_elegido)
            
        await ejecutar_juego(interaction, self.juego, dificultad_choice)

class PlayAgainView(discord.ui.View):
    def __init__(self, juego: str):
        super().__init__(timeout=60)
        self.add_item(DificultadSelect(juego))


# 6. Lógica de Juego Unificada
async def ejecutar_juego(interaction: discord.Interaction, modo_juego: str, dificultad: app_commands.Choice[str] = None):
    global http_session
    try:
        await interaction.response.defer(thinking=True)
    except Exception:
        pass

    db_canciones = cargar_canciones(modo_juego)

    if dificultad:
        mapas_filtrados = db_canciones.get(dificultad.value, [])
    else:
        mapas_filtrados = db_canciones.get("facil", []) + db_canciones.get("normal", []) + db_canciones.get("dificil", [])

    if not mapas_filtrados:
        await interaction.followup.send(f"⚠️ No hay canciones registradas para **{modo_juego.upper()}**.")
        return

    mapa = random.choice(mapas_filtrados)
    diff_str = dificultad.value if dificultad else mapa.get("dificultad", "normal")

    imagen_bytes = None

    if modo_juego == "osu":
        set_id = extraer_beatmapset_id(mapa.get("url", ""))
        urls_imagen = [f"https://assets.ppy.sh/beatmaps/{set_id}/covers/raw.jpg", f"https://assets.ppy.sh/beatmaps/{set_id}/covers/cover.jpg"] if set_id else []
    else:
        set_id = None
        url_phigros = mapa.get("imagen_url") or mapa.get("url_imagen") or mapa.get("imagen")
        urls_imagen = [url_phigros] if url_phigros else []

    # Reutiliza la sesión HTTP global
    for url in urls_imagen:
        if not url:
            continue
        try:
            async with http_session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    imagen_bytes = await resp.read()
                    break
        except Exception:
            continue

    if not imagen_bytes:
        await interaction.followup.send("❌ Error al descargar la imagen de la canción.")
        return

    buffer_recorte = recortar_imagen(imagen_bytes)
    archivo_discord = discord.File(fp=buffer_recorte, filename="pista.jpg")

    puntos_mapa = {"facil": 1, "normal": 3, "dificil": 5}.get(diff_str, 3)
    titulo_embed = "🎮 ¡Adivina el mapa de osu!" if modo_juego == "osu" else "🎵 ¡Adivina la canción de Phigros!"
    color_embed = discord.Color.from_rgb(255, 102, 170) if modo_juego == "osu" else discord.Color.purple()

    embed = discord.Embed(
    title=titulo_embed,
    description=(
        f"**Dificultad:** `{diff_str.capitalize()}` (+{puntos_mapa} pts)\n"
        f"Escribe en el chat el nombre de la canción. Tienes **30 segundos**."
    ),
    color=color_embed
)

    embed.set_image(url="attachment://pista.jpg")

    await interaction.followup.send(embed=embed, file=archivo_discord)

    respuestas_validas = [r.lower().strip() for r in mapa.get("respuestas", [])]

    def check(msg: discord.Message):
        if msg.channel.id != interaction.channel_id or msg.author.bot:
            return False
        return msg.content.strip().lower() in respuestas_validas

    if modo_juego == "osu" and set_id:
        info_mapa = await obtener_info_beatmapset(set_id)
        nombre_oficial = f"{info_mapa.get('artist', '')} - {info_mapa.get('title', '')}" if info_mapa else mapa.get("nombre", mapa["respuestas"][0].title())
        link_str = f"🔗 **[Link al Beatmapset]({mapa['url']})**"
    else:
        nombre_oficial = mapa.get("nombre", mapa["respuestas"][0].title())
        link_str = ""

    buffer_completo = io.BytesIO(imagen_bytes)
    archivo_completo = discord.File(fp=buffer_completo, filename="completo.jpg")

    try:
        msg_ganador = await bot.wait_for("message", check=check, timeout=30.0)

        total_puntos = sumar_puntos(modo_juego, msg_ganador.author.id, msg_ganador.author.display_name, puntos_mapa)
        racha = registrar_racha(modo_juego, msg_ganador.author.id)
        registrar_victoria_stats(modo_juego, msg_ganador.author.id, diff_str, racha)

        tag_racha = f" 🔥 **(Racha: {racha})**" if racha >= 2 else ""

        desc_vic = f"👤 **Ganador:** {msg_ganador.author.mention}{tag_racha}\n💬 **Respuesta enviada:** `{msg_ganador.content}`\n⭐ **Puntos ganados:** `+{puntos_mapa}` (Total: **{total_puntos} pts**)\n\n🎵 **Canción:** {nombre_oficial}{link_str}"
        embed_victoria = discord.Embed(
            title="🎉 ¡Correcto!",
            description=desc_vic,
            color=discord.Color.green()
        )
        embed_victoria.set_image(url="attachment://completo.jpg")

        await interaction.channel.send(embed=embed_victoria, file=archivo_completo, view=PlayAgainView(modo_juego))

    except asyncio.TimeoutError:
        reiniciar_racha(modo_juego)

       desc_der = f"Nadie adivinó a tiempo.\n\n🎵 **La canción era:** {nombre_oficial}{link_str}"
        embed_derrota = discord.Embed(
            title="⏰ ¡Tiempo agotado!",
            description=desc_der,
            color=discord.Color.red()
        )
        embed_derrota.set_image(url="attachment://completo.jpg")

        await interaction.channel.send(embed=embed_derrota, file=archivo_completo, view=PlayAgainView(modo_juego))


# 7. Eventos y Comandos Slash
@bot.event
async def on_ready():
    global http_session
    if http_session is None or http_session.closed:
        http_session = aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0 DiscordBot/1.0"})
    
    print(f"🤖 Bot iniciado como: {bot.user.name}")
    await obtener_token_osu()
    try:
        synced = await bot.tree.sync()
        print(f"✅ Sincronizados {len(synced)} comandos.")
    except Exception as e:
        print(f"❌ Error al sincronizar: {e}")

@bot.event
async def on_close():
    global http_session
    if http_session and not http_session.closed:
        await http_session.close()

@bot.tree.command(name="adivina", description="Inicia un juego de adivinanza")
@app_commands.describe(juego="Elige el modo de juego", dificultad="Selecciona la dificultad (opcional)")
@app_commands.choices(
    juego=[
        app_commands.Choice(name="osu!", value="osu"),
        app_commands.Choice(name="Phigros", value="phigros")
    ],
    dificultad=[
        app_commands.Choice(name="🟢 Fácil (1 pt)", value="facil"),
        app_commands.Choice(name="🟡 Normal (3 pts)", value="normal"),
        app_commands.Choice(name="🔴 Difícil (5 pts)", value="dificil")
    ]
)
async def adivina(interaction: discord.Interaction, juego: app_commands.Choice[str], dificultad: app_commands.Choice[str] = None):
    await ejecutar_juego(interaction, juego.value, dificultad)


@bot.tree.command(name="puntos", description="Muestra la clasificación por juego")
@app_commands.describe(juego="Selecciona el juego")
@app_commands.choices(
    juego=[
        app_commands.Choice(name="osu!", value="osu"),
        app_commands.Choice(name="Phigros", value="phigros")
    ]
)
async def puntos(interaction: discord.Interaction, juego: app_commands.Choice[str]):
    await interaction.response.defer()

    pts_data = cargar_puntos(juego.value)
    
    top = sorted(
        pts_data.items(), 
        key=lambda x: x[1]["puntos"] if isinstance(x[1], dict) else x[1], 
        reverse=True
    )[:10]

    lineas = []
    for i, (uid, data) in enumerate(top):
        nombre = data.get("nombre", f"<@{uid}>") if isinstance(data, dict) else f"<@{uid}>"
        pts = data.get("puntos", data) if isinstance(data, dict) else data
        lineas.append(f"`#{i+1}` `{nombre}` — **{pts} pts**")

    txt_ranking = "
".join(lineas) or "Sin registros aún."

    embed = discord.Embed(
        title=f"🏆 Tabla de Clasificación ({juego.name})",
        description=txt_ranking,
        color=discord.Color.gold()
    )

    await interaction.followup.send(embed=embed)


@bot.tree.command(name="stats", description="Muestra tus estadísticas según el juego")
@app_commands.describe(juego="Selecciona el juego a consultar", usuario="Usuario a consultar (opcional)")
@app_commands.choices(
    juego=[
        app_commands.Choice(name="osu!", value="osu"),
        app_commands.Choice(name="Phigros", value="phigros")
    ]
)
async def stats(interaction: discord.Interaction, juego: app_commands.Choice[str], usuario: discord.Member = None):
    await interaction.response.defer()

    target_user = usuario or interaction.user
    stats_db = cargar_stats(juego.value)
    uid_str = str(target_user.id)
    
    user_data = stats_db.get(uid_str, {"facil": 0, "normal": 0, "dificil": 0, "max_racha": 0})
    total = user_data.get("facil", 0) + user_data.get("normal", 0) + user_data.get("dificil", 0)

    embed = discord.Embed(
        title=f"📊 Estadísticas de {target_user.display_name} ({juego.name})",
        color=discord.Color.blue()
    )
    embed.set_thumbnail(url=target_user.display_avatar.url)
    embed.add_field(name="🟢 Fáciles", value=f"`{user_data.get('facil', 0)}`", inline=True)
    embed.add_field(name="🟡 Normales", value=f"`{user_data.get('normal', 0)}`", inline=True)
    embed.add_field(name="🔴 Difíciles", value=f"`{user_data.get('dificil', 0)}`", inline=True)
    embed.add_field(name="🔥 Racha Máxima", value=f"`{user_data.get('max_racha', 0)}` seguidas", inline=True)
    embed.add_field(name="🎮 Total Adivinadas", value=f"`{total}`", inline=True)

    await interaction.followup.send(embed=embed)


# 8. Iniciar Servidor Web Keep-Alive y Lanzar Bot
if __name__ == "__main__":
    keep_alive()
    bot.run(DISCORD_TOKEN)
