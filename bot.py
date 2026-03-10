import discord
from discord.ui import View, Button
import os

# ── Config ──────────────────────────────────────────────────────────────────
TOKEN = os.environ.get("DISCORD_TOKEN")  # set this in Railway as an env variable

CATEGORIES = {
    "active":    1476810735194607647,
    "delivered": 1480672015278014514,
    "declined":  1480672102775390299,
    "cold":      1480607284387250187,
}

# ── Bot setup ────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
client = discord.Client(intents=intents)

# ── Status panel view (buttons) ──────────────────────────────────────────────
class LeadStatusView(View):
    def __init__(self):
        super().__init__(timeout=None)  # persistent across restarts

    @discord.ui.button(label="📤 Offer Delivered", style=discord.ButtonStyle.primary,   custom_id="move_delivered")
    async def move_delivered(self, interaction: discord.Interaction, button: Button):
        await move_channel(interaction, "delivered", "📤 Offer Delivered")

    @discord.ui.button(label="🚫 Offer Declined",  style=discord.ButtonStyle.danger,    custom_id="move_declined")
    async def move_declined(self, interaction: discord.Interaction, button: Button):
        await move_channel(interaction, "declined", "🚫 Offer Declined")

    @discord.ui.button(label="🥶 Not Interested",  style=discord.ButtonStyle.secondary, custom_id="move_cold")
    async def move_cold(self, interaction: discord.Interaction, button: Button):
        await move_channel(interaction, "cold", "🥶 Not Interested")

    @discord.ui.button(label="🔄 Back to Active",  style=discord.ButtonStyle.success,   custom_id="move_active")
    async def move_active(self, interaction: discord.Interaction, button: Button):
        await move_channel(interaction, "active", "🔄 Active Leads")


async def move_channel(interaction: discord.Interaction, category_key: str, label: str):
    guild    = interaction.guild
    channel  = interaction.channel
    cat_id   = CATEGORIES[category_key]
    category = guild.get_channel(cat_id)

    if category is None:
        await interaction.response.send_message("❗ Category not found. Check the IDs in bot.py.", ephemeral=True)
        return

    await channel.edit(category=category)
    await interaction.response.send_message(f"✅ Moved to **{label}** by {interaction.user.mention}", ephemeral=False)


# ── Auto-panel on new channel ────────────────────────────────────────────────
@client.event
async def on_guild_channel_create(channel):
    # Only post panel if the new channel is inside one of our lead categories
    if not isinstance(channel, discord.TextChannel):
        return
    if channel.category_id not in CATEGORIES.values():
        return

    embed = discord.Embed(
        title="📋 Lead Status",
        description="Move this lead to a different stage:",
        color=0x5865F2
    )
    await channel.send(embed=embed, view=LeadStatusView())


# ── Commands ─────────────────────────────────────────────────────────────────
@client.event
async def on_ready():
    client.add_view(LeadStatusView())  # re-register persistent view on restart
    print(f"✅ Logged in as {client.user}")


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # !panel  →  posts the button panel in the current channel
    if message.content.strip().lower() == "!panel":
        embed = discord.Embed(
            title="📋 Lead Status",
            description="Move this lead to a different stage:",
            color=0x5865F2
        )
        await message.channel.send(embed=embed, view=LeadStatusView())
        await message.delete()


client.run(TOKEN)
