import json
import os
from threading import Thread
import discord
from discord import app_commands
from discord.ext import commands
from flask import Flask

# Intentar importar dependencias de Firebase
try:
  import firebase_admin
  from firebase_admin import credentials, firestore

  HAS_FIREBASE = True
except ImportError:
  HAS_FIREBASE = False

# ==========================================
# 1. SERVIDOR FLASK (Para mantener vivo el puerto en Render)
# ==========================================
app = Flask("")


@app.route("/")
def home():
  return "Bot de Discord activo 24/7"


def run_flask():
  port = int(os.environ.get("PORT", 8080))
  app.run(host="0.0.0.0", port=port)


def keep_alive():
  t = Thread(target=run_flask)
  t.daemon = True
  t.start()


# Iniciar servidor web antes de la lógica del bot
keep_alive()

# ==========================================
# 2. INICIALIZACIÓN DE FIREBASE FIRESTORE
# ==========================================
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
  if not HAS_FIREBASE:
    print("⚠️ La librería firebase-admin no está instalada.")
  else:
    print(
        "⚠️ Variable FIREBASE_CREDENTIALS no encontrada. Usando modo de"
        " respaldo local."
    )

# ==========================================
# 3. FUNCIONES AUXILIARES PARA PUNTOS
# ==========================================


def guardar_puntos_usuario(
    user_id: int, user_name: str, juego: str, puntos_a_sumar: int
):
  """Suma puntos a un usuario en Firestore o cae en respaldo local."""
  if db:
    try:
      doc_ref = db.collection(f"puntos_{juego}").document(str(user_id))
      doc = doc_ref.get()

      puntos_actuales = doc.to_dict().get("puntos", 0) if doc.exists else 0
      nuevos_puntos = puntos_actuales + puntos_a_sumar

      doc_ref.set(
          {
              "nombre": user_name,
              "puntos": nuevos_puntos,
              "ultima_actualizacion": firestore.SERVER_TIMESTAMP,
          },
          merge=True,
      )
      print(
          f"✅ [{juego.upper()}] {user_name} ahora tiene {nuevos_puntos} pts en"
          " Firestore."
      )
      return nuevos_puntos
    except Exception as e:
      print(f"❌ Error al guardar puntos en Firestore: {e}")

  # Respaldo en archivo JSON local en caso de que falle Firestore
  archivo = f"puntos_{juego}.json"
  pts_data = {}
  if os.path.exists(archivo):
    try:
      with open(archivo, "r", encoding="utf-8") as f:
        pts_data = json.load(f)
    except Exception:
      pts_data = {}

  uid_str = str(user_id)
  puntos_actuales = pts_data.get(uid_str, 0)
  nuevos_puntos = puntos_actuales + puntos_a_sumar
  pts_data[uid_str] = nuevos_puntos

  try:
    with open(archivo, "w", encoding="utf-8") as f:
      json.dump(pts_data, f, indent=4)
  except Exception as e:
    print(f"❌ Error al guardar localmente: {e}")

  return nuevos_puntos


def obtener_ranking(juego: str, limite: int = 10):
  """Obtiene el top de usuarios desde Firestore o el respaldo local."""
  top_list = []

  if db:
    try:
      docs = (
          db.collection(f"puntos_{juego}")
          .order_by("puntos", direction=firestore.Query.DESCENDING)
          .limit(limite)
          .stream()
      )
      for doc in docs:
        top_list.append((doc.id, doc.to_dict()))
      return top_list
    except Exception as e:
      print(f"❌ Error al leer ranking desde Firestore: {e}")

  # Respaldo desde JSON
  archivo = f"puntos_{juego}.json"
  pts_data = {}
  if os.path.exists(archivo):
    try:
      with open(archivo, "r", encoding="utf-8") as f:
        pts_data = json.load(f)
    except Exception:
      pts_data = {}

  top_sorted = sorted(pts_data.items(), key=lambda x: x[1], reverse=True)[
      :limite
  ]
  for uid, pts in top_sorted:
    top_list.append((uid, {"nombre": f"<@{uid}>", "puntos": pts}))

  return top_list


# ==========================================
# 4. CONFIGURACIÓN DEL BOT DE DISCORD
# ==========================================
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
  print(f"🤖 Bot conectado como {bot.user}")
  try:
    synced = await bot.tree.sync()
    print(f"✨ Se sincronizaron {len(synced)} comandos de barra (/).")
  except Exception as e:
    print(f"❌ Error al sincronizar comandos: {e}")


# ==========================================
# 5. COMANDOS DE BARRA (SLASH COMMANDS)
# ==========================================


@bot.tree.command(
    name="puntos", description="Muestra la tabla de clasificación por juego"
)
@app_commands.describe(juego="Selecciona el juego")
@app_commands.choices(
    juego=[
        app_commands.Choice(name="osu!", value="osu"),
        app_commands.Choice(name="Phigros", value="phigros"),
    ]
)
async def puntos(
    interaction: discord.Interaction, juego: app_commands.Choice[str]
):
  # Defer obligatorio de entrada para evitar el error 404 (timeout de 3s)
  await interaction.response.defer()

  ranking = obtener_ranking(juego.value)

  lineas = []
  for i, (uid, data) in enumerate(ranking):
    nombre = data.get("nombre", f"<@{uid}>")
    pts = data.get("puntos", 0)
    lineas.append(f"`#{i+1}` **{nombre}** — `{pts} pts`")

  txt_ranking = "\n".join(lineas) if lineas else "Aún no hay puntos registrados."

  embed = discord.Embed(
      title=f"🏆 Tabla de Clasificación — {juego.name}",
      description=txt_ranking,
      color=discord.Color.gold(),
  )

  await interaction.followup.send(embed=embed)


@bot.tree.command(
    name="sumar", description="Suma puntos a un usuario (Comando de prueba)"
)
@app_commands.describe(
    usuario="Usuario a otorgar puntos",
    juego="Juego para el ranking",
    puntos="Cantidad de puntos",
)
@app_commands.choices(
    juego=[
        app_commands.Choice(name="osu!", value="osu"),
        app_commands.Choice(name="Phigros", value="phigros"),
    ]
)
async def sumar(
    interaction: discord.Interaction,
    usuario: discord.User,
    juego: app_commands.Choice[str],
    puntos: int,
):
  await interaction.response.defer()

  total = guardar_puntos_usuario(
      user_id=usuario.id,
      user_name=usuario.display_name,
      juego=juego.value,
      puntos_a_sumar=puntos,
  )

  embed = discord.Embed(
      title="🎉 Puntos Actualizados",
      description=(
          f"Se le han otorgado **{puntos} pts** a {usuario.mention} en"
          f" **{juego.name}**.\n\nTotal acumulado: **{total} pts**."
      ),
      color=discord.Color.green(),
  )

  await interaction.followup.send(embed=embed)


# ==========================================
# 6. EJECUCIÓN
# ==========================================
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")

if __name__ == "__main__":
  if DISCORD_TOKEN:
    bot.run(DISCORD_TOKEN)
  else:
    print("❌ ERROR: No se encontró la variable de entorno DISCORD_TOKEN.")
