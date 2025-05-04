import sqlite3
import os
from dotenv import load_dotenv
import google.generativeai as genai

# Load environment variables from .env file
load_dotenv()
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
ADMIN_USER_ID = os.getenv('ADMIN_USER_ID')
DB_FILE = "personas.db"
DEFAULT_PERSONA_NAME = "default" # Changed from "wise-tree-default"

def initialize_database():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS personas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            content TEXT NOT NULL,
            creator_id INTEGER NOT NULL,
            is_default BOOLEAN DEFAULT 0,
            original_content_before_last_append TEXT DEFAULT NULL
        )
    ''')
    try:
        cursor.execute("ALTER TABLE personas ADD COLUMN original_content_before_last_append TEXT DEFAULT NULL")
        print("Added 'original_content_before_last_append' column to personas table.")
    except sqlite3.OperationalError as e:
        if "duplicate column name" in str(e):
            pass
        else:
            raise
    cursor.execute("SELECT COUNT(*) FROM personas WHERE is_default = 1")
    if cursor.fetchone()[0] == 0:
        try:
            # Check if the specific 'default' persona exists first
            cursor.execute("SELECT COUNT(*) FROM personas WHERE name = ?", (DEFAULT_PERSONA_NAME,))
            default_exists = cursor.fetchone()[0] > 0

            if not default_exists:
                # If 'default' persona doesn't exist, create it from the file
                print(f"Persona '{DEFAULT_PERSONA_NAME}' not found. Creating from system_message.txt...")
                with open("system_message.txt", "r", encoding="utf-8") as f:
                    default_content = f.read().strip()
                # Insert 'default' persona, but don't set it as default yet
                cursor.execute(
                    "INSERT OR IGNORE INTO personas (name, content, creator_id, is_default) VALUES (?, ?, ?, ?)",
                    (DEFAULT_PERSONA_NAME, default_content, ADMIN_USER_ID, 0) # Insert with is_default = 0 initially
                )
            else:
                 print(f"Persona '{DEFAULT_PERSONA_NAME}' found.")

            # Now, ensure *a* default exists. If none was marked (e.g., clean DB), mark 'default' as the default.
            cursor.execute("SELECT COUNT(*) FROM personas WHERE is_default = 1")
            if cursor.fetchone()[0] == 0:
                print(f"No default persona set. Setting '{DEFAULT_PERSONA_NAME}' as default.")
                cursor.execute("UPDATE personas SET is_default = 0") # Clear any potential stray defaults first
                cursor.execute("UPDATE personas SET is_default = 1 WHERE name = ?", (DEFAULT_PERSONA_NAME,))
            else:
                print("A default persona already exists.")

        except FileNotFoundError:
            print(f"CRITICAL ERROR: system_message.txt not found. Cannot create default persona '{DEFAULT_PERSONA_NAME}'.")
            conn.close()
            exit()
        except Exception as e:
            print(f"Error initializing default persona '{DEFAULT_PERSONA_NAME}': {e}")
            conn.close()
            exit()
    else:
        print("Default persona check: A default persona already exists in the database.")
    conn.commit()
    conn.close()

# --- Helper functions for append/undo ---
def store_original_content(original_content: str):
    """Store the current default content for potential undo."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute(
            "UPDATE personas SET original_content_before_last_append = ? WHERE is_default = 1",
            (original_content,)
        )
        conn.commit()
        print("Stored original content for undo.")
    except Exception as e:
        print(f"Error storing original content: {e}")
    finally:
        conn.close()

def get_last_original_content() -> str:
    """Retrieve the stored original content from the default persona."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT original_content_before_last_append FROM personas WHERE is_default = 1")
        result = cursor.fetchone()
        return result[0] if result else ''
    except Exception as e:
        print(f"Error retrieving original content: {e}")
        return ''
    finally:
        conn.close()

def clear_last_original_content():
    """Clear the stored original content after an undo."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE personas SET original_content_before_last_append = NULL WHERE is_default = 1")
        conn.commit()
        print("Cleared stored original content.")
    except Exception as e:
        print(f"Error clearing stored original content: {e}")
    finally:
        conn.close()

def get_persona(name=None):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    if name:
        cursor.execute("SELECT content, creator_id FROM personas WHERE name = ?", (name,))
    else:
        cursor.execute("SELECT content, creator_id FROM personas WHERE is_default = 1")
    result = cursor.fetchone()
    conn.close()
    return result

def is_admin_or_creator(interaction, creator_id):
    return interaction.user.id == ADMIN_USER_ID or interaction.user.id == creator_id
