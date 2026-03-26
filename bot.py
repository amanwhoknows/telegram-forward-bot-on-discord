import discord
from discord.ext import commands
import asyncio
import re
import os
import glob
import io
import aiohttp
from bs4 import BeautifulSoup
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.types import Channel, Chat
from telethon.errors.rpcerrorlist import PeerFloodError, UserPrivacyRestrictedError, ChatWriteForbiddenError

# --- CONFIGURATION ---
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
TG_API_ID = 25163164
TG_API_HASH = '43ca49c7549ffd65e275fc531514e8c4'
SESSIONS_DIR = "tg_sessions" # Folder to store multiple session files

# Ensure the sessions directory exists
if not os.path.exists(SESSIONS_DIR):
    os.makedirs(SESSIONS_DIR)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Dictionary to track multiple active sessions
# Format: {"session_name": {"client": TelegramClient, "running": True}}
active_sessions = {}

def parse_telegram_url(url):
    match = re.search(r't\.me/(.+)/(\d+)', url)
    if match:
        return match.group(1), int(match.group(2))
    return None, None

@bot.event
async def on_ready():
    print(f'Discord Bot logged in as {bot.user}')
    print('Ready to manage multiple sessions!')

@bot.command()
async def login(ctx, session_name: str, session_string: str):
    """
    Usage: !login session1 <long_string_here>
    """
    try:
        await ctx.message.delete()
    except discord.Forbidden:
        await ctx.send("⚠️ Please delete your session string manually for security.")

    await ctx.send(f"🔄 Testing connection for `{session_name}`...")
    
    try:
        temp_client = TelegramClient(StringSession(session_string), TG_API_ID, TG_API_HASH)
        await temp_client.connect()
        
        if await temp_client.is_user_authorized():
            # Save the session string to a file named after the session
            file_path = os.path.join(SESSIONS_DIR, f"{session_name}.txt")
            with open(file_path, "w") as f:
                f.write(session_string)
            await ctx.send(f"✅ **Account linked!** Saved as `{session_name}`. You can now use `!broadcast {session_name} ...`")
        else:
            await ctx.send(f"❌ Failed to authorize `{session_name}`.")
            
        await temp_client.disconnect()
    except Exception as e:
        await ctx.send(f"❌ Error connecting to Telegram: {str(e)}")
        
@bot.command(name="sessions")
async def check_sessions(ctx):
    # Optional: Add a check here so only YOU can run this command
    # if ctx.author.id != YOUR_DISCORD_ID: return

    # Let the user know it's working (Telethon connections take a second)
    status_msg = await ctx.send("🔍 Scanning local directory for Telegram sessions...")
    
    # Find all Telethon session files in the current folder
    session_files = glob.glob("*.session")
    
    if not session_files:
        await status_msg.edit(content="❌ No `.session` files found in the bot's directory.")
        return

    results = []
    
    for file in session_files:
        session_name = file.replace(".session", "")
        
        # Initialize the Telethon client
        client = TelegramClient(session_name, API_ID, API_HASH)
        
        try:
            # Connect to Telegram servers without starting a new login prompt
            await client.connect()
            
            # Check if the session file is still valid and logged in
            if await client.is_user_authorized():
                me = await client.get_me()
                # Telegram sometimes hides the phone number depending on privacy settings
                phone = me.phone if me.phone else "Hidden by Privacy Settings"
                results.append(f"✅ **{session_name}** ➔ 📱 `+{phone}`")
            else:
                results.append(f"❌ **{session_name}** ➔ ⚠️ Dead Session (Needs Re-auth)")
                
        except Exception as e:
            results.append(f"⚠️ **{session_name}** ➔ Connection Error")
            
        finally:
            # Always disconnect so you don't leak memory or hang the bot
            await client.disconnect()

    # Build a nice looking Discord Embed for the output
    embed = discord.Embed(
        title="📱 Telegram Fleet Status", 
        description="\n\n".join(results),
        color=discord.Color.green()
    )
    embed.set_footer(text=f"Total Sessions Found: {len(session_files)}")
    
    # Update the original message with the final embed
    await status_msg.edit(content="", embed=embed)


