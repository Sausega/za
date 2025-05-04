import discord
from discord import app_commands
import sqlite3
import uuid # Import uuid for unique request IDs
from shared import DB_FILE, ADMIN_USER_ID, is_admin_or_creator, get_persona

# REMOVED: Global SYSTEM_MESSAGE declaration
generation_config = None
safety_settings = None
discord_client = None # Added to store the client instance

# Dictionary to store pending requests: {request_id: {'type': 'create'/'modify', 'user_id': int, 'name': str, 'content': str, 'original_interaction': discord.Interaction}}
pending_persona_requests = {}
# Dictionary to map approval message ID to request ID: {message_id: request_id}
approval_message_to_request = {}

# Updated: Removed system_message parameter, added client parameter
def set_gemini_globals(gen_config, safety, client):
    global generation_config, safety_settings, discord_client
    generation_config = gen_config
    safety_settings = safety
    discord_client = client # Store the client instance

class PersonaListView(discord.ui.View):
    def __init__(self, personas, page=0, search=None):
        super().__init__(timeout=180)  # 3 minute timeout
        self.personas = personas
        self.page = page
        self.search = search
        self.per_page = 20
        self.update_buttons() # Initial button setup

    def update_buttons(self):
        # Clear previous buttons before adding new ones
        self.clear_items()
        # Add buttons conditionally based on page number
        if self.page > 0:
            self.add_item(PrevButton()) # Add instance of the button class
        if (self.page + 1) * self.per_page < len(self.personas):
            self.add_item(NextButton()) # Add instance of the button class

    def get_current_page_content(self):
        start = self.page * self.per_page
        end = start + self.per_page
        current_personas = self.personas[start:end]
        
        # Format the message
        lines = []
        for i, (name, is_default, creator_id) in enumerate(current_personas, start=start+1):
            default_mark = "📌" if is_default else "  "
            lines.append(f"{default_mark} {i}. {name}")
        
        # Add header
        header = "🌳 Available Personas"
        if self.search:
            header += f" (Search: '{self.search}')"
        header += f"\nPage {self.page + 1}/{(len(self.personas) + self.per_page - 1) // self.per_page}"
        
        return header + "\n```\n" + "\n".join(lines) + "\n```"

    # REMOVED button_callback method

    # Define button callbacks within the View using decorators
    # Note: We define separate button classes below for clarity,
    # or you could define the decorated methods directly here if preferred.

# Define Button subclasses or use decorators directly in PersonaListView
class PrevButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Previous", style=discord.ButtonStyle.primary, custom_id="prev_page")

    async def callback(self, interaction: discord.Interaction):
        # Get the view instance this button belongs to
        view: PersonaListView = self.view
        if view is None:
            await interaction.response.send_message("Error: View context lost.", ephemeral=True)
            return

        view.page = max(0, view.page - 1)
        view.update_buttons()
        await interaction.response.edit_message(content=view.get_current_page_content(), view=view)

class NextButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Next", style=discord.ButtonStyle.primary, custom_id="next_page")

    async def callback(self, interaction: discord.Interaction):
        # Get the view instance this button belongs to
        view: PersonaListView = self.view
        if view is None:
            await interaction.response.send_message("Error: View context lost.", ephemeral=True)
            return

        view.page = min((len(view.personas) - 1) // view.per_page, view.page + 1)
        view.update_buttons()
        await interaction.response.edit_message(content=view.get_current_page_content(), view=view)

# --- Approval View ---
class ApprovalView(discord.ui.View):
    # REMOVED request_id from __init__
    def __init__(self):
        super().__init__(timeout=86400) # 24 hour timeout for approval
        # REMOVED: Explicit button additions, as decorators handle this.
        # self.add_item(discord.ui.Button(label="Approve", style=discord.ButtonStyle.success, custom_id="approve_request"))
        # self.add_item(discord.ui.Button(label="Reject", style=discord.ButtonStyle.danger, custom_id="reject_request"))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Only allow the admin to interact
        return interaction.user.id == ADMIN_USER_ID

    # MODIFIED: Added request_id parameter
    async def handle_approval(self, interaction: discord.Interaction, request_id: str):
        request_data = pending_persona_requests.get(request_id)
        if not request_data:
             # No need for fallback here as request_id comes from our mapping
             print(f"Error in handle_approval: Request ID '{request_id}' (from message {interaction.message.id}) not found in pending_persona_requests.")
             await interaction.edit_original_response(content="This request is no longer valid or has already been processed.", view=None)
             # Clean up message mapping if it exists
             if interaction.message.id in approval_message_to_request:
                 del approval_message_to_request[interaction.message.id]
             return

        # Log full request details
        print("\n=== Processing Persona Request ===")
        print(f"Request ID: {request_id}") # Log the ID being used
        print(f"Approval Message ID: {interaction.message.id}")
        # ... rest of handle_approval logic using request_id and request_data ...
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        original_user_id = request_data['user_id']
        # Fetch original user - Ensure discord_client is available
        if not discord_client:
             print("Error: discord_client is None in handle_approval")
             await interaction.edit_original_response(content="Internal error: Bot client reference lost.", view=None)
             # Clean up pending request if possible
             if request_id in pending_persona_requests:
                 del pending_persona_requests[request_id]
             # Clean up message mapping
             if interaction.message.id in approval_message_to_request:
                 del approval_message_to_request[interaction.message.id]
             return
        original_user = await discord_client.fetch_user(original_user_id)

        # Handle potential None for name/content if request type is append
        name = request_data.get('name', 'default') # Default to 'default' for append
        content = request_data.get('content', '') # Default to empty for append
        request_type = request_data['type']
        success = False
        message_to_user = ""
        admin_feedback = ""

        try:
            if request_type == 'create':
                cursor.execute(
                    "INSERT INTO personas (name, content, creator_id, is_default) VALUES (?, ?, ?, ?)",
                    (name, content, original_user_id, 0)
                )
                conn.commit()
                success = True
                admin_feedback = f"✅ Approved creation of persona '{name}' by {original_user.mention}."
                message_to_user = f"Your request to create persona '{name}' has been approved by the admin."
            elif request_type == 'modify':
                cursor.execute("UPDATE personas SET content = ? WHERE name = ?", (content, name))
                conn.commit()
                success = True
                admin_feedback = f"✅ Approved modification of persona '{name}' by {original_user.mention}."
                message_to_user = f"Your request to modify persona '{name}' has been approved by the admin."
            elif request_type == 'append':
                text_to_append = request_data['text_to_append']
                cursor.execute("SELECT content FROM personas WHERE is_default = 1")
                current_default = cursor.fetchone()
                if not current_default:
                    raise Exception("Default persona not found.")
                current_content = current_default[0]
                # Store current content for undo support
                from shared import store_original_content
                store_original_content(current_content)
                new_content = current_content + "\n\n" + text_to_append
                cursor.execute("UPDATE personas SET content = ? WHERE is_default = 1", (new_content,))
                conn.commit()
                success = True
                admin_feedback = f"✅ Approved append to default persona by {original_user.mention}."
                message_to_user = "Your request to append to the default system message has been approved by the admin."

        except sqlite3.IntegrityError:
            admin_feedback = f"⚠️ Could not approve creation: Persona '{name}' already exists."
            message_to_user = f"Your request to create persona '{name}' could not be approved because a persona with that name already exists."
        except Exception as e:
            admin_feedback = f"❌ An error occurred while approving {request_type} for '{name}': {e}"
            message_to_user = f"An error occurred while processing the approval for your {request_type} request for persona '{name}'."
            print(f"Error during approval: {e}")
        finally:
            conn.close()

        # Disable buttons and update admin message
        for item in self.children:
            item.disabled = True
        # Edit the original deferred response
        await interaction.edit_original_response(content=admin_feedback, view=self)

        # Notify the original user via DM
        if original_user:
            try:
                await original_user.send(message_to_user)
            except discord.Forbidden:
                print(f"Could not DM user {original_user_id}") # User might have DMs disabled

        # Clean up pending request
        if request_id in pending_persona_requests:
            print(f"Cleaning up request ID: {request_id}")
            del pending_persona_requests[request_id]
        else:
            print(f"Attempted to clean up request ID {request_id}, but it was already removed.")
        # Clean up message mapping
        if interaction.message.id in approval_message_to_request:
            print(f"Cleaning up message mapping for message ID: {interaction.message.id}")
            del approval_message_to_request[interaction.message.id]


    # MODIFIED: Added request_id parameter
    async def handle_rejection(self, interaction: discord.Interaction, request_id: str):
        request_data = pending_persona_requests.get(request_id)
        if not request_data:
             print(f"Error in handle_rejection: Request ID '{request_id}' (from message {interaction.message.id}) not found in pending_persona_requests.")
             await interaction.edit_original_response(content="This request is no longer valid or has already been processed.", view=None)
             # Clean up message mapping if it exists
             if interaction.message.id in approval_message_to_request:
                 del approval_message_to_request[interaction.message.id]
             return

        print(f"\n=== Rejecting Persona Request ===")
        print(f"Request ID: {request_id}") # Log the ID being used
        print(f"Approval Message ID: {interaction.message.id}")

        original_user_id = request_data['user_id']
         # Ensure discord_client is available
        if not discord_client:
             print("Error: discord_client is None in handle_rejection")
             await interaction.edit_original_response(content="Internal error: Bot client reference lost.", view=None)
             # Clean up pending request if possible
             if request_id in pending_persona_requests:
                 del pending_persona_requests[request_id]
             # Clean up message mapping
             if interaction.message.id in approval_message_to_request:
                 del approval_message_to_request[interaction.message.id]
             return
        original_user = await discord_client.fetch_user(original_user_id)
        # Handle potential None for name if request type is append
        name = request_data.get('name', 'default') # Default to 'default' for append
        request_type = request_data['type']
        admin_feedback = f"❌ Rejected {request_type} request for persona '{name}' by {original_user.mention}."
        message_to_user = f"Your request to {request_type} persona '{name}' has been rejected by the admin."

        # Disable buttons and update admin message
        for item in self.children:
            item.disabled = True
        # Edit the original deferred response
        await interaction.edit_original_response(content=admin_feedback, view=self)

        # Notify the original user via DM
        if original_user:
            try:
                await original_user.send(message_to_user)
            except discord.Forbidden:
                 print(f"Could not DM user {original_user_id}")

        # Clean up pending request
        if request_id in pending_persona_requests:
            print(f"Cleaning up rejected request ID: {request_id}")
            del pending_persona_requests[request_id]
        else:
             print(f"Attempted to clean up rejected request ID {request_id}, but it was already removed.")
        # Clean up message mapping
        if interaction.message.id in approval_message_to_request:
            print(f"Cleaning up message mapping for message ID: {interaction.message.id}")
            del approval_message_to_request[interaction.message.id]


    # MODIFIED: Use fixed custom_id, retrieve request_id from mapping
    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, custom_id="approve_request")
    async def approve_button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer() # Defer immediately
        message_id = interaction.message.id
        request_id = approval_message_to_request.get(message_id)

        print(f"--- Approve Button Clicked (Callback) ---")
        print(f"Interaction Custom ID: {interaction.data['custom_id']}") # Should be 'approve_request'
        print(f"Message ID: {message_id}")

        if request_id:
            print(f"Found Request ID '{request_id}' for Message ID {message_id}")
            await self.handle_approval(interaction, request_id) # Pass request_id
        else:
            print(f"!!! Request ID NOT FOUND for Message ID {message_id} !!!")
            # Check if the view still has items; if not, the message might have been processed already.
            if not self.children:
                 await interaction.edit_original_response(content="This request has already been processed.", view=None)
            else:
                 await interaction.edit_original_response(content="Could not find the original request associated with this message. It might be too old or already processed.", view=None)


    # MODIFIED: Use fixed custom_id, retrieve request_id from mapping
    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger, custom_id="reject_request")
    async def reject_button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer() # Defer immediately
        message_id = interaction.message.id
        request_id = approval_message_to_request.get(message_id)

        print(f"--- Reject Button Clicked (Callback) ---")
        print(f"Interaction Custom ID: {interaction.data['custom_id']}") # Should be 'reject_request'
        print(f"Message ID: {message_id}")

        if request_id:
            print(f"Found Request ID '{request_id}' for Message ID {message_id}")
            await self.handle_rejection(interaction, request_id) # Pass request_id
        else:
            print(f"!!! Request ID NOT FOUND for Message ID {message_id} !!!")
            # Check if the view still has items; if not, the message might have been processed already.
            if not self.children:
                 await interaction.edit_original_response(content="This request has already been processed.", view=None)
            else:
                 await interaction.edit_original_response(content="Could not find the original request associated with this message. It might be too old or already processed.", view=None)


