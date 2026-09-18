import os
import json
import random
import re
import io
import asyncio
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from PIL import Image
from threading import Thread
from flask import Flask

# Módulo opcional de Firebase Firestore
try:
    import firebase_admin
    from firebase_admin import credentials, firestore
    HAS_FIREBASE = True
except ImportError:
    HAS_FIREBASE = False

# ==========================================
# 1. SERVIDOR FLASK (Evita errores de puerto en Render)
# ==========================================
app = Flask("")

@app.route("/")
def home():
    return "Bot activo 24/7"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

def keep_alive():
    t = Thread(target=run_flask)
    t.daemon = True
    t.start()

keep_alive()

# ==========================================
# 2. INICIALIZACIÓN DE VARIABLES Y FIREBASE
# ==========================================
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OSU_CLIENT_ID = os.getenv("OSU_CLIENT_ID")
OSU_CLIENT_SECRET = os.getenv("OSU_CLIENT_SECRET")

db = None
firebase_json = os.environ.get("FIREBASE_CREDENTIALS")

if firebase_json and HAS_FIREBASE:
    try:
        cred_dict = json.loads(firebase_json)
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        print("✅ Firebase Firestore conectado correctamente.")
    except Exception as e:
        print(f"❌ Error al inicializar Firebase Firestore: {e}")
else:
    print("⚠️ No se encontró la variable FIREBASE_CREDENTIALS o la librería firebase-admin. Usando sistema local JSON.")

OSU_TOKEN = None
http_session: aiohttp.ClientSession = None

racha_actual = {
    "osu": {"user_id": None, "streak": 0},
    "phigros": {"user_id": None, "streak": 0}
}

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ==========================================
# 3. MIGRACIÓN AUTOMÁTICA DE ARCHIVOS JSON A FIRESTORE
# ==========================================
def migrar_json_a_firestore():
    """Migra los datos locales existentes a Firestore al iniciar."""
    if not db:
        return

    archivos_migracion = [
        ("puntos.json", "puntos_osu", True),
        ("puntos_phigros.json", "puntos_phigros", True),
        ("stats.json", "stats_osu", False),
        ("stats_phigros.json", "stats_phigros", False)
    ]

    for archivo, coleccion, es_puntos in archivos_migracion:
        if os.path.exists(archivo):
            try:
                with open(archivo, "r", encoding="utf-8") as f:
                    datos = json.load(f)

                if isinstance(datos, dict) and datos:
                    print(f"📦 Migrando {archivo} a la colección Firestore '{coleccion}'...")
                    for uid_str, item in datos.items():
                        doc_ref = db.collection(coleccion).document(uid_str)
                        if es_puntos:
                            nombre = item.get("nombre", f"<@{uid_str}>") if isinstance(item, dict) else f"<@{uid_str}>"
                            pts = item.get("puntos", item) if isinstance(item, dict) else item
                            doc_ref.set({"nombre": nombre, "puntos": pts}, merge=True)
                        else:
                            doc_ref.set(item, merge=True)
                    print(f"✅ Migración de {archivo} completada.")
            except Exception as e:
                print(f"❌ Error al migrar {archivo}: {e}")

# ==========================================
# 4. LECTURA Y GUARDADO DE DATOS (Firestore + Respaldo JSON)
# ==========================================
def cargar_canciones(juego: str):
    archivo = "phigros_songs.json" if juego == "phigros" else "beatmaps.json"
    try:
        with open(archivo, "r", encoding="utf-8") as f:
            datos = json.load(f)
            
            if isinstance(datos, list):
                clasificado = {"facil": [], "normal": [], "dificil": []}
                for item in datos:
                    dif = str(item.get("dificultad", "normal")).lower().strip()
                    if dif in clasificado:
                        clasificado[dif].append(item)
                    else:
                        clasificado["normal"].append(item)
                return clasificado
            
            if isinstance(datos, dict):
                for key in ["facil", "normal", "dificil"]:
                    if key not in datos:
                        datos[key] = []
                return datos

            return {"facil": [], "normal": [], "dificil": []}
    except Exception as e:
        print(f"❌ Error al cargar {archivo}: {e}")
        return {"facil": [], "normal": [], "dificil": []}