@bot.command()
async def stop(ctx, session_name: str):
    """Command to stop a specific broadcast loop."""
    if session_name in active_sessions:
        active_sessions[session_name]['running'] = False
        await ctx.send(f"🛑 Stopping the broadcast for `{session_name}` after its current action finishes...")
    else:
        await ctx.send(f"⚠️ No active broadcast found for `{session_name}`.")

# --- THE BACKGROUND BROADCAST LOOP ---
async def run_broadcast_loop(session_name, client, groups, message_id, channel_id, interval, log_channel):
    """This function runs in the background for each session."""
    round_number = 1
    
    while active_sessions[session_name]['running']:
        await log_channel.send(f"🔄 **Starting Round {round_number}**")
        success_count = 0
        batch_results = []

        for index, group in enumerate(groups):
            if not active_sessions[session_name]['running']:
                break 

            display_name = group.split('/')[-1].replace('@', '')

            try:
                await client.forward_messages(group, message_id, channel_id)
                batch_results.append(f"✅ `{display_name}` - Sent successfully")
                success_count += 1
                await asyncio.sleep(2) 
                
            except PeerFloodError:
                batch_results.append(f"🛑 `{display_name}` - Flood Limit Hit")
                if batch_results:
                    await log_channel.send(f"📊 **Batch Update:**\n" + "\n".join(batch_results))
                    batch_results = []
                await log_channel.send("🛑 **Flood Error!** Pausing for 2 minutes...")
                await asyncio.sleep(120)
                
            except ChatWriteForbiddenError:
                try:
                    await client(JoinChannelRequest(group))
                    await client.forward_messages(group, message_id, channel_id)
                    batch_results.append(f"✅ `{display_name}` - Sent (After Joining)")
                    success_count += 1
                    if batch_results:
                        await log_channel.send(f"📊 **Batch Update:**\n" + "\n".join(batch_results))
                        batch_results = []
                    await log_channel.send(f"⏳ **Joined `{display_name}`. Waiting 300s...**")
                    await asyncio.sleep(300) 
                except Exception as e:
                    batch_results.append(f"❌ `{display_name}` - Failed to Join")
                    
            except UserPrivacyRestrictedError:
                 batch_results.append(f"❌ `{display_name}` - Privacy Blocked")
            except Exception as e:
                 batch_results.append(f"❌ `{display_name}` - Error: {str(e)}")

            # Batch send logic
            if len(batch_results) == 10 or index == len(groups) - 1:
                if batch_results: 
                    report = "\n".join(batch_results)
                    await log_channel.send(f"📊 **Update ({index + 1}/{len(groups)}):**\n{report}")
                    batch_results = [] 

        if not active_sessions[session_name]['running']:
            await log_channel.send(f"🛑 **Broadcast for {session_name} stopped manually.**")
            break

        await log_channel.send(f"🏁 **Round {round_number} Complete!** Sent to {success_count}/{len(groups)} groups. Sleeping for {interval}s...")
        
        for _ in range(interval):
            if not active_sessions[session_name]['running']:
                break
            await asyncio.sleep(1)
            
        round_number += 1

    # Cleanup when the loop ends
    await client.disconnect()
    del active_sessions[session_name]