# --- Slash Commands for Persona Management ---

async def setup_persona_commands(tree, client): # Added client parameter
    global discord_client
    discord_client = client # Store client instance globally within the module

    @tree.command(name="create-type", description="Create a new system message persona type.")
    @app_commands.describe(name="The unique name for this persona type.", content="The system message content for the persona.")
    async def create_type(interaction: discord.Interaction, name: str, content: str):
        # Check if user is admin
        if interaction.user.id == ADMIN_USER_ID:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            try:
                cursor.execute(
                    "INSERT INTO personas (name, content, creator_id, is_default) VALUES (?, ?, ?, ?)",
                    (name, content, interaction.user.id, 0) # Creator is the admin
                )
                conn.commit()
                await interaction.response.send_message(f"Persona type '{name}' created successfully.", ephemeral=True)
            except sqlite3.IntegrityError:
                await interaction.response.send_message(f"Error: A persona type named '{name}' already exists.", ephemeral=True)
            except Exception as e:
                await interaction.response.send_message(f"An error occurred: {e}", ephemeral=True)
            finally:
                conn.close()
        else:
            # Non-admin user: Send for approval
            request_id = str(uuid.uuid4())
            pending_persona_requests[request_id] = {
                'type': 'create',
                'user_id': interaction.user.id,
                'name': name,
                'content': content,
                'original_interaction': interaction # Store for potential future use (e.g., followup)
            }

            try:
                admin_user = await discord_client.fetch_user(ADMIN_USER_ID)
                if not admin_user:
                    await interaction.response.send_message("Error: Could not find the admin user to send approval request.", ephemeral=True)
                    del pending_persona_requests[request_id] # Clean up failed request
                    return

                # Use the view without passing request_id
                approval_view = ApprovalView()
                # Send the message and get the message object
                sent_message = await admin_user.send(
                    f"**Persona Creation Request**\n"
                    f"User: {interaction.user.mention} ({interaction.user.id})\n"
                    f"Requested Name: `{name}`\n"
                    f"Content:\n```\n{content[:1500]}{'...' if len(content) > 1500 else ''}\n```",
                    view=approval_view
                )
                # Store the mapping
                approval_message_to_request[sent_message.id] = request_id
                print(f"Stored mapping: Message ID {sent_message.id} -> Request ID {request_id}")

                await interaction.response.send_message(f"Your request to create persona '{name}' has been sent to the admin for approval.", ephemeral=True)
            except discord.Forbidden:
                 await interaction.response.send_message("Error: Could not DM the admin for approval. Please contact them directly.", ephemeral=True)
                 del pending_persona_requests[request_id] # Clean up failed request
            except Exception as e:
                await interaction.response.send_message(f"An error occurred while sending the approval request: {e}", ephemeral=True)
                print(f"Error sending approval DM: {e}")
                if request_id in pending_persona_requests:
                    del pending_persona_requests[request_id]


    @tree.command(name="modify-type", description="Modify an existing system message persona type.")
    @app_commands.describe(name="The name of the persona type to modify.", new_content="The new system message content.")
    async def modify_type(interaction: discord.Interaction, name: str, new_content: str):
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT creator_id FROM personas WHERE name = ?", (name,))
            result = cursor.fetchone()
            if not result:
                await interaction.response.send_message(f"Error: Persona type '{name}' not found.", ephemeral=True)
                return

            creator_id = result[0]
            # Check if user is admin OR the original creator
            if is_admin_or_creator(interaction, creator_id):
                cursor.execute("UPDATE personas SET content = ? WHERE name = ?", (new_content, name))
                conn.commit()
                await interaction.response.send_message(f"Persona type '{name}' updated successfully.", ephemeral=True)
            else:
                # Non-admin/creator user: Send for approval
                request_id = str(uuid.uuid4())
                pending_persona_requests[request_id] = {
                    'type': 'modify',
                    'user_id': interaction.user.id,
                    'name': name,
                    'content': new_content, # Store the *new* content
                    'original_interaction': interaction
                }

                try:
                    admin_user = await discord_client.fetch_user(ADMIN_USER_ID)
                    if not admin_user:
                        await interaction.response.send_message("Error: Could not find the admin user to send approval request.", ephemeral=True)
                        del pending_persona_requests[request_id]
                        return

                    # Use the view without passing request_id
                    approval_view = ApprovalView()
                    sent_message = await admin_user.send(
                        f"**Persona Modification Request**\n"
                        f"User: {interaction.user.mention} ({interaction.user.id})\n"
                        f"Persona Name: `{name}`\n"
                        f"New Content:\n```\n{new_content[:1500]}{'...' if len(new_content) > 1500 else ''}\n```",
                        view=approval_view
                    )
                    # Store the mapping
                    approval_message_to_request[sent_message.id] = request_id
                    print(f"Stored mapping: Message ID {sent_message.id} -> Request ID {request_id}")

                    await interaction.response.send_message(f"Your request to modify persona '{name}' has been sent to the admin for approval.", ephemeral=True)
                except discord.Forbidden:
                    await interaction.response.send_message("Error: Could not DM the admin for approval. Please contact them directly.", ephemeral=True)
                    del pending_persona_requests[request_id]
                except Exception as e:
                    await interaction.response.send_message(f"An error occurred while sending the approval request: {e}", ephemeral=True)
                    print(f"Error sending approval DM: {e}")
                    if request_id in pending_persona_requests:
                        del pending_persona_requests[request_id]

        except Exception as e:
            await interaction.response.send_message(f"An error occurred: {e}", ephemeral=True)
        finally:
            conn.close()

    @tree.command(name="delete-type", description="Delete a system message persona type.")
    @app_commands.describe(name="The name of the persona type to delete.")
    async def delete_type(interaction: discord.Interaction, name: str):
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT creator_id, is_default FROM personas WHERE name = ?", (name,))
            result = cursor.fetchone()
            if not result:
                await interaction.response.send_message(f"Error: Persona type '{name}' not found.", ephemeral=True)
                return

            creator_id, is_default = result
            if not is_admin_or_creator(interaction, creator_id):
                await interaction.response.send_message("Error: You do not have permission to delete this persona type.", ephemeral=True)
                return

            if is_default:
                await interaction.response.send_message("Error: Cannot delete the default persona type. Change the default first.", ephemeral=True)
                return

            cursor.execute("DELETE FROM personas WHERE name = ?", (name,))
            conn.commit()
            await interaction.response.send_message(f"Persona type '{name}' deleted successfully.", ephemeral=True)

        except Exception as e:
            await interaction.response.send_message(f"An error occurred: {e}", ephemeral=True)
        finally:
            conn.close()

    @tree.command(name="change-default-type", description="Change the default system message persona type.")
    @app_commands.describe(name="The name of the persona type to set as default.")
    async def change_default_type(interaction: discord.Interaction, name: str):
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT content FROM personas WHERE name = ?", (name,))
            result = cursor.fetchone()
            if not result:
                await interaction.response.send_message(f"Error: Persona type '{name}' not found.", ephemeral=True)
                return

            cursor.execute("UPDATE personas SET is_default = 0 WHERE is_default = 1")
            cursor.execute("UPDATE personas SET is_default = 1 WHERE name = ?", (name,))
            conn.commit()
            print(f"Default persona changed to '{name}' in database.")
            await interaction.response.send_message(f"Default persona type changed to '{name}'.", ephemeral=False)

        except Exception as e:
            await interaction.response.send_message(f"An error occurred: {e}", ephemeral=True)
            print(f"Error changing default persona: {e}")
        finally:
            conn.close()

    @tree.command(name="list-personas", description="List all available personas")
    @app_commands.describe(search="Optional search term to filter personas")
    async def list_personas(interaction: discord.Interaction, search: str = None):
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        try:
            if (search):
                cursor.execute(
                    "SELECT name, is_default, creator_id FROM personas WHERE name LIKE ? ORDER BY is_default DESC, name ASC",
                    (f"%{search}%",)
                )
            else:
                cursor.execute(
                    "SELECT name, is_default, creator_id FROM personas ORDER BY is_default DESC, name ASC"
                )
            
            personas = cursor.fetchall()
            
            if not personas:
                msg = "No personas found."
                if search:
                    msg += f" (Search: '{search}')"
                await interaction.response.send_message(msg, ephemeral=True)
                return

            # Create the view instance - buttons are now handled by the class itself
            view = PersonaListView(personas, search=search)
            await interaction.response.send_message(
                content=view.get_current_page_content(),
                view=view
            )

            # REMOVED manual callback assignment loop:
            # for button in view.children:
            #     button.callback = lambda i, b=button: view.button_callback(i, b)

        except Exception as e:
            await interaction.response.send_message(f"An error occurred: {e}", ephemeral=True)
            print(f"Error listing personas: {e}")
        finally:
            conn.close()

    @tree.command(name="append-system-message", description="Append text to the default system message.")
    @app_commands.describe(text_to_append="Text to append to the default system message.")
    async def append_system_message(interaction: discord.Interaction, text_to_append: str):
        request_id = str(uuid.uuid4())
        pending_persona_requests[request_id] = {
            'type': 'append',
            'user_id': interaction.user.id,
            'text_to_append': text_to_append,
            'original_interaction': interaction
        }
        try:
            admin_user = await discord_client.fetch_user(ADMIN_USER_ID)
            if not admin_user:
                await interaction.response.send_message("Error: Admin user not found.", ephemeral=True)
                del pending_persona_requests[request_id]
                return
            # Use the view without passing request_id
            approval_view = ApprovalView()
            sent_message = await admin_user.send(
                f"**System Message Append Request**\n"
                f"User: {interaction.user.mention} ({interaction.user.id})\n"
                f"Text to Append:\n```\n{text_to_append[:1500]}{'...' if len(text_to_append) > 1500 else ''}\n```",
                view=approval_view
            )
            # Store the mapping
            approval_message_to_request[sent_message.id] = request_id
            print(f"Stored mapping: Message ID {sent_message.id} -> Request ID {request_id}")

            await interaction.response.send_message("Your request to append to the default system message has been sent for admin approval.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("Error: Could not DM the admin.", ephemeral=True)
            del pending_persona_requests[request_id]
        except Exception as e:
            await interaction.response.send_message(f"An error occurred: {e}", ephemeral=True)
            if request_id in pending_persona_requests:
                del pending_persona_requests[request_id]

    @tree.command(name="undo-append", description="Undo the last append operation on the default system message (Admin only).")
    async def undo_append(interaction: discord.Interaction):
        if interaction.user.id != ADMIN_USER_ID:
            await interaction.response.send_message("Error: Only the admin can perform undo.", ephemeral=True)
            return
        from shared import get_last_original_content, clear_last_original_content
        original_content = get_last_original_content()
        if original_content is None:
            await interaction.response.send_message("There is no append operation to undo.", ephemeral=True)
            return
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        try:
            cursor.execute("UPDATE personas SET content = ? WHERE is_default = 1", (original_content,))
            conn.commit()
            clear_last_original_content()
            await interaction.response.send_message("Successfully reverted the default system message to the state before the last append.", ephemeral=False)
        except Exception as e:
            await interaction.response.send_message(f"An error occurred while undoing the append: {e}", ephemeral=True)
        finally:
            conn.close()
