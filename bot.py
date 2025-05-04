import discord
from discord import app_commands # Import app_commands
import os
import google.generativeai as genai
from dotenv import load_dotenv
import re # Import re for parsing
from commands.persona import setup_persona_commands, set_gemini_globals, ApprovalView # Import ApprovalView if needed for on_ready handling (optional for now)
from shared import DB_FILE, ADMIN_USER_ID, is_admin_or_creator, get_persona, initialize_database, DISCORD_TOKEN, GEMINI_API_KEY

# Load environment variables from .env file
load_dotenv()

# --- Basic Input Validation ---
if not DISCORD_TOKEN:
    print("Error: DISCORD_TOKEN not found in .env file.")
    exit()
if not GEMINI_API_KEY:
    print("Error: GEMINI_API_KEY not found in .env file.")
    exit()

# Initialize DB before anything else that might need it
initialize_database()

# --- Configure Discord Bot ---
intents = discord.Intents.default()
intents.messages = True
intents.message_content = True # Enable message content intent

# Use Client instead of Bot for simplicity here, but Bot is often preferred for commands
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client) # Create a command tree

# --- Configure Google Gemini ---
try:
    genai.configure(api_key=GEMINI_API_KEY)
    # Store config and safety settings globally for reuse
    generation_config = {
      "temperature": 0.9,
      "top_p": 1,
      "top_k": 1,
      "max_output_tokens": 2048,
    }
    # Default safety settings (will be adjusted per message based on channel)
    default_safety_settings = [
      {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
      {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
      {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_ONLY_HIGH"}, # Base default
      {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"}, # Base default
    ]
    # Pass necessary globals to the persona module
    # Note: model and chat are no longer created globally here
    set_gemini_globals(generation_config, default_safety_settings, client) # Pass client instance (Now defined)
    print("Gemini API configured. Model will be created per message.")
except Exception as e:
    print(f"Error configuring Gemini: {e}")
    exit()

# Register persona commands
import asyncio

# --- Discord Event Handlers ---

@client.event
async def on_ready():
    # If using persistent views across restarts, you might re-add them here.
    # For now, we rely on the view timeout set in ApprovalView.
    # client.add_view(ApprovalView(request_id="*")) # This approach needs refinement for dynamic request_ids

    await setup_persona_commands(tree, client) # Pass client instance
    await tree.sync()
    print(f'Logged in as {client.user.name} ({client.user.id})')
    print(f'Command tree synced. Ready.')
    print('------')

@client.event
async def on_message(message):
    """Event handler for when a message is sent."""
    if message.author == client.user or message.author.bot:
        return

    is_mentioned = client.user.mentioned_in(message)
    is_dm = isinstance(message.channel, discord.DMChannel)

    if is_mentioned or is_dm:
        async with message.channel.typing():
            try:
                raw_content = message.content
                persona_name_override = None
                persona_content_override = None
                # Dynamically load the default persona
                default_persona_data = get_persona()
                if not default_persona_data:
                    print("CRITICAL ERROR: Could not load default persona during message processing.")
                    await message.channel.send("Sorry, I couldn't load my default personality.")
                    return
                system_instruction_to_use = default_persona_data[0]

                # 1. Determine base content (remove mention if applicable)
                if is_mentioned:
                    # Remove the bot mention first
                    base_content = re.sub(f'<@!?{client.user.id}>', '', raw_content, count=1).strip()
                else: # It's a DM
                    base_content = raw_content.strip()

                # 2. Check for -type argument in the base content
                type_match = re.search(r'-type\s+"([^"]+)"', base_content, re.IGNORECASE)
                if type_match:
                    persona_name_override = type_match.group(1)
                    # Remove the argument from the base content
                    base_content = (base_content[:type_match.start()] + base_content[type_match.end():]).strip()
                    print(f"Detected type override: '{persona_name_override}'")
                    # Fetch the specified persona content
                    persona_data = get_persona(persona_name_override)
                    if persona_data:
                        persona_content_override = persona_data[0]
                        system_instruction_to_use = persona_content_override # Use override
                        print(f"Using persona '{persona_name_override}' for this message.")
                    else:
                        await message.channel.send(f"(Couldn't find persona '{persona_name_override}', using default.)")
                        print(f"Persona '{persona_name_override}' not found, using default.")

                # 3. Fetch history (limit adjusted slightly)
                history_messages = []
                async for msg in message.channel.history(limit=10, before=message): # Fetch messages *before* current one
                    history_messages.append(msg)
                history_messages.reverse() # Oldest first

                formatted_history_lines = []
                for msg in history_messages:
                    # Use display_name which respects server nicknames
                    author_name = "You" if msg.author == client.user else msg.author.display_name
                    # Basic cleaning of mentions in history (might need refinement)
                    # Also clean the -type argument from history messages if present
                    cleaned_hist_content = re.sub(r'<@!?\d+>', '', msg.content).strip()
                    cleaned_hist_content = re.sub(r'-type\s+"[^"]+"', '', cleaned_hist_content, flags=re.IGNORECASE).strip()
                    if cleaned_hist_content: # Avoid empty history lines
                        formatted_history_lines.append(f"{author_name}: {cleaned_hist_content}")
                formatted_history = "\n".join(formatted_history_lines)

                # 4. Check if base_content is empty after processing
                if not base_content:
                    await message.channel.send("Did you mean to ask something?")
                    return

                # 5. Prepare content for Gemini
                current_query = f"{message.author.display_name}: {base_content}" # Use the processed base_content

                # Persona is now handled via system_instruction
                content_for_gemini = (
                    f"## Message History:\n{formatted_history}\n\n"
                    f"## Current Message (Respond to this):\n{current_query}"
                )

                print(f"--- Sending to Gemini ---")
                print(f"Persona Used: {'Override: '+persona_name_override if persona_name_override else 'Default'}")
                # print(f"System Instruction Used:\n{system_instruction_to_use[:100]}...") # Optional: Log instruction
                print(f"History Length: {len(formatted_history_lines)} messages")
                print(f"Current Query: {current_query}") # Log the final query sent
                print(f"-------------------------")

                # Determine safety settings based on channel NSFW status
                if hasattr(message.channel, "is_nsfw") and callable(message.channel.is_nsfw) and message.channel.is_nsfw():
                    # NSFW channel configuration
                    safety_settings_local = [
                        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"}, # Less restrictive
                        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_ONLY_HIGH"}, # Less restrictive
                    ]
                    print("Using NSFW safety settings.")
                else:
                    # Non-NSFW channel configuration (use defaults or specific non-NSFW)
                    safety_settings_local = [
                        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"}, # More restrictive
                        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"}, # More restrictive
                    ]
                    print("Using SFW safety settings.")

                # Create the model and chat locally for this specific message
                local_model = genai.GenerativeModel(
                    model_name="gemini-2.5-flash-preview-04-17", # Updated model name
                    generation_config=generation_config,
                    system_instruction=system_instruction_to_use, # Pass the determined persona here
                    safety_settings=safety_settings_local # Pass the determined safety settings
                )
                # Start a fresh chat for each message to ensure context isolation and correct system instruction/safety
                local_chat = local_model.start_chat(history=[])
                response = await local_chat.send_message_async(content_for_gemini) # Use async version

                print(f"Received from Gemini: {response.text[:100]}...") # Log truncated output

                # Send Gemini's response back to Discord in chunks if needed
                if len(response.text) == 0:
                    await message.channel.send("I received an empty response.")
                else:
                    # Split into chunks of 2000 characters, trying to break at newlines when possible
                    chunks = []
                    text = response.text
                    while text:
                        if len(text) <= 2000:
                            chunks.append(text)
                            break
                        
                        # Try to find a newline to split at
                        split_point = text[:2000].rfind('\n')
                        if split_point == -1:  # No newline found, just split at 2000
                            split_point = 2000

                        chunks.append(text[:split_point])
                        text = text[split_point:].lstrip()  # Remove leading whitespace from next chunk
                    
                    # Send each chunk
                    for i, chunk in enumerate(chunks, 1):
                        if len(chunks) > 1:
                            chunk = f"[Part {i}/{len(chunks)}]\n{chunk}"
                        await message.channel.send(chunk)

            except discord.errors.Forbidden as e:
                print(f"Error: Missing permissions in channel {message.channel.name}: {e}")
            except Exception as e:
                print(f"Error processing message {message.id}: {e}")
                import traceback
                traceback.print_exc() # Print full traceback for debugging
                try:
                    await message.channel.send("Sorry, I encountered an error trying to respond.")
                except discord.errors.Forbidden:
                    print(f"Error: Also missing send message permissions in channel {message.channel.name}")


# --- Run the Bot ---
try:
    # Initialize database before running the client
    initialize_database()
    client.run(DISCORD_TOKEN)
except discord.errors.LoginFailure:
    print("Error: Invalid Discord Token. Please check your .env file.")
except discord.errors.PrivilegedIntentsRequired:
     print("Error: Message Content Intent is not enabled for the bot in the Discord Developer Portal.")
except Exception as e:
    print(f"An unexpected error occurred while running the bot: {e}")
    import traceback
    traceback.print_exc()