@bot.command()
async def broadcast(ctx, session_name: str, tg_url: str, interval: int):
    """
    Usage: !broadcast session1 https://t.me/channel/123 3600
    """
    # 1. Check if it's already running
    if session_name in active_sessions:
        await ctx.send(f"⚠️ `{session_name}` is already running a broadcast! Use `!stop {session_name}` first.")
        return

    # 2. Check if we have a saved login for this session name
    file_path = os.path.join(SESSIONS_DIR, f"{session_name}.txt")
    if not os.path.exists(file_path):
        await ctx.send(f"🛑 No account saved under `{session_name}`. Use `!login {session_name} <string>` first.")
        return

    if not ctx.message.attachments:
        await ctx.send("⚠️ Please attach a `.txt` file containing the group lists.")
        return

    channel_id, message_id = parse_telegram_url(tg_url)
    if not channel_id or not message_id:
        await ctx.send("⚠️ Invalid Telegram URL.")
        return

    # 3. Create the new Discord Channel for this specific session
    await ctx.send(f"⚙️ Setting up `{session_name}`...")
    try:
        # Creates a channel named after the session (e.g., #session1)
        log_channel = await ctx.guild.create_text_channel(session_name)
        await log_channel.send(f"👋 **Initialization Complete for `{session_name}`**\nAll updates for this session will be posted here.")
    except discord.Forbidden:
        await ctx.send("❌ I do not have permission to create channels in this server! Please give the bot 'Manage Channels' permission.")
        return

    # 4. Download the specific file for this session
    attachment = ctx.message.attachments[0]
    file_bytes = await attachment.read()
    groups = file_bytes.decode('utf-8').splitlines()
    groups = [g.strip() for g in groups if g.strip()] 

    # 5. Connect the specific Telegram Client
    with open(file_path, "r") as f:
        session_string = f.read().strip()
        
    client = TelegramClient(StringSession(session_string), TG_API_ID, TG_API_HASH)
    await client.connect()

    # 6. Register it as active and start the background task
    active_sessions[session_name] = {"client": client, "running": True}
    
    await ctx.send(f"🚀 Started `{session_name}`! Check {log_channel.mention} for live updates.")
    
    # Fire off the background loop
    bot.loop.create_task(run_broadcast_loop(
        session_name, client, groups, message_id, channel_id, interval, log_channel
    ))

@bot.command()
async def check(ctx):
    """
    Usage: !check (Attach a .txt file)
    Analyzes links via web and returns a .txt file of valid public groups.
    """
    if not ctx.message.attachments:
        await ctx.send("⚠️ Please attach a `.txt` file containing the Telegram links.")
        return

    attachment = ctx.message.attachments[0]
    if not attachment.filename.endswith('.txt'):
        await ctx.send("⚠️ The attached file must be a `.txt` file.")
        return

    await ctx.send("📥 Downloading file and starting link analysis...")
    file_bytes = await attachment.read()
    links = file_bytes.decode('utf-8').splitlines()
    links = [link.strip() for link in links if link.strip()]

    batch_results = []
    valid_groups_list = [] # This will store links for the final .txt file

    async with aiohttp.ClientSession() as web_session:
        for index, link in enumerate(links):
            try:
                # Format URL for the web request
                clean_link = link if link.startswith('http') else f"https://{link}"

                async with web_session.get(clean_link) as response:
                    html = await response.text()
                    soup = BeautifulSoup(html, 'html.parser')
                    extra_info = soup.find('div', class_='tgme_page_extra')
                    
                    if not extra_info:
                        batch_results.append(f"⚠️ <{link}> - **Invalid/Private**")
                    else:
                        info_text = extra_info.text.lower()
                        
                        if 'subscriber' in info_text:
                            batch_results.append(f"❌ <{link}> - **Channel**")
                        elif 'member' in info_text:
                            batch_results.append(f"✅ <{link}> - **Public Group**")
                            valid_groups_list.append(link) # Save the original link format
                        else:
                            batch_results.append(f"❓ <{link}> - **Unknown**")

            except Exception as e:
                batch_results.append(f"❓ <{link}> - **Web Error**")

            # Progress updates in batches of 10
            if len(batch_results) == 10 or index == len(links) - 1:
                if batch_results:
                    report = "\n".join(batch_results)
                    await ctx.send(f"📊 **Progress ({index + 1}/{len(links)}):**\n{report}")
                    batch_results = []
                await asyncio.sleep(1) 

    # --- FINAL STEP: Generate and upload the .txt file ---
    if valid_groups_list:
        # Join the valid links with newlines
        output_content = "\n".join(valid_groups_list)
        
        # Create a file-like object in memory
        with io.BytesIO(output_content.encode('utf-8')) as out_file:
            discord_file = discord.File(fp=out_file, filename="valid_public_groups.txt")
            await ctx.send(
                content=f"🏁 **Check Complete!** Found {len(valid_groups_list)} public groups.\n"
                        f"Below is the filtered list for your next broadcast:",
                file=discord_file
            )
    else:
        await ctx.send("🏁 **Check Complete!** No valid public groups were found in that list.")

bot.run(DISCORD_TOKEN)
