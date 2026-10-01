import os
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread

# ==========================================
# 1. SERVIDOR HTTP LIGERO (Salud para Render)
# ==========================================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()
        self.wfile.write(b"Bot activo 24/7")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    # Silenciar logs HTTP molestos en la consola de Render
    def log_message(self, format, *args):
        return

def run_http_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

def keep_alive():
    t = Thread(target=run_http_server, daemon=True)
    t.start()

# Iniciar servidor inmediatamente para Render
keep_alive()

# ==========================================
# 2. IMPORTS PESADOS E INICIALIZACIÓN
# ==========================================
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

try:
    import firebase_admin
    from firebase_admin import credentials, firestore
    HAS_FIREBASE = True
except Exception as e:
    HAS_FIREBASE = False
    print(f"⚠️ No se pudo importar firebase_admin: {e}", flush=True)

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
        print("✅ Firebase Firestore conectado correctamente.", flush=True)
    except Exception as e:
        print(f"❌ Error al inicializar Firebase Firestore: {e}", flush=True)
else:
    print("⚠️ No se encontró la variable FIREBASE_CREDENTIALS o la librería. Usando modo JSON.", flush=True)

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
# 3. LECTURA Y GUARDADO DE DATOS
# ==========================================
def cargar_canciones(juego: str):
    archivo = "phigros_songs.json" if juego == "phigros" else "beatmaps.json"
    try:
        if not os.path.exists(archivo):
            print(f"⚠️ Archivo no encontrado: {archivo}", flush=True)
            return {"facil": [], "normal": [], "dificil": []}

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
        print(f"❌ Error al cargar {archivo}: {e}", flush=True)
        return {"facil": [], "normal": [], "dificil": []}

def obtener_catalogo_plano(juego: str):
    """Obtiene la lista completa de canciones de forma segura."""
    try:
        db_canciones = cargar_canciones(juego)
        plano = []
        for dif, lista in db_canciones.items():
            if isinstance(lista, list):
                plano.extend(lista)
        return plano
    except Exception as e:
        print(f"❌ Error obteniendo catálogo plano ({juego}): {e}", flush=True)
        return []

def obtener_id_cancion(mapa: dict, juego: str) -> str:
    """Genera un ID único para registrar en la colección."""
    if juego == "osu":
        set_id = extraer_beatmapset_id(mapa.get("url", ""))
        if set_id:
            return set_id
    
    nombre = mapa.get("nombre") or (mapa.get("respuestas", ["sin_nombre"])[0])
    return re.sub(r'[^a-zA-Z0-9_]', '', str(nombre).lower().replace(" ", "_"))

def registrar_cancion_adivinada(juego: str, mapa: dict, user_name: str):
    song_id = obtener_id_cancion(mapa, juego)
    nombre = mapa.get("nombre") or mapa.get("respuestas", ["Desconocida"])[0].title()

    if db:
        try:
            doc_ref = db.collection(f"coleccion_{juego}").document(song_id)
            doc_ref.set({
                "nombre": nombre,
                "adivinada": True,
                "ultimo_ganador": user_name,
                "ultima_fecha": firestore.SERVER_TIMESTAMP
            }, merge=True)
        except Exception as e:
            print(f"❌ Error al registrar colección en Firestore: {e}", flush=True)

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
            print(f"❌ Error al actualizar puntos en Firestore: {e}", flush=True)

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
        print(f"❌ Error al guardar localmente: {e}", flush=True)
        
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
            print(f"❌ Error al guardar stats en Firestore: {e}", flush=True)

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
        print(f"❌ Error al guardar stats localmente: {e}", flush=True)

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
        print(f"❌ Error al obtener token de osu!: {e}", flush=True)

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

def recortar_imagen(imagen_bytes, juego: str = "osu"):
    with Image.open(io.BytesIO(imagen_bytes)).convert("RGB") as img:
        ancho, alto = img.size

        if juego == "osu":
            ancho_crop = int(ancho * random.uniform(0.3, 0.4))
            alto_crop = int(alto * random.uniform(0.3, 0.4))
        else:
            ancho_crop = int(ancho * random.uniform(0.23, 0.27))
            alto_crop = int(alto * random.uniform(0.22, 0.3))

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
# 4. COMPONENTES UI
# ==========================================
class BotonFaltantesView(discord.ui.View):
    def __init__(self, juego: str, faltantes: list):
        super().__init__(timeout=120)
        self.juego = juego
        self.faltantes = faltantes

    @discord.ui.button(label="❌ Ver Canciones Faltantes", style=discord.ButtonStyle.secondary, emoji="📜")
    async def ver_faltantes(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        if not self.faltantes:
            await interaction.followup.send("🎉 ¡Increíble! Ya han adivinado todas las canciones registradas.", ephemeral=True)
            return

        lineas = []
        for item in self.faltantes:
            nombre = item.get("nombre") or item.get("respuestas", ["Desconocida"])[0].title()
            dif = item.get("dificultad", "normal").capitalize()
            lineas.append(f"• **{nombre}** (`{dif}`)")

        texto_completo = "\n".join(lineas)
        if len(texto_completo) > 3900:
            texto_completo = texto_completo[:3900] + "\n\n*(Lista recortada por límite de espacio)*"

        embed = discord.Embed(
            title=f"📋 Canciones pendientes por adivinar — {self.juego.upper()}",
            description=texto_completo,
            color=discord.Color.orange()
        )
        embed.set_footer(text=f"Total no adivinadas: {len(self.faltantes)}")

        await interaction.followup.send(embed=embed, ephemeral=True)

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

# ==========================================
# 5. LÓGICA DEL JUEGO
# ==========================================
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

    buffer_recorte = recortar_imagen(imagen_bytes, juego=modo_juego)
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

        # Registrar en colección
        registrar_cancion_adivinada(modo_juego, mapa, msg_ganador.author.display_name)

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
    http_session = aiohttp.ClientSession(
        headers={"User-Agent": "Mozilla/5.0 DiscordBot/1.0"}
    )

  print(f"🤖 Bot iniciado como: {bot.user.name}", flush=True)
  await obtener_token_osu()

  try:
    synced = await bot.tree.sync()
    print(f"✅ Sincronizados {len(synced)} comandos.", flush=True)
  except Exception as e:
    print(f"❌ Error al sincronizar: {e}", flush=True)

