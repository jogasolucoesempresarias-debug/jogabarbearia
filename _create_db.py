"""Cria o database joga_barbearia se não existir (uso local; psql não está no PATH)."""
import os
import psycopg2
from dotenv import load_dotenv

load_dotenv()
alvo = os.getenv('DB_NAME', 'joga_barbearia')
conn = psycopg2.connect(
    host=os.getenv('DB_HOST', 'localhost'), port=os.getenv('DB_PORT', '5432'),
    dbname='postgres', user=os.getenv('DB_USER', 'postgres'), password=os.getenv('DB_PASSWORD', ''),
)
conn.autocommit = True
cur = conn.cursor()
cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (alvo,))
if cur.fetchone():
    print(f"[OK] Database '{alvo}' ja existe.")
else:
    cur.execute(f'CREATE DATABASE "{alvo}"')
    print(f"[OK] Database '{alvo}' criado.")
cur.close()
conn.close()