def sumar_puntos(juego: str, user_id: int, user_name: str, puntos: int) -> int:
    uid_str = str(user_id)
    
    if db:
        try:
            doc_ref = db.collection(f"puntos_{juego}").document(uid_str)
            doc = doc_ref.get()
            puntos_actuales = 0
            
            if doc.exists:
                data = doc.to_dict()
                puntos_actuales = data.get("puntos", 0)
                
            nuevo_total = puntos_actuales + puntos
            doc_ref.set({
                "nombre": user_name,
                "puntos": nuevo_total
            }, merge=True)
            return nuevo_total
        except Exception as e:
            print(f"❌ Error al actualizar puntos en Firestore: {e}")

    # Fallback JSON local
    archivo = "puntos_phigros.json" if juego == "phigros" else "puntos.json"
    puntos_db = {}
    if os.path.exists(archivo):
        try:
            with open(archivo, "r", encoding="utf-8") as f:
                puntos_db = json.load(f)
        except Exception:
            puntos_db = {}

    if uid_str not in puntos_db:
        puntos_db[uid_str] = {"nombre": user_name, "puntos": 0}
    elif isinstance(puntos_db[uid_str], int):
        puntos_db[uid_str] = {"nombre": user_name, "puntos": puntos_db[uid_str]}

    puntos_db[uid_str]["nombre"] = user_name
    puntos_db[uid_str]["puntos"] = puntos_db[uid_str].get("puntos", 0) + puntos

    try:
        with open(archivo, "w", encoding="utf-8") as f:
            json.dump(puntos_db, f, indent=4)
    except Exception as e:
        print(f"❌ Error al guardar localmente: {e}")
        
    return puntos_db[uid_str]["puntos"]

def registrar_victoria_stats(juego: str, user_id: int, dificultad_str: str, racha_conseguida: int):
    uid_str = str(user_id)
    dif = dificultad_str.lower().strip()

    clave_dif = "normal"
    if dif in ["facil", "easy"]:
        clave_dif = "facil"
    elif dif in ["normal", "medium"]:
        clave_dif = "normal"
    elif dif in ["dificil", "hard", "insane", "expert"]:
        clave_dif = "dificil"

    if db:
        try:
            doc_ref = db.collection(f"stats_{juego}").document(uid_str)
            doc = doc_ref.get()
            stats_data = {"facil": 0, "normal": 0, "dificil": 0, "max_racha": 0}
            
            if doc.exists:
                stats_data.update(doc.to_dict())

            stats_data[clave_dif] = stats_data.get(clave_dif, 0) + 1
            if racha_conseguida > stats_data.get("max_racha", 0):
                stats_data["max_racha"] = racha_conseguida

            doc_ref.set(stats_data, merge=True)
            return
        except Exception as e:
            print(f"❌ Error al guardar stats en Firestore: {e}")

    # Fallback JSON local
    archivo = "stats_phigros.json" if juego == "phigros" else "stats.json"
    stats_db = {}
    if os.path.exists(archivo):
        try:
            with open(archivo, "r", encoding="utf-8") as f:
                stats_db = json.load(f)
        except Exception:
            stats_db = {}

    if uid_str not in stats_db:
        stats_db[uid_str] = {"facil": 0, "normal": 0, "dificil": 0, "max_racha": 0}

    stats_db[uid_str][clave_dif] = stats_db[uid_str].get(clave_dif, 0) + 1
    if racha_conseguida > stats_db[uid_str].get("max_racha", 0):
        stats_db[uid_str]["max_racha"] = racha_conseguida

    try:
        with open(archivo, "w", encoding="utf-8") as f:
            json.dump(stats_db, f, indent=4)
    except Exception as e:
        print(f"❌ Error al guardar stats localmente: {e}")

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
            img_recortada.save(buffer, format="JPEG", quality=85, optimize=True)
    
    buffer.seek(0)
    return buffer

