import asyncio
import asyncpg
import os
from dotenv import load_dotenv

load_dotenv()

async def setup_postgres():
    DATABASE_URL = os.getenv("DATABASE_URL", "")
    
    if "postgresql" not in DATABASE_URL:
        print("ERROR: DATABASE_URL is not set to PostgreSQL in .env")
        return
    
    # Parse connection params
    # postgresql+asyncpg://user:pass@host:port/dbname
    db_url = DATABASE_URL.replace("postgresql+asyncpg://", "")
    parts = db_url.split("@")
    user_pass = parts[0].split(":")
    host_db = parts[1].split("/")
    host_port = host_db[0].split(":")
    
    user = user_pass[0]
    password = user_pass[1]
    host = host_port[0]
    port = int(host_port[1]) if len(host_port) > 1 else 5432
    dbname = host_db[1]
    
    print(f"Connecting to PostgreSQL at {host}:{port}...")
    
    try:
        # Connect to default postgres database to create our database
        conn = await asyncpg.connect(
            user=user,
            password=password,
            host=host,
            port=port,
            database="postgres"
        )
        
        # Check if database exists
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", dbname
        )
        
        if not exists:
            await conn.execute(f"CREATE DATABASE {dbname}")
            print(f"Database '{dbname}' created successfully!")
        else:
            print(f"Database '{dbname}' already exists")
        
        await conn.close()
        
        # Now connect to our database and create tables
        conn = await asyncpg.connect(
            user=user,
            password=password,
            host=host,
            port=port,
            database=dbname
        )
        
        # Create users table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username VARCHAR(255) UNIQUE NOT NULL,
                name VARCHAR(255) NOT NULL,
                hashed_password VARCHAR(255) NOT NULL,
                role VARCHAR(50) DEFAULT 'trader',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_login TIMESTAMP
            )
        """)
        
        print("Users table created!")
        
        # No accounts are created here. Accounts with passwords written in this file
        # used to be created. Add them with:  python create_admin.py
        print("No accounts created. Run `python create_admin.py` to add the admin.")
        await conn.close()
        print("\n✅ PostgreSQL setup complete!")
        
    except Exception as e:
        print(f"Error: {e}")
        print("\nTo fix PostgreSQL authentication, you may need to:")
        print("1. Set a password for postgres user: ALTER USER postgres PASSWORD 'yourpassword';")
        print("2. Or update pg_hba.conf to use 'trust' method for localhost")

if __name__ == "__main__":
    asyncio.run(setup_postgres())