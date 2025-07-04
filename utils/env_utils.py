import os

def save_credentials_to_env_file(email, password, env_path=".env"):
    """Save credentials to a .env file for persistence."""
    with open(env_path, "w") as f:
        f.write(f'CUISTOVOICE_EMAIL="{email}"\n')
        f.write(f'CUISTOVOICE_PASSWORD="{password}"\n')

def load_credentials_from_env_file(env_path=".env"):
    """Load credentials from a .env file if it exists."""
    if not os.path.exists(env_path):
        return None, None
    email = password = None
    with open(env_path, "r") as f:
        for line in f:
            if line.startswith("CUISTOVOICE_EMAIL="):
                email = line.strip().split("=", 1)[1].strip('"')
            elif line.startswith("CUISTOVOICE_PASSWORD="):
                password = line.strip().split("=", 1)[1].strip('"')
    return email, password