# JOGA Barbearia

SaaS de gestão para barbearia — **mobile-first + PWA**. Agenda interna, comanda (serviços +
produtos), plano de assinatura dos clientes, comissão dos barbeiros e caixa.

Construído sobre a espinha do **Gestão JOGA** (auth, financeiro, recorrência, deploy), como produto
vertical da JOGA Soluções Empresariais. Instância própria por cliente.

## Stack
Flask + Waitress · PostgreSQL · HTML/CSS/JS puro (mobile-first) + PWA · Docker Swarm/Traefik/GHCR.

## Perfis
- **dono** — admin, configura tudo, relatórios
- **caixa/recepção** — agenda, comanda, caixa
- **barbeiro** — vê a própria agenda + comissão

## Conceitos
- **Comanda** é o pivô: ao fechar vira receita no caixa, gera comissão (45% por serviço, configurável)
  e respeita o **plano** (assinante coberto não paga avulso, mas o barbeiro ganha comissão sobre tabela).
- **Plano de assinatura** (configurável: valor, serviços, dias, vencimento 10/30) — assinante paga
  **no balcão** (manual). *(A mensalidade que a JOGA cobra da barbearia é outra coisa, fica no Gestão JOGA.)*
- **Agenda**: slots de 30min, horário por dia, walk-in, bloqueio/folga, cancelamento sem taxa.

## Setup local (dev)
```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
# .env: DB_HOST/DB_NAME/DB_PASSWORD
.\.venv\Scripts\python.exe -X utf8 _create_db.py     # cria o database
.\.venv\Scripts\python.exe -X utf8 init_db.py        # schema
.\.venv\Scripts\python.exe -X utf8 seed_barbearia.py # dados pré-configurados
.\.venv\Scripts\python.exe -X utf8 server.py         # http://localhost:5000
```
Login: `caixa@barbearia.local` / `joga123` (dono@ e `<barbeiro>@barbearia.local` também). Troca de senha no 1º acesso.

Smoke test da API: `.\.venv\Scripts\python.exe -X utf8 _smoke.py` (limpa o resíduo no fim).

## Deploy
`git push main` → Actions builda e publica no GHCR → no servidor `docker service update --force` +
`init_db.py` + `seed_barbearia.py`. Stack em `docker-compose.prod.yml` (Traefik → `barbearia.jogasolucoes.com.br`).

## Estrutura
```
server.py            # backend (auth, agenda, comanda, assinaturas, caixa, relatórios)
init_db.py           # schema
seed_barbearia.py    # dados pré-configurados (serviços, produtos, barbeiros, horários, plano)
static/app.css|js    # shell mobile-first (bottom-nav)
agenda|comanda|clientes|assinaturas|caixa|relatorios|config|barbeiro.html
manifest.json sw.js  # PWA
```