# ==========================================
# 5. COMPONENTES UI Y JUEGO
# ==========================================
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

    for url in urls_imagen:
        if not url:
            continue
        try:
            async with http_session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
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
        link_str = f"\n🔗 **[Link al Beatmapset]({mapa['url']})**"
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

        embed_victoria = discord.Embed(
            title="🎉 ¡Correcto!",
            description=(
                f"👤 **Ganador:** {msg_ganador.author.mention}{tag_racha}\n"
                f"💬 **Respuesta enviada:** `{msg_ganador.content}`\n"
                f"⭐ **Puntos ganados:** `+{puntos_mapa}` (Total: **{total_puntos} pts**)\n\n"
                f"🎵 **Canción:** {nombre_oficial}{link_str}"
            ),
            color=discord.Color.green()
        )
        embed_victoria.set_image(url="attachment://completo.jpg")

        await interaction.channel.send(embed=embed_victoria, file=archivo_completo, view=PlayAgainView(modo_juego))

    except asyncio.TimeoutError:
        reiniciar_racha(modo_juego)

        embed_derrota = discord.Embed(
            title="⏰ ¡Tiempo agotado!",
            description=(
                f"Nadie adivinó a tiempo.\n\n"
                f"🎵 **La canción era:** {nombre_oficial}{link_str}"
            ),
            color=discord.Color.red()
        )
        embed_derrota.set_image(url="attachment://completo.jpg")

        await interaction.channel.send(embed=embed_derrota, file=archivo_completo, view=PlayAgainView(modo_juego))

# ==========================================
# 6. EVENTOS Y COMANDOS SLASH
# ==========================================
@bot.event
async def on_ready():
    global http_session
    if http_session is None or http_session.closed:
        http_session = aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0 DiscordBot/1.0"})
    
    print(f"🤖 Bot iniciado como: {bot.user.name}")
    await obtener_token_osu()

    # Ejecutar migración de datos locales a Firestore
    migrar_json_a_firestore()

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
    # Defer inmediato para evitar Timeout (Error 10062)
    await interaction.response.defer()

    top_list = []

    if db:
        try:
            docs = db.collection(f"puntos_{juego.value}").order_by("puntos", direction=firestore.Query.DESCENDING).limit(10).stream()
            for doc in docs:
                data = doc.to_dict()
                top_list.append((doc.id, data))
        except Exception as e:
            print(f"❌ Error al leer puntos desde Firestore: {e}")

    if not top_list and not db:
        archivo = "puntos_phigros.json" if juego.value == "phigros" else "puntos.json"
        pts_data = {}
        if os.path.exists(archivo):
            try:
                with open(archivo, "r", encoding="utf-8") as f:
                    pts_data = json.load(f)
            except Exception:
                pts_data = {}
                
        top_list = sorted(
            pts_data.items(), 
            key=lambda x: x[1]["puntos"] if isinstance(x[1], dict) else x[1], 
            reverse=True
        )[:10]

    lineas = []
    for i, (uid, data) in enumerate(top_list):
        nombre = data.get("nombre", f"<@{uid}>") if isinstance(data, dict) else f"<@{uid}>"
        pts = data.get("puntos", data) if isinstance(data, dict) else data
        lineas.append(f"`#{i+1}` **{nombre}** — `{pts} pts`")

    txt_ranking = "\n".join(lineas) or "Sin registros aún."

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
    # Defer inmediato para evitar Timeout (Error 10062)
    await interaction.response.defer()

    target_user = usuario or interaction.user
    uid_str = str(target_user.id)
    user_data = {"facil": 0, "normal": 0, "dificil": 0, "max_racha": 0}

    if db:
        try:
            doc = db.collection(f"stats_{juego.value}").document(uid_str).get()
            if doc.exists:
                user_data.update(doc.to_dict())
        except Exception as e:
            print(f"❌ Error al consultar stats en Firestore: {e}")
    else:
        archivo = "stats_phigros.json" if juego.value == "phigros" else "stats.json"
        if os.path.exists(archivo):
            try:
                with open(archivo, "r", encoding="utf-8") as f:
                    stats_db = json.load(f)
                    user_data.update(stats_db.get(uid_str, {}))
            except Exception:
                pass

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

# ==========================================
# 7. INICIAR BOT
# ==========================================
if __name__ == "__main__":
    if DISCORD_TOKEN:
        bot.run(DISCORD_TOKEN)
    else:
        print("❌ ERROR: No se encontró la variable DISCORD_TOKEN.")
