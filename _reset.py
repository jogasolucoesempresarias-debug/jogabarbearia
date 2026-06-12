"""Reset local: dropa e recria o database (uso DEV/teste). Servidor: ver comandos no chat."""
import os
import psycopg2
from dotenv import load_dotenv

load_dotenv()
alvo = os.getenv('DB_NAME', 'joga_barbearia')
conn = psycopg2.connect(host=os.getenv('DB_HOST'), port=os.getenv('DB_PORT'), dbname='postgres',
                        user=os.getenv('DB_USER'), password=os.getenv('DB_PASSWORD'))
conn.autocommit = True
cur = conn.cursor()
cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s AND pid<>pg_backend_pid()", (alvo,))
cur.execute(f'DROP DATABASE IF EXISTS "{alvo}"')
cur.execute(f'CREATE DATABASE "{alvo}"')
cur.close()
conn.close()
print(f"[OK] Database '{alvo}' resetado (dropado e recriado).")